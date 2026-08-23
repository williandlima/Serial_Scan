"""A interface grafica, exercitada de verdade.

Estes testes so rodam onde existe Tk e um display; em servidor sem X eles se
declaram pulados em vez de falhar. Localmente (Windows, macOS, Linux com
Xvfb) eles cobrem o core: escolher o protocolo, ver comando novo chegando,
rotular.
"""

from __future__ import annotations

import time
from types import SimpleNamespace

import pytest

tk = pytest.importorskip("tkinter", reason="Tk nao instalado (no Debian/Ubuntu: python3-tk)")

from serial_scan.framing import Frame  # noqa: E402
from serial_scan.protocols import Protocol  # noqa: E402


@pytest.fixture
def app(tmp_path):
    """Uma janela real, com o barramento simulado por tras."""
    from serial_scan.ui import app as uiapp

    try:
        root = tk.Tk()
    except tk.TclError as exc:  # pragma: no cover - depende do ambiente
        pytest.skip(f"sem display grafico: {exc}")
    root.geometry("1400x880")
    args = SimpleNamespace(
        protocolo="485",
        porta=None,
        simular=True,
        config="19200 8N1",
        rotulos=str(tmp_path / "rotulos.json"),
    )
    window = uiapp.SerialScanApp(root, args)
    root.update_idletasks()
    root.update()
    yield window
    _teardown(window, root)


def _teardown(window, root) -> None:
    """Desmonta a janela na ordem certa.

    As ``tk.Variable`` precisam ser finalizadas *antes* de o root morrer: se
    sobrarem para o coletor de lixo, o ``__del__`` delas chama um interpretador
    Tcl que ja nao existe e o processo cai com "main thread is not in main
    loop" no fim da suite - com todos os testes verdes, o que e pior ainda.
    """
    import gc

    window._closing = True
    if window._pump_id is not None:
        try:
            window.after_cancel(window._pump_id)
        except tk.TclError:  # pragma: no cover - janela ja destruida
            pass
        window._pump_id = None
    try:
        window._stop_capture()
    except tk.TclError:  # pragma: no cover - janela ja destruida
        pass

    for nome, valor in list(vars(window).items()):
        if isinstance(valor, tk.Variable):
            delattr(window, nome)
    gc.collect()

    try:
        root.destroy()
    except tk.TclError:  # pragma: no cover - ja destruida por on_close()
        pass
    gc.collect()


def pump(app, seconds: float) -> None:
    """Gira o loop do Tk pelo tempo pedido, como um usuario olhando a tela."""
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        app.master.update()
        time.sleep(0.02)


def rows(app) -> list:
    return [app.command_tree.item(iid)["values"] for iid in app.command_tree.get_children()]


def tinted(app) -> int:
    return sum(
        1 for iid in app.command_tree.get_children() if "novo" in app.command_tree.item(iid)["tags"]
    )


class TestProtocolo:
    """Passo 1: escolher o protocolo."""

    def test_os_tres_protocolos_estao_disponiveis(self, app) -> None:
        assert set(app.protocol_buttons) == {p.value for p in Protocol}

    def test_a_escolha_aparece_no_titulo(self, app) -> None:
        """O radio perde o indicador quando desabilitado durante a captura,
        entao o protocolo ativo precisa estar escrito em algum lugar."""
        assert "RS-485" in app.chooser.cget("text")
        app.protocol_var.set("RS232")
        app._on_protocol_change()
        assert "RS-232" in app.chooser.cget("text")

    def test_a_dica_muda_com_o_protocolo(self, app) -> None:
        app.protocol_var.set("RS232")
        app._on_protocol_change()
        assert "Ponto a ponto" in app.hint_label.cget("text")
        app.protocol_var.set("RS485")
        app._on_protocol_change()
        assert "Multiponto" in app.hint_label.cget("text")

    def test_nao_da_para_trocar_no_meio_da_captura(self, app) -> None:
        app._start_capture()
        assert str(app.protocol_buttons["RS232"].cget("state")) == "disabled"
        app._stop_capture()
        assert str(app.protocol_buttons["RS232"].cget("state")) == "normal"


