"""Where the bytes come from: a real port, a recorded capture, or a simulator.

Everything downstream talks to :class:`ByteSource`, so the analyser, the CLI
and the GUI behave identically whether they are driving an FTDI cable or
replaying a file on a laptop with no serial hardware at all.
"""

from __future__ import annotations

import bisect
import json
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable, Sequence

from .checksums import ALGORITHMS_BY_NAME, ChecksumAlgorithm
from .framing import TimedByte
from .portconfig import SerialConfig
from .uart import decode_samples, encode_samples


class SourceError(RuntimeError):
    """Raised when a source cannot be opened or read."""


class ByteSource(ABC):
    """A stream of timestamped bytes that can (sometimes) be reconfigured."""

    #: Whether :meth:`reconfigure` really changes how the line is sampled.
    supports_reconfigure: bool = False

    @property
    @abstractmethod
    def name(self) -> str: ...

    @property
    @abstractmethod
    def config(self) -> SerialConfig: ...

    @abstractmethod
    def open(self) -> None: ...

    @abstractmethod
    def close(self) -> None: ...

    @abstractmethod
    def read(self, timeout: float) -> list[TimedByte]:
        """Return whatever arrived within ``timeout`` seconds (possibly none)."""

    @property
    def now(self) -> float:
        """How far this source's clock has advanced.

        A frame can only be closed once the reader knows that nothing more
        arrived before ``now``. For a real port that is the wall clock; for
        replay and simulation it is the virtual clock, which runs far ahead of
        it. Getting this wrong chops frames at every read boundary.
        """
        return time.monotonic()

    def reconfigure(self, config: SerialConfig) -> None:
        raise SourceError(f"{type(self).__name__} cannot be reconfigured")

    def drain(self) -> None:
        """Discard buffered bytes; called after changing the configuration."""

    def __enter__(self) -> "ByteSource":
        self.open()
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


# ---------------------------------------------------------------------------
# Real hardware
# ---------------------------------------------------------------------------


def list_ports() -> list[tuple[str, str]]:
    """Available serial ports as ``(device, description)``.

    Returns an empty list (rather than raising) when pyserial is missing, so
    the GUI can still start and explain the problem.
    """
    try:
        from serial.tools import list_ports as _list_ports
    except ImportError:
        return []
    return [(p.device, p.description or p.device) for p in _list_ports.comports()]


