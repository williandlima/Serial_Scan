"""The whole pipeline: protocol -> configuration -> frames -> commands -> labels."""

from __future__ import annotations

import json

import pytest

from serial_scan.checksums import ALGORITHMS_BY_NAME
from serial_scan.framing import Direction
from serial_scan.labels import LabelStore
from serial_scan.portconfig import SerialConfig
from serial_scan.protocols import Protocol
from serial_scan.session import (
    DirectionInferencer,
    ScanSession,
    session_from_capture,
    session_from_simulator,
)
from serial_scan.sources import (
    ReplaySource,
    SimulatedSource,
    SourceError,
    ascii_script,
)

TRUE_CONFIG = SerialConfig(19200, 8, "N", 1.0)


@pytest.fixture
def session() -> ScanSession:
    built, _ = session_from_simulator(TRUE_CONFIG, Protocol.RS485)
    return built


class TestEndToEnd:
    def test_detects_configures_frames_and_segregates(self, session: ScanSession) -> None:
        frames = session.run_for(1.5)

        assert session.config == TRUE_CONFIG
        assert frames
        # The simulated bus runs three slaves with a read each, plus a write
        # that appears part-way through: seven distinct commands.
        assert len(session.catalog.entries) == 7
        assert session.checksum is not None
        assert session.checksum.name == "CRC-16/MODBUS"

    def test_every_frame_passes_its_checksum(self, session: ScanSession) -> None:
        session.run_for(1.5)
        assert session.stats.checksum_bad == 0
        assert session.stats.checksum_ok == session.stats.frames_seen

    def test_frames_are_not_chopped_at_read_boundaries(self, session: ScanSession) -> None:
        """Every frame must be a whole transaction, never a fragment.

        Reading in slices must not influence framing: a slice boundary is not
        a silence on the wire.
        """
        session.run_for(1.5, slice_size=0.007)
        lengths = {len(frame) for frame in session.recent_frames if not frame.truncated}
        assert lengths == {8, 11}

    def test_requests_and_responses_are_told_apart(self, session: ScanSession) -> None:
        session.run_for(1.5)
        assert session.stats.directions[Direction.REQUEST] > 0
        assert session.stats.directions[Direction.RESPONSE] > 0
        # In the read loop (function 0x03) the 8-byte frame is the poll and the
        # 11-byte one is the answer. The write (0x06) is excluded here: Modbus
        # answers it by echoing the request, so request and response are
        # byte-identical and only timing separates them.
        for frame in session.recent_frames:
            if frame.data[1] != 0x03:
                continue
            expected = Direction.RESPONSE if len(frame) == 11 else Direction.REQUEST
            assert frame.direction is expected, frame

    def test_an_echoed_response_is_separated_by_timing_alone(
        self, session: ScanSession
    ) -> None:
        """Function 0x06 echoes the request, so content cannot separate them."""
        session.run_for(1.5)
        echoes = [f for f in session.recent_frames if f.data[1] == 0x06]
        assert echoes
        # Identical bytes, so they land in one command...
        assert len({f.data for f in echoes}) == 1
        assert len({f.signature for f in echoes}) == 1
        # ...but the turnaround gap still tells the two sides apart.
        assert {f.direction for f in echoes} == {Direction.REQUEST, Direction.RESPONSE}

    def test_a_new_command_raises_an_event(self, session: ScanSession) -> None:
        session.run_for(1.5)
        events = session.drain_events(limit=100_000)
        announced = {
            event.entry.signature for event in events if event.kind == "command"
        }
        assert announced == set(session.catalog.entries)
        assert "L8:01-06" in announced  # the write that shows up mid-capture

    def test_capture_span_is_measured_on_the_bus_clock(self, session: ScanSession) -> None:
        session.run_for(1.5)
        assert session.stats.capture_span == pytest.approx(1.5, abs=0.1)

    def test_text_traffic_is_handled_too(self) -> None:
        built, report = session_from_simulator(
            SerialConfig(9600, 7, "E", 1.0), Protocol.RS485, script=ascii_script()
        )
        assert report is not None and report.accepts(SerialConfig(9600, 7, "E", 1.0))
        built.run_for(1.5)
        assert built.catalog.entries
        assert all(
            frame.data.startswith(b":") for frame in built.recent_frames
        )


