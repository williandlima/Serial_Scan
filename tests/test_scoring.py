"""Plausibility scoring: telling a good configuration from a bad one."""

from __future__ import annotations

import pytest

from serial_scan.checksums import ALGORITHMS_BY_NAME
from serial_scan.framing import Frame
from serial_scan.scoring import (
    EVIDENCE_FLOOR,
    byte_health,
    length_consistency,
    printable_ratio,
    repetition,
    score_frames,
    text_evidence,
)

CRC = ALGORITHMS_BY_NAME["CRC-16/MODBUS"]


def frames_from(payloads: list[bytes]) -> list[Frame]:
    return [
        Frame(data=data, time_start=index * 0.01, time_end=index * 0.01 + 0.005, index=index)
        for index, data in enumerate(payloads)
    ]


def poll_loop(count: int = 30) -> list[Frame]:
    """Realistic traffic: a repeating poll with a valid CRC on every frame."""
    payloads = []
    for index in range(count):
        slave = (index % 3) + 1
        payloads.append(CRC.append(bytes([slave, 0x03, 0x00, 0x6B, 0x00, 0x03])))
    return frames_from(payloads)


def garbage(count: int = 30, seed: int = 7) -> list[Frame]:
    """What a wrong baud rate produces: drifting boundaries, no valid CRC."""
    import random

    rng = random.Random(seed)
    payloads = []
    for _ in range(count):
        length = rng.randint(3, 19)
        payloads.append(bytes(rng.choice([0x00, 0xFF, rng.randrange(256)]) for _ in range(length)))
    return frames_from(payloads)


class TestMetrics:
    def test_byte_health_penalises_stuck_lines(self) -> None:
        assert byte_health(bytes(range(64))) > 0.9
        assert byte_health(b"\xFF" * 64) < 0.3
        assert byte_health(b"\x00" * 64) < 0.3
        assert byte_health(b"") == 0.0

    def test_byte_health_tolerates_normal_zeros(self) -> None:
        payload = bytes([0x01, 0x03, 0x00, 0x6B, 0x00, 0x03] * 10)
        assert byte_health(payload) > 0.7

    def test_printable_ratio(self) -> None:
        assert printable_ratio(b"HELLO\r\n") == 1.0
        assert printable_ratio(b"\x00\x01\x02\x03") == 0.0
        assert printable_ratio(b"") == 0.0

    def test_text_evidence_is_strict(self) -> None:
        assert text_evidence(b"CONFIGURACAO OK\r\n" * 4) == 1.0
        # Binary data sits near 40% printable by chance and must score zero.
        assert text_evidence(bytes(range(256))) == 0.0

    def test_length_consistency(self) -> None:
        same = frames_from([b"\x01" * 8] * 20)
        assert length_consistency(same) > 0.9
        varied = frames_from([b"\x01" * n for n in range(3, 23)])
        assert length_consistency(varied) < 0.2
        assert length_consistency(frames_from([b"\x01"])) == 0.0

    def test_repetition_recognises_a_poll_loop(self) -> None:
        assert repetition(poll_loop(), (0, 1)) > 0.8
        unique = frames_from([bytes([i, i, i, i]) for i in range(30)])
        assert repetition(unique, (0, 1)) < 0.3


class TestScoring:
    def test_real_traffic_scores_high(self) -> None:
        score = score_frames(poll_loop(), key_offsets=(0, 1))
        assert score.total > 0.75
        assert score.checksum is not None
        assert score.checksum.name == "CRC-16/MODBUS"

    def test_garbage_scores_low(self) -> None:
        assert score_frames(garbage(), key_offsets=(0, 1)).total < 0.45

    def test_repetitive_garbage_is_still_rejected(self) -> None:
        """The case a weighted sum gets wrong.

        A poll loop decoded at the wrong baud rate repeats just as faithfully
        as the right one, so structural metrics alone score it perfectly. Only
        the absence of evidence must hold it down.
        """
        repetitive_noise = frames_from([bytes([0x9C, 0x4E, 0x13, 0xE7, 0x08])] * 30)
        score = score_frames(repetitive_noise, key_offsets=(0, 1))

        assert score.parts["repetition"] > 0.9
        assert score.parts["length_consistency"] > 0.9
        assert score.parts["evidence"] == 0.0
        assert score.total <= EVIDENCE_FLOOR

    def test_text_without_a_checksum_still_scores_well(self) -> None:
        lines = [f":0103006B0003{index:02X}\r\n".encode() for index in range(20)]
        score = score_frames(frames_from(lines), key_offsets=(0, 1))
        assert score.checksum is None
        assert score.parts["evidence"] == 1.0
        assert score.total > 0.6

    def test_short_captures_are_discounted(self) -> None:
        few = score_frames(poll_loop(2), key_offsets=(0, 1), min_frames=3)
        many = score_frames(poll_loop(30), key_offsets=(0, 1), min_frames=3)
        assert few.total < many.total
        assert any("confianca reduzida" in note for note in few.notes)

    def test_empty_capture(self) -> None:
        score = score_frames([], key_offsets=(0, 1))
        assert score.total == 0.0
        assert score.frames == 0

    def test_notes_explain_the_verdict(self) -> None:
        good = score_frames(poll_loop(), key_offsets=(0, 1))
        assert any("CRC-16/MODBUS" in note for note in good.notes)
        bad = score_frames(garbage(), key_offsets=(0, 1))
        assert any("Sem evidencia" in note for note in bad.notes)

    def test_percent_is_reported(self) -> None:
        score = score_frames(poll_loop(), key_offsets=(0, 1))
        assert score.percent == pytest.approx(round(score.total * 100), abs=1)
        assert 0 <= score.percent <= 100