class SerialSource(ByteSource):
    """A real serial port, opened as a **passive tap**.

    This analyser is wired *in parallel* with a live bus: it listens to a
    conversation between other equipment and must never become a participant.
    Two things follow from that, and both need explicit care.

    **It never transmits.** There is no write path in this class at all, and
    :meth:`write` exists only to fail loudly if some future code tries.

    **It must not assert the handshake lines.** This is the subtle one.
    pyserial defaults to ``rts=True`` and ``dtr=True`` and applies them the
    moment the port opens. On the great majority of USB-RS485 adapters - and
    on every MAX485-style breakout - RTS (sometimes DTR) drives *DE*, the
    driver enable. Opening the port with the defaults therefore switches the
    adapter's transmitter on and starts driving the pair, colliding with the
    very traffic being observed. DTR also resets boards that wire it to the
    reset line, an Arduino among them. So the port is built unopened, the
    lines are deasserted, and only then is it opened - pyserial stores the
    state and applies the deasserted values at open, instead of asserting and
    then dropping them a moment later.

    Timing note: pyserial hands over a *chunk* of bytes with a single
    timestamp, so per-byte arrival times are reconstructed backwards from the
    read instant using the character time of the current configuration. That
    is accurate enough for gap-based framing, which only needs to tell "one
    character time" apart from "several character times".
    """

    supports_reconfigure = True

    def __init__(
        self,
        port: str,
        config: SerialConfig,
        read_chunk: int = 4096,
        passive: bool = True,
        exclusive: bool = True,
    ) -> None:
        self._port = port
        self._config = config
        self._read_chunk = read_chunk
        self.passive = passive
        self.exclusive = exclusive
        self._serial = None

    @property
    def name(self) -> str:
        return self._port

    @property
    def config(self) -> SerialConfig:
        return self._config

    def open(self) -> None:
        try:
            import serial
        except ImportError as exc:  # pragma: no cover - depends on environment
            raise SourceError(
                "pyserial nao esta instalado. Rode: pip install pyserial"
            ) from exc

        handle = serial.Serial()
        handle.port = self._port
        handle.timeout = 0
        for key, value in self._config.to_pyserial().items():
            setattr(handle, key, value)
        # Nenhum controle de fluxo: o grampo nao negocia com ninguem.
        handle.rtscts = False
        handle.dsrdtr = False
        handle.xonxoff = False
        if self.passive:
            # Definidos ANTES de abrir: o pyserial guarda o estado e aplica os
            # valores desligados na abertura, em vez de ligar as linhas e so
            # depois derruba-las.
            handle.dtr = False
            handle.rts = False
        if self.exclusive:
            try:
                handle.exclusive = True
            except (AttributeError, ValueError):  # pragma: no cover - so no POSIX
                pass

        try:
            handle.open()
        except Exception as exc:  # pragma: no cover - hardware dependent
            raise SourceError(f"nao foi possivel abrir {self._port}: {exc}") from exc
        self._serial = handle

    def write(self, data: bytes) -> None:
        """Sempre falha: um grampo em paralelo nao pode transmitir."""
        raise SourceError(
            "Serial Scan e um analisador passivo, ligado em paralelo ao "
            "barramento: ele nunca transmite. Se algo tentou escrever na "
            "porta, e um erro de programacao."
        )

    def close(self) -> None:
        if self._serial is not None:
            try:
                self._serial.close()
            finally:
                self._serial = None

    def reconfigure(self, config: SerialConfig) -> None:
        self._config = config
        if self._serial is None:
            return
        for key, value in config.to_pyserial().items():
            setattr(self._serial, key, value)
        self.drain()

    def drain(self) -> None:
        if self._serial is not None:
            self._serial.reset_input_buffer()

    def read(self, timeout: float) -> list[TimedByte]:
        if self._serial is None:
            raise SourceError("porta nao esta aberta")
        deadline = time.monotonic() + timeout
        out: list[TimedByte] = []
        char_time = self._config.char_time
        while True:
            waiting = self._serial.in_waiting
            if waiting:
                chunk = self._serial.read(min(waiting, self._read_chunk))
                now = time.monotonic()
                # The last byte of the chunk landed at `now`; the ones before
                # it are spaced by one character time.
                base = now - (len(chunk) - 1) * char_time
                out.extend(
                    TimedByte(time=base + i * char_time, value=b)
                    for i, b in enumerate(chunk)
                )
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return out
            # Poll fast enough to keep gap measurement meaningful, but do not
            # spin: one character time, clamped to a sane range.
            time.sleep(min(remaining, max(0.001, min(0.02, char_time * 4))))


# ---------------------------------------------------------------------------
# Recording and replay
# ---------------------------------------------------------------------------


class CaptureWriter:
    """Append a capture to a JSON-lines file, one record per read burst."""

    def __init__(self, path: str | Path, config: SerialConfig, protocol: str) -> None:
        self.path = Path(path)
        self._handle = self.path.open("w", encoding="utf-8")
        header = {
            "type": "header",
            "version": 1,
            "protocol": protocol,
            "config": config.label,
            "created": time.strftime("%Y-%m-%dT%H:%M:%S"),
        }
        self._handle.write(json.dumps(header) + "\n")

    def write(self, items: Sequence[TimedByte]) -> None:
        for item in items:
            self._handle.write(
                json.dumps({"t": round(item.time, 6), "b": item.value}) + "\n"
            )

    def close(self) -> None:
        if not self._handle.closed:
            self._handle.close()

    def __enter__(self) -> "CaptureWriter":
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()