class TestChecksumAdoption:
    def test_frames_seen_before_detection_are_recounted(self, session: ScanSession) -> None:
        """The first frames arrive before the algorithm is known.

        Without a back-fill the summary would for ever read "valid in 204/212"
        on a bus where every frame is in fact valid.
        """
        session.run_for(1.5)
        total = session.stats.checksum_ok + session.stats.checksum_bad
        assert total == session.stats.frames_seen
        assert all(frame.checksum_ok is not None for frame in session.recent_frames)

    def test_per_command_tallies_match_the_totals(self, session: ScanSession) -> None:
        session.run_for(1.5)
        summed = sum(
            entry.checksum_ok + entry.checksum_bad
            for entry in session.catalog.entries.values()
        )
        assert summed == session.stats.checksum_ok + session.stats.checksum_bad

    def test_a_forced_algorithm_is_used_as_is(self) -> None:
        source = SimulatedSource(TRUE_CONFIG)
        source.open()
        built = ScanSession(
            source,
            Protocol.RS485,
            config=TRUE_CONFIG,
            checksum=ALGORITHMS_BY_NAME["XOR-8"],
        )
        built.run_for(0.5)
        # The bus really uses CRC-16, so an imposed XOR-8 must fail loudly.
        assert built.checksum.name == "XOR-8"
        assert built.stats.checksum_bad > 0


class TestLabelling:
    def test_labels_are_saved_and_reloaded(self, tmp_path) -> None:
        path = tmp_path / "rotulos.json"
        first, _ = session_from_simulator(
            TRUE_CONFIG, Protocol.RS485, label_store=LabelStore(path)
        )
        first.run_for(1.0)
        signature = first.catalog.sorted_entries()[0].signature
        first.set_label(signature, "Leitura do escravo 1")
        first.save_labels()

        second, _ = session_from_simulator(
            TRUE_CONFIG, Protocol.RS485, label_store=LabelStore(path).load()
        )
        second.run_for(1.0)
        assert second.catalog.entries[signature].label == "Leitura do escravo 1"

    def test_reset_keeps_the_labels(self, tmp_path) -> None:
        built, _ = session_from_simulator(
            TRUE_CONFIG, Protocol.RS485, label_store=LabelStore(tmp_path / "l.json")
        )
        built.run_for(1.0)
        signature = built.catalog.sorted_entries()[0].signature
        built.set_label(signature, "Etiqueta")
        built.reset()
        assert built.stats.frames_seen == 0
        assert not built.catalog.entries

        built.run_for(1.0)
        assert built.catalog.entries[signature].label == "Etiqueta"

    def test_labels_reach_the_exported_report(self, session: ScanSession) -> None:
        session.run_for(1.0)
        signature = session.catalog.sorted_entries()[0].signature
        session.set_label(signature, "Comando principal")
        report = session.report()

        assert report["protocol"] == "RS485"
        assert report["config"] == TRUE_CONFIG.label
        assert report["checksum"] == "CRC-16/MODBUS"
        entry = next(c for c in report["commands"] if c["signature"] == signature)
        assert entry["label"] == "Comando principal"
        json.dumps(report)  # must stay serialisable


