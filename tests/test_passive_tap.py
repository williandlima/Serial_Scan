"""O analisador fica em paralelo com um barramento vivo: nunca pode interferir.

Estes testes travam a garantia. Nao e detalhe de estilo: em quase todo
adaptador USB-RS485 e em toda placa MAX485, o RTS aciona o DE (driver enable).
Abrir a porta com o padrao do pyserial (``rts=True``, ``dtr=True``) liga o
transmissor do adaptador e ele passa a *dirigir* o par - colidindo justamente
com o trafego que se queria observar. O DTR ainda reinicia placas que o ligam
ao reset, Arduino entre elas.
"""

from __future__ import annotations

import pytest

from serial_scan.portconfig import SerialConfig
from serial_scan.protocols import PROFILES, Protocol, get_profile
from serial_scan.sources import SerialSource, SourceError


class FakeSerial:
    """Um dublê de ``serial.Serial`` que registra a ordem das operacoes."""

    def __init__(self) -> None:
        # Os mesmos padroes do pyserial: e exatamente isso que precisa ser
        # sobrescrito antes da abertura.
        self.rts = True
        self.dtr = True
        self.rtscts = False
        self.dsrdtr = False
        self.xonxoff = False
        self.port = None
        self.timeout = None
        self.baudrate = None
        self.bytesize = None
        self.parity = None
        self.stopbits = None
        self.exclusive = None
        self.is_open = False
        self.eventos: list[tuple[str, object]] = []
        self.escritas: list[bytes] = []

    def __setattr__(self, nome: str, valor: object) -> None:
        object.__setattr__(self, nome, valor)
        if nome != "eventos" and hasattr(self, "eventos"):
            self.eventos.append((nome, valor))

    def open(self) -> None:
        self.is_open = True
        self.eventos.append(("open", None))

    def close(self) -> None:
        self.is_open = False

    def write(self, data: bytes) -> int:  # pragma: no cover - nunca deve ocorrer
        self.escritas.append(data)
        return len(data)

    def reset_input_buffer(self) -> None:
        self.eventos.append(("reset_input_buffer", None))

    @property
    def in_waiting(self) -> int:
        return 0

    def read(self, n: int) -> bytes:  # pragma: no cover - nao usado aqui
        return b""

    def indice(self, nome: str) -> int:
        """Posicao da primeira ocorrencia de um evento."""
        for posicao, (chave, _) in enumerate(self.eventos):
            if chave == nome:
                return posicao
        raise AssertionError(f"evento {nome!r} nunca aconteceu: {self.eventos}")

    def valor_em(self, nome: str) -> object:
        for chave, valor in reversed(self.eventos):
            if chave == nome:
                return valor
        raise AssertionError(f"evento {nome!r} nunca aconteceu")


@pytest.fixture
def fake(monkeypatch):
    """Substitui ``serial.Serial`` pelo dublê, sem hardware nenhum."""
    import serial

    dublê = FakeSerial()
    monkeypatch.setattr(serial, "Serial", lambda *a, **k: dublê)
    return dublê


class TestAberturaPassiva:
    def test_rts_e_dtr_sao_desligados(self, fake) -> None:
        SerialSource("/dev/ttyFAKE", SerialConfig(19200)).open()
        assert fake.rts is False
        assert fake.dtr is False

    def test_desligados_ANTES_de_abrir(self, fake) -> None:
        """A ordem e o que importa.

        Ligar as linhas e derruba-las logo depois ainda daria um pulso no DE,
        suficiente para atropelar um frame em transito. O pyserial guarda o
        estado e o aplica na abertura, entao a atribuicao precisa vir antes.
        """
        SerialSource("/dev/ttyFAKE", SerialConfig(19200)).open()
        assert fake.indice("rts") < fake.indice("open")
        assert fake.indice("dtr") < fake.indice("open")
        # E nao podem ter sido religados depois.
        assert fake.valor_em("rts") is False
        assert fake.valor_em("dtr") is False

    def test_sem_controle_de_fluxo(self, fake) -> None:
        """Handshake por hardware faria o analisador mexer nas linhas."""
        SerialSource("/dev/ttyFAKE", SerialConfig(19200)).open()
        assert fake.rtscts is False
        assert fake.dsrdtr is False
        assert fake.xonxoff is False

    def test_a_configuracao_da_linha_e_aplicada(self, fake) -> None:
        SerialSource("/dev/ttyFAKE", SerialConfig(38400, 7, "E", 2.0)).open()
        assert fake.port == "/dev/ttyFAKE"
        assert fake.baudrate == 38400
        assert fake.timeout == 0

    def test_acesso_exclusivo_por_padrao(self, fake) -> None:
        """Duas sessoes na mesma porta roubariam bytes uma da outra."""
        SerialSource("/dev/ttyFAKE", SerialConfig(19200)).open()
        assert fake.exclusive is True

    def test_modo_ativo_e_explicito(self, fake) -> None:
        """Quem quiser as linhas ligadas precisa pedir; o padrao e passivo."""
        SerialSource("/dev/ttyFAKE", SerialConfig(19200), passive=False).open()
        assert fake.rts is True
        assert fake.dtr is True


class TestNuncaTransmite:
    def test_escrever_e_recusado(self, fake) -> None:
        origem = SerialSource("/dev/ttyFAKE", SerialConfig(19200))
        origem.open()
        with pytest.raises(SourceError, match="passivo"):
            origem.write(b"\x01\x03")
        assert fake.escritas == []

    def test_ler_nao_escreve_nada(self, fake) -> None:
        origem = SerialSource("/dev/ttyFAKE", SerialConfig(19200))
        origem.open()
        origem.read(0.01)
        origem.drain()
        assert fake.escritas == []

    def test_nenhuma_fonte_expoe_transmissao(self) -> None:
        """A classe base nao tem caminho de escrita: nada a jusante pode usar."""
        from serial_scan.sources import ByteSource

        assert not hasattr(ByteSource, "write")


class TestOrientacaoDeLigacao:
    @pytest.mark.parametrize("protocolo", list(Protocol))
    def test_todo_protocolo_explica_como_grampear(self, protocolo: Protocol) -> None:
        perfil = PROFILES[protocolo]
        assert perfil.tap, f"{protocolo.value} sem orientacao de ligacao"
        assert perfil.directions_per_adapter >= 1

    def test_rs485_ve_os_dois_sentidos_com_um_adaptador(self) -> None:
        """Half duplex num par so: pergunta e resposta caem na mesma captura."""
        assert get_profile("485").directions_per_adapter == 2

    def test_full_duplex_precisa_de_um_adaptador_por_sentido(self) -> None:
        assert get_profile("232").directions_per_adapter == 1
        assert get_profile("422").directions_per_adapter == 1

    def test_avisa_para_nao_terminar_o_barramento(self) -> None:
        """Um terceiro terminador no ramo carrega a linha."""
        texto = " ".join(get_profile("485").tap).lower()
        assert "terminacao" in texto

    def test_avisa_sobre_o_driver_enable(self) -> None:
        texto = " ".join(get_profile("485").tap).lower()
        assert "de" in texto and "driver enable" in texto
