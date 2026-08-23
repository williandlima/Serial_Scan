"""Cut a byte stream into frames using inter-character silence.

Binary serial protocols almost never carry an explicit length prefix that an
analyser can trust before it knows the protocol. What they *do* have is
timing: a frame is a burst of characters sent back to back, followed by a
silence. Modbus RTU formalises it as "3.5 character times of idle line ends
the frame" and most vendor protocols behave the same way, so that is the rule
used here, with the threshold taken from the selected protocol profile.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Iterable, Iterator


class Direction(str, Enum):
    """Who is speaking, as far as the analyser can tell."""

    UNKNOWN = "?"
    REQUEST = "REQ"
    RESPONSE = "RSP"


@dataclass(frozen=True)
class TimedByte:
    """A received byte with the instant it arrived (monotonic seconds)."""

    time: float
    value: int


@dataclass
class Frame:
    """A burst of bytes delimited by silence."""

    data: bytes
    time_start: float
    time_end: float
    index: int = 0
    #: Silence observed before this frame, in seconds (``inf`` for the first).
    gap_before: float = float("inf")
    direction: Direction = Direction.UNKNOWN
    #: True when the frame was closed by the end of the capture rather than by
    #: an observed silence, so it may be only the beginning of a real frame.
    truncated: bool = False
    #: Filled in by :mod:`serial_scan.commands` once the frame is classified.
    signature: str | None = None
    checksum_ok: bool | None = None

    def __len__(self) -> int:
        return len(self.data)

    @property
    def duration(self) -> float:
        return self.time_end - self.time_start

    @property
    def hex(self) -> str:
        return self.data.hex(" ").upper()

    @property
    def ascii(self) -> str:
        return "".join(chr(b) if 0x20 <= b < 0x7F else "." for b in self.data)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<Frame #{self.index} {len(self.data)}B {self.hex[:32]}>"


class FrameSplitter:
    """Incremental splitter: feed bytes, get frames out.

    ``idle_gap`` is the silence in seconds that closes a frame. ``max_bytes``
    and ``max_duration`` are safety valves so that a continuously streaming
    device (no silence at all) still produces frames instead of one endless
    buffer.
    """

    def __init__(
        self,
        idle_gap: float,
        max_bytes: int = 512,
        max_duration: float = 2.0,
    ) -> None:
        if idle_gap <= 0:
            raise ValueError("idle_gap must be positive")
        self.idle_gap = idle_gap
        self.max_bytes = max_bytes
        self.max_duration = max_duration
        self._buffer = bytearray()
        self._start: float = 0.0
        self._last: float = 0.0
        self._previous_end: float | None = None
        self._count = 0

    @property
    def pending(self) -> int:
        return len(self._buffer)

    def feed(self, item: TimedByte) -> list[Frame]:
        """Push one byte in; returns the frames it completed (0 or 1, or 2 when
        the byte both closes an oversized buffer and starts a new frame)."""
        out: list[Frame] = []
        if self._buffer:
            gap = item.time - self._last
            too_long = len(self._buffer) >= self.max_bytes
            too_old = (self._last - self._start) >= self.max_duration
            if gap >= self.idle_gap or too_long or too_old:
                out.append(self._close())
        if not self._buffer:
            self._start = item.time
        self._buffer.append(item.value & 0xFF)
        self._last = item.time
        return out

    def feed_many(self, items: Iterable[TimedByte]) -> list[Frame]:
        out: list[Frame] = []
        for item in items:
            out.extend(self.feed(item))
        return out

    def tick(self, now: float) -> list[Frame]:
        """Close the pending frame if the line has been quiet long enough.

        Call this from the capture loop when no byte arrived: without it the
        last frame of a burst would only be emitted when the *next* burst
        starts, which is useless for a live display.
        """
        if self._buffer and (now - self._last) >= self.idle_gap:
            return [self._close()]
        return []

    def flush(self) -> list[Frame]:
        """Emit whatever is buffered, regardless of timing (end of capture).

        Anything still buffered here never had its trailing silence observed -
        :meth:`tick` would already have closed it otherwise - so the frame is
        marked truncated. It is very likely the front half of a real frame
        that the capture cut in the middle.
        """
        return [self._close(truncated=True)] if self._buffer else []

    def _close(self, truncated: bool = False) -> Frame:
        gap = float("inf")
        if self._previous_end is not None:
            gap = self._start - self._previous_end
        frame = Frame(
            data=bytes(self._buffer),
            time_start=self._start,
            time_end=self._last,
            index=self._count,
            gap_before=gap,
            truncated=truncated,
        )
        self._count += 1
        self._previous_end = self._last
        self._buffer.clear()
        return frame


def split_frames(
    items: Iterable[TimedByte],
    idle_gap: float,
    max_bytes: int = 512,
    max_duration: float = 2.0,
) -> list[Frame]:
    """Convenience wrapper for offline splitting of a complete capture."""
    splitter = FrameSplitter(idle_gap, max_bytes=max_bytes, max_duration=max_duration)
    frames = splitter.feed_many(items)
    frames.extend(splitter.flush())
    return frames


def synth_timed_bytes(
    data: bytes,
    char_time: float,
    t0: float = 0.0,
    gaps: dict[int, float] | None = None,
) -> Iterator[TimedByte]:
    """Attach plausible timestamps to a byte string.

    Useful when a capture arrives without per-byte timing (a replayed hex
    dump, or a driver that only timestamps read() calls): bytes inside a burst
    are one character time apart, and ``gaps`` injects extra silence before the
    given byte offsets.
    """
    gaps = gaps or {}
    t = t0
    for offset, byte in enumerate(data):
        t += gaps.get(offset, 0.0)
        yield TimedByte(time=t, value=byte)
        t += char_time


@dataclass
class GapHistogram:
    """Distribution of inter-byte gaps, used to pick an idle threshold."""

    gaps: list[float] = field(default_factory=list)

    def add(self, gap: float) -> None:
        if gap > 0:
            self.gaps.append(gap)

    def suggest_idle_gap(self, char_time: float, floor_chars: float = 1.5) -> float:
        """Find the silence that best separates "inside a frame" from "between
        frames".

        Inter-byte gaps in a UART stream are bimodal: a tight cluster near one
        character time, and a much sparser tail of inter-frame silences. The
        widest empty interval between consecutive sorted gaps is the natural
        cut point; the character-time floor keeps the answer sane when the
        capture happens to contain a single frame.
        """
        floor = floor_chars * char_time
        if len(self.gaps) < 4:
            return max(floor, 3.5 * char_time)
        ordered = sorted(self.gaps)
        best_cut = None
        best_span = 0.0
        for previous, following in zip(ordered, ordered[1:]):
            if following <= floor:
                continue
            span = following / max(previous, 1e-9)
            if span > best_span:
                best_span = span
                best_cut = (previous + following) / 2.0
        if best_cut is None or best_span < 2.0:
            return max(floor, 3.5 * char_time)
        return max(floor, best_cut)
