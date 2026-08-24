"""The capture session: the object that ties all five project steps together.

    protocol -> configuration -> frames -> commands -> labels

A session owns a source, a frame splitter and a command catalog, and can run
either synchronously (CLI, tests) or on a background thread feeding a queue
(GUI). Everything the user interface needs is exposed as events so that no
part of the pipeline has to know a display exists.
"""

from __future__ import annotations

import queue
import threading
import time
from collections import Counter, deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable

from .autodetect import DetectionReport, analyse_capture, analyse_samples, scan_candidates
from .checksums import ALGORITHMS_BY_NAME, ChecksumAlgorithm, ChecksumMatch, detect_checksum
from .commands import CommandCatalog, CommandEntry
from .framing import Direction, Frame, FrameSplitter, TimedByte
from .labels import LabelStore
from .portconfig import SerialConfig
from .protocols import Protocol, ProtocolProfile, get_profile
from .sources import ByteSource, CaptureWriter, ReplaySource, SimulatedSource

#: Quantos frames o detector de checksum guarda para comparar.
CHECKSUM_POOL_SIZE = 32
#: Teto do intervalo entre tentativas de deteccao (em frames).
CHECKSUM_MAX_INTERVAL = 512
#: Depois disso a busca por checksum para de vez.
CHECKSUM_GIVE_UP_AFTER = 4000


class DirectionInferencer:
    """Guess which side of a half-duplex bus sent each frame.

    On RS-485 both directions share one pair, so a capture is a single
    interleaved stream. Timing still separates them: a master polls after a
    long idle, and the slave answers quickly. So a frame preceded by a long
    silence starts a transaction (request) and a frame that follows closely
    answers it (response).

    This is a heuristic, and it is labelled as one in the UI. It breaks on
    unsolicited slave reports and on masters that burst several requests, so
    it never feeds anything but the display.
    """

    def __init__(self, profile: ProtocolProfile, config: SerialConfig) -> None:
        self.profile = profile
        self.threshold = profile.turnaround_gap_chars * config.char_time
        self._previous: Direction = Direction.UNKNOWN

    def update_config(self, config: SerialConfig) -> None:
        self.threshold = self.profile.turnaround_gap_chars * config.char_time

    def classify(self, frame: Frame) -> Direction:
        if not self.profile.infer_direction:
            return Direction.UNKNOWN
        if frame.gap_before == float("inf") or frame.gap_before >= self.threshold:
            direction = Direction.REQUEST
        elif self._previous is Direction.REQUEST:
            direction = Direction.RESPONSE
        else:
            direction = Direction.REQUEST
        self._previous = direction
        return direction


@dataclass
class SessionEvent:
    """Something the interface may want to show."""

    kind: str  # "frame" | "command" | "status" | "error" | "stopped"
    frame: Frame | None = None
    entry: CommandEntry | None = None
    text: str = ""


@dataclass
class SessionStats:
    started: float = 0.0
    bytes_seen: int = 0
    frames_seen: int = 0
    checksum_ok: int = 0
    checksum_bad: int = 0
    #: Frames cut short by the end of the capture, excluded from the catalog.
    truncated: int = 0
    directions: Counter = field(default_factory=Counter)
    #: Span covered by the capture itself, taken from the byte timestamps.
    #: For replay and simulation this runs far faster than the wall clock, so
    #: reporting elapsed wall time would be meaningless.
    first_time: float | None = None
    last_time: float = 0.0

    def mark(self, time_value: float) -> None:
        if self.first_time is None:
            self.first_time = time_value
        self.last_time = max(self.last_time, time_value)

    @property
    def capture_span(self) -> float:
        if self.first_time is None:
            return 0.0
        return max(0.0, self.last_time - self.first_time)

    @property
    def wall_elapsed(self) -> float:
        return max(0.0, time.monotonic() - self.started) if self.started else 0.0

    @property
    def elapsed(self) -> float:
        """Capture time, falling back to wall time before any byte arrives."""
        return self.capture_span or self.wall_elapsed

    @property
    def frames_per_minute(self) -> float:
        return self.frames_seen / self.elapsed * 60.0 if self.elapsed > 0 else 0.0


