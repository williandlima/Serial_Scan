"""Identifying, segregating and describing commands (step 4)."""

from __future__ import annotations

import pytest

from serial_scan.commands import CommandCatalog, FieldKind, signature_for
from serial_scan.framing import Direction, Frame

CHECKSUM_WIDTH = 2


def frame(data: bytes, index: int = 0, t: float = 0.0, direction=Direction.UNKNOWN) -> Frame:
    return Frame(
        data=data,
        time_start=t,
        time_end=t + 0.001 * len(data),
        index=index,
        direction=direction,
    )


def poll_traffic(cycles: int = 6) -> list[Frame]:
    """A Modbus-like poll loop: three slaves, request and response each."""
    frames: list[Frame] = []
    index = 0
    for cycle in range(cycles):
        for slave in (1, 2, 3):
            frames.append(frame(bytes([slave, 0x03, 0x00, 0x6B, 0x00, 0x03]), index, index * 0.01))
            index += 1
            frames.append(
                frame(bytes([slave, 0x03, 0x02, cycle, 0x00, 0x64, 0x01]), index, index * 0.01)
            )
            index += 1
    return frames


class TestSignature:
    def test_includes_length_by_default(self) -> None:
        assert signature_for(b"\x01\x03\x00", (0, 1), True) == "L3:01-03"
        assert signature_for(b"\x01\x03\x00", (0, 1), False) == "01-03"

    def test_tolerates_frames_shorter_than_the_key(self) -> None:
        assert signature_for(b"\x01", (0, 1), True) == "L1:01"
        assert signature_for(b"", (0, 1), True) == "L0:??"


class TestSegregation:
    def test_each_command_gets_its_own_entry(self) -> None:
        catalog = CommandCatalog(key_offsets=(0, 1))
        catalog.observe_many(poll_traffic())
        # Three slaves x (request, response) = six distinct commands.
        assert len(catalog.entries) == 6
        assert catalog.total_frames == 36
        assert all(entry.count == 6 for entry in catalog.entries.values())

    def test_a_command_seen_for_the_first_time_is_flagged(self) -> None:
        catalog = CommandCatalog(key_offsets=(0, 1))
        results = catalog.observe_many(poll_traffic(cycles=2))
        new_flags = [observation.is_new for observation in results]
        # The first cycle reveals all six; the second reveals nothing.
        assert sum(new_flags) == 6
        assert not any(new_flags[6:])

    def test_a_new_command_appearing_late_is_still_flagged(self) -> None:
        catalog = CommandCatalog(key_offsets=(0, 1))
        catalog.observe_many(poll_traffic())
        observation = catalog.observe(frame(b"\x01\x06\x00\x10\x00\x01", 99, 9.9))
        assert observation.is_new
        assert observation.entry.signature == "L6:01-06"
        assert len(catalog.entries) == 7

    def test_same_key_but_different_length_stays_separate(self) -> None:
        catalog = CommandCatalog(key_offsets=(0, 1), use_length=True)
        catalog.observe(frame(b"\x01\x03\x00\x01"))
        catalog.observe(frame(b"\x01\x03\x00\x01\x02\x03"))
        assert len(catalog.entries) == 2

    def test_length_can_be_ignored(self) -> None:
        catalog = CommandCatalog(key_offsets=(0, 1), use_length=False)
        catalog.observe(frame(b"\x01\x03\x00\x01"))
        catalog.observe(frame(b"\x01\x03\x00\x01\x02\x03"))
        assert len(catalog.entries) == 1

    def test_statistics_are_accumulated(self) -> None:
        catalog = CommandCatalog(key_offsets=(0, 1))
        for index in range(5):
            f = frame(b"\x01\x03\x00\x6B", index, index * 0.5, Direction.REQUEST)
            f.checksum_ok = index != 2
            catalog.observe(f)
        entry = catalog.entries["L4:01-03"]
        assert entry.count == 5
        assert entry.direction is Direction.REQUEST
        assert entry.checksum_ok == 4
        assert entry.checksum_bad == 1
        assert entry.checksum_ratio == pytest.approx(0.8)
        assert entry.first_seen == 0.0
        assert entry.last_seen > 0


