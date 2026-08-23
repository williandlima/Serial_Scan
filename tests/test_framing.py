"""Splitting a byte stream into frames by inter-character silence."""

from __future__ import annotations

import pytest

from serial_scan.framing import (
    Direction,
    Frame,
    FrameSplitter,
    GapHistogram,
    TimedByte,
    split_frames,
    synth_timed_bytes,
)
from serial_scan.portconfig import SerialConfig

CONFIG = SerialConfig(9600, 8, "N", 1.0)
CHAR = CONFIG.char_time


def stream(*bursts: bytes, gap_chars: float = 8.0) -> list[TimedByte]:
    """Build a timed byte stream from bursts separated by a silence."""
    items: list[TimedByte] = []
    t = 0.0
    for burst in bursts:
        for byte in burst:
            items.append(TimedByte(time=t, value=byte))
            t += CHAR
        t += gap_chars * CHAR
    return items


def test_bursts_become_frames() -> None:
    frames = split_frames(stream(b"\x01\x02\x03", b"\xAA\xBB"), 3.5 * CHAR)
    assert [f.data for f in frames] == [b"\x01\x02\x03", b"\xAA\xBB"]
    assert [f.index for f in frames] == [0, 1]


def test_gap_below_threshold_keeps_one_frame() -> None:
    frames = split_frames(stream(b"\x01\x02", b"\x03\x04", gap_chars=2.0), 3.5 * CHAR)
    assert [f.data for f in frames] == [b"\x01\x02\x03\x04"]


def test_first_frame_has_infinite_gap_before() -> None:
    frames = split_frames(stream(b"\x01", b"\x02"), 3.5 * CHAR)
    assert frames[0].gap_before == float("inf")
    # ``stream`` advances one character past the last byte before adding the
    # gap, and ``time_end`` is that last byte's own timestamp, so the silence
    # measured between the two frames is one character longer than the gap.
    assert frames[1].gap_before == pytest.approx(9.0 * CHAR, rel=0.01)


def test_oversized_burst_is_cut_at_max_bytes() -> None:
    items = [TimedByte(time=i * CHAR, value=i & 0xFF) for i in range(20)]
    frames = split_frames(items, 3.5 * CHAR, max_bytes=8)
    assert [len(f) for f in frames] == [8, 8, 4]


def test_continuous_stream_is_cut_at_max_duration() -> None:
    items = [TimedByte(time=i * CHAR, value=0x55) for i in range(400)]
    frames = split_frames(items, 3.5 * CHAR, max_bytes=10_000, max_duration=0.05)
    assert len(frames) > 1
    assert all(f.duration <= 0.06 for f in frames)


def test_tick_closes_a_frame_after_the_silence() -> None:
    splitter = FrameSplitter(3.5 * CHAR)
    assert splitter.feed(TimedByte(0.0, 0x01)) == []
    assert splitter.feed(TimedByte(CHAR, 0x02)) == []
    # Not yet: the silence has not elapsed.
    assert splitter.tick(CHAR + 1.0 * CHAR) == []
    closed = splitter.tick(CHAR + 4.0 * CHAR)
    assert [f.data for f in closed] == [b"\x01\x02"]
    assert splitter.pending == 0


def test_flush_emits_the_tail() -> None:
    splitter = FrameSplitter(3.5 * CHAR)
    splitter.feed(TimedByte(0.0, 0x99))
    assert [f.data for f in splitter.flush()] == [b"\x99"]
    assert splitter.flush() == []


def test_splitter_rejects_nonsense_threshold() -> None:
    with pytest.raises(ValueError):
        FrameSplitter(0)


def test_frame_rendering() -> None:
    frame = Frame(data=b"\x01\x41\xFF", time_start=1.0, time_end=1.1)
    assert frame.hex == "01 41 FF"
    assert frame.ascii == ".A."
    assert len(frame) == 3
    assert frame.duration == pytest.approx(0.1)
    assert frame.direction is Direction.UNKNOWN


def test_synth_timed_bytes_spaces_by_character_time() -> None:
    items = list(synth_timed_bytes(b"\x01\x02\x03", CHAR, gaps={2: 10 * CHAR}))
    assert items[1].time - items[0].time == pytest.approx(CHAR)
    assert items[2].time - items[1].time == pytest.approx(11 * CHAR)


class TestGapHistogram:
    def test_finds_the_cut_between_the_two_populations(self) -> None:
        histogram = GapHistogram()
        for _ in range(40):
            histogram.add(CHAR)  # inside a frame
        for _ in range(8):
            histogram.add(9 * CHAR)  # between frames
        suggested = histogram.suggest_idle_gap(CHAR)
        assert CHAR < suggested < 9 * CHAR

    def test_falls_back_to_the_modbus_rule_without_data(self) -> None:
        assert GapHistogram().suggest_idle_gap(CHAR) == pytest.approx(3.5 * CHAR)

    def test_falls_back_when_gaps_are_all_alike(self) -> None:
        histogram = GapHistogram()
        for _ in range(30):
            histogram.add(CHAR)
        assert histogram.suggest_idle_gap(CHAR) == pytest.approx(3.5 * CHAR)

    def test_suggestion_separates_real_traffic(self) -> None:
        """The threshold it proposes must reproduce the original bursts."""
        items = stream(b"\x01\x02\x03\x04", b"\xAA\xBB", b"\x01\x02\x03\x04", gap_chars=7.0)
        histogram = GapHistogram()
        for previous, following in zip(items, items[1:]):
            histogram.add(following.time - previous.time)
        threshold = histogram.suggest_idle_gap(CHAR)
        frames = split_frames(items, threshold)
        assert [f.data for f in frames] == [b"\x01\x02\x03\x04", b"\xAA\xBB", b"\x01\x02\x03\x04"]
