"""Linha de comando do Serial Scan.

    serial-scan portas
    serial-scan detectar --porta /dev/ttyUSB0 --protocolo 485
    serial-scan capturar  --porta /dev/ttyUSB0 --protocolo 485 --auto
    serial-scan demo      --protocolo 485
    serial-scan reproduzir captura.jsonl
    serial-scan rotulos   --listar

Os subcomandos aceitam tambem os nomes em ingles (``ports``, ``detect``,
``capture``, ``replay``, ``labels``) para quem preferir.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .autodetect import DetectionReport, analyse_samples, scan_candidates
from .commands import CommandEntry
from .labels import DEFAULT_FILENAME, LabelStore
from .portconfig import SerialConfig
from .protocols import PROFILES, Protocol
from .session import ScanSession, session_from_capture, session_from_simulator
from .sources import (
    SerialSource,
    SimulatedSource,
    SourceError,
    ascii_script,
    list_ports,
    modbus_like_script,
)

# --------------------------------------------------------------------------
# apresentacao
# --------------------------------------------------------------------------


def _print_report(report: DetectionReport, verbose: bool = True) -> None:
    print()
    print(report.summary())
    if report.baud_estimate and report.baud_estimate.snapped:
        estimate = report.baud_estimate
        print(
            f"  velocidade medida: {estimate.measured:,.0f} bps "
            f"({estimate.samples_per_bit:.2f} amostras/bit)"
        )
    if report.idle_gap:
        print(f"  silencio entre frames: {report.idle_gap * 1000:.3f} ms")
    print(f"  metodo: {report.method}, {report.elapsed:.1f} s")
    for note in report.notes:
        print(f"  - {note}")
    if verbose and report.candidates:
        print()
        print(report.table())


def _command_table(session: ScanSession) -> str:
    entries = session.catalog.sorted_entries()
    if not entries:
        return "Nenhum comando identificado."
    total = max(1, session.catalog.total_frames)
    width = max((len(e.label) for e in entries), default=0)
    width = min(max(width, 8), 34)
    lines = [
        f"{'assinatura':<12} {'rotulo':<{width}} {'n':>5} {'%':>5} "
        f"{'dir':<4} {'crc':>5}  exemplo"
    ]
    for entry in entries:
        ratio = entry.checksum_ratio
        crc = f"{ratio * 100:.0f}%" if ratio is not None else "-"
        sample = entry.samples[-1].hex(" ").upper() if entry.samples else ""
        if len(sample) > 44:
            sample = sample[:41] + "..."
        label = entry.label[:width] if entry.label else "-"
        lines.append(
            f"{entry.signature:<12} {label:<{width}} "
            f"{entry.count:>5} {entry.count / total * 100:>4.0f}% "
            f"{entry.direction.value:<4} {crc:>5}  {sample}"
        )
    return "\n".join(lines)


def _print_fields(entry: CommandEntry, checksum_width: int) -> None:
    fields = entry.field_map(checksum_width)
    if not fields:
        return
    print(f"\nMapa de campos de {entry.display_name} ({entry.count} amostras):")
    for stats in fields:
        print(f"  byte {stats.offset:>2}: {stats.kind.value:<11} {stats.describe()}")


def _print_summary(session: ScanSession, show_fields: bool = False) -> None:
    stats = session.stats
    print()
    print(
        f"{stats.frames_seen} frames / {stats.bytes_seen} bytes em "
        f"{stats.capture_span:.2f} s de barramento | checksum: "
        f"{session.checksum.name if session.checksum else 'nao identificado'}"
    )
    if stats.checksum_ok or stats.checksum_bad:
        total = stats.checksum_ok + stats.checksum_bad
        print(f"  checksum valido em {stats.checksum_ok}/{total} frames")
    if stats.truncated:
        print(
            f"  {stats.truncated} frame(s) cortado(s) pelo fim da captura, "
            f"fora do catalogo"
        )
    print()
    print(_command_table(session))
    if show_fields:
        for entry in session.catalog.sorted_entries():
            _print_fields(entry, session.catalog.checksum_width)


def _frame_line(session: ScanSession, frame, entry) -> str:
    mark = {True: "ok", False: "ERRO", None: "  -"}[frame.checksum_ok]
    name = entry.display_name if entry else ("(cortado no fim da captura)" if frame.truncated else "")
    return (
        f"{frame.time_start:9.4f} {frame.direction.value:<4} {len(frame.data):>3}B "
        f"crc:{mark:<4} {frame.hex[:48]:<48} {name}"
    )


# --------------------------------------------------------------------------
# fontes
# --------------------------------------------------------------------------


def _build_source(args) -> tuple[object, str]:
    """Return (source, description) for the options given."""
    if getattr(args, "simular", False) or getattr(args, "demo_source", False):
        true_config = SerialConfig.parse(args.config) if args.config else SerialConfig(19200)
        script = ascii_script() if getattr(args, "texto", False) else modbus_like_script()
        source = SimulatedSource(true_config, script=script)
        return source, f"barramento simulado em {true_config.label}"
    if not args.porta:
        raise SystemExit(
            "Informe --porta (ou use --simular para o barramento de demonstracao). "
            "Rode 'serial-scan portas' para ver as portas disponiveis."
        )
    config = SerialConfig.parse(args.config) if args.config else SerialConfig(9600)
    return SerialSource(args.porta, config), f"{args.porta}"


def _label_store(args) -> LabelStore:
    path = Path(args.rotulos) if getattr(args, "rotulos", None) else Path.cwd() / DEFAULT_FILENAME
    return LabelStore(path).load()


# --------------------------------------------------------------------------
# subcomandos
# --------------------------------------------------------------------------


def cmd_ports(args) -> int:
    ports = list_ports()
    if not ports:
        print(
            "Nenhuma porta serial encontrada.\n"
            "Se voce tem um adaptador conectado, verifique se o pyserial esta "
            "instalado (pip install pyserial) e se voce tem permissao de acesso "
            "(no Linux: sudo usermod -aG dialout $USER)."
        )
        return 1
    print(f"{'porta':<24} descricao")
    for device, description in ports:
        print(f"{device:<24} {description}")
    return 0


def cmd_protocols(args) -> int:
    for protocol, profile in PROFILES.items():
        print(f"\n{protocol.value} - {profile.description}")
        print(
            f"  fios: {profile.wires} | "
            f"{'full' if profile.full_duplex else 'half'} duplex | "
            f"{'multiponto' if profile.multidrop else 'ponto a ponto'}"
        )
        print(
            f"  offsets de chave padrao: {profile.key_offsets} | "
            f"silencio de frame: {profile.idle_gap_chars} caracteres"
        )
        for hint in profile.hints:
            print(f"  - {hint}")
        if profile.tap:
            sentidos = (
                "os dois sentidos"
                if profile.directions_per_adapter > 1
                else "um sentido por adaptador"
            )
            print(f"\n  Ligacao em paralelo ({sentidos}):")
            for linha in profile.tap:
                print(f"    * {linha}")
    print(
        "\nO Serial Scan e passivo: abre a porta com RTS e DTR desligados e "
        "nunca transmite."
    )
    return 0


def cmd_detect(args) -> int:
    source, description = _build_source(args)
    print(f"Detectando configuracao em {description} ({args.protocolo})...")
    try:
        source.open()
    except SourceError as exc:
        print(f"erro: {exc}", file=sys.stderr)
        return 1
    try:
        if args.bits and isinstance(source, SimulatedSource):
            report = analyse_samples(source.raw_samples(), source.sample_rate, args.protocolo)
        else:
            def progress(candidate, index):
                print(
                    f"  [{index:>3}] {candidate.config.label:<14} {candidate.percent:>3}%",
                    flush=True,
                )

            report = scan_candidates(
                source,
                args.protocolo,
                dwell=args.tempo,
                progress=progress if args.verboso else None,
            )
    finally:
        source.close()
    _print_report(report)
    return 0 if report.is_conclusive else 2


def cmd_capture(args) -> int:
    source, description = _build_source(args)
    store = _label_store(args)
    try:
        source.open()
    except SourceError as exc:
        print(f"erro: {exc}", file=sys.stderr)
        return 1

    report: DetectionReport | None = None
    config = source.config
    idle_gap = None
    try:
        if args.auto:
            print(f"Identificando a configuracao em {description}...")
            report = scan_candidates(source, args.protocolo, dwell=args.tempo_deteccao)
            _print_report(report, verbose=args.verboso)
            if report.config is None:
                print("Nao foi possivel identificar a configuracao.", file=sys.stderr)
                return 2
            config = report.config
            idle_gap = report.idle_gap or None
            source.reconfigure(config)

        session = ScanSession(
            source,
            args.protocolo,
            config=config,
            idle_gap=idle_gap,
            label_store=store,
        )
        if args.gravar:
            session.record_to(args.gravar)
            print(f"Gravando em {args.gravar}")

        print(
            f"\nCapturando {args.duracao:.0f} s em {description} "
            f"@ {config.label} ({args.protocolo})..."
        )
        if not args.resumo:
            print(f"{'tempo':>9} {'dir':<4} {'tam':>4} {'crc':<8} {'bytes':<48} comando")

        seen: set[str] = set()

        def on_frame(frame) -> None:
            entry = session.catalog.entries.get(frame.signature or "")
            if not args.resumo:
                print(_frame_line(session, frame, entry))
            if entry is not None and entry.signature not in seen:
                seen.add(entry.signature)
                print(f"  >> comando novo identificado: {entry.signature}")

        # One call, not a loop of short ones: each call ends with a flush that
        # would cut the frame in flight.
        session.run_for(args.duracao, on_frame=on_frame)

        if args.autotune and session.catalog.autotune():
            print(
                f"\nOffsets de chave ajustados para {session.catalog.key_offsets} "
                f"e comandos reagrupados."
            )
        _print_summary(session, show_fields=args.campos)

        if args.exportar:
            Path(args.exportar).write_text(
                json.dumps(session.report(), indent=2, ensure_ascii=False),
                encoding="utf-8",
            )
            print(f"\nRelatorio salvo em {args.exportar}")
        session.save_labels()
    finally:
        source.close()
    return 0


def cmd_replay(args) -> int:
    store = _label_store(args)
    session, report = session_from_capture(args.arquivo, args.protocolo, store)
    _print_report(report, verbose=False)
    session.run_for(args.duracao if args.duracao else 1e6)
    if args.autotune:
        session.catalog.autotune()
    _print_summary(session, show_fields=args.campos)
    return 0


def cmd_demo(args) -> int:
    store = _label_store(args)
    true_config = SerialConfig.parse(args.config) if args.config else SerialConfig(19200)
    script = ascii_script() if args.texto else modbus_like_script()
    session, report = session_from_simulator(
        true_config, args.protocolo, label_store=store, script=script
    )
    print(
        "Barramento de demonstracao: o trafego e gerado bit a bit em "
        f"{true_config.label} e o app tenta descobrir isso sozinho."
    )
    if report:
        _print_report(report, verbose=args.verboso)

    if args.frames:
        print(f"\n{'tempo':>9} {'dir':<4} {'tam':>4} {'crc':<8} {'bytes':<48} comando")

        def on_frame(frame) -> None:
            entry = session.catalog.entries.get(frame.signature or "")
            print(_frame_line(session, frame, entry))

        session.run_for(args.duracao, on_frame=on_frame)
    else:
        session.run_for(args.duracao)
    if args.autotune:
        session.catalog.autotune()
    _print_summary(session, show_fields=args.campos)
    return 0


def cmd_labels(args) -> int:
    store = _label_store(args)
    protocol = Protocol.parse(args.protocolo).value
    if args.definir:
        signature, _, label = args.definir.partition("=")
        if not signature or not label:
            print("Use --definir ASSINATURA=Rotulo", file=sys.stderr)
            return 1
        store.set_label(protocol, signature.strip(), label)
        store.save()
        print(f"{signature.strip()} = {label.strip()}")
        return 0
    if args.remover:
        store.remove(protocol, args.remover)
        store.save()
        print(f"removido: {args.remover}")
        return 0
    section = store.section(protocol)
    if not section:
        print(f"Nenhum rotulo salvo para {protocol} em {store.path}")
        return 0
    print(f"Rotulos de {protocol} ({store.path}):")
    for signature, record in sorted(section.items()):
        line = f"  {signature:<14} {record.label}"
        if record.notes:
            line += f"   # {record.notes}"
        print(line)
    return 0


def cmd_gui(args) -> int:
    try:
        from .ui.app import main as gui_main
    except ImportError as exc:
        print(
            f"Nao foi possivel abrir a interface grafica: {exc}\n"
            "No Linux instale o Tk: sudo apt install python3-tk",
            file=sys.stderr,
        )
        return 1
    return gui_main(args)


# --------------------------------------------------------------------------
# parser
# --------------------------------------------------------------------------


def _add_common(parser: argparse.ArgumentParser, with_port: bool = True) -> None:
    parser.add_argument(
        "--protocolo",
        "--protocol",
        default="485",
        help="232, 485 ou 422 (padrao: 485)",
    )
    if with_port:
        parser.add_argument("--porta", "--port", help="porta serial, ex. /dev/ttyUSB0 ou COM3")
        parser.add_argument(
            "--simular",
            action="store_true",
            help="usa o barramento simulado no lugar de uma porta real",
        )
    parser.add_argument(
        "--config",
        help="configuracao da linha, ex. '9600 8N1' (padrao: descobrir ou 9600 8N1)",
    )
    parser.add_argument("--rotulos", "--labels", help=f"arquivo de rotulos (padrao: ./{DEFAULT_FILENAME})")
    parser.add_argument("--verboso", "-v", action="store_true", help="mais detalhes")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="serial-scan",
        description=(
            "Analisador de barramentos seriais RS-232/485/422: descobre a "
            "configuracao da linha, mostra os frames, segrega os comandos e "
            "guarda um rotulo para cada um."
        ),
    )
    subparsers = parser.add_subparsers(dest="comando", required=True)

    p = subparsers.add_parser("portas", aliases=["ports"], help="lista as portas seriais")
    p.set_defaults(func=cmd_ports)

    p = subparsers.add_parser(
        "protocolos", aliases=["protocols"], help="explica RS-232, RS-485 e RS-422"
    )
    p.set_defaults(func=cmd_protocols)

    p = subparsers.add_parser(
        "detectar", aliases=["detect"], help="descobre a configuracao da linha (passo 2)"
    )
    _add_common(p)
    p.add_argument("--tempo", type=float, default=0.8, help="segundos por candidato (padrao: 0.8)")
    p.add_argument(
        "--bits",
        action="store_true",
        help="usa a analise bit a bit (sobreamostragem); so com --simular",
    )
    p.set_defaults(func=cmd_detect)

    p = subparsers.add_parser(
        "capturar", aliases=["capture"], help="captura e segrega comandos (passos 3, 4 e 5)"
    )
    _add_common(p)
    p.add_argument("--duracao", type=float, default=10.0, help="segundos de captura (padrao: 10)")
    p.add_argument("--auto", action="store_true", help="identifica a configuracao antes de capturar")
    p.add_argument(
        "--tempo-deteccao", type=float, default=0.8, help="segundos por candidato na deteccao"
    )
    p.add_argument("--gravar", help="grava a captura bruta neste arquivo .jsonl")
    p.add_argument("--exportar", help="salva o relatorio final em JSON")
    p.add_argument("--resumo", action="store_true", help="nao imprime frame a frame")
    p.add_argument("--campos", action="store_true", help="mostra o mapa de campos de cada comando")
    p.add_argument(
        "--autotune",
        action="store_true",
        help="deixa o app escolher os bytes que identificam o comando",
    )
    p.add_argument("--texto", action="store_true", help="simulacao com trafego ASCII")
    p.set_defaults(func=cmd_capture)

    p = subparsers.add_parser(
        "reproduzir", aliases=["replay"], help="reanalisa uma captura gravada"
    )
    p.add_argument("arquivo", help="arquivo .jsonl gerado por --gravar")
    _add_common(p, with_port=False)
    p.add_argument("--duracao", type=float, default=0.0, help="limita a reproducao (0 = tudo)")
    p.add_argument("--campos", action="store_true", help="mostra o mapa de campos")
    p.add_argument("--autotune", action="store_true", help="reagrupa pelos offsets sugeridos")
    p.set_defaults(func=cmd_replay)

    p = subparsers.add_parser(
        "demo", help="roda o fluxo completo contra um barramento simulado"
    )
    _add_common(p, with_port=False)
    p.add_argument("--duracao", type=float, default=3.0, help="segundos de captura (padrao: 3)")
    p.add_argument("--campos", action="store_true", help="mostra o mapa de campos")
    p.add_argument("--autotune", action="store_true", help="reagrupa pelos offsets sugeridos")
    p.add_argument("--texto", action="store_true", help="trafego ASCII em vez de Modbus RTU")
    p.add_argument("--frames", action="store_true", help="imprime frame a frame (passo 3)")
    p.set_defaults(func=cmd_demo, demo_source=True)

    p = subparsers.add_parser("rotulos", aliases=["labels"], help="gerencia os rotulos (passo 5)")
    _add_common(p, with_port=False)
    p.add_argument("--definir", "--set", help="ASSINATURA=Rotulo")
    p.add_argument("--remover", "--remove", help="apaga o rotulo de uma assinatura")
    p.set_defaults(func=cmd_labels)

    p = subparsers.add_parser("gui", aliases=["interface"], help="abre a interface grafica")
    _add_common(p)
    p.set_defaults(func=cmd_gui)

    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except KeyboardInterrupt:
        print("\ninterrompido")
        return 130
    except SourceError as exc:
        print(f"erro: {exc}", file=sys.stderr)
        return 1
    except BrokenPipeError:
        # The output was piped into something that closed early ("| head").
        # Point stdout at the void so the interpreter's own flush on exit does
        # not raise a second time.
        import os

        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
