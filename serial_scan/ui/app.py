"""Interface grafica: os cinco passos do projeto em uma janela.

    [1] protocolo -> [2] detectar -> [3] frames -> [4] comandos -> [5] rotulos

O Tkinter e importado aqui e nao no pacote raiz, de modo que a biblioteca
continue utilizavel (e testavel) em maquinas sem Tk instalado.
"""

from __future__ import annotations

import queue
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from ..autodetect import DetectionReport, scan_candidates
from ..labels import DEFAULT_FILENAME, LabelStore
from ..portconfig import COMMON_FRAME_FORMATS, SerialConfig
from ..protocols import Protocol, get_profile
from ..session import ScanSession
from ..sources import SerialSource, SimulatedSource, SourceError, list_ports, modbus_like_script

REFRESH_MS = 120
MAX_FRAME_ROWS = 3000

COLOURS = {
    "new": "#fff3c4",       # comando visto pela primeira vez
    "bad": "#ffd6d6",       # checksum invalido
    "labelled": "#e8f5e9",  # comando ja rotulado
}


class SerialScanApp(ttk.Frame):
    def __init__(self, master: tk.Tk, args=None) -> None:
        super().__init__(master, padding=6)
        self.master = master
        self.grid(row=0, column=0, sticky="nsew")
        master.rowconfigure(0, weight=1)
        master.columnconfigure(0, weight=1)
        self.rowconfigure(1, weight=1)
        self.columnconfigure(0, weight=1)

        self.session: ScanSession | None = None
        self.source = None
        self.store = LabelStore(
            Path(getattr(args, "rotulos", None) or Path.cwd() / DEFAULT_FILENAME)
        )
        try:
            self.store.load()
        except ValueError as exc:
            messagebox.showwarning("Rotulos", str(exc))

        self._detect_queue: queue.Queue = queue.Queue()
        self._frame_rows = 0
        self._selected_signature: str | None = None
        self._filter_signature: str | None = None

        self.protocol_var = tk.StringVar(value=self._initial_protocol(args))
        self.port_var = tk.StringVar(value=getattr(args, "porta", None) or "")
        self.config_var = tk.StringVar(
            value=getattr(args, "config", None) or "9600 8N1"
        )
        self.simulate_var = tk.BooleanVar(value=bool(getattr(args, "simular", False)))
        self.status_var = tk.StringVar(value="Pronto.")
        self.label_var = tk.StringVar(value="")
        self._closing = False
        self._pump_id: str | None = None

        self._build_toolbar()
        self._build_body()
        self._build_statusbar()
        self._refresh_ports()
        self._on_protocol_change()
        self._pump_id = self.after(REFRESH_MS, self._pump)

    @staticmethod
    def _initial_protocol(args) -> str:
        raw = getattr(args, "protocolo", None) or "485"
        try:
            return Protocol.parse(str(raw)).value
        except ValueError:
            return Protocol.RS485.value

    # ------------------------------------------------------------------
    # construcao da janela
    # ------------------------------------------------------------------

    def _build_toolbar(self) -> None:
        bar = ttk.LabelFrame(self, text="1. Protocolo e porta", padding=6)
        bar.grid(row=0, column=0, sticky="ew", pady=(0, 6))
        for column in (2, 5):
            bar.columnconfigure(column, weight=1)

        ttk.Label(bar, text="Protocolo:").grid(row=0, column=0, sticky="w")
        self.protocol_combo = ttk.Combobox(
            bar,
            textvariable=self.protocol_var,
            values=[p.value for p in Protocol],
            state="readonly",
            width=10,
        )
        self.protocol_combo.grid(row=0, column=1, sticky="w", padx=(4, 12))
        self.protocol_combo.bind("<<ComboboxSelected>>", lambda _event: self._on_protocol_change())

        ttk.Label(bar, text="Porta:").grid(row=0, column=2, sticky="e")
        self.port_combo = ttk.Combobox(bar, textvariable=self.port_var, width=26)
        self.port_combo.grid(row=0, column=3, sticky="w", padx=4)
        ttk.Button(bar, text="Atualizar", command=self._refresh_ports).grid(row=0, column=4, padx=4)

        ttk.Checkbutton(
            bar,
            text="Usar barramento simulado (sem hardware)",
            variable=self.simulate_var,
            command=self._refresh_ports,
        ).grid(row=0, column=5, sticky="w", padx=8)

        ttk.Label(bar, text="Configuracao:").grid(row=1, column=0, sticky="w", pady=(6, 0))
        self.config_combo = ttk.Combobox(
            bar, textvariable=self.config_var, width=16, values=self._config_choices()
        )
        self.config_combo.grid(row=1, column=1, sticky="w", padx=(4, 12), pady=(6, 0))

        self.detect_button = ttk.Button(
            bar, text="2. Identificar automaticamente", command=self._start_detection
        )
        self.detect_button.grid(row=1, column=2, columnspan=2, sticky="w", pady=(6, 0))

        self.start_button = ttk.Button(bar, text="Iniciar captura", command=self._start_capture)
        self.start_button.grid(row=1, column=4, sticky="ew", padx=4, pady=(6, 0))
        self.stop_button = ttk.Button(
            bar, text="Parar", command=self._stop_capture, state="disabled"
        )
        self.stop_button.grid(row=1, column=5, sticky="w", padx=4, pady=(6, 0))

        self.hint_label = ttk.Label(bar, text="", foreground="#555", wraplength=900, justify="left")
        self.hint_label.grid(row=2, column=0, columnspan=6, sticky="w", pady=(6, 0))

    @staticmethod
    def _config_choices() -> list[str]:
        common = (9600, 19200, 38400, 57600, 115200, 4800, 2400)
        return [
            SerialConfig(baud, *fmt).label
            for baud in common
            for fmt in COMMON_FRAME_FORMATS[:3]
        ]

    def _build_body(self) -> None:
        panes = ttk.PanedWindow(self, orient="horizontal")
        panes.grid(row=1, column=0, sticky="nsew")

        # -- esquerda: comandos identificados (passos 4 e 5) --------------
        left = ttk.LabelFrame(panes, text="4. Comandos identificados", padding=4)
        left.rowconfigure(0, weight=1)
        left.columnconfigure(0, weight=1)
        panes.add(left, weight=1)

        columns = ("assinatura", "rotulo", "n", "dir", "crc", "tam")
        self.command_tree = ttk.Treeview(left, columns=columns, show="headings", height=14)
        headings = {
            "assinatura": ("Assinatura", 110),
            "rotulo": ("Rotulo", 150),
            "n": ("N", 50),
            "dir": ("Dir", 45),
            "crc": ("CRC", 55),
            "tam": ("Bytes", 50),
        }
        for key, (text, width) in headings.items():
            self.command_tree.heading(key, text=text)
            self.command_tree.column(key, width=width, anchor="w" if key in ("assinatura", "rotulo") else "center")
        self.command_tree.grid(row=0, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(left, orient="vertical", command=self.command_tree.yview)
        self.command_tree.configure(yscrollcommand=scroll.set)
        scroll.grid(row=0, column=1, sticky="ns")
        self.command_tree.tag_configure("new", background=COLOURS["new"])
        self.command_tree.tag_configure("labelled", background=COLOURS["labelled"])
        self.command_tree.bind("<<TreeviewSelect>>", self._on_command_selected)
        self.command_tree.bind("<Double-1>", lambda _event: self._focus_label_entry())

        editor = ttk.Frame(left)
        editor.grid(row=1, column=0, columnspan=2, sticky="ew", pady=(6, 0))
        editor.columnconfigure(1, weight=1)
        ttk.Label(editor, text="5. Rotulo:").grid(row=0, column=0, sticky="w")
        self.label_entry = ttk.Entry(editor, textvariable=self.label_var)
        self.label_entry.grid(row=0, column=1, sticky="ew", padx=4)
        self.label_entry.bind("<Return>", lambda _event: self._save_label())
        ttk.Button(editor, text="Salvar", command=self._save_label).grid(row=0, column=2)

        buttons = ttk.Frame(left)
        buttons.grid(row=2, column=0, columnspan=2, sticky="ew", pady=(4, 0))
        ttk.Button(buttons, text="Filtrar frames", command=self._toggle_filter).pack(
            side="left", padx=(0, 4)
        )
        ttk.Button(buttons, text="Reagrupar", command=self._autotune).pack(side="left", padx=4)
        ttk.Button(buttons, text="Exportar", command=self._export).pack(side="left", padx=4)
        ttk.Button(buttons, text="Limpar", command=self._reset).pack(side="left", padx=4)

        # -- direita: frames e detalhes (passo 3) -------------------------
        right = ttk.PanedWindow(panes, orient="vertical")
        panes.add(right, weight=3)

        frames_box = ttk.LabelFrame(right, text="3. Frames recebidos", padding=4)
        frames_box.rowconfigure(0, weight=1)
        frames_box.columnconfigure(0, weight=1)
        right.add(frames_box, weight=3)

        columns = ("tempo", "dir", "tam", "crc", "hex", "ascii", "comando")
        self.frame_tree = ttk.Treeview(frames_box, columns=columns, show="headings", height=16)
        headings = {
            "tempo": ("Tempo (s)", 90),
            "dir": ("Dir", 45),
            "tam": ("Bytes", 50),
            "crc": ("CRC", 55),
            "hex": ("Dados (hex)", 430),
            "ascii": ("ASCII", 150),
            "comando": ("Comando", 160),
        }
        for key, (text, width) in headings.items():
            self.frame_tree.heading(key, text=text)
            anchor = "w" if key in ("hex", "ascii", "comando") else "center"
            self.frame_tree.column(key, width=width, anchor=anchor)
        self.frame_tree.grid(row=0, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(frames_box, orient="vertical", command=self.frame_tree.yview)
        self.frame_tree.configure(yscrollcommand=scroll.set)
        scroll.grid(row=0, column=1, sticky="ns")
        self.frame_tree.tag_configure("new", background=COLOURS["new"])
        self.frame_tree.tag_configure("bad", background=COLOURS["bad"])

        self.autoscroll_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(
            frames_box, text="Rolar automaticamente", variable=self.autoscroll_var
        ).grid(row=1, column=0, sticky="w", pady=(4, 0))

        notebook = ttk.Notebook(right)
        right.add(notebook, weight=2)

        detail = ttk.Frame(notebook, padding=4)
        detail.rowconfigure(0, weight=1)
        detail.columnconfigure(0, weight=1)
        self.detail_text = tk.Text(detail, height=10, wrap="none", font=("TkFixedFont", 9))
        self.detail_text.grid(row=0, column=0, sticky="nsew")
        detail_scroll = ttk.Scrollbar(detail, orient="vertical", command=self.detail_text.yview)
        self.detail_text.configure(yscrollcommand=detail_scroll.set, state="disabled")
        detail_scroll.grid(row=0, column=1, sticky="ns")
        notebook.add(detail, text="Detalhes do comando")

        log = ttk.Frame(notebook, padding=4)
        log.rowconfigure(0, weight=1)
        log.columnconfigure(0, weight=1)
        self.log_text = tk.Text(log, height=10, wrap="word", font=("TkFixedFont", 9))
        self.log_text.grid(row=0, column=0, sticky="nsew")
        log_scroll = ttk.Scrollbar(log, orient="vertical", command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=log_scroll.set, state="disabled")
        log_scroll.grid(row=0, column=1, sticky="ns")
        notebook.add(log, text="Registro")

    def _build_statusbar(self) -> None:
        bar = ttk.Frame(self)
        bar.grid(row=2, column=0, sticky="ew", pady=(6, 0))
        bar.columnconfigure(0, weight=1)
        ttk.Label(bar, textvariable=self.status_var, relief="sunken", anchor="w").grid(
            row=0, column=0, sticky="ew"
        )

    # ------------------------------------------------------------------
    # helpers
    # ------------------------------------------------------------------

    def _log(self, text: str) -> None:
        self.log_text.configure(state="normal")
        self.log_text.insert("end", text.rstrip() + "\n")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def _set_status(self, text: str) -> None:
        self.status_var.set(text)

    def _refresh_ports(self) -> None:
        if self.simulate_var.get():
            self.port_combo.configure(values=["(simulado)"], state="disabled")
            self._set_status(
                "Barramento simulado: o trafego e gerado na configuracao escrita "
                "no campo Configuracao, e a deteccao tenta descobri-la do zero."
            )
            return
        self.port_combo.configure(state="normal")
        ports = list_ports()
        self.port_combo.configure(values=[device for device, _ in ports])
        if ports and not self.port_var.get():
            self.port_var.set(ports[0][0])
        if not ports:
            self._set_status(
                "Nenhuma porta serial encontrada. Conecte o adaptador ou marque "
                "'Usar barramento simulado'."
            )
        else:
            self._set_status(f"{len(ports)} porta(s) encontrada(s).")

    def _on_protocol_change(self) -> None:
        profile = get_profile(self.protocol_var.get())
        self.hint_label.configure(
            text=f"{profile.description}  |  " + "  ".join(profile.hints[:2])
        )

    def _current_config(self) -> SerialConfig | None:
        try:
            return SerialConfig.parse(self.config_var.get())
        except ValueError as exc:
            messagebox.showerror("Configuracao invalida", str(exc))
            return None

    def _make_source(self):
        config = self._current_config()
        if config is None:
            return None
        if self.simulate_var.get():
            return SimulatedSource(config, script=modbus_like_script(), realtime=True)
        port = self.port_var.get().strip()
        if not port:
            messagebox.showerror("Porta", "Escolha uma porta serial.")
            return None
        return SerialSource(port, config)

    # ------------------------------------------------------------------
    # passo 2: deteccao automatica
    # ------------------------------------------------------------------

    def _start_detection(self) -> None:
        if self.session is not None and self.session.running:
            messagebox.showinfo("Captura em andamento", "Pare a captura antes de detectar.")
            return
        source = self._make_source()
        if source is None:
            return
        self.detect_button.configure(state="disabled")
        self._set_status("Identificando a configuracao da linha... isto leva alguns segundos.")
        protocol = self.protocol_var.get()

        def worker() -> None:
            try:
                source.open()
                report = scan_candidates(source, protocol, dwell=0.8)
                self._detect_queue.put(("ok", report))
            except (SourceError, ValueError) as exc:
                self._detect_queue.put(("error", str(exc)))
            finally:
                try:
                    source.close()
                except Exception:  # pragma: no cover - best effort cleanup
                    pass

        threading.Thread(target=worker, daemon=True, name="serial-scan-detect").start()

    def _finish_detection(self, report: DetectionReport) -> None:
        self.detect_button.configure(state="normal")
        if report.config is None:
            self._set_status("Nao foi possivel identificar a configuracao.")
            self._log("Deteccao sem resultado.")
            return
        self.config_var.set(report.config.label)
        self._set_status(report.summary())
        self._log(report.summary())
        for note in report.notes:
            self._log(f"  - {note}")
        self._log(report.table(5))
        if not report.is_conclusive:
            messagebox.showwarning(
                "Resultado sem certeza",
                f"{report.summary()}\n\nO segundo colocado ficou perto. Verifique se o "
                f"barramento estava trafegando durante a varredura e tente de novo com "
                f"mais tempo, ou escolha a configuracao manualmente.",
            )

    # ------------------------------------------------------------------
    # passo 3: captura
    # ------------------------------------------------------------------

    def _start_capture(self) -> None:
        if self.session is not None and self.session.running:
            return
        source = self._make_source()
        if source is None:
            return
        config = self._current_config()
        if config is None:
            return
        try:
            source.open()
        except SourceError as exc:
            messagebox.showerror("Nao foi possivel abrir", str(exc))
            return

        self.source = source
        self.session = ScanSession(
            source, self.protocol_var.get(), config=config, label_store=self.store
        )
        self._clear_views()
        self.session.start()
        self.start_button.configure(state="disabled")
        self.stop_button.configure(state="normal")
        self.detect_button.configure(state="disabled")
        self._set_status(f"Capturando em {source.name} @ {config.label}...")
        self._log(f"Captura iniciada: {source.name} @ {config.label} ({self.protocol_var.get()})")

    def _stop_capture(self) -> None:
        if self.session is not None:
            self.session.stop()
            self.session.save_labels()
        if self.source is not None:
            try:
                self.source.close()
            except Exception:  # pragma: no cover - best effort cleanup
                pass
            self.source = None
        self.start_button.configure(state="normal")
        self.stop_button.configure(state="disabled")
        self.detect_button.configure(state="normal")
        self._set_status("Captura parada. Rotulos salvos.")

    def _clear_views(self) -> None:
        self.frame_tree.delete(*self.frame_tree.get_children())
        self.command_tree.delete(*self.command_tree.get_children())
        self._frame_rows = 0

    def _reset(self) -> None:
        if self.session is not None:
            self.session.reset()
        self._clear_views()
        self._set_status("Contadores zerados (os rotulos foram mantidos).")

    # ------------------------------------------------------------------
    # atualizacao periodica
    # ------------------------------------------------------------------

    def _pump(self) -> None:
        if self._closing:
            return
        try:
            while True:
                kind, payload = self._detect_queue.get_nowait()
                if kind == "ok":
                    self._finish_detection(payload)
                else:
                    self.detect_button.configure(state="normal")
                    self._set_status(f"Erro na deteccao: {payload}")
                    messagebox.showerror("Erro na deteccao", str(payload))
        except queue.Empty:
            pass

        if self.session is not None:
            self._drain_session()
        self._pump_id = self.after(REFRESH_MS, self._pump)

    def _drain_session(self) -> None:
        session = self.session
        assert session is not None
        touched: set[str] = set()
        new_signatures: set[str] = set()

        for event in session.drain_events():
            if event.kind == "frame" and event.frame is not None:
                self._append_frame(event.frame, event.entry)
                if event.entry is not None:
                    touched.add(event.entry.signature)
            elif event.kind == "command" and event.entry is not None:
                new_signatures.add(event.entry.signature)
                touched.add(event.entry.signature)
                self._log(
                    f"Comando novo: {event.entry.signature} "
                    f"({event.entry.length} bytes) -> clique para rotular"
                )
            elif event.kind == "status":
                self._set_status(event.text)
                self._log(event.text)
            elif event.kind == "error":
                self._log(f"ERRO: {event.text}")
                self._set_status(f"Erro: {event.text}")
            elif event.kind == "stopped":
                self._log(event.text)
                self.start_button.configure(state="normal")
                self.stop_button.configure(state="disabled")
                self.detect_button.configure(state="normal")

        for signature in touched:
            self._update_command_row(signature, is_new=signature in new_signatures)
        if touched:
            stats = session.stats
            self._set_status(
                f"{stats.frames_seen} frames | {len(session.catalog.entries)} comandos | "
                f"checksum: {session.checksum.name if session.checksum else 'procurando...'}"
            )
        if self._selected_signature in touched:
            self._show_details(self._selected_signature)

    def _append_frame(self, frame, entry) -> None:
        if self._filter_signature and frame.signature != self._filter_signature:
            return
        tags = []
        if frame.checksum_ok is False:
            tags.append("bad")
        crc = {True: "ok", False: "ERRO", None: "-"}[frame.checksum_ok]
        label = entry.display_name if entry is not None else ""
        self.frame_tree.insert(
            "",
            "end",
            values=(
                f"{frame.time_start:.4f}",
                frame.direction.value,
                len(frame.data),
                crc,
                frame.hex,
                frame.ascii,
                label,
            ),
            tags=tuple(tags),
        )
        self._frame_rows += 1
        if self._frame_rows > MAX_FRAME_ROWS:
            children = self.frame_tree.get_children()
            self.frame_tree.delete(*children[: self._frame_rows - MAX_FRAME_ROWS])
            self._frame_rows = len(self.frame_tree.get_children())
        if self.autoscroll_var.get():
            self.frame_tree.yview_moveto(1.0)

    def _update_command_row(self, signature: str, is_new: bool = False) -> None:
        session = self.session
        if session is None:
            return
        entry = session.catalog.entries.get(signature)
        if entry is None:
            return
        ratio = entry.checksum_ratio
        values = (
            entry.signature,
            entry.label or "",
            entry.count,
            entry.direction.value,
            f"{ratio * 100:.0f}%" if ratio is not None else "-",
            entry.length,
        )
        tags = ("labelled",) if entry.is_labelled else (("new",) if is_new else ())
        if self.command_tree.exists(signature):
            self.command_tree.item(signature, values=values, tags=tags)
        else:
            self.command_tree.insert("", "end", iid=signature, values=values, tags=tags)

    # ------------------------------------------------------------------
    # passos 4 e 5: selecao, detalhes e rotulos
    # ------------------------------------------------------------------

    def _on_command_selected(self, _event=None) -> None:
        selection = self.command_tree.selection()
        if not selection:
            return
        signature = selection[0]
        self._selected_signature = signature
        session = self.session
        entry = session.catalog.entries.get(signature) if session else None
        self.label_var.set(entry.label if entry else "")
        self._show_details(signature)

    def _focus_label_entry(self) -> None:
        self.label_entry.focus_set()
        self.label_entry.select_range(0, "end")

    def _show_details(self, signature: str | None) -> None:
        if signature is None or self.session is None:
            return
        entry = self.session.catalog.entries.get(signature)
        if entry is None:
            return
        width = self.session.catalog.checksum_width
        lines = [
            f"Assinatura : {entry.signature}",
            f"Rotulo     : {entry.label or '(sem rotulo)'}",
            f"Chave      : bytes {entry.key_offsets} = {entry.key_hex}",
            f"Tamanho    : {entry.length} bytes",
            f"Ocorrencias: {entry.count}   sentido: {entry.direction.value}",
        ]
        ratio = entry.checksum_ratio
        if ratio is not None:
            lines.append(
                f"Checksum   : {entry.checksum_ok} validos / "
                f"{entry.checksum_ok + entry.checksum_bad} ({ratio * 100:.0f}%)"
            )
        if entry.notes:
            lines.append(f"Notas      : {entry.notes}")

        fields = entry.field_map(width)
        if fields:
            lines.append("")
            lines.append("Mapa de campos (comparando todas as amostras):")
            lines.append(f"  {'byte':>4}  {'tipo':<11} conteudo")
            for stats in fields:
                lines.append(
                    f"  {stats.offset:>4}  {stats.kind.value:<11} {stats.describe()}"
                )
        if entry.samples:
            lines.append("")
            lines.append("Ultimas amostras:")
            for sample in list(entry.samples)[-6:]:
                ascii_view = "".join(
                    chr(b) if 0x20 <= b < 0x7F else "." for b in sample
                )
                lines.append(f"  {sample.hex(' ').upper():<56} {ascii_view}")

        self.detail_text.configure(state="normal")
        self.detail_text.delete("1.0", "end")
        self.detail_text.insert("1.0", "\n".join(lines))
        self.detail_text.configure(state="disabled")

    def _save_label(self) -> None:
        if self.session is None or self._selected_signature is None:
            messagebox.showinfo("Rotulo", "Selecione um comando na lista primeiro.")
            return
        label = self.label_var.get().strip()
        self.session.set_label(self._selected_signature, label)
        self.session.save_labels()
        self._update_command_row(self._selected_signature)
        self._show_details(self._selected_signature)
        self._set_status(f"Rotulo salvo: {self._selected_signature} = {label or '(vazio)'}")

    def _toggle_filter(self) -> None:
        if self._filter_signature:
            self._filter_signature = None
            self._set_status("Filtro removido: mostrando todos os frames.")
        elif self._selected_signature:
            self._filter_signature = self._selected_signature
            self._set_status(f"Mostrando apenas {self._filter_signature}.")
        else:
            messagebox.showinfo("Filtro", "Selecione um comando para filtrar.")
            return
        self.frame_tree.delete(*self.frame_tree.get_children())
        self._frame_rows = 0

    def _autotune(self) -> None:
        if self.session is None:
            return
        suggested = self.session.catalog.suggest_key_offsets()
        current = self.session.catalog.key_offsets
        if suggested == current:
            messagebox.showinfo(
                "Reagrupar",
                f"Os bytes {current} ja sao a melhor chave para este trafego.",
            )
            return
        if not messagebox.askyesno(
            "Reagrupar comandos",
            f"Usar os bytes {suggested} para identificar os comandos, "
            f"no lugar de {current}?\n\n"
            f"Os comandos serao reagrupados a partir do que ja foi capturado. "
            f"Os rotulos que continuarem valendo sao mantidos.",
        ):
            return
        self.session.catalog.regroup(suggested)
        self.command_tree.delete(*self.command_tree.get_children())
        for signature in self.session.catalog.entries:
            self._update_command_row(signature)
        self._set_status(f"Comandos reagrupados pelos bytes {suggested}.")
        self._log(f"Reagrupado: chave {current} -> {suggested}")

    def _export(self) -> None:
        if self.session is None:
            messagebox.showinfo("Exportar", "Nada capturado ainda.")
            return
        path = filedialog.asksaveasfilename(
            title="Salvar relatorio",
            defaultextension=".json",
            filetypes=[("JSON", "*.json"), ("Todos", "*.*")],
        )
        if not path:
            return
        import json

        Path(path).write_text(
            json.dumps(self.session.report(), indent=2, ensure_ascii=False), encoding="utf-8"
        )
        self._set_status(f"Relatorio salvo em {path}")

    # ------------------------------------------------------------------

    def on_close(self) -> None:
        # Cancel the periodic callback first: a pending `after` that fires
        # against a destroyed widget raises TclError on the way out.
        self._closing = True
        if self._pump_id is not None:
            try:
                self.after_cancel(self._pump_id)
            except tk.TclError:
                pass
            self._pump_id = None
        try:
            self._stop_capture()
        finally:
            self.master.destroy()


def main(args=None) -> int:
    root = tk.Tk()
    root.title("Serial Scan - analisador RS-232 / RS-485 / RS-422")
    root.geometry("1350x800")
    root.minsize(1000, 640)
    app = SerialScanApp(root, args)
    root.protocol("WM_DELETE_WINDOW", app.on_close)
    root.mainloop()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
