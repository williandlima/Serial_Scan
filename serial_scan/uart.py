"""Software UART: turn line samples into characters and back.

This module is what makes the automatic identification of the frame settings
(step 2 of the project) possible *without* guessing blindly.

Two capture strategies exist and both end up here:

``oversampling``
    The adapter is opened at a much higher baud rate than the line under
    test, so each received byte is really 8 samples of the line level. The
    resulting sample train is decoded here in software, which lets us measure
    the bit time and try every framing hypothesis on the exact same data.

``candidate scan``
    The adapter is opened at a candidate configuration and we score whatever
    comes out. No bit-level access, so :mod:`serial_scan.scoring` does the
    work instead.

Samples are plain ``0``/``1`` integers, oldest first, taken at a constant
``sample_rate`` (Hz). Idle line is logic ``1``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Sequence

from .portconfig import (
    PARITY_EVEN,
    PARITY_MARK,
    PARITY_ODD,
    PARITY_SPACE,
    SerialConfig,
    nearest_standard_baudrate,
)

IDLE = 1
START = 0


def parity_bit(value: int, bytesize: int, parity: str) -> int:
    """Return the parity bit that a transmitter would append to ``value``."""
    if parity == PARITY_MARK:
        return 1
    if parity == PARITY_SPACE:
        return 0
    ones = bin(value & ((1 << bytesize) - 1)).count("1")
    if parity == PARITY_EVEN:
        return ones & 1
    if parity == PARITY_ODD:
        return 1 - (ones & 1)
    raise ValueError(f"no parity bit for parity {parity!r}")


def encode_char(value: int, config: SerialConfig) -> list[int]:
    """Encode one character as full-bit levels: start, data (LSB first), parity.

    The stop period is *not* included because it can last a fractional number
    of bit times; :func:`encode_samples` appends it at sample resolution.
    """
    mask = (1 << config.bytesize) - 1
    value &= mask
    bits = [START]
    bits.extend((value >> i) & 1 for i in range(config.bytesize))
    if config.has_parity:
        bits.append(parity_bit(value, config.bytesize, config.parity))
    return bits


def encode_samples(
    data: Iterable[int],
    config: SerialConfig,
    samples_per_bit: int,
    idle_before: int = 8,
    idle_after: int = 8,
    inter_char_idle_bits: float = 0.0,
) -> list[int]:
    """Render a byte sequence as an oversampled line-level train.

    Used by the tests and by the traffic simulator; also handy to build
    reference captures. ``samples_per_bit`` must be at least 4 for the decoder
    to have a usable mid-bit sampling point.
    """
    if samples_per_bit < 4:
        raise ValueError("samples_per_bit must be >= 4 to be decodable")
    stop_samples = int(round(config.stopbits * samples_per_bit))
    gap_samples = int(round(inter_char_idle_bits * samples_per_bit))
    samples: list[int] = [IDLE] * (idle_before * samples_per_bit)
    for value in data:
        for bit in encode_char(value, config):
            samples.extend([bit] * samples_per_bit)
        samples.extend([IDLE] * stop_samples)
        if gap_samples:
            samples.extend([IDLE] * gap_samples)
    samples.extend([IDLE] * (idle_after * samples_per_bit))
    return samples


@dataclass
class DecodedChar:
    """One character recovered from the sample train."""

    value: int
    start_index: int
    time: float
    framing_error: bool = False
    parity_error: bool = False

    @property
    def ok(self) -> bool:
        return not (self.framing_error or self.parity_error)


@dataclass
class DecodeResult:
    config: SerialConfig
    sample_rate: float
    chars: list[DecodedChar] = field(default_factory=list)
    #: Falling edges that did not survive the mid-start-bit check.
    glitches: int = 0

    @property
    def data(self) -> bytes:
        return bytes(c.value & 0xFF for c in self.chars)

    @property
    def clean_data(self) -> bytes:
        return bytes(c.value & 0xFF for c in self.chars if c.ok)

    @property
    def framing_errors(self) -> int:
        return sum(1 for c in self.chars if c.framing_error)

    @property
    def parity_errors(self) -> int:
        return sum(1 for c in self.chars if c.parity_error)

    @property
    def error_rate(self) -> float:
        if not self.chars:
            return 1.0
        bad = sum(1 for c in self.chars if not c.ok)
        return bad / len(self.chars)

    def __len__(self) -> int:  # pragma: no cover - trivial
        return len(self.chars)


def decode_samples(
    samples: Sequence[int],
    sample_rate: float,
    config: SerialConfig,
    t0: float = 0.0,
) -> DecodeResult:
    """Decode a sample train under one framing hypothesis.

    The decoder behaves like a real UART receiver: it hunts for a falling
    edge, confirms it in the middle of the start bit, samples every following
    bit at its centre, and resynchronises on the next edge after an error.
    """
    result = DecodeResult(config=config, sample_rate=sample_rate)
    bit_samples = sample_rate / config.baudrate
    if bit_samples < 3:
        # Below ~3 samples per bit the mid-bit points are meaningless.
        return result

    n = len(samples)
    data_bits = config.bytesize
    parity_slots = 1 if config.has_parity else 0
    # Index of the first stop bit, counting the start bit as index 0.
    stop_index = 1 + data_bits + parity_slots

    i = 1
    while i < n:
        # Hunt for a falling edge (idle -> start).
        if not (samples[i - 1] == IDLE and samples[i] == START):
            i += 1
            continue
        edge = i
        mid_start = edge + bit_samples / 2.0
        if mid_start >= n:
            break
        if samples[int(mid_start)] != START:
            result.glitches += 1
            i = edge + 1
            continue

        def sample_at(bit_position: float) -> int | None:
            idx = int(edge + bit_samples * (bit_position + 0.5))
            if idx >= n:
                return None
            return samples[idx]

        value = 0
        truncated = False
        for k in range(data_bits):
            bit = sample_at(1 + k)
            if bit is None:
                truncated = True
                break
            value |= bit << k
        if truncated:
            break

        parity_error = False
        if config.has_parity:
            observed = sample_at(1 + data_bits)
            if observed is None:
                break
            expected = parity_bit(value, data_bits, config.parity)
            parity_error = observed != expected

        stop = sample_at(stop_index)
        if stop is None:
            break
        framing_error = stop != IDLE

        result.chars.append(
            DecodedChar(
                value=value,
                start_index=edge,
                time=t0 + edge / sample_rate,
                framing_error=framing_error,
                parity_error=parity_error,
            )
        )

        if framing_error:
            # The byte boundary is not trustworthy any more. Behave like real
            # hardware: wait for the line to go idle again before accepting a
            # new start bit, otherwise a 1->0 transition inside the data bits
            # would be mistaken for one.
            i = edge + int(bit_samples)
            while i < n and samples[i] != IDLE:
                i += 1
        else:
            # Resume a fraction of a bit before the stop period ends, so the
            # falling edge of a back-to-back character is never missed.
            resume = stop_index + max(0.1, config.stopbits - 0.4)
            i = max(edge + 1, edge + int(bit_samples * resume))
    return result


# ---------------------------------------------------------------------------
# Bit-time estimation
# ---------------------------------------------------------------------------


def run_lengths(samples: Sequence[int]) -> list[int]:
    """Length of every constant-level run in the sample train."""
    if not samples:
        return []
    runs: list[int] = []
    current = samples[0]
    length = 1
    for value in samples[1:]:
        if value == current:
            length += 1
        else:
            runs.append(length)
            current = value
            length = 1
    runs.append(length)
    return runs


@dataclass
class BaudEstimate:
    measured: float
    snapped: int | None
    samples_per_bit: float
    fit_error: float
    runs_considered: int

    @property
    def baudrate(self) -> int | None:
        return self.snapped


def estimate_baudrate(
    samples: Sequence[int],
    sample_rate: float,
    tolerance: float = 0.05,
) -> BaudEstimate | None:
    """Measure the bit time from the shortest pulses in the capture.

    The shortest run in a UART stream is exactly one bit time (a lone ``0`` or
    ``1`` between two opposite bits). Longer runs are integer multiples of it,
    so the correct bit time is the one that makes every run land closest to a
    whole number of bits. Long idle runs are dropped first: they carry no
    timing information and would dominate the fit.
    """
    runs = run_lengths(samples)
    if len(runs) < 4:
        return None
    # Drop the leading/trailing idle and any run longer than ~16 bit times
    # relative to the shortest observed pulse.
    shortest = min(runs)
    if shortest <= 0:
        return None
    useful = [r for r in runs if r <= shortest * 16]
    if len(useful) < 4:
        useful = runs

    best_cost = float("inf")
    best_spb = float(shortest)
    # Search around the shortest run: it is the best first guess, but noise can
    # make it a little short, so scan a window and keep the cleanest fit.
    lo = max(1.0, shortest * 0.7)
    hi = shortest * 1.6
    steps = 240
    for step in range(steps + 1):
        spb = lo + (hi - lo) * step / steps
        if spb < 1.0:
            continue
        cost = 0.0
        for run in useful:
            multiples = run / spb
            if multiples < 0.5:
                cost += 1.0
                continue
            cost += abs(multiples - round(multiples)) ** 2
        cost /= len(useful)
        # Prefer larger bit times when the fit is equivalent: a half-size bit
        # time also fits every run, and would double the reported baud rate.
        cost -= spb * 1e-9
        if cost < best_cost:
            best_cost = cost
            best_spb = spb

    measured = sample_rate / best_spb
    snapped = nearest_standard_baudrate(measured, tolerance=tolerance)
    return BaudEstimate(
        measured=measured,
        snapped=snapped,
        samples_per_bit=best_spb,
        fit_error=max(best_cost, 0.0),
        runs_considered=len(useful),
    )


def bytes_to_samples(raw: bytes, msb_first: bool = False) -> list[int]:
    """Expand bytes captured by an oversampling adapter into line samples.

    When a port is opened at N times the line's baud rate with 8N1 framing,
    every received byte holds 8 consecutive samples of the line. The UART
    delivers data bits LSB first, so that is the default order here.
    """
    samples: list[int] = []
    for byte in raw:
        if msb_first:
            samples.extend((byte >> i) & 1 for i in range(7, -1, -1))
        else:
            samples.extend((byte >> i) & 1 for i in range(8))
    return samples
