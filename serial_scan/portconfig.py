"""Serial port configuration: the "frame settings" the app has to discover.

A UART character on the wire looks like this (idle line = logic 1)::

    idle  start   d0 d1 d2 ... dn   parity   stop(s)  idle
    ----+       +--+--+--+-----+--+--------+--------+------
        |_______|  data bits, LSB first    |        |

So a configuration is fully described by (baudrate, bytesize, parity, stopbits).
Everything else in this package is built on top of that quadruple.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Iterator

#: Baud rates worth probing, ordered from the most to the least common.
STANDARD_BAUDRATES: tuple[int, ...] = (
    9600,
    19200,
    115200,
    38400,
    57600,
    4800,
    2400,
    1200,
    76800,
    128000,
    230400,
    250000,
    460800,
    500000,
    921600,
    600,
    300,
    14400,
    28800,
)

PARITY_NONE = "N"
PARITY_EVEN = "E"
PARITY_ODD = "O"
PARITY_MARK = "M"
PARITY_SPACE = "S"

VALID_PARITIES = (PARITY_NONE, PARITY_EVEN, PARITY_ODD, PARITY_MARK, PARITY_SPACE)
VALID_BYTESIZES = (5, 6, 7, 8)
VALID_STOPBITS = (1.0, 1.5, 2.0)


class InvalidConfig(ValueError):
    """Raised when a :class:`SerialConfig` is built with impossible values."""


@dataclass(frozen=True, order=True)
class SerialConfig:
    """An immutable UART line configuration.

    ``stopbits`` is a float because 1.5 stop bits is legal (and only legal for
    5 data bits on most hardware, but we do not enforce that: sniffing is not
    transmitting).
    """

    baudrate: int
    bytesize: int = 8
    parity: str = PARITY_NONE
    stopbits: float = 1.0

    def __post_init__(self) -> None:
        # Normalise before validating: "e" is a perfectly reasonable way to
        # write even parity, and 1 must hash the same as 1.0.
        object.__setattr__(self, "parity", str(self.parity).upper())
        object.__setattr__(self, "stopbits", float(self.stopbits))

        if self.baudrate <= 0:
            raise InvalidConfig(f"baudrate deve ser positivo, recebido {self.baudrate}")
        if self.bytesize not in VALID_BYTESIZES:
            raise InvalidConfig(f"bytesize deve ser um de {VALID_BYTESIZES}")
        if self.parity not in VALID_PARITIES:
            raise InvalidConfig(f"paridade deve ser uma de {VALID_PARITIES}")
        if self.stopbits not in VALID_STOPBITS:
            raise InvalidConfig(f"stopbits deve ser um de {VALID_STOPBITS}")

    # -- derived timing ---------------------------------------------------

    @property
    def has_parity(self) -> bool:
        return self.parity != PARITY_NONE

    @property
    def bits_per_char(self) -> float:
        """Total bit times occupied by one character, start and stop included."""
        return 1.0 + self.bytesize + (1.0 if self.has_parity else 0.0) + self.stopbits

    @property
    def bit_time(self) -> float:
        """Duration of a single bit, in seconds."""
        return 1.0 / self.baudrate

    @property
    def char_time(self) -> float:
        """Duration of a full character, in seconds."""
        return self.bits_per_char / self.baudrate

    def gap_seconds(self, chars: float) -> float:
        """Convert a silence expressed in character times into seconds."""
        return chars * self.char_time

    # -- naming -----------------------------------------------------------

    @property
    def frame_format(self) -> str:
        """The classic ``8N1`` style shorthand."""
        stop = "1.5" if self.stopbits == 1.5 else str(int(self.stopbits))
        return f"{self.bytesize}{self.parity}{stop}"

    @property
    def label(self) -> str:
        return f"{self.baudrate} {self.frame_format}"

    def __str__(self) -> str:  # pragma: no cover - trivial
        return self.label

    # -- parsing / interop ------------------------------------------------

    @classmethod
    def parse(cls, text: str) -> "SerialConfig":
        """Parse ``"9600 8N1"``, ``"9600-8N1"`` or ``"9600/8E1.5"``."""
        cleaned = text.strip().replace("/", " ").replace("-", " ").replace(",", " ")
        parts = cleaned.split()
        if len(parts) == 1:
            # Allow a bare baudrate, defaulting to the most common framing.
            return cls(baudrate=int(parts[0]))
        if len(parts) != 2:
            raise InvalidConfig(f"nao foi possivel interpretar a configuracao {text!r}")
        baud_text, fmt = parts
        fmt = fmt.upper()
        if len(fmt) < 3:
            raise InvalidConfig(f"nao foi possivel interpretar o formato {fmt!r}")
        try:
            bytesize = int(fmt[0])
            parity = fmt[1]
            stopbits = float(fmt[2:])
        except ValueError as exc:
            raise InvalidConfig(f"nao foi possivel interpretar o formato {fmt!r}") from exc
        return cls(int(baud_text), bytesize, parity, stopbits)

    def to_pyserial(self) -> dict:
        """Keyword arguments for :class:`serial.Serial`.

        Imported lazily by the caller so that the core package keeps working
        on machines without pyserial installed.
        """
        import serial  # local import: optional dependency

        stopbit_map = {
            1.0: serial.STOPBITS_ONE,
            1.5: serial.STOPBITS_ONE_POINT_FIVE,
            2.0: serial.STOPBITS_TWO,
        }
        bytesize_map = {
            5: serial.FIVEBITS,
            6: serial.SIXBITS,
            7: serial.SEVENBITS,
            8: serial.EIGHTBITS,
        }
        return {
            "baudrate": self.baudrate,
            "bytesize": bytesize_map[self.bytesize],
            "parity": self.parity,
            "stopbits": stopbit_map[self.stopbits],
        }


#: Frame formats worth probing, ordered by how often they appear in the field.
COMMON_FRAME_FORMATS: tuple[tuple[int, str, float], ...] = (
    (8, PARITY_NONE, 1.0),
    (8, PARITY_EVEN, 1.0),
    (8, PARITY_ODD, 1.0),
    (7, PARITY_EVEN, 1.0),
    (7, PARITY_ODD, 1.0),
    (8, PARITY_NONE, 2.0),
    (7, PARITY_NONE, 1.0),
    (7, PARITY_EVEN, 2.0),
    (8, PARITY_EVEN, 2.0),
    (7, PARITY_NONE, 2.0),
)


def candidate_configs(
    baudrates: tuple[int, ...] = STANDARD_BAUDRATES,
    frame_formats: tuple[tuple[int, str, float], ...] = COMMON_FRAME_FORMATS,
) -> Iterator[SerialConfig]:
    """Yield every (baudrate x frame format) combination, most likely first."""
    for baud in baudrates:
        for bytesize, parity, stopbits in frame_formats:
            yield SerialConfig(baud, bytesize, parity, stopbits)


def nearest_standard_baudrate(measured: float, tolerance: float = 0.05) -> int | None:
    """Snap a measured bit rate to the closest standard baud rate.

    Returns ``None`` when nothing is within ``tolerance`` (5% by default),
    which is the honest answer for a badly sampled signal.
    """
    if measured <= 0:
        return None
    best: int | None = None
    best_error = float("inf")
    for baud in STANDARD_BAUDRATES:
        error = abs(measured - baud) / baud
        if error < best_error:
            best_error = error
            best = baud
    if best_error > tolerance:
        return None
    return best