class ScanSession:
    """Capture, frame, classify and label serial traffic."""

    def __init__(
        self,
        source: ByteSource,
        protocol: Protocol | str,
        config: SerialConfig | None = None,
        idle_gap: float | None = None,
        label_store: LabelStore | None = None,
        checksum: ChecksumAlgorithm | None = None,
        max_recent_frames: int = 2000,
    ) -> None:
        self.source = source
        self.profile = get_profile(protocol)
        self.config = config or source.config
        self.idle_gap = idle_gap or self.profile.idle_gap_chars * self.config.char_time
        self.catalog = CommandCatalog(key_offsets=self.profile.key_offsets)
        self.labels = label_store
        self.checksum = checksum
        self.stats = SessionStats()

        self._splitter = FrameSplitter(self.idle_gap)
        self._direction = DirectionInferencer(self.profile, self.config)
        self._recent: deque[Frame] = deque(maxlen=max_recent_frames)
        self._events: queue.Queue[SessionEvent] = queue.Queue()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._writer: CaptureWriter | None = None
        self._lock = threading.RLock()
        #: Frames kept aside to auto-detect the checksum once there are enough.
        self._checksum_pool: list[bytes] = []
        self._checksum_interval = 1
        self._checksum_countdown = 1
        self._checksum_tried = 0
        self._checksum_exhausted = False

        if self.labels is not None:
            self.catalog.apply_labels(
                self.labels.labels_for(self.profile.name),
                self.labels.notes_for(self.profile.name),
            )

    # -- configuration ----------------------------------------------------

    def apply(self, config: SerialConfig, idle_gap: float | None = None) -> None:
        """Switch to a new line configuration, resetting the framing state."""
        with self._lock:
            self.config = config
            self.idle_gap = idle_gap or self.profile.idle_gap_chars * config.char_time
            self._splitter = FrameSplitter(self.idle_gap)
            self._direction.update_config(config)
            if self.source.supports_reconfigure:
                self.source.reconfigure(config)

    def reset(self) -> None:
        """Forget every frame and command, keeping the labels."""
        with self._lock:
            self.catalog = CommandCatalog(key_offsets=self.catalog.key_offsets)
            if self.labels is not None:
                self.catalog.apply_labels(
                    self.labels.labels_for(self.profile.name),
                    self.labels.notes_for(self.profile.name),
                )
            self._recent.clear()
            self._checksum_pool.clear()
            self._checksum_interval = 1
            self._checksum_countdown = 1
            self._checksum_tried = 0
            self._checksum_exhausted = False
            self.stats = SessionStats(started=time.monotonic())

    # -- detection --------------------------------------------------------

    def detect(self, dwell: float = 0.6, apply_result: bool = True, **kwargs) -> DetectionReport:
        """Run automatic identification against the live source (step 2)."""
        report = scan_candidates(self.source, self.profile.protocol, dwell=dwell, **kwargs)
        if apply_result and report.config is not None:
            self.apply(report.config, idle_gap=report.idle_gap or None)
        return report

    # -- ingestion --------------------------------------------------------

    def ingest(self, items: Iterable[TimedByte]) -> list[Frame]:
        """Push raw bytes through the pipeline; returns the frames completed."""
        produced: list[Frame] = []
        with self._lock:
            for item in items:
                self.stats.bytes_seen += 1
                self.stats.mark(item.time)
                if self._writer is not None:
                    self._writer.write([item])
                for frame in self._splitter.feed(item):
                    produced.append(self._handle(frame))
        return produced

    def tick(self, now: float | None = None) -> list[Frame]:
        """Close a frame that is waiting only for the idle timeout to expire."""
        with self._lock:
            closed = self._splitter.tick(now if now is not None else time.monotonic())
            return [self._handle(frame) for frame in closed]

    def flush(self) -> list[Frame]:
        with self._lock:
            return [self._handle(frame) for frame in self._splitter.flush()]

    def _handle(self, frame: Frame) -> Frame:
        frame.direction = self._direction.classify(frame)
        if frame.truncated:
            # The capture stopped in the middle of this frame. Show it, but
            # keep it out of the catalog: a half frame would otherwise appear
            # as a command of its own, with a checksum that never validates.
            self._recent.append(frame)
            self.stats.truncated += 1
            self._emit(SessionEvent("frame", frame=frame))
            return frame
        self._verify_checksum(frame)

        observation = self.catalog.observe(frame)
        self._recent.append(frame)
        self.stats.frames_seen += 1
        self.stats.directions[frame.direction] += 1
        if frame.checksum_ok is True:
            self.stats.checksum_ok += 1
        elif frame.checksum_ok is False:
            self.stats.checksum_bad += 1

        self._emit(SessionEvent("frame", frame=frame, entry=observation.entry))
        if observation.is_new:
            # Step 4: a command never seen before is announced, not merely
            # counted, so the operator can label it while it is on screen.
            self._emit(
                SessionEvent(
                    "command",
                    frame=frame,
                    entry=observation.entry,
                    text=f"Novo comando: {observation.entry.signature}",
                )
            )
        return frame

    def _verify_checksum(self, frame: Frame) -> None:
        if self.checksum is None:
            self._try_detect_checksum(frame)
        # Deliberately re-checked rather than returned early above: the frame
        # that *triggers* the adoption is not yet in the catalog history, so
        # the back-fill in recount_checksums() cannot see it. Without this it
        # would stay unverified for ever, one frame short of the total.
        if self.checksum is not None:
            frame.checksum_ok = self.checksum.check(frame.data)

    def _try_detect_checksum(self, frame: Frame) -> None:
        """Look for the trailer algorithm, backing off as attempts fail.

        Detection costs ``pool x algorithms x frame length`` byte operations,
        all of it in Python. Running it on *every* frame is fine for the two
        or three frames it usually takes to succeed, and ruinous when it never
        does - which is precisely the case this tool exists for, a proprietary
        protocol whose checksum is not in the table. Measured on a bus with an
        unrecognised trailer, retrying every frame cost 140 us per byte
        against 1.7 us once an algorithm is known: an 80x penalty, enough to
        peg a CPU at 115200 baud.

        So the interval between attempts doubles on each failure, and after a
        budget of frames the search stops for good. New evidence never stops
        arriving on a live bus, but if 4000 frames were not enough, another
        4000 will not be either.
        """
        if self._checksum_exhausted:
            return
        self._checksum_pool.append(frame.data)
        if len(self._checksum_pool) > CHECKSUM_POOL_SIZE:
            del self._checksum_pool[:-CHECKSUM_POOL_SIZE]

        self._checksum_countdown -= 1
        if self._checksum_countdown > 0 or len(self._checksum_pool) < 8:
            return

        match = detect_checksum(self._checksum_pool)
        if match is not None:
            self.adopt_checksum(match)
            return

        self._checksum_interval = min(self._checksum_interval * 2, CHECKSUM_MAX_INTERVAL)
        self._checksum_countdown = self._checksum_interval
        self._checksum_tried += 1
        if self.stats.frames_seen >= CHECKSUM_GIVE_UP_AFTER:
            self._checksum_exhausted = True
            self._checksum_pool.clear()
            self._emit(
                SessionEvent(
                    "status",
                    text=(
                        f"Nenhum checksum conhecido confere apos "
                        f"{self.stats.frames_seen} frames: busca encerrada. O "
                        f"protocolo pode nao ter checksum, ou usar um algoritmo "
                        f"proprietario."
                    ),
                )
            )

    def adopt_checksum(self, match: ChecksumMatch | ChecksumAlgorithm | str) -> None:
        if isinstance(match, ChecksumMatch):
            algorithm = match.algorithm
        elif isinstance(match, str):
            algorithm = ALGORITHMS_BY_NAME[match]
        else:
            algorithm = match
        self.checksum = algorithm
        self.catalog.checksum_width = algorithm.width
        self._checksum_pool.clear()
        self._checksum_exhausted = True
        self._emit(SessionEvent("status", text=f"Checksum identificado: {algorithm.name}"))
        self.recount_checksums()

    def recount_checksums(self) -> None:
        """Re-verify every retained frame and rebuild the tallies.

        The checksum is only identified after the first handful of frames, and
        those were recorded as "unknown". Without this pass the summary would
        for ever read "valid in 204/212 frames" on a bus where every single
        frame is in fact valid.
        """
        if self.checksum is None:
            return
        self.stats.checksum_ok = 0
        self.stats.checksum_bad = 0
        for entry in self.catalog.entries.values():
            entry.checksum_ok = 0
            entry.checksum_bad = 0
        for frame in self.catalog.history():
            frame.checksum_ok = self.checksum.check(frame.data)
            if frame.checksum_ok:
                self.stats.checksum_ok += 1
            else:
                self.stats.checksum_bad += 1
            entry = self.catalog.entries.get(frame.signature or "")
            if entry is not None:
                if frame.checksum_ok:
                    entry.checksum_ok += 1
                else:
                    entry.checksum_bad += 1

    # -- labelling (step 5) ----------------------------------------------

    def set_label(self, signature: str, label: str) -> None:
        self.catalog.set_label(signature, label)
        if self.labels is not None:
            self.labels.set_label(self.profile.name, signature, label)

    def set_notes(self, signature: str, notes: str) -> None:
        self.catalog.set_notes(signature, notes)
        if self.labels is not None:
            self.labels.set_notes(self.profile.name, signature, notes)

    def save_labels(self) -> None:
        if self.labels is not None:
            self.labels.save()

    # -- recording --------------------------------------------------------

    def record_to(self, path: str | Path) -> None:
        self.stop_recording()
        self._writer = CaptureWriter(path, self.config, self.profile.name)

    def stop_recording(self) -> None:
        if self._writer is not None:
            self._writer.close()
            self._writer = None

    # -- events -----------------------------------------------------------

    def _emit(self, event: SessionEvent) -> None:
        self._events.put(event)

    def drain_events(self, limit: int = 500) -> list[SessionEvent]:
        """Collect pending events; the GUI calls this from its timer."""
        out: list[SessionEvent] = []
        for _ in range(limit):
            try:
                out.append(self._events.get_nowait())
            except queue.Empty:
                break
        return out

    @property
    def recent_frames(self) -> list[Frame]:
        with self._lock:
            return list(self._recent)

    # -- running ----------------------------------------------------------

    def run_for(
        self,
        duration: float,
        slice_size: float = 0.05,
        on_frame: Callable[[Frame], None] | None = None,
        flush: bool = True,
    ) -> list[Frame]:
        """Capture synchronously for ``duration`` seconds (CLI and tests).

        Pass ``on_frame`` to display frames as they complete. Do *not* call
        this repeatedly in a loop to get the same effect: the closing
        ``flush`` would cut whatever frame is mid-flight at every call
        boundary, splitting real frames into fragments.
        """
        if not self.stats.started:
            self.stats.started = time.monotonic()
        frames: list[Frame] = []

        def collect(new_frames: list[Frame]) -> None:
            frames.extend(new_frames)
            if on_frame is not None:
                for frame in new_frames:
                    on_frame(frame)

        remaining = duration
        while remaining > 0:
            chunk = min(slice_size, remaining)
            items = self.source.read(chunk)
            collect(self.ingest(items))
            # Close a frame only once the source's clock proves the silence
            # really elapsed. Using "last byte + idle_gap" instead would cut a
            # frame at every read boundary.
            collect(self.tick(self.source.now))
            remaining -= chunk
        if flush:
            collect(self.flush())
        return frames

    def start(self, slice_size: float = 0.05) -> None:
        """Capture on a background thread, feeding the event queue."""
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self.stats.started = time.monotonic()
        self._thread = threading.Thread(
            target=self._loop, args=(slice_size,), name="serial-scan", daemon=True
        )
        self._thread.start()

    def _loop(self, slice_size: float) -> None:
        try:
            while not self._stop.is_set():
                items = self.source.read(slice_size)
                self.ingest(items)
                # Close a frame whose trailing silence has elapsed, using the
                # capture's own clock: for replay and simulation it runs
                # independently of the wall clock.
                self.tick(self.source.now)
                if isinstance(self.source, ReplaySource) and self.source.exhausted:
                    self.flush()
                    self._emit(SessionEvent("status", text="Fim do arquivo de captura."))
                    break
        except Exception as exc:  # surfaced in the UI rather than killing the thread
            self._emit(SessionEvent("error", text=str(exc)))
        finally:
            self.flush()
            self.stop_recording()
            self._emit(SessionEvent("stopped", text="Captura encerrada."))

    def stop(self, timeout: float = 2.0) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            self._thread = None

    @property
    def running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    # -- reporting --------------------------------------------------------

    def report(self) -> dict:
        return {
            "protocol": self.profile.name,
            "config": self.config.label,
            "idle_gap_ms": round(self.idle_gap * 1000, 3),
            "checksum": self.checksum.name if self.checksum else None,
            "frames": self.stats.frames_seen,
            "truncated": self.stats.truncated,
            "bytes": self.stats.bytes_seen,
            "capture_span_s": round(self.stats.capture_span, 3),
            "commands": self.catalog.to_dict()["commands"],
        }


