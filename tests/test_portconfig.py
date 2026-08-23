"""Line configuration and protocol profiles (step 1)."""

from __future__ import annotations

import pytest

from serial_scan.portconfig import (
    COMMON_FRAME_FORMATS,
    STANDARD_BAUDRATES,
    InvalidConfig,
    SerialConfig,
    candidate_configs,
    nearest_standard_baudrate,
)
from serial_scan.protocols import PROFILES, Protocol, get_profile


class TestSerialConfig:
    def test_frame_format_shorthand(self) -> None:
        assert SerialConfig(9600, 8, "N", 1.0).frame_format == "8N1"
        assert SerialConfig(9600, 7, "E", 2.0).frame_format == "7E2"
        assert SerialConfig(9600, 5, "O", 1.5).frame_format == "5O1.5"
        assert SerialConfig(19200).label == "19200 8N1"

    @pytest.mark.parametrize(
        "text, expected",
        [
            ("9600 8N1", SerialConfig(9600, 8, "N", 1.0)),
            ("9600-8N1", SerialConfig(9600, 8, "N", 1.0)),
            ("115200/8E1", SerialConfig(115200, 8, "E", 1.0)),
            ("19200 7o2", SerialConfig(19200, 7, "O", 2.0)),
            ("4800", SerialConfig(4800, 8, "N", 1.0)),
            ("  9600   8N1  ", SerialConfig(9600, 8, "N", 1.0)),
        ],
    )
    def test_parse(self, text: str, expected: SerialConfig) -> None:
        assert SerialConfig.parse(text) == expected

    @pytest.mark.parametrize("text", ["", "abc", "9600 8X1", "9600 8N1 extra", "9600 8"])
    def test_parse_rejects_nonsense(self, text: str) -> None:
        with pytest.raises((InvalidConfig, ValueError)):
            SerialConfig.parse(text)

    def test_parse_round_trips_the_label(self) -> None:
        for config in list(candidate_configs())[:40]:
            assert SerialConfig.parse(config.label) == config

    @pytest.mark.parametrize(
        "kwargs",
        [
            {"baudrate": 0},
            {"baudrate": -9600},
            {"baudrate": 9600, "bytesize": 9},
            {"baudrate": 9600, "parity": "Z"},
            {"baudrate": 9600, "stopbits": 3},
        ],
    )
    def test_invalid_values_are_refused(self, kwargs) -> None:
        with pytest.raises(InvalidConfig):
            SerialConfig(**kwargs)

    def test_timing(self) -> None:
        config = SerialConfig(9600, 8, "N", 1.0)
        assert config.bits_per_char == 10.0
        assert config.char_time == pytest.approx(10 / 9600)
        assert config.bit_time == pytest.approx(1 / 9600)
        assert config.gap_seconds(3.5) == pytest.approx(3.5 * 10 / 9600)

        with_parity = SerialConfig(9600, 8, "E", 2.0)
        assert with_parity.bits_per_char == 12.0
        assert with_parity.has_parity

    def test_is_hashable_and_comparable(self) -> None:
        a = SerialConfig(9600, 8, "N", 1)
        b = SerialConfig(9600, 8, "N", 1.0)
        assert a == b
        assert len({a, b}) == 1
        assert SerialConfig(4800) < SerialConfig(9600)

    def test_parity_is_normalised_to_upper_case(self) -> None:
        assert SerialConfig(9600, 7, "e", 1.0).parity == "E"

    def test_pyserial_mapping(self) -> None:
        import serial

        mapping = SerialConfig(19200, 7, "E", 2.0).to_pyserial()
        assert mapping["baudrate"] == 19200
        assert mapping["bytesize"] == serial.SEVENBITS
        assert mapping["parity"] == serial.PARITY_EVEN
        assert mapping["stopbits"] == serial.STOPBITS_TWO


class TestBaudrateSnapping:
    @pytest.mark.parametrize("baud", STANDARD_BAUDRATES)
    def test_exact_rates_snap_to_themselves(self, baud: int) -> None:
        assert nearest_standard_baudrate(baud) == baud

    def test_small_errors_are_absorbed(self) -> None:
        assert nearest_standard_baudrate(9550) == 9600
        assert nearest_standard_baudrate(115900) == 115200

    def test_a_rate_far_from_any_standard_is_refused(self) -> None:
        assert nearest_standard_baudrate(7000) is None
        assert nearest_standard_baudrate(0) is None
        assert nearest_standard_baudrate(-1) is None

    def test_tolerance_is_configurable(self) -> None:
        assert nearest_standard_baudrate(7000, tolerance=0.5) is not None


class TestCandidates:
    def test_covers_every_combination(self) -> None:
        candidates = list(candidate_configs())
        assert len(candidates) == len(STANDARD_BAUDRATES) * len(COMMON_FRAME_FORMATS)
        assert len(set(candidates)) == len(candidates)

    def test_most_common_first(self) -> None:
        first = next(iter(candidate_configs()))
        assert first == SerialConfig(9600, 8, "N", 1.0)


class TestProtocols:
    @pytest.mark.parametrize(
        "text, expected",
        [
            ("232", Protocol.RS232),
            ("485", Protocol.RS485),
            ("422", Protocol.RS422),
            ("RS485", Protocol.RS485),
            ("rs-485", Protocol.RS485),
            (" RS 485 ".replace(" ", ""), Protocol.RS485),
        ],
    )
    def test_parse(self, text: str, expected: Protocol) -> None:
        assert Protocol.parse(text) is expected

    def test_parse_rejects_unknown(self) -> None:
        with pytest.raises(ValueError, match="232, 485 ou 422"):
            Protocol.parse("999")

    def test_every_protocol_has_a_profile(self) -> None:
        for protocol in Protocol:
            profile = PROFILES[protocol]
            assert profile.protocol is protocol
            assert profile.description
            assert profile.hints
            assert profile.key_offsets
            assert profile.idle_gap_chars > 0
            assert profile.baud_candidates

    def test_electrical_facts_are_right(self) -> None:
        assert get_profile("232").full_duplex and not get_profile("232").multidrop
        # RS-485 shares one pair, so both directions land in one capture.
        assert not get_profile("485").full_duplex
        assert get_profile("485").multidrop
        assert get_profile("485").wires == 2
        # RS-422 is four-wire full duplex with one master.
        assert get_profile("422").full_duplex and get_profile("422").multidrop
        assert get_profile("422").wires == 4

    def test_direction_inference_only_where_it_makes_sense(self) -> None:
        # Point to point on separate wires: nothing to infer.
        assert not get_profile("232").infer_direction
        assert get_profile("485").infer_direction
        assert get_profile("422").infer_direction

    def test_get_profile_accepts_strings_and_enums(self) -> None:
        assert get_profile("485") is get_profile(Protocol.RS485)

    def test_short_name(self) -> None:
        assert Protocol.RS485.short == "485"
