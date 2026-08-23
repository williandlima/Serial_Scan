"""Interface grafica do Serial Scan.

A janela e organizada em volta do que importa: **escolher o protocolo** e
**ver os comandos novos chegando**. Tudo o mais - frames byte a byte, log,
deteccao de configuracao, exportacao - existe, mas em segundo plano.

    [1] protocolo -> [2] configuracao -> [3] frames -> [4] comandos -> [5] rotulo

O Tkinter e importado aqui e nao no pacote raiz, de modo que a biblioteca
continue utilizavel (e testavel) em maquinas sem Tk instalado.
"""

from __future__ import annotations

import json
import queue
import threading
import time
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
MAX_FRAME_ROWS = 2000

#: Por quanto tempo um comando recem-descoberto fica destacado.
NEW_HIGHLIGHT_SECONDS = 12.0

#: Nos primeiros segundos de captura *todo* comando e inedito - e o
#: inventario do barramento, nao uma novidade. Destacar os sete de uma vez
#: deixa a tela inteira amarela e nao destaca nada. Durante essa janela os
#: comandos entram na lista sem alarde; passada ela, cada comando inedito
#: acende a faixa e a linha, que e o que a pessoa esta esperando ver.
BASELINE_SECONDS = 3.0

PALETTE = {
    "new": "#ffe9a8",       # comando visto agora
    "new_text": "#7a5200",
    "labelled": "#e3f4e5",  # comando ja rotulado
    "bad": "#ffdada",       # checksum invalido
    "banner": "#fff4cc",
    "banner_idle": "#ededed",
    "muted": "#666666",
}


