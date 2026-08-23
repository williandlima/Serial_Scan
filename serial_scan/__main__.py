"""Ponto de entrada do app.

Roda de tres formas, todas equivalentes::

    serial-scan demo              # comando instalado
    python -m serial_scan demo    # como modulo
    python serial_scan/__main__.py demo

A terceira e o que o botao "Run Python File" do VS Code faz. Executado
assim, o arquivo nao tem pacote pai e o import relativo falharia com
``attempted relative import with no known parent package``, por isso o
fallback abaixo: ele poe a raiz do projeto no ``sys.path`` e importa pelo
caminho absoluto.
"""

try:
    from .cli import main
except ImportError:  # executado como script solto, sem contexto de pacote
    import sys
    from pathlib import Path

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from serial_scan.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