class ReplaySource(ByteSource):
    """Replay a capture file written by :class:`CaptureWriter`.

    Cannot be reconfigured: the bytes were already decoded by whatever
    configuration was in use during the recording.
    """

    supports_reconfigure = False

    def __init__(self, path: str | Path, realtime: bool = False) -> None:
        self.path = Path(path)
        self.realtime = realtime
        self._items: list[TimedByte] = []
        self._cursor = 0
        self._clock = 0.0
        self._config = SerialConfig(9600)
        self._protocol = "RS232"

    @property
    def name(self) -> str:
        return f"replay:{self.path.name}"

    @property
    def config(self) -> SerialConfig:
        return self._config

    @property
    def protocol(self) -> str:
        return self._protocol

    def open(self) -> None:
        if not self.path.exists():
            raise SourceError(f"arquivo de captura nao encontrado: {self.path}")
        items: list[TimedByte] = []
        with self.path.open(encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                record = json.loads(line)
                if record.get("type") == "header":
                    self._protocol = record.get("protocol", self._protocol)
                    try:
                        self._config = SerialConfig.parse(record.get("config", "9600 8N1"))
                    except ValueError:
                        pass
                    continue
                items.append(TimedByte(time=float(record["t"]), value=int(record["b"])))
        self._items = items
        self._cursor = 0
        self._clock = items[0].time if items else 0.0

    def close(self) -> None:
        self._items = []
        self._cursor = 0

    @property
    def now(self) -> float:
        return self._clock

    @property
    def exhausted(self) -> bool:
        return self._cursor >= len(self._items)

    def read(self, timeout: float) -> list[TimedByte]:
        if self.exhausted:
            if self.realtime:
                time.sleep(min(timeout, 0.05))
            return []
        if self.realtime:
            time.sleep(min(timeout, 0.05))
        horizon = self._clock + timeout
        out: list[TimedByte] = []
        while self._cursor < len(self._items) and self._items[self._cursor].time < horizon:
            out.append(self._items[self._cursor])
            self._cursor += 1
        self._clock = horizon
        return out

    def all_bytes(self) -> list[TimedByte]:
        return list(self._items)


# ---------------------------------------------------------------------------
# Simulation
# ---------------------------------------------------------------------------


@dataclass
class ScriptedFrame:
    """One frame the simulator should put on the wire."""

    body: bytes
    #: Silence before this frame, in character times.
    gap_chars: float = 8.0
    #: Appended automatically when set; ``None`` sends ``body`` untouched.
    checksum: str | None = "CRC-16/MODBUS"

    def render(self) -> bytes:
        if self.checksum is None:
            return self.body
        algorithm: ChecksumAlgorithm = ALGORITHMS_BY_NAME[self.checksum]
        return algorithm.append(self.body)


def modbus_like_script(include_new_command_after: int = 12) -> list[ScriptedFrame]:
    """A poll loop that grows a new command part-way through.

    Three slaves are polled in a loop (function 0x03, read holding registers)
    and each answers. After ``include_new_command_after`` frames a write
    command (0x06) shows up, which is what the catalog should flag as new.
    """
    script: list[ScriptedFrame] = []
    emitted = 0
    for cycle in range(6):
        for slave in (1, 2, 3):
            script.append(
                ScriptedFrame(bytes([slave, 0x03, 0x00, 0x6B, 0x00, 0x03]), gap_chars=12.0)
            )
            payload = bytes([slave, 0x03, 0x06]) + bytes(
                [0x00, 0x2A + cycle, 0x00, 0x64, 0x01, 0x00]
            )
            script.append(ScriptedFrame(payload, gap_chars=5.0))
            emitted += 2
            if emitted == include_new_command_after:
                # Modbus function 0x06 answers by echoing the request, so both
                # frames are byte-identical: the request and its response
                # cannot be told apart by content, only by timing.
                write = bytes([0x01, 0x06, 0x00, 0x10, 0x00, 0x01])
                script.append(ScriptedFrame(write, gap_chars=14.0))
                script.append(ScriptedFrame(write, gap_chars=5.0))
    return script


def ascii_script(include_new_command_after: int = 10) -> list[ScriptedFrame]:
    """Modbus-ASCII-style text traffic, the kind that really runs at 7E1.

    Frames look like ``:0103006B0003<LRC>\\r\\n``. The checksum is inside the
    text as two hex digits, so the binary checksum detector will not find it;
    what identifies this traffic is that it is readable text end to end.
    """

    def frame(payload: bytes) -> ScriptedFrame:
        lrc = ((sum(payload) ^ 0xFF) + 1) & 0xFF
        text = ":" + payload.hex().upper() + f"{lrc:02X}" + "\r\n"
        return ScriptedFrame(text.encode("ascii"), gap_chars=10.0, checksum=None)

    script: list[ScriptedFrame] = []
    emitted = 0
    for cycle in range(6):
        for slave in (1, 2):
            script.append(frame(bytes([slave, 0x03, 0x00, 0x6B, 0x00, 0x03])))
            script.append(frame(bytes([slave, 0x03, 0x04, 0x00, 0x2A + cycle])))
            emitted += 2
            if emitted == include_new_command_after:
                script.append(frame(bytes([0x01, 0x06, 0x00, 0x10, 0x00, 0x01])))
    return script


class SimulatedSource(ByteSource):
    """A virtual bus, faithful down to the bit level.

    The traffic is rendered as a line-level sample train at the *true*
    configuration, exactly as a transmitter would drive the wire. Reading it
    back runs the software UART at the *currently selected* configuration, so
    a wrong guess produces genuinely corrupted bytes instead of a made-up
    approximation of corruption. That makes the automatic detection path
    testable without any hardware attached.
    """

    supports_reconfigure = True

    def __init__(
        self,
        true_config: SerialConfig | None = None,
        script: Sequence[ScriptedFrame] | None = None,
        samples_per_bit: int = 16,
        realtime: bool = False,
        loop: bool = True,
    ) -> None:
        self.true_config = true_config or SerialConfig(9600, 8, "N", 1.0)
        self.script = list(script) if script is not None else modbus_like_script()
        self.samples_per_bit = samples_per_bit
        self.realtime = realtime
        self.loop = loop
        self._config = self.true_config
        self._samples: list[int] = []
        self._sample_rate = self.true_config.baudrate * samples_per_bit
        self._clock = 0.0
        self._decoded: list[TimedByte] | None = None
        self._times: list[float] | None = None

    @property
    def name(self) -> str:
        return f"sim:{self.true_config.label}"

    @property
    def config(self) -> SerialConfig:
        return self._config

    @property
    def now(self) -> float:
        return self._clock

    @property
    def duration(self) -> float:
        return len(self._samples) / self._sample_rate if self._samples else 0.0

    def open(self) -> None:
        self._samples = self._render()
        self._clock = 0.0
        self._decoded = None
        self._times = None

    def close(self) -> None:
        self._samples = []
        self._decoded = None
        self._times = None

    def reconfigure(self, config: SerialConfig) -> None:
        if config != self._config:
            self._config = config
            self._decoded = None
            self._times = None

    def drain(self) -> None:
        pass

    def _render(self) -> list[int]:
        samples: list[int] = []
        for scripted in self.script:
            idle_bits = scripted.gap_chars * self.true_config.bits_per_char
            samples.extend([1] * int(round(idle_bits * self.samples_per_bit)))
            samples.extend(
                encode_samples(
                    scripted.render(),
                    self.true_config,
                    self.samples_per_bit,
                    idle_before=0,
                    idle_after=0,
                )
            )
        samples.extend([1] * (self.samples_per_bit * 40))
        return samples

    def _decode_all(self) -> list[TimedByte]:
        if self._decoded is None:
            result = decode_samples(self._samples, self._sample_rate, self._config)
            # A framing error means the receiver produced a garbage character;
            # a real UART still delivers it, so keep it. That is precisely the
            # noise the scoring engine must learn to reject.
            self._decoded = [
                TimedByte(time=c.time, value=c.value & 0xFF) for c in result.chars
            ]
            self._times = None
        return self._decoded

    def _decoded_times(self) -> list[float]:
        if self._times is None:
            self._times = [item.time for item in self._decode_all()]
        return self._times

    def raw_samples(self) -> list[int]:
        """The line-level train, for the oversampling detection path."""
        return list(self._samples)

    @property
    def sample_rate(self) -> float:
        return self._sample_rate

    def read(self, timeout: float) -> list[TimedByte]:
        """Return the bytes falling in ``[clock, clock + timeout)``.

        Derived from the virtual clock rather than from a stored cursor, so
        that reconfiguring mid-capture (which changes how many characters the
        same wire produces) can never leave the reader out of step.
        """
        if not self._samples:
            raise SourceError("fonte simulada nao foi aberta")
        if self.realtime:
            time.sleep(min(timeout, 0.05))
        items = self._decode_all()
        times = self._decoded_times()
        period = self.duration
        horizon = self._clock + timeout
        out: list[TimedByte] = []
        if items and period > 0:
            first_cycle = int(self._clock // period)
            last_cycle = int(horizon // period)
            for cycle in range(first_cycle, last_cycle + 1):
                if cycle > 0 and not self.loop:
                    break
                offset = cycle * period
                lo = bisect.bisect_left(times, self._clock - offset)
                hi = bisect.bisect_left(times, horizon - offset)
                out.extend(
                    TimedByte(time=item.time + offset, value=item.value)
                    for item in items[lo:hi]
                )
        self._clock = horizon
        return out


def iter_timed_bytes(source: ByteSource, duration: float, slice_size: float = 0.05) -> Iterable[TimedByte]:
    """Read a source for ``duration`` seconds and yield everything it gave."""
    remaining = duration
    while remaining > 0:
        chunk = min(slice_size, remaining)
        yield from source.read(chunk)
        remaining -= chunk