class TestRecordAndReplay:
    def test_a_recording_replays_to_the_same_commands(self, tmp_path) -> None:
        path = tmp_path / "captura.jsonl"
        live, _ = session_from_simulator(TRUE_CONFIG, Protocol.RS485)
        live.record_to(path)
        live.run_for(1.5)
        live.stop_recording()

        replayed, report = session_from_capture(path, Protocol.RS485)
        replayed.run_for(10.0)

        assert report.method == "capture"
        assert set(replayed.catalog.entries) == set(live.catalog.entries)
        assert replayed.stats.frames_seen == live.stats.frames_seen

    def test_the_header_carries_protocol_and_configuration(self, tmp_path) -> None:
        path = tmp_path / "captura.jsonl"
        live, _ = session_from_simulator(TRUE_CONFIG, Protocol.RS232)
        live.record_to(path)
        live.run_for(0.5)
        live.stop_recording()

        source = ReplaySource(path)
        source.open()
        assert source.protocol == "RS232"
        assert source.config == TRUE_CONFIG

    def test_replay_of_a_missing_file_is_reported(self, tmp_path) -> None:
        with pytest.raises(SourceError, match="nao encontrado"):
            ReplaySource(tmp_path / "nada.jsonl").open()


class TestDirectionInference:
    def test_rs232_makes_no_claim(self) -> None:
        from serial_scan.protocols import get_profile
        from serial_scan.framing import Frame

        inferencer = DirectionInferencer(get_profile(Protocol.RS232), TRUE_CONFIG)
        frame = Frame(b"\x01", 0.0, 0.0, gap_before=1.0)
        assert inferencer.classify(frame) is Direction.UNKNOWN

    def test_a_long_silence_starts_a_transaction(self) -> None:
        from serial_scan.protocols import get_profile
        from serial_scan.framing import Frame

        profile = get_profile(Protocol.RS485)
        inferencer = DirectionInferencer(profile, TRUE_CONFIG)
        threshold = profile.turnaround_gap_chars * TRUE_CONFIG.char_time

        request = Frame(b"\x01", 0.0, 0.0, gap_before=threshold * 2)
        response = Frame(b"\x02", 0.0, 0.0, gap_before=threshold / 4)
        assert inferencer.classify(request) is Direction.REQUEST
        assert inferencer.classify(response) is Direction.RESPONSE
        # Two quick frames in a row: the second cannot answer a response.
        assert inferencer.classify(response) is Direction.REQUEST


class TestBackgroundCapture:
    def test_start_and_stop(self) -> None:
        built, _ = session_from_simulator(TRUE_CONFIG, Protocol.RS485)
        built.start(slice_size=0.02)
        assert built.running
        deadline = 200
        while built.stats.frames_seen < 20 and deadline:
            import time

            time.sleep(0.01)
            deadline -= 1
        built.stop()
        assert not built.running
        assert built.stats.frames_seen >= 20
        assert built.catalog.entries


class TestTruncatedFrames:
    def test_a_frame_cut_by_the_end_of_capture_is_not_a_command(self) -> None:
        """Stopping mid-frame must not invent a command with a broken CRC."""
        built, _ = session_from_simulator(TRUE_CONFIG, Protocol.RS485)
        # 0.8 s lands in the middle of a frame on this bus.
        built.run_for(0.8)

        assert built.stats.truncated == 1
        cut = [f for f in built.recent_frames if f.truncated]
        assert len(cut) == 1
        # It is shown to the operator...
        assert cut[0] in built.recent_frames
        # ...but never becomes a command, and never counts as a bad checksum.
        assert all(len(entry.key_bytes) == 2 for entry in built.catalog.entries.values())
        assert built.stats.checksum_bad == 0
        assert cut[0].signature is None

    def test_command_count_is_stable_across_capture_lengths(self) -> None:
        counts = set()
        for duration in (1.2, 1.35, 1.5, 1.7):
            built, _ = session_from_simulator(TRUE_CONFIG, Protocol.RS485)
            built.run_for(duration)
            counts.add(len(built.catalog.entries))
        assert counts == {7}

    def test_a_clean_stop_truncates_nothing(self) -> None:
        built, _ = session_from_simulator(TRUE_CONFIG, Protocol.RS485)
        # The simulated script ends with a long idle, so a capture covering a
        # whole cycle closes every frame on an observed silence.
        built.run_for(built.source.duration)
        assert built.stats.truncated == 0