class TestComandosChegando:
    """Passo 4: ver os comandos novos chegando, que e o core."""

    def test_o_inventario_inicial_nao_acende_a_tela_toda(self, app) -> None:
        """Na largada todo comando e inedito. Acender os sete de uma vez nao
        destaca nada, entao a fase de inventario entra sem alarde."""
        app._start_capture()
        pump(app, 2.0)
        assert len(rows(app)) >= 5
        assert tinted(app) == 0
        assert "Mapeando" in app.banner_var.get()

    def test_comando_inedito_depois_do_inventario_acende(self, app) -> None:
        from serial_scan.ui.app import BASELINE_SECONDS

        app._start_capture()
        pump(app, BASELINE_SECONDS + 0.6)
        antes = len(rows(app))
        assert tinted(app) == 0

        app.session._handle(
            Frame(data=bytes.fromhex("0A100000000201F4"), time_start=99.0, time_end=99.01)
        )
        pump(app, 0.4)

        assert len(rows(app)) == antes + 1
        assert tinted(app) == 1
        assert "COMANDO NOVO" in app.banner_var.get()
        assert "L8:0A-10" in app.banner_var.get()
        assert app.banner.cget("background") != "#ededed"

    def test_o_comando_novo_vai_para_o_topo(self, app) -> None:
        from serial_scan.ui.app import BASELINE_SECONDS

        app._start_capture()
        pump(app, BASELINE_SECONDS + 0.6)
        app.session._handle(
            Frame(data=bytes.fromhex("0A100000000201F4"), time_start=99.0, time_end=99.01)
        )
        pump(app, 0.4)
        assert rows(app)[0][0] == "NOVO"
        assert rows(app)[0][1] == "L8:0A-10"

    def test_o_destaque_expira_sozinho(self, app, monkeypatch) -> None:
        from serial_scan.ui import app as uiapp

        monkeypatch.setattr(uiapp, "NEW_HIGHLIGHT_SECONDS", 0.5)
        monkeypatch.setattr(uiapp, "BASELINE_SECONDS", 0.5)
        app._start_capture()
        pump(app, 1.2)
        app.session._handle(
            Frame(data=bytes.fromhex("0A100000000201F4"), time_start=99.0, time_end=99.01)
        )
        pump(app, 0.3)
        assert tinted(app) == 1
        pump(app, 1.0)
        assert tinted(app) == 0
        assert app.banner.cget("background") == "#ededed"

    def test_o_contador_acompanha(self, app) -> None:
        app._start_capture()
        pump(app, 2.0)
        assert "comandos" in app.counter_var.get()
        assert "sem rotulo" in app.counter_var.get()

    def test_ordenacao(self, app) -> None:
        app._start_capture()
        pump(app, 2.5)
        app.sort_var.set("mais frequentes")
        app._reorder_commands()
        contagens = [int(linha[3]) for linha in rows(app)]
        assert contagens == sorted(contagens, reverse=True)


class TestFramesEDetalhe:
    """Passo 3 e o painel de detalhe."""

    def test_os_frames_aparecem_com_o_comando_ao_lado(self, app) -> None:
        app._start_capture()
        pump(app, 1.5)
        assert len(app.frame_tree.get_children()) > 10
        valores = app.frame_tree.item(app.frame_tree.get_children()[-1])["values"]
        assert valores[3] in ("ok", "ERRO", "-")

    def test_o_detalhe_traz_o_mapa_de_campos(self, app) -> None:
        app._start_capture()
        pump(app, 2.0)
        primeiro = app.command_tree.get_children()[0]
        app.command_tree.selection_set(primeiro)
        app._on_command_selected()
        texto = app.detail_text.get("1.0", "end")
        assert "Mapa de campos" in texto
        assert "chave" in texto
        assert "checksum" in texto

    def test_filtrar_por_um_comando(self, app) -> None:
        app._start_capture()
        pump(app, 2.0)
        alvo = app.command_tree.get_children()[0]
        app.command_tree.selection_set(alvo)
        app._on_command_selected()
        app._toggle_filter()
        pump(app, 1.0)
        assinatura = app.command_tree.item(alvo)["values"][1]
        tamanho = int(app.command_tree.item(alvo)["values"][6])
        assert app.frame_tree.get_children()
        for iid in app.frame_tree.get_children():
            assert int(app.frame_tree.item(iid)["values"][2]) == tamanho
        del assinatura


class TestRotulo:
    """Passo 5: rotular cada comando identificado."""

    def test_rotular_grava_e_pinta_a_linha(self, app) -> None:
        from serial_scan.ui import app as uiapp

        app._start_capture()
        pump(app, 2.5)
        alvo = app.command_tree.get_children()[0]
        app.command_tree.selection_set(alvo)
        app._on_command_selected()
        app.label_var.set("Leitura de temperatura")
        app._save_label()

        assert app.command_tree.item(alvo)["values"][2] == "Leitura de temperatura"
        # Enquanto esta na janela de novidade o amarelo vence; depois vira verde.
        app._new_until.clear()
        app._refresh_command_row(alvo)
        assert "rotulado" in app.command_tree.item(alvo)["tags"]
        del uiapp

    def test_o_rotulo_sobrevive_a_gravacao_em_disco(self, app, tmp_path) -> None:
        app._start_capture()
        pump(app, 2.0)
        alvo = app.command_tree.get_children()[0]
        assinatura = app.command_tree.item(alvo)["values"][1]
        app.command_tree.selection_set(alvo)
        app._on_command_selected()
        app.label_var.set("Comando principal")
        app._save_label()
        app._stop_capture()

        from serial_scan.labels import LabelStore

        gravado = LabelStore(tmp_path / "rotulos.json").load()
        assert gravado.labels_for("RS485")[assinatura] == "Comando principal"

    def test_o_rotulo_aparece_na_tabela_de_frames(self, app) -> None:
        app._start_capture()
        pump(app, 2.0)
        alvo = app.command_tree.get_children()[0]
        app.command_tree.selection_set(alvo)
        app._on_command_selected()
        app.label_var.set("Etiquetado")
        app._save_label()
        pump(app, 1.0)
        rotulos = {app.frame_tree.item(i)["values"][6] for i in app.frame_tree.get_children()}
        assert "Etiquetado" in rotulos


class TestCicloDeVida:
    def test_zerar_limpa_as_tabelas(self, app) -> None:
        app._start_capture()
        pump(app, 1.5)
        assert app.command_tree.get_children()
        app._reset()
        assert not app.command_tree.get_children()
        assert not app.frame_tree.get_children()

    def test_fechar_nao_deixa_callback_pendente(self, app) -> None:
        """Um `after` disparando contra widget destruido levanta TclError."""
        app._start_capture()
        pump(app, 0.5)
        app.on_close()
        assert app._closing is True
        assert app._pump_id is None
