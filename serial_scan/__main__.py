"""Permite rodar o app com ``python -m serial_scan``."""

from .cli import main

if __name__ == "__main__":
    raise SystemExit(main())
