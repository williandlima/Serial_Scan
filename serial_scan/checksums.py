"""Checksum / CRC detection.

Once frames are separated, finding the trailing integrity field is the single
most valuable confirmation the analyser can get: if the same algorithm
validates dozens of frames of different lengths, then the baud rate, the frame
format *and* the framing rule are all certainly right. It is also what tells
the operator where the payload ends and the trailer begins.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Iterable, Sequence

# ---------------------------------------------------------------------------
# Algorithms
# ---------------------------------------------------------------------------


def sum8(data: bytes) -> int:
    return sum(data) & 0xFF


def sum8_twos_complement(data: bytes) -> int:
    return (-sum(data)) & 0xFF


def xor8(data: bytes) -> int:
    acc = 0
    for byte in data:
        acc ^= byte
    return acc


def lrc8(data: bytes) -> int:
    """Longitudinal redundancy check used by Modbus ASCII and many terminals."""
    return ((sum(data) ^ 0xFF) + 1) & 0xFF


def crc8_dallas(data: bytes) -> int:
    """CRC-8/MAXIM (Dallas / 1-Wire), poly 0x31 reflected."""
    crc = 0
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = (crc >> 1) ^ 0x8C if crc & 1 else crc >> 1
    return crc & 0xFF


def crc16_modbus(data: bytes) -> int:
    """CRC-16/MODBUS: poly 0xA001 reflected, init 0xFFFF."""
    crc = 0xFFFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
    return crc & 0xFFFF


def crc16_ccitt_false(data: bytes) -> int:
    """CRC-16/IBM-3740, often labelled "CCITT-FALSE": poly 0x1021, init 0xFFFF."""
    crc = 0xFFFF
    for byte in data:
        crc ^= byte << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) & 0xFFFF if crc & 0x8000 else (crc << 1) & 0xFFFF
    return crc & 0xFFFF


def crc16_kermit(data: bytes) -> int:
    """CRC-16/KERMIT: poly 0x1021 reflected, init 0x0000."""
    crc = 0x0000
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = (crc >> 1) ^ 0x8408 if crc & 1 else crc >> 1
    return crc & 0xFFFF


def crc16_xmodem(data: bytes) -> int:
    """CRC-16/XMODEM: poly 0x1021, init 0x0000, not reflected."""
    crc = 0x0000
    for byte in data:
        crc ^= byte << 8
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1021) & 0xFFFF if crc & 0x8000 else (crc << 1) & 0xFFFF
    return crc & 0xFFFF


@dataclass(frozen=True)
class ChecksumAlgorithm:
    name: str
    width: int  # bytes occupied in the frame
    compute: Callable[[bytes], int]
    #: ``"little"`` or ``"big"``; irrelevant for one-byte checksums.
    byteorder: str = "big"
    description: str = ""

    def encode(self, value: int) -> bytes:
        return value.to_bytes(self.width, self.byteorder)  # type: ignore[arg-type]

    def check(self, frame: bytes) -> bool:
        """True when the last ``width`` bytes match the checksum of the rest."""
        if len(frame) <= self.width:
            return False
        body, trailer = frame[: -self.width], frame[-self.width :]
        return self.encode(self.compute(body)) == trailer

    def append(self, body: bytes) -> bytes:
        return body + self.encode(self.compute(body))


#: Ordered by how likely they are on an industrial serial bus.
ALGORITHMS: tuple[ChecksumAlgorithm, ...] = (
    ChecksumAlgorithm("CRC-16/MODBUS", 2, crc16_modbus, "little", "Modbus RTU (LSB primeiro)"),
    ChecksumAlgorithm("CRC-16/MODBUS-BE", 2, crc16_modbus, "big", "CRC Modbus com MSB primeiro"),
    ChecksumAlgorithm("CRC-16/CCITT-FALSE", 2, crc16_ccitt_false, "big", "Poly 0x1021, init 0xFFFF"),
    ChecksumAlgorithm("CRC-16/KERMIT", 2, crc16_kermit, "little", "Poly 0x1021 refletido"),
    ChecksumAlgorithm("CRC-16/XMODEM", 2, crc16_xmodem, "big", "Poly 0x1021, init 0x0000"),
    ChecksumAlgorithm("SUM-8", 1, sum8, "big", "Soma simples truncada em 8 bits"),
    ChecksumAlgorithm("SUM-8/2C", 1, sum8_twos_complement, "big", "Complemento de dois da soma"),
    ChecksumAlgorithm("XOR-8", 1, xor8, "big", "OU-exclusivo de todos os bytes"),
    ChecksumAlgorithm("LRC-8", 1, lrc8, "big", "LRC do Modbus ASCII"),
    ChecksumAlgorithm("CRC-8/MAXIM", 1, crc8_dallas, "big", "CRC-8 Dallas / 1-Wire"),
)

ALGORITHMS_BY_NAME = {algorithm.name: algorithm for algorithm in ALGORITHMS}


# ---------------------------------------------------------------------------
# Detection
# ---------------------------------------------------------------------------


@dataclass
class ChecksumMatch:
    algorithm: ChecksumAlgorithm
    valid: int
    tested: int

    @property
    def ratio(self) -> float:
        return self.valid / self.tested if self.tested else 0.0

    @property
    def name(self) -> str:
        return self.algorithm.name

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<ChecksumMatch {self.name} {self.valid}/{self.tested}>"


def detect_checksum(
    frames: Sequence[bytes] | Iterable[bytes],
    min_ratio: float = 0.9,
    min_frames: int = 3,
    algorithms: Sequence[ChecksumAlgorithm] = ALGORITHMS,
) -> ChecksumMatch | None:
    """Find the algorithm that validates the largest share of frames.

    A one-byte checksum matches 1 frame in 256 by chance, so ``min_frames``
    guards against declaring victory on a handful of samples. Frames shorter
    than the trailer are skipped rather than counted as failures.
    """
    bodies = [f for f in frames if f]
    if len(bodies) < min_frames:
        return None

    best: ChecksumMatch | None = None
    for algorithm in algorithms:
        testable = [f for f in bodies if len(f) > algorithm.width + 1]
        if len(testable) < min_frames:
            continue
        valid = sum(1 for f in testable if algorithm.check(f))
        match = ChecksumMatch(algorithm, valid, len(testable))
        if match.ratio < min_ratio:
            continue
        if best is None or (match.ratio, match.algorithm.width) > (
            best.ratio,
            best.algorithm.width,
        ):
            # Wider trailers win ties: a 16-bit CRC matching everywhere is far
            # less likely to be a coincidence than an 8-bit XOR.
            best = match
    return best


def checksum_confidence(match: ChecksumMatch | None) -> float:
    """Turn a match into a 0..1 confidence contribution.

    Chance of a false positive is ``ratio ** frames`` over a 2^(8*width)
    space, so both the number of frames and the trailer width matter.
    """
    if match is None or match.tested == 0:
        return 0.0
    strength = min(1.0, match.tested / 10.0)
    width_bonus = 1.0 if match.algorithm.width >= 2 else 0.7
    return match.ratio * strength * width_bonus