class TestFieldMap:
    def test_classifies_constant_key_counter_and_checksum(self) -> None:
        catalog = CommandCatalog(key_offsets=(0, 1))
        for cycle in range(8):
            catalog.observe(
                frame(bytes([0x01, 0x03, 0x02, cycle, 0x00, 0xAA, 0xBB]), cycle, cycle * 0.1)
            )
        entry = catalog.entries["L7:01-03"]
        kinds = {stats.offset: stats.kind for stats in entry.field_map(CHECKSUM_WIDTH)}

        assert kinds[0] is FieldKind.KEY
        assert kinds[1] is FieldKind.KEY
        assert kinds[2] is FieldKind.CONSTANT
        assert kinds[3] is FieldKind.COUNTER
        assert kinds[4] is FieldKind.CONSTANT
        assert kinds[5] is FieldKind.CHECKSUM
        assert kinds[6] is FieldKind.CHECKSUM

    def test_classifies_enumerated_and_variable_fields(self) -> None:
        catalog = CommandCatalog(key_offsets=(0,))
        states = [0x00, 0x01, 0x00, 0x01, 0x00, 0x01, 0x00, 0x01]
        for index, state in enumerate(states):
            catalog.observe(
                frame(bytes([0x05, state, (index * 71) % 256]), index, index * 0.1)
            )
        entry = catalog.entries["L3:05"]
        kinds = {stats.offset: stats.kind for stats in entry.field_map()}
        assert kinds[1] is FieldKind.ENUM
        assert kinds[2] is FieldKind.VARIABLE

    def test_describe_is_human_readable(self) -> None:
        catalog = CommandCatalog(key_offsets=(0,))
        for index in range(6):
            catalog.observe(frame(bytes([0x01, 0xFF, index]), index, index * 0.1))
        described = {s.offset: s.describe() for s in catalog.entries["L3:01"].field_map()}
        assert described[1] == "0xFF"
        assert "contador" in described[2]

    def test_empty_entry_has_no_map(self) -> None:
        catalog = CommandCatalog()
        catalog.observe(frame(b"\x01\x02"))
        entry = catalog.entries["L2:01-02"]
        entry.samples.clear()
        assert entry.field_map() == []


def mixed_traffic(cycles: int = 6) -> list[Frame]:
    """Two function codes on three slaves, so offset 1 really identifies.

    ``poll_traffic`` alone cannot exercise key discovery: its function code is
    always 0x03, and a byte that never changes identifies nothing.
    """
    frames = poll_traffic(cycles)
    index = len(frames)
    for cycle in range(cycles):
        # Same length as the read request, so only offset 1 tells them apart.
        frames.append(
            frame(bytes([0x01, 0x06, 0x00, 0x10, 0x00, cycle]), index, 100.0 + index * 0.01)
        )
        index += 1
    return frames


class TestKeyDiscovery:
    def test_suggests_the_address_and_function_offsets(self) -> None:
        catalog = CommandCatalog(key_offsets=(0,))
        catalog.observe_many(mixed_traffic())
        assert catalog.suggest_key_offsets() == (0, 1)

    def test_skips_a_constant_header_byte(self) -> None:
        """With a fixed STX at offset 0, the identifier is further in."""
        catalog = CommandCatalog(key_offsets=(0,))
        for index in range(12):
            opcode = 0x10 + (index % 3)
            catalog.observe(frame(bytes([0x02, opcode, index, 0x03]), index, index * 0.1))
        # Offset 0 is a constant STX and offset 3 a constant ETX: neither
        # identifies anything. Offset 2 counts up once per frame, so it is
        # payload, not an opcode.
        assert catalog.suggest_key_offsets() == (1,)

    def test_does_not_propose_a_key_per_payload_value(self) -> None:
        catalog = CommandCatalog(key_offsets=(0,))
        catalog.observe_many(mixed_traffic())
        assert len(catalog.suggest_key_offsets()) <= 2

    def test_keeps_the_current_key_without_enough_data(self) -> None:
        catalog = CommandCatalog(key_offsets=(0, 1))
        catalog.observe(frame(b"\x01\x03\x00"))
        assert catalog.suggest_key_offsets() == (0, 1)

    def test_regroup_rebuilds_from_the_retained_history(self) -> None:
        catalog = CommandCatalog(key_offsets=(0,))
        catalog.observe_many(mixed_traffic())
        # Keyed on the address alone, the read and write requests of slave 1
        # collide: they share both the address and the length.
        assert "L6:01" in catalog.entries
        assert catalog.entries["L6:01"].count == 12
        before = catalog.total_frames

        catalog.regroup((0, 1))
        assert catalog.key_offsets == (0, 1)
        assert catalog.total_frames == before
        assert "L6:01" not in catalog.entries
        assert catalog.entries["L6:01-03"].count == 6
        assert catalog.entries["L6:01-06"].count == 6

    def test_autotune_adopts_the_suggestion(self) -> None:
        catalog = CommandCatalog(key_offsets=(0,))
        catalog.observe_many(mixed_traffic())
        assert catalog.autotune() is True
        assert catalog.key_offsets == (0, 1)
        # A second run has nothing left to change.
        assert catalog.autotune() is False


