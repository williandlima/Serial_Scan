"""Software UART: encoding, decoding and bit-time measurement."""

from __future__ import annotations

import pytest

from serial_scan.portconfig import SerialConfig
from serial_scan.uart import (
    bytes_to_samples,
    decode_samples,
    encode_samples,
    estimate_baudrate,
    parity_bit,
    run_lengths,
)

FORMATS = ["8N1", "8E1", "8O1", "7E1", "7O1", "8N2", "7N1"]
PAYLOAD_8BIT = bytes([0x01, 0x03, 0x00, 0x6B, 0xFF, 0x55, 0xAA, 0x00, 0x7F, 0x80])


def _payload_for(config: SerialConfig) -> bytes:
    mask = (1 << config.bytesize) - 1
    return bytes(b & mask for b in PAYLOAD_8BIT)


@pytest.mark.parametrize("fmt", FORMATS)
@pytest.mark.parametrize("baud", [1200, 9600, 115200])
def test_roundtrip_is_lossless(fmt: str, baud: int) -> None:
    config = SerialConfig.parse(f"{baud} {fmt}")
    payload = _payload_for(config)
    samples = encode_samples(payload, config, samples_per_bit=16)
    result = decode_samples(samples, config.baudrate * 16, config)

    assert result.data == payload
    assert result.framing_errors == 0
    assert result.parity_errors == 0
    assert result.glitches == 0


@pytest.mark.parametrize("fmt", FORMATS)
def test_roundtrip_with_gaps_between_characters(fmt: str) -> None:
    config = SerialConfig.parse(f"9600 {fmt}")
    payload = _payload_for(config)
    samples = encode_samples(payload, config, 16, inter_char_idle_bits=3.5)
    result = decode_samples(samples, config.baudrate * 16, config)
    assert result.data == payload
    assert result.error_rate == 0.0


def test_back_to_back_characters_are_not_dropped() -> None:
    """No idle at all between characters is the tightest case for resync."""
    config = SerialConfig(9600, 8, "N", 1.0)
    payload = bytes(range(64))
    samples = encode_samples(payload, config, 16, inter_char_idle_bits=0.0)
    result = decode_samples(samples, config.baudrate * 16, config)
    assert result.data == payload


def test_parity_bit_matches_definition() -> None:
    # 0x03 has two set bits: even parity appends 0, odd parity appends 1.
    assert parity_bit(0x03, 8, "E") == 0
    assert parity_bit(0x03, 8, "O") == 1
    # 0x07 has three set bits.
    assert parity_bit(0x07, 8, "E") == 1
    assert parity_bit(0x07, 8, "O") == 0
    assert parity_bit(0x00, 8, "M") == 1
    assert parity_bit(0xFF, 8, "S") == 0


def test_wrong_parity_hypothesis_is_reported() -> None:
    """Decoding an even-parity line as odd must flag, not silently accept."""
    truth = SerialConfig(9600, 8, "E", 1.0)
    samples = encode_samples(PAYLOAD_8BIT, truth, 16)
    wrong = decode_samples(samples, truth.baudrate * 16, SerialConfig(9600, 8, "O", 1.0))

    assert wrong.parity_errors == len(wrong.chars)
    # The data bits themselves are still recovered: that is exactly why parity
    # cannot be told apart from the byte stream alone.
    assert wrong.data == PAYLOAD_8BIT


def test_wrong_baudrate_produces_a_broken_stream() -> None:
    truth = SerialConfig(9600, 8, "N", 1.0)
    samples = encode_samples(PAYLOAD_8BIT * 4, truth, 16)
    wrong = decode_samples(samples, truth.baudrate * 16, SerialConfig(19200, 8, "N", 1.0))
    assert wrong.data != PAYLOAD_8BIT * 4


def test_decoder_refuses_insufficient_oversampling() -> None:
    config = SerialConfig(9600, 8, "N", 1.0)
    samples = encode_samples(PAYLOAD_8BIT, config, 16)
    # Claiming a sample rate barely above the baud rate leaves no mid-bit point.
    result = decode_samples(samples, config.baudrate * 2, config)
    assert result.chars == []


@pytest.mark.parametrize("baud", [300, 1200, 9600, 19200, 57600, 115200, 921600])
@pytest.mark.parametrize("spb", [8, 16, 32])
def test_baudrate_is_measured_from_the_shortest_pulses(baud: int, spb: int) -> None:
    config = SerialConfig(baud, 8, "N", 1.0)
    samples = encode_samples(PAYLOAD_8BIT * 3, config, spb, inter_char_idle_bits=2.0)
    estimate = estimate_baudrate(samples, baud * spb)

    assert estimate is not None
    assert estimate.snapped == baud
    assert estimate.samples_per_bit == pytest.approx(spb, rel=0.05)


def test_baudrate_estimate_needs_transitions() -> None:
    assert estimate_baudrate([1] * 1000, 100000) is None
    assert estimate_baudrate([], 100000) is None


def test_run_lengths() -> None:
    assert run_lengths([1, 1, 0, 0, 0, 1]) == [2, 3, 1]
    assert run_lengths([]) == []
    assert run_lengths([1]) == [1]


def test_bytes_to_samples_is_lsb_first_by_default() -> None:
    assert bytes_to_samples(b"\x01") == [1, 0, 0, 0, 0, 0, 0, 0]
    assert bytes_to_samples(b"\x01", msb_first=True) == [0, 0, 0, 0, 0, 0, 0, 1]
    assert bytes_to_samples(b"\xF0") == [0, 0, 0, 0, 1, 1, 1, 1]


def test_encode_samples_rejects_undersampling() -> None:
    with pytest.raises(ValueError):
        encode_samples(b"\x00", SerialConfig(9600), samples_per_bit=2)


def test_stop_bit_length_is_respected() -> None:
    """8N2 occupies two more sample-widths per character than 8N1."""
    one = encode_samples(b"\x00" * 10, SerialConfig(9600, 8, "N", 1.0), 16, 0, 0)
    two = encode_samples(b"\x00" * 10, SerialConfig(9600, 8, "N", 2.0), 16, 0, 0)
    assert len(two) - len(one) == 10 * 16