class SerialScanApp(ttk.Frame):
    def __init__(self, master: tk.Tk, args=None) -> None:
        super().__init__(master, padding=8)
        self.grid(row=0, column=0, sticky="nsew")
        master.rowconfigure(0, weight=1)
        master.columnconfigure(0, weight=1)
        self.rowconfigure(2, weight=3)
        self.rowconfigure(3, weight=2)
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
        self._selected: str | None = None
        self._filter: str | None = None
        self._new_until: dict[str, float] = {}
        self._new_count = 0
        self._baseline_until = 0.0
        self._closing = False
        self._pump_id: str | None = None

        self.protocol_var = tk.StringVar(value=self._initial_protocol(args))
        self.port_var = tk.StringVar(value=getattr(args, "porta", None) or "")
        self.config_var = tk.StringVar(value=getattr(args, "config", None) or "19200 8N1")
        self.simulate_var = tk.BooleanVar(value=bool(getattr(args, "simular", False)))
        self.status_var = tk.StringVar(value="Escolha o protocolo e inicie a captura.")
        self.banner_var = tk.StringVar(value="Nenhum comando identificado ainda.")
        self.counter_var = tk.StringVar(value="0 comandos")
        self.label_var = tk.StringVar(value="")
        self.autoscroll_var = tk.BooleanVar(value=True)
        self.sort_var = tk.StringVar(value="novos primeiro")

        self._build_styles()
        self._build_header()
        self._build_banner()
        self._build_main()
        self._build_bottom()
        self._build_status()

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

    def _build_styles(self) -> None:
        style = ttk.Style()
        # "Toolbutton" desenha o radio como botao pressionavel, que e o que da
        # ao seletor de protocolo cara de escolha principal e nao de campo.
        style.configure(
            "Protocolo.Toolbutton", font=("TkDefaultFont", 11, "bold"), padding=(18, 8)
        )
        style.configure("Acao.TButton", font=("TkDefaultFont", 10, "bold"), padding=(12, 6))
        style.configure("Titulo.TLabel", font=("TkDefaultFont", 10, "bold"))

    # ------------------------------------------------------------------
    # 1. protocolo e fonte
    # ------------------------------------------------------------------

    def _build_header(self) -> None:
        header = ttk.Frame(self)
        header.grid(row=0, column=0, sticky="ew")
        header.columnconfigure(1, weight=1)

        self.chooser = ttk.LabelFrame(header, text="1. Protocolo do barramento", padding=8)
        chooser = self.chooser
        chooser.grid(row=0, column=0, sticky="nsw")
        self.protocol_buttons: dict[str, ttk.Radiobutton] = {}
        for column, protocol in enumerate(Protocol):
            button = ttk.Radiobutton(
                chooser,
                text=f"RS-{protocol.short}",
                value=protocol.value,
                variable=self.protocol_var,
                style="Protocolo.Toolbutton",
                command=self._on_protocol_change,
            )
            button.grid(row=0, column=column, padx=3)
            self.protocol_buttons[protocol.value] = button

        self.hint_label = ttk.Label(
            chooser, text="", foreground=PALETTE["muted"], wraplength=420, justify="left"
        )
        self.hint_label.grid(row=1, column=0, columnspan=3, sticky="w", pady=(8, 0))

        controls = ttk.LabelFrame(header, text="2. Fonte e configuracao da linha", padding=8)
        controls.grid(row=0, column=1, sticky="nsew", padx=(8, 0))
        controls.columnconfigure(1, weight=1)

        ttk.Label(controls, text="Porta:").grid(row=0, column=0, sticky="w")
        self.port_combo = ttk.Combobox(controls, textvariable=self.port_var, width=22)
        self.port_combo.grid(row=0, column=1, sticky="ew", padx=4)
        ttk.Button(controls, text="Atualizar", command=self._refresh_ports).grid(row=0, column=2)
        ttk.Checkbutton(
            controls,
            text="Barramento simulado (sem hardware)",
            variable=self.simulate_var,
            command=self._refresh_ports,
        ).grid(row=0, column=3, sticky="w", padx=(10, 0))

        ttk.Label(controls, text="Configuracao:").grid(row=1, column=0, sticky="w", pady=(6, 0))
        self.config_combo = ttk.Combobox(
            controls, textvariable=self.config_var, width=22, values=self._config_choices()
        )
        self.config_combo.grid(row=1, column=1, sticky="ew", padx=4, pady=(6, 0))
        self.detect_button = ttk.Button(
            controls, text="Identificar sozinho", command=self._start_detection
        )
        self.detect_button.grid(row=1, column=2, columnspan=2, sticky="w", pady=(6, 0))

        actions = ttk.Frame(controls)
        actions.grid(row=2, column=0, columnspan=4, sticky="ew", pady=(10, 0))
        self.start_button = ttk.Button(
            actions, text="Iniciar captura", style="Acao.TButton", command=self._start_capture
        )
        self.start_button.pack(side="left")
        self.stop_button = ttk.Button(
            actions,
            text="Parar",
            style="Acao.TButton",
            command=self._stop_capture,
            state="disabled",
        )
        self.stop_button.pack(side="left", padx=6)
        ttk.Button(actions, text="Zerar", command=self._reset).pack(side="left", padx=6)
        ttk.Button(actions, text="Exportar", command=self._export).pack(side="left")

    @staticmethod
    def _config_choices() -> list[str]:
        common = (9600, 19200, 38400, 57600, 115200, 4800, 2400)
        return [
            SerialConfig(baud, *fmt).label for baud in common for fmt in COMMON_FRAME_FORMATS[:3]
        ]

    # ------------------------------------------------------------------
    # aviso de comando novo
    # ------------------------------------------------------------------

    def _build_banner(self) -> None:
        self.banner = tk.Frame(self, background=PALETTE["banner_idle"], padx=10, pady=8)
        self.banner.grid(row=1, column=0, sticky="ew", pady=(8, 0))
        self.banner.columnconfigure(0, weight=1)

        self.banner_label = tk.Label(
            self.banner,
            textvariable=self.banner_var,
            background=PALETTE["banner_idle"],
            anchor="w",
            font=("TkDefaultFont", 12, "bold"),
        )
        self.banner_label.grid(row=0, column=0, sticky="ew")

        self.counter_label = tk.Label(
            self.banner,
            textvariable=self.counter_var,
            background=PALETTE["banner_idle"],
            anchor="e",
            font=("TkDefaultFont", 11, "bold"),
        )
        self.counter_label.grid(row=0, column=1, sticky="e")

    def _flash_new_command(self, entry) -> None:
        """Anuncia na faixa que um comando inedito acabou de aparecer."""
        sample = entry.samples[-1].hex(" ").upper() if entry.samples else ""
        self.banner_var.set(f"COMANDO NOVO:  {entry.signature}    {sample}")
        for widget in (self.banner, self.banner_label, self.counter_label):
            widget.configure(background=PALETTE["banner"])
        self.banner_label.configure(foreground=PALETTE["new_text"])

    def _fade_banner(self) -> None:
        for widget in (self.banner, self.banner_label, self.counter_label):
            widget.configure(background=PALETTE["banner_idle"])
        self.banner_label.configure(foreground="black")

    # ------------------------------------------------------------------
    # 4 e 5. comandos identificados e rotulos
    # ------------------------------------------------------------------

    def _build_main(self) -> None:
        panes = ttk.PanedWindow(self, orient="horizontal")
        panes.grid(row=2, column=0, sticky="nsew", pady=(8, 0))

        left = ttk.LabelFrame(panes, text="4. Comandos identificados", padding=6)
        left.rowconfigure(1, weight=1)
        left.columnconfigure(0, weight=1)
        panes.add(left, weight=3)

        toolbar = ttk.Frame(left)
        toolbar.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 6))
        ttk.Label(toolbar, text="Ordenar:").pack(side="left")
        sorter = ttk.Combobox(
            toolbar,
            textvariable=self.sort_var,
            values=["novos primeiro", "mais frequentes", "mais recentes", "assinatura"],
            state="readonly",
            width=17,
        )
        sorter.pack(side="left", padx=4)
        sorter.bind("<<ComboboxSelected>>", lambda _e: self._reorder_commands())
        self.filter_button = ttk.Button(
            toolbar, text="So este nos frames", command=self._toggle_filter
        )
        self.filter_button.pack(side="left", padx=4)
        ttk.Button(toolbar, text="Reagrupar", command=self._autotune).pack(side="left")

        columns = ("novo", "assinatura", "rotulo", "n", "dir", "crc", "tam", "exemplo")
        self.command_tree = ttk.Treeview(left, columns=columns, show="headings", height=14)
        headings = {
            "novo": ("", 62, "center"),
            "assinatura": ("Assinatura", 105, "w"),
            "rotulo": ("Rotulo", 170, "w"),
            "n": ("N", 55, "center"),
            "dir": ("Dir", 45, "center"),
            "crc": ("CRC", 55, "center"),
            "tam": ("Bytes", 50, "center"),
            "exemplo": ("Exemplo", 290, "w"),
        }
        for key, (text, width, anchor) in headings.items():
            self.command_tree.heading(key, text=text)
            self.command_tree.column(key, width=width, anchor=anchor)
        self.command_tree.grid(row=1, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(left, orient="vertical", command=self.command_tree.yview)
        self.command_tree.configure(yscrollcommand=scroll.set)
        scroll.grid(row=1, column=1, sticky="ns")
        self.command_tree.tag_configure("novo", background=PALETTE["new"])
        self.command_tree.tag_configure("rotulado", background=PALETTE["labelled"])
        self.command_tree.bind("<<TreeviewSelect>>", self._on_command_selected)
        self.command_tree.bind("<Double-1>", lambda _e: self._focus_label())

        right = ttk.LabelFrame(panes, text="Detalhe do comando", padding=6)
        right.rowconfigure(1, weight=1)
        right.columnconfigure(0, weight=1)
        panes.add(right, weight=2)

        editor = ttk.Frame(right)
        editor.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 6))
        editor.columnconfigure(0, weight=1)
        ttk.Label(editor, text="5. Rotulo deste comando:", style="Titulo.TLabel").grid(
            row=0, column=0, columnspan=2, sticky="w"
        )
        self.label_entry = ttk.Entry(editor, textvariable=self.label_var)
        self.label_entry.grid(row=1, column=0, sticky="ew", pady=(4, 0))
        self.label_entry.bind("<Return>", lambda _e: self._save_label())
        ttk.Button(editor, text="Salvar", command=self._save_label).grid(
            row=1, column=1, padx=(6, 0), pady=(4, 0)
        )

        self.detail_text = tk.Text(right, height=12, wrap="none", font=("TkFixedFont", 9))
        self.detail_text.grid(row=1, column=0, sticky="nsew")
        detail_scroll = ttk.Scrollbar(right, orient="vertical", command=self.detail_text.yview)
        self.detail_text.configure(yscrollcommand=detail_scroll.set, state="disabled")
        detail_scroll.grid(row=1, column=1, sticky="ns")

    # ------------------------------------------------------------------
    # 3. frames e registro, em segundo plano
    # ------------------------------------------------------------------

    def _build_bottom(self) -> None:
        notebook = ttk.Notebook(self)
        notebook.grid(row=3, column=0, sticky="nsew", pady=(8, 0))

        frames_tab = ttk.Frame(notebook, padding=4)
        frames_tab.rowconfigure(0, weight=1)
        frames_tab.columnconfigure(0, weight=1)
        notebook.add(frames_tab, text="3. Frames ao vivo")

        columns = ("tempo", "dir", "tam", "crc", "hex", "ascii", "comando")
        self.frame_tree = ttk.Treeview(frames_tab, columns=columns, show="headings", height=8)
        headings = {
            "tempo": ("Tempo (s)", 85, "center"),
            "dir": ("Dir", 45, "center"),
            "tam": ("Bytes", 50, "center"),
            "crc": ("CRC", 55, "center"),
            "hex": ("Dados (hex)", 410, "w"),
            "ascii": ("ASCII", 140, "w"),
            "comando": ("Comando", 190, "w"),
        }
        for key, (text, width, anchor) in headings.items():
            self.frame_tree.heading(key, text=text)
            self.frame_tree.column(key, width=width, anchor=anchor)
        self.frame_tree.grid(row=0, column=0, sticky="nsew")
        scroll = ttk.Scrollbar(frames_tab, orient="vertical", command=self.frame_tree.yview)
        self.frame_tree.configure(yscrollcommand=scroll.set)
        scroll.grid(row=0, column=1, sticky="ns")
        self.frame_tree.tag_configure("bad", background=PALETTE["bad"])
        self.frame_tree.tag_configure("novo", background=PALETTE["new"])
        ttk.Checkbutton(
            frames_tab, text="Rolar automaticamente", variable=self.autoscroll_var
        ).grid(row=1, column=0, sticky="w", pady=(4, 0))

        log_tab = ttk.Frame(notebook, padding=4)
        log_tab.rowconfigure(0, weight=1)
        log_tab.columnconfigure(0, weight=1)
        notebook.add(log_tab, text="Registro")
        self.log_text = tk.Text(log_tab, height=8, wrap="word", font=("TkFixedFont", 9))
        self.log_text.grid(row=0, column=0, sticky="nsew")
        log_scroll = ttk.Scrollbar(log_tab, orient="vertical", command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=log_scroll.set, state="disabled")
        log_scroll.grid(row=0, column=1, sticky="ns")

    def _build_status(self) -> None:
        ttk.Label(self, textvariable=self.status_var, relief="sunken", anchor="w").grid(
            row=4, column=0, sticky="ew", pady=(8, 0)
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
                "Barramento simulado: o trafego e gerado na configuracao escrita ao "
                "lado, e a deteccao tenta descobri-la do zero."
            )
            return
        self.port_combo.configure(state="normal")
        ports = list_ports()
        self.port_combo.configure(values=[device for device, _ in ports])
        if ports and not self.port_var.get():
            self.port_var.set(ports[0][0])
        self._set_status(
            f"{len(ports)} porta(s) encontrada(s)."
            if ports
            else "Nenhuma porta serial. Conecte o adaptador ou marque 'Barramento simulado'."
        )

    def _on_protocol_change(self) -> None:
        profile = get_profile(self.protocol_var.get())
        # O radio "Toolbutton" perde o indicador de selecao quando desabilitado
        # durante a captura, entao o protocolo ativo tambem vai no titulo.
        self.chooser.configure(text=f"1. Protocolo do barramento: RS-{profile.protocol.short}")
        self.hint_label.configure(text=f"{profile.description}\n{profile.hints[0]}")
        if self.session is not None and self.session.running:
            self._set_status(
                "O protocolo so vale a partir da proxima captura: pare e inicie de novo."
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
    # deteccao automatica
    # ------------------------------------------------------------------

    def _start_detection(self) -> None:
        if self.session is not None and self.session.running:
            messagebox.showinfo("Captura em andamento", "Pare a captura antes de detectar.")
            return
        source = self._make_source()
        if source is None:
            return
        self.detect_button.configure(state="disabled")
        self._set_status("Identificando a configuracao da linha... leva alguns segundos.")
        protocol = self.protocol_var.get()

        def worker() -> None:
            try:
                source.open()
                self._detect_queue.put(("ok", scan_candidates(source, protocol, dwell=0.8)))
            except (SourceError, ValueError) as exc:
                self._detect_queue.put(("error", str(exc)))
            finally:
                try:
                    source.close()
                except Exception:  # pragma: no cover - limpeza best effort
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
        if not report.is_conclusive:
            messagebox.showwarning(
                "Resultado sem certeza",
                f"{report.summary()}\n\nO segundo colocado ficou perto. Confirme se o "
                f"barramento estava trafegando durante a varredura, ou escolha a "
                f"configuracao na mao.",
            )

    # ------------------------------------------------------------------
    # captura
    # ------------------------------------------------------------------

    def _start_capture(self) -> None:
        if self.session is not None and self.session.running:
            return
        source = self._make_source()
        config = self._current_config()
        if source is None or config is None:
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
        self._baseline_until = time.monotonic() + BASELINE_SECONDS
        self.session.start()
        self._set_running(True)
        self.banner_var.set("Mapeando o barramento...")
        self._set_status(f"Capturando em {source.name} @ {config.label}")
        self._log(f"Captura iniciada: {source.name} @ {config.label} ({self.protocol_var.get()})")

    def _set_running(self, running: bool) -> None:
        """Trava o que nao pode mudar no meio de uma captura."""
        self.start_button.configure(state="disabled" if running else "normal")
        self.stop_button.configure(state="normal" if running else "disabled")
        self.detect_button.configure(state="disabled" if running else "normal")
        for button in self.protocol_buttons.values():
            button.configure(state="disabled" if running else "normal")

    def _stop_capture(self) -> None:
        if self.session is not None:
            self.session.stop()
            self.session.save_labels()
        if self.source is not None:
            try:
                self.source.close()
            except Exception:  # pragma: no cover - limpeza best effort
                pass
            self.source = None
        self._set_running(False)
        self._set_status("Captura parada. Rotulos salvos.")

    def _clear_views(self) -> None:
        self.frame_tree.delete(*self.frame_tree.get_children())
        self.command_tree.delete(*self.command_tree.get_children())
        self._frame_rows = 0
        self._new_until.clear()
        self._new_count = 0
        self._selected = None
        self.counter_var.set("0 comandos")

    def _reset(self) -> None:
        if self.session is not None:
            self.session.reset()
        self._clear_views()
        self.banner_var.set("Contadores zerados. Os rotulos foram mantidos.")
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
        self._expire_highlights()
        self._pump_id = self.after(REFRESH_MS, self._pump)

    def _expire_highlights(self) -> None:
        """Apaga o destaque dos comandos que ja deixaram de ser novidade."""
        if not self._new_until:
            return
        now = time.monotonic()
        expired = [sig for sig, until in self._new_until.items() if until <= now]
        for signature in expired:
            self._new_until.pop(signature, None)
            self._refresh_command_row(signature)
        if expired and not self._new_until:
            self._fade_banner()

    def _drain_session(self) -> None:
        session = self.session
        assert session is not None
        touched: set[str] = set()
        arrived: list = []

        for event in session.drain_events():
            if event.kind == "frame" and event.frame is not None:
                self._append_frame(event.frame, event.entry)
                if event.entry is not None:
                    touched.add(event.entry.signature)
            elif event.kind == "command" and event.entry is not None:
                arrived.append(event.entry)
                touched.add(event.entry.signature)
            elif event.kind == "status":
                self._set_status(event.text)
                self._log(event.text)
            elif event.kind == "error":
                self._log(f"ERRO: {event.text}")
                self._set_status(f"Erro: {event.text}")
            elif event.kind == "stopped":
                self._log(event.text)
                self._set_running(False)

        now = time.monotonic()
        inventorying = now < self._baseline_until
        for entry in arrived:
            self._new_count += 1
            if inventorying:
                # Inventario inicial: registra, mas nao acende.
                self.banner_var.set(
                    f"Mapeando o barramento... {self._new_count} comandos encontrados"
                )
                self._log(f"Comando no inventario inicial: {entry.signature}")
            else:
                self._new_until[entry.signature] = now + NEW_HIGHLIGHT_SECONDS
                self._flash_new_command(entry)
                self._log(
                    f"COMANDO NOVO #{self._new_count}: {entry.signature} "
                    f"({entry.length} bytes) - clique para rotular"
                )

        for signature in touched:
            self._refresh_command_row(signature)
        if arrived:
            self._reorder_commands()
        if touched:
            self._update_counter()
        if self._selected in touched:
            self._show_details(self._selected)

    def _update_counter(self) -> None:
        session = self.session
        if session is None:
            return
        total = len(session.catalog.entries)
        unlabelled = len(session.catalog.unlabelled)
        self.counter_var.set(f"{total} comandos  |  {unlabelled} sem rotulo")
        stats = session.stats
        self._set_status(
            f"{stats.frames_seen} frames  |  {total} comandos  |  checksum: "
            f"{session.checksum.name if session.checksum else 'procurando...'}"
        )

    def _append_frame(self, frame, entry) -> None:
        if self._filter and frame.signature != self._filter:
            return
        tags: list[str] = []
        if frame.checksum_ok is False:
            tags.append("bad")
        elif frame.signature in self._new_until:
            tags.append("novo")
        crc = {True: "ok", False: "ERRO", None: "-"}[frame.checksum_ok]
        label = (
            entry.display_name
            if entry is not None
            else ("(cortado no fim da captura)" if frame.truncated else "")
        )
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

    def _refresh_command_row(self, signature: str) -> None:
        session = self.session
        if session is None:
            return
        entry = session.catalog.entries.get(signature)
        if entry is None:
            return
        ratio = entry.checksum_ratio
        sample = entry.samples[-1].hex(" ").upper() if entry.samples else ""
        is_new = signature in self._new_until
        values = (
            "NOVO" if is_new else "",
            entry.signature,
            entry.label or "",
            entry.count,
            entry.direction.value,
            f"{ratio * 100:.0f}%" if ratio is not None else "-",
            entry.length,
            sample[:60],
        )
        tags = ("novo",) if is_new else (("rotulado",) if entry.is_labelled else ())
        if self.command_tree.exists(signature):
            self.command_tree.item(signature, values=values, tags=tags)
        else:
            self.command_tree.insert("", "end", iid=signature, values=values, tags=tags)

    def _reorder_commands(self) -> None:
        """Reordena a lista conforme o criterio escolhido.

        O padrao poe os comandos novos no topo: e o que a pessoa esta
        esperando ver quando liga o analisador em um equipamento estranho.
        """
        session = self.session
        if session is None:
            return
        criterion = self.sort_var.get()
        entries = list(session.catalog.entries.values())
        if criterion == "novos primeiro":
            entries.sort(key=lambda e: -e.first_seen)
        elif criterion == "mais frequentes":
            entries.sort(key=lambda e: (-e.count, e.signature))
        elif criterion == "mais recentes":
            entries.sort(key=lambda e: -e.last_seen)
        else:
            entries.sort(key=lambda e: e.signature)
        for position, entry in enumerate(entries):
            if self.command_tree.exists(entry.signature):
                self.command_tree.move(entry.signature, "", position)

    # ------------------------------------------------------------------
    # selecao, detalhe e rotulo
    # ------------------------------------------------------------------

    def _on_command_selected(self, _event=None) -> None:
        selection = self.command_tree.selection()
        if not selection:
            return
        self._selected = selection[0]
        session = self.session
        entry = session.catalog.entries.get(self._selected) if session else None
        self.label_var.set(entry.label if entry else "")
        self._show_details(self._selected)

    def _focus_label(self) -> None:
        self.label_entry.focus_set()
        self.label_entry.select_range(0, "end")

    def _show_details(self, signature: str | None) -> None:
        if signature is None or self.session is None:
            return
        entry = self.session.catalog.entries.get(signature)
        if entry is None:
            return
        lines = [
            f"Assinatura : {entry.signature}",
            f"Rotulo     : {entry.label or '(sem rotulo)'}",
            f"Chave      : bytes {entry.key_offsets} = {entry.key_hex}",
            f"Tamanho    : {entry.length} bytes",
            f"Ocorrencias: {entry.count}    sentido: {entry.direction.value}",
        ]
        ratio = entry.checksum_ratio
        if ratio is not None:
            total = entry.checksum_ok + entry.checksum_bad
            lines.append(f"Checksum   : {entry.checksum_ok}/{total} validos ({ratio * 100:.0f}%)")

        fields = entry.field_map(self.session.catalog.checksum_width)
        if fields:
            lines += ["", "Mapa de campos:", f"  {'byte':>4}  {'tipo':<11} conteudo"]
            lines += [
                f"  {stats.offset:>4}  {stats.kind.value:<11} {stats.describe()}"
                for stats in fields
            ]
        if entry.samples:
            lines += ["", "Ultimas amostras:"]
            for sample in list(entry.samples)[-6:]:
                readable = "".join(chr(b) if 0x20 <= b < 0x7F else "." for b in sample)
                lines.append(f"  {sample.hex(' ').upper():<52} {readable}")

        self.detail_text.configure(state="normal")
        self.detail_text.delete("1.0", "end")
        self.detail_text.insert("1.0", "\n".join(lines))
        self.detail_text.configure(state="disabled")

    def _save_label(self) -> None:
        if self.session is None or self._selected is None:
            messagebox.showinfo("Rotulo", "Selecione um comando na lista primeiro.")
            return
        label = self.label_var.get().strip()
        self.session.set_label(self._selected, label)
        self.session.save_labels()
        self._refresh_command_row(self._selected)
        self._show_details(self._selected)
        self._update_counter()
        self._set_status(f"Rotulo salvo: {self._selected} = {label or '(vazio)'}")

    # ------------------------------------------------------------------
    # acoes secundarias
    # ------------------------------------------------------------------

    def _toggle_filter(self) -> None:
        if self._filter:
            self._filter = None
            self.filter_button.configure(text="So este nos frames")
            self._set_status("Filtro removido: mostrando todos os frames.")
        elif self._selected:
            self._filter = self._selected
            self.filter_button.configure(text="Mostrar todos")
            self._set_status(f"Mostrando apenas {self._filter} na tabela de frames.")
        else:
            messagebox.showinfo("Filtro", "Selecione um comando para filtrar.")
            return
        self.frame_tree.delete(*self.frame_tree.get_children())
        self._frame_rows = 0

    def _autotune(self) -> None:
        if self.session is None:
            messagebox.showinfo("Reagrupar", "Nada capturado ainda.")
            return
        suggested = self.session.catalog.suggest_key_offsets()
        current = self.session.catalog.key_offsets
        if suggested == current:
            messagebox.showinfo(
                "Reagrupar", f"Os bytes {current} ja sao a melhor chave para este trafego."
            )
            return
        if not messagebox.askyesno(
            "Reagrupar comandos",
            f"Usar os bytes {suggested} para identificar os comandos, no lugar de "
            f"{current}?\n\nOs comandos serao reagrupados a partir do que ja foi "
            f"capturado. Os rotulos que continuarem valendo sao mantidos.",
        ):
            return
        self.session.catalog.regroup(suggested)
        self.command_tree.delete(*self.command_tree.get_children())
        self._new_until.clear()
        for signature in self.session.catalog.entries:
            self._refresh_command_row(signature)
        self._reorder_commands()
        self._update_counter()
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
        Path(path).write_text(
            json.dumps(self.session.report(), indent=2, ensure_ascii=False), encoding="utf-8"
        )
        self._set_status(f"Relatorio salvo em {path}")

    # ------------------------------------------------------------------

    def on_close(self) -> None:
        # Cancela o callback periodico antes de destruir: um `after` pendente
        # disparando contra widget destruido levanta TclError na saida.
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
    root.geometry("1400x900")
    root.minsize(1050, 700)
    app = SerialScanApp(root, args)
    root.protocol("WM_DELETE_WINDOW", app.on_close)
    root.mainloop()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