class TestLabelling:
    def test_label_is_applied_to_the_entry(self) -> None:
        catalog = CommandCatalog(key_offsets=(0, 1))
        catalog.observe(frame(b"\x01\x03\x00\x6B"))
        catalog.set_label("L4:01-03", "  Leitura de temperatura  ")
        entry = catalog.entries["L4:01-03"]
        assert entry.label == "Leitura de temperatura"
        assert entry.is_labelled
        assert entry.display_name == "Leitura de temperatura"

    def test_label_set_before_the_command_appears_is_applied_on_arrival(self) -> None:
        catalog = CommandCatalog(key_offsets=(0, 1))
        catalog.apply_labels({"L4:01-03": "Leitura"})
        catalog.observe(frame(b"\x01\x03\x00\x6B"))
        assert catalog.entries["L4:01-03"].label == "Leitura"

    def test_labels_survive_a_regroup(self) -> None:
        catalog = CommandCatalog(key_offsets=(0, 1))
        catalog.observe_many(poll_traffic())
        catalog.set_label("L6:01-03", "Pergunta escravo 1")
        catalog.regroup((0, 1))
        assert catalog.entries["L6:01-03"].label == "Pergunta escravo 1"

    def test_unlabelled_lists_what_still_needs_attention(self) -> None:
        catalog = CommandCatalog(key_offsets=(0, 1))
        catalog.observe_many(poll_traffic())
        catalog.set_label("L6:01-03", "Pergunta")
        unlabelled = catalog.unlabelled
        assert len(unlabelled) == 5
        assert all(not e.is_labelled for e in unlabelled)

    def test_display_name_falls_back_to_the_signature(self) -> None:
        catalog = CommandCatalog(key_offsets=(0, 1))
        catalog.observe(frame(b"\x01\x03"))
        assert catalog.entries["L2:01-03"].display_name == "L2:01-03"


class TestViews:
    def test_sorting(self) -> None:
        catalog = CommandCatalog(key_offsets=(0, 1))
        catalog.observe_many(poll_traffic(cycles=3))
        catalog.observe(frame(b"\x09\x06\x00\x01", 99, 99.0))

        by_count = catalog.sorted_entries("count")
        assert by_count[0].count >= by_count[-1].count
        assert catalog.sorted_entries("recent")[0].signature == "L4:09-06"
        assert catalog.sorted_entries("first")[0].first_seen == 0.0

    def test_export_shape(self) -> None:
        catalog = CommandCatalog(key_offsets=(0, 1))
        catalog.observe_many(poll_traffic(cycles=2))
        catalog.set_label("L6:01-03", "Pergunta")
        exported = catalog.to_dict()
        assert exported["key_offsets"] == [0, 1]
        assert len(exported["commands"]) == 6
        first = next(c for c in exported["commands"] if c["signature"] == "L6:01-03")
        assert first["label"] == "Pergunta"
        assert first["count"] == 2
        assert first["last_sample"]
