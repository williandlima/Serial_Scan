"""The command line interface end to end."""

from __future__ import annotations

import json

import pytest

from serial_scan.cli import main


def run(capsys, *argv: str) -> tuple[int, str]:
    code = main(list(argv))
    return code, capsys.readouterr().out


def test_protocols_explains_all_three(capsys) -> None:
    code, out = run(capsys, "protocolos")
    assert code == 0
    for name in ("RS232", "RS485", "RS422"):
        assert name in out
    assert "half duplex" in out


def test_demo_runs_the_whole_pipeline(capsys) -> None:
    code, out = run(capsys, "demo", "--duracao", "1.5")
    assert code == 0
    assert "Configuracao identificada: 19200 8N1" in out
    assert "CRC-16/MODBUS" in out
    assert "L8:01-03" in out
    assert "L8:01-06" in out  # the command that shows up mid-capture


def test_demo_with_a_chosen_configuration(capsys) -> None:
    code, out = run(capsys, "demo", "--config", "38400 8E1", "--duracao", "1.0")
    assert code == 0
    assert "38400 8E1" in out


def test_demo_with_text_traffic(capsys) -> None:
    code, out = run(capsys, "demo", "--config", "9600 7E1", "--texto", "--duracao", "1.5")
    assert code == 0
    assert "9600 7E1" in out


def test_demo_shows_the_field_map(capsys) -> None:
    code, out = run(capsys, "demo", "--duracao", "1.5", "--campos")
    assert code == 0
    assert "Mapa de campos" in out
    assert "constante" in out
    assert "checksum" in out


def test_detect_on_the_simulator(capsys) -> None:
    code, out = run(capsys, "detectar", "--simular", "--config", "19200 8N1", "--tempo", "0.6")
    assert code == 0
    assert "19200 8N1" in out


def test_detect_bit_level(capsys) -> None:
    code, out = run(capsys, "detectar", "--simular", "--config", "57600 8O1", "--bits")
    assert code == 0
    assert "57600 8O1" in out
    assert "Bit time medido" in out


def test_capture_records_and_replays(capsys, tmp_path) -> None:
    capture = tmp_path / "captura.jsonl"
    report = tmp_path / "relatorio.json"
    code, out = run(
        capsys,
        "capturar",
        "--simular",
        "--config",
        "19200 8N1",
        "--duracao",
        "1.5",
        "--resumo",
        "--gravar",
        str(capture),
        "--exportar",
        str(report),
        "--rotulos",
        str(tmp_path / "rotulos.json"),
    )
    assert code == 0
    assert capture.exists()
    assert "comando novo identificado" in out

    exported = json.loads(report.read_text(encoding="utf-8"))
    assert exported["config"] == "19200 8N1"
    assert exported["checksum"] == "CRC-16/MODBUS"
    assert len(exported["commands"]) == 7

    code, out = run(capsys, "reproduzir", str(capture))
    assert code == 0
    assert "L8:01-03" in out


def test_capture_prints_frames_by_default(capsys, tmp_path) -> None:
    code, out = run(
        capsys,
        "capturar",
        "--simular",
        "--config",
        "9600 8N1",
        "--duracao",
        "0.6",
        "--rotulos",
        str(tmp_path / "rotulos.json"),
    )
    assert code == 0
    assert "Dados" in out or "crc:ok" in out
    assert "01 03 00 6B 00 03" in out


def test_capture_requires_a_port_or_the_simulator(capsys) -> None:
    with pytest.raises(SystemExit, match="--porta"):
        main(["capturar", "--duracao", "0.1"])


def test_labels_round_trip(capsys, tmp_path) -> None:
    path = tmp_path / "rotulos.json"
    code, _ = run(
        capsys, "rotulos", "--rotulos", str(path), "--definir", "L8:01-03=Leitura"
    )
    assert code == 0

    code, out = run(capsys, "rotulos", "--rotulos", str(path))
    assert code == 0
    assert "L8:01-03" in out
    assert "Leitura" in out

    code, _ = run(capsys, "rotulos", "--rotulos", str(path), "--remover", "L8:01-03")
    assert code == 0
    code, out = run(capsys, "rotulos", "--rotulos", str(path))
    assert "Nenhum rotulo" in out


def test_labels_reject_a_malformed_assignment(capsys) -> None:
    code, _ = run(capsys, "rotulos", "--definir", "semigual")
    assert code == 1


def test_labels_are_applied_during_a_capture(capsys, tmp_path) -> None:
    path = tmp_path / "rotulos.json"
    run(capsys, "rotulos", "--rotulos", str(path), "--definir", "L8:01-03=Poll escravo 1")
    code, out = run(
        capsys,
        "capturar",
        "--simular",
        "--config",
        "19200 8N1",
        "--duracao",
        "1.0",
        "--resumo",
        "--rotulos",
        str(path),
    )
    assert code == 0
    assert "Poll escravo 1" in out


def test_autotune_regroups(capsys, tmp_path) -> None:
    code, out = run(
        capsys,
        "capturar",
        "--simular",
        "--config",
        "19200 8N1",
        "--duracao",
        "1.5",
        "--resumo",
        "--autotune",
        "--rotulos",
        str(tmp_path / "rotulos.json"),
    )
    assert code == 0
    # The default key for RS-485 is already (0, 1), so nothing should change.
    assert "L8:01-03" in out


def test_english_aliases_work(capsys) -> None:
    code, out = run(capsys, "protocols")
    assert code == 0
    assert "RS485" in out


def test_help_is_available(capsys) -> None:
    with pytest.raises(SystemExit) as excinfo:
        main(["--help"])
    assert excinfo.value.code == 0