def session_from_capture(
    path: str | Path,
    protocol: Protocol | str | None = None,
    label_store: LabelStore | None = None,
) -> tuple[ScanSession, DetectionReport]:
    """Build a session that replays a recorded capture, framing it sensibly."""
    source = ReplaySource(path)
    source.open()
    chosen = protocol or source.protocol
    report = analyse_capture(source.all_bytes(), chosen, source.config)
    session = ScanSession(
        source,
        chosen,
        config=source.config,
        idle_gap=report.idle_gap or None,
        label_store=label_store,
    )
    return session, report


def session_from_simulator(
    true_config: SerialConfig | None = None,
    protocol: Protocol | str = Protocol.RS485,
    label_store: LabelStore | None = None,
    detect: bool = True,
    **kwargs,
) -> tuple[ScanSession, DetectionReport | None]:
    """Build a session against the built-in virtual bus, for demos and tests."""
    source = SimulatedSource(true_config, **kwargs)
    source.open()
    report: DetectionReport | None = None
    config = source.true_config
    idle_gap = None
    if detect:
        report = analyse_samples(source.raw_samples(), source.sample_rate, protocol)
        if report.config is not None:
            config = report.config
            idle_gap = report.idle_gap or None
        source.reconfigure(config)
    session = ScanSession(
        source, protocol, config=config, idle_gap=idle_gap, label_store=label_store
    )
    return session, report
