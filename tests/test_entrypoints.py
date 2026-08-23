"""As tres formas de iniciar o app precisam funcionar.

O modo "arquivo solto" e o que o botao Run do VS Code usa, e e justamente o
que quebra com import relativo se ninguem cuidar.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pytest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
MAIN_FILE = PROJECT_ROOT / "serial_scan" / "__main__.py"


def run(*argv: str, cwd: Path | None = None) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, *argv],
        cwd=str(cwd or PROJECT_ROOT),
        capture_output=True,
        text=True,
        timeout=120,
    )


def test_runs_as_a_module() -> None:
    result = run("-m", "serial_scan", "protocolos")
    assert result.returncode == 0, result.stderr
    assert "RS485" in result.stdout


def test_runs_as_a_loose_script() -> None:
    """``python serial_scan/__main__.py`` - the VS Code Run button."""
    result = run(str(MAIN_FILE), "protocolos")
    assert result.returncode == 0, result.stderr
    assert "relative import" not in result.stderr
    assert "RS485" in result.stdout


def test_runs_as_a_loose_script_from_another_directory(tmp_path) -> None:
    """The fallback must not depend on where the interpreter was started."""
    result = run(str(MAIN_FILE), "protocolos", cwd=tmp_path)
    assert result.returncode == 0, result.stderr
    assert "RS485" in result.stdout


@pytest.mark.parametrize("invocation", [("-m", "serial_scan"), (str(MAIN_FILE),)])
def test_the_demo_works_from_every_entry_point(invocation) -> None:
    result = run(*invocation, "demo", "--duracao", "1.0")
    assert result.returncode == 0, result.stderr
    assert "Configuracao identificada" in result.stdout
    assert "CRC-16/MODBUS" in result.stdout
