"""Automatic identification of the line configuration (step 2).

Both strategies are exercised against the bit-level simulator, which renders
traffic exactly as a transmitter would drive the wire and then decodes it with
whatever configuration is being probed. A wrong guess therefore produces
genuinely corrupted bytes, not a stand-in for corruption.
"""

from __future__ import annotations

import pytest

from serial_scan.autodetect import (
    analyse_capture,
    analyse_samples,
    infer_seven_bit_parity,
    scan_candidates,
)
from serial_scan.portconfig import SerialConfig
from serial_scan.protocols import Protocol
from serial_scan.sources import SimulatedSource, ascii_script, iter_timed_bytes

BINARY_CASES = ["9600 8N1", "19200 8N1", "115200 8N1", "38400 8E1", "57600 8O1", "9600 8N2"]
TEXT_CASES = ["9600 7E1", "19200 7O1", "9600 8N1"]


def simulator(label: str, text: bool = False) -> SimulatedSource:
    source = SimulatedSource(
        SerialConfig.parse(label), script=ascii_script() if text else None
    )
    source.open()
    return source


class TestOversampling:
    """Bit-level analysis: the strategy that can see the parity bit."""

    @pytest.mark.parametrize("label", BINARY_CASES + TEXT_CASES)
    def test_identifies_the_configuration(self, label: str) -> None:
        truth = SerialConfig.parse(label)
        source = simulator(label, text=label.startswith(("9600 7", "19200 7")))
        report = analyse_samples(source.raw_samples(), source.sample_rate, Protocol.RS485)

        assert report.accepts(truth), f"{label}: escolheu {report.config}"
        assert report.is_conclusive
        assert report.method == "oversample"

    @pytest.mark.parametrize("label", BINARY_CASES)
    def test_measures_the_baudrate(self, label: str) -> None:
        truth = SerialConfig.parse(label)
        source = simulator(label)
        report = analyse_samples(source.raw_samples(), source.sample_rate, Protocol.RS485)
        assert report.baud_estimate is not None
        assert report.baud_estimate.snapped == truth.baudrate

    def test_parity_is_resolved_when_the_bits_are_visible(self) -> None:
        """8E1 and 8O1 carry identical data bytes; only the parity bit differs.

        The bit-level path observes that bit, so it must pick the right one
        instead of declaring an equivalence.
        """
        source = simulator("38400 8O1")
        report = analyse_samples(source.raw_samples(), source.sample_rate, Protocol.RS485)
        assert report.config == SerialConfig(38400, 8, "O", 1.0)
        assert SerialConfig(38400, 8, "E", 1.0) not in report.equivalent

    def test_reports_failure_on_a_silent_line(self) -> None:
        report = analyse_samples([1] * 5000, 153600, Protocol.RS485)
        assert report.best is None
        assert not report.is_conclusive
        assert report.notes


class TestCandidateScan:
    """Byte-level analysis: what an ordinary USB adapter can do."""

    @pytest.mark.parametrize("label", BINARY_CASES)
    def test_identifies_the_configuration(self, label: str) -> None:
        truth = SerialConfig.parse(label)
        source = simulator(label)
        report = scan_candidates(source, Protocol.RS485, dwell=0.8)

        assert report.accepts(truth), f"{label}: escolheu {report.config}"
        assert report.is_conclusive
        assert report.method == "scan"

    @pytest.mark.parametrize("label", TEXT_CASES)
    def test_identifies_text_protocols_without_a_checksum(self, label: str) -> None:
        truth = SerialConfig.parse(label)
        source = simulator(label, text=True)
        report = scan_candidates(source, Protocol.RS485, dwell=0.8)
        assert report.accepts(truth), f"{label}: escolheu {report.config}"

    def test_wrong_configurations_score_far_below_the_winner(self) -> None:
        source = simulator("19200 8N1")
        report = scan_candidates(source, Protocol.RS485, dwell=0.8)
        rival = report.rival
        assert rival is not None
        assert report.confidence - rival.confidence > 0.2

    def test_two_phase_scan_stays_cheap(self) -> None:
        """A full sweep would be ~190 probes; the two-phase search is far less."""
        source = simulator("9600 8N1")
        report = scan_candidates(source, Protocol.RS485, dwell=0.3)
        assert len(report.candidates) <= 80

    def test_identical_formats_are_grouped_not_ranked(self) -> None:
        """8N2 reads perfectly as 8N1: report that, do not invent a winner."""
        source = simulator("9600 8N2")
        report = scan_candidates(source, Protocol.RS485, dwell=0.8)
        assert report.accepts(SerialConfig(9600, 8, "N", 2.0))
        assert report.equivalent
        assert any("mesmos bytes" in note for note in report.notes)

    def test_refuses_a_source_that_cannot_be_reconfigured(self) -> None:
        class Fixed(SimulatedSource):
            supports_reconfigure = False

        source = Fixed(SerialConfig(9600))
        source.open()
        with pytest.raises(ValueError, match="nao permite trocar"):
            scan_candidates(source, Protocol.RS485)

    def test_restores_the_original_configuration(self) -> None:
        source = simulator("9600 8N1")
        source.reconfigure(SerialConfig(2400, 7, "E", 1.0))
        before = source.config
        scan_candidates(source, Protocol.RS485, dwell=0.2, baudrates=(9600, 19200))
        assert source.config == before


class TestSevenBitParity:
    def test_detects_even_parity_carried_in_bit_7(self) -> None:
        # 7E1 text read as 8N1: the parity bit lands in the top data bit.
        payload = b"HELLO WORLD 12345"
        encoded = bytes(
            byte | (bin(byte).count("1") & 1) << 7 for byte in payload
        )
        hint = infer_seven_bit_parity(encoded)
        assert hint is not None
        assert hint.parity == "E"
        assert hint.strong

    def test_detects_odd_parity(self) -> None:
        payload = b"HELLO WORLD 12345"
        encoded = bytes(
            byte | (1 - (bin(byte).count("1") & 1)) << 7 for byte in payload
        )
        hint = infer_seven_bit_parity(encoded)
        assert hint is not None
        assert hint.parity == "O"
        assert hint.strong

    def test_real_eight_bit_data_is_not_mistaken_for_parity(self) -> None:
        payload = bytes(range(256))
        hint = infer_seven_bit_parity(payload)
        assert hint is not None
        assert not hint.strong
        assert hint.ratio == pytest.approx(0.5, abs=0.05)

    def test_needs_enough_bytes(self) -> None:
        assert infer_seven_bit_parity(b"\x01") is None


class TestCaptureAnalysis:
    def test_scores_an_already_decoded_capture(self) -> None:
        source = simulator("19200 8N1")
        items = list(iter_timed_bytes(source, 1.0))
        report = analyse_capture(items, Protocol.RS485, source.true_config)

        assert report.method == "capture"
        assert report.best is not None
        assert report.best.score.checksum is not None
        assert report.best.score.checksum.name == "CRC-16/MODBUS"
        assert report.idle_gap > 0
