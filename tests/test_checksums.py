"""Checksum algorithms and automatic detection of which one a bus uses."""

from __future__ import annotations

import pytest

from serial_scan.checksums import (
    ALGORITHMS,
    ALGORITHMS_BY_NAME,
    checksum_confidence,
    crc8_dallas,
    crc16_ccitt_false,
    crc16_kermit,
    crc16_modbus,
    crc16_xmodem,
    detect_checksum,
    lrc8,
    sum8,
    sum8_twos_complement,
    xor8,
)

CHECK_INPUT = b"123456789"


@pytest.mark.parametrize(
    "function, expected",
    [
        # Published check values for the string "123456789".
        (crc16_modbus, 0x4B37),
        (crc16_ccitt_false, 0x29B1),
        (crc16_kermit, 0x2189),
        (crc16_xmodem, 0x31C3),
        (crc8_dallas, 0xA1),
    ],
)
def test_known_check_vectors(function, expected: int) -> None:
    assert function(CHECK_INPUT) == expected


def test_simple_checksums() -> None:
    assert sum8(b"\x01\x02\x03") == 0x06
    assert sum8(b"\xFF\x02") == 0x01  # truncated to 8 bits
    assert sum8_twos_complement(b"\x01\x02\x03") == 0xFA
    assert xor8(b"\x0F\xF0") == 0xFF
    assert xor8(b"\xAA\xAA") == 0x00
    assert lrc8(b"\x01\x02\x03") == 0xFA


@pytest.mark.parametrize("algorithm", ALGORITHMS, ids=lambda a: a.name)
def test_append_then_check_round_trips(algorithm) -> None:
    body = bytes([0x01, 0x03, 0x00, 0x6B, 0x00, 0x03])
    framed = algorithm.append(body)
    assert len(framed) == len(body) + algorithm.width
    assert algorithm.check(framed)
    # Corrupting any payload byte must break it.
    broken = bytearray(framed)
    broken[2] ^= 0xFF
    assert not algorithm.check(bytes(broken))


def test_check_rejects_frames_shorter_than_the_trailer() -> None:
    crc = ALGORITHMS_BY_NAME["CRC-16/MODBUS"]
    assert not crc.check(b"\x01")
    assert not crc.check(b"\x01\x02")


@pytest.mark.parametrize(
    "name", ["CRC-16/MODBUS", "CRC-16/CCITT-FALSE", "XOR-8", "SUM-8", "LRC-8"]
)
def test_detection_finds_the_algorithm_actually_used(name: str) -> None:
    algorithm = ALGORITHMS_BY_NAME[name]
    frames = [
        algorithm.append(bytes([slave, 0x03, 0x00, 0x6B, 0x00, index]))
        for slave in (1, 2, 3)
        for index in range(4)
    ]
    match = detect_checksum(frames)
    assert match is not None
    assert match.ratio == 1.0
    # A different algorithm may coincide on some frames, but the winner must
    # be the real one (or an alias computing the same trailer).
    assert algorithm.compute(frames[0][: -algorithm.width]) == match.algorithm.compute(
        frames[0][: -match.algorithm.width]
    )


def test_detection_returns_none_without_a_checksum() -> None:
    frames = [bytes([1, 2, 3, 4, 5, 6, 7, index]) for index in range(20)]
    assert detect_checksum(frames) is None


def test_detection_needs_a_minimum_number_of_frames() -> None:
    crc = ALGORITHMS_BY_NAME["CRC-16/MODBUS"]
    frames = [crc.append(b"\x01\x03\x00\x01")]
    assert detect_checksum(frames, min_frames=3) is None


def test_detection_tolerates_a_few_corrupt_frames() -> None:
    crc = ALGORITHMS_BY_NAME["CRC-16/MODBUS"]
    frames = [crc.append(bytes([1, 3, 0, index])) for index in range(20)]
    frames.append(b"\x01\x03\x00\x63\xDE\xAD")  # noise on the bus
    match = detect_checksum(frames, min_ratio=0.9)
    assert match is not None
    assert match.valid == 20
    assert match.tested == 21


def test_confidence_grows_with_evidence() -> None:
    crc = ALGORITHMS_BY_NAME["CRC-16/MODBUS"]
    few = detect_checksum([crc.append(bytes([1, 3, i])) for i in range(4)])
    many = detect_checksum([crc.append(bytes([1, 3, i])) for i in range(40)])
    assert checksum_confidence(few) < checksum_confidence(many)
    assert checksum_confidence(None) == 0.0
    assert checksum_confidence(many) == pytest.approx(1.0)


def test_wide_trailers_win_ties() -> None:
    """A 16-bit CRC matching everywhere beats an 8-bit one that also matches."""
    crc = ALGORITHMS_BY_NAME["CRC-16/MODBUS"]
    frames = [crc.append(bytes([1, 3, 0, index])) for index in range(30)]
    match = detect_checksum(frames)
    assert match is not None
    assert match.algorithm.width == 2
