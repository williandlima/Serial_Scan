"""RS-232 / RS-485 / RS-422 profiles.

The electrical standard does not change how a UART character is encoded, but
it changes a lot of what the analyser should *expect*:

* how many devices can talk on the bus,
* whether both directions land on the same pair of wires,
* whether it is worth trying to tell a request from a response,
* which byte offsets usually carry the address / function code.

Selecting the protocol (step 1 of the project) is therefore what seeds every
heuristic downstream.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum

from .portconfig import STANDARD_BAUDRATES


class Protocol(str, Enum):
    RS232 = "RS232"
    RS485 = "RS485"
    RS422 = "RS422"

    @classmethod
    def parse(cls, text: str) -> "Protocol":
        normalised = text.strip().upper().replace("-", "").replace("RS", "")
        for member in cls:
            if member.value.endswith(normalised):
                return member
        raise ValueError(f"protocolo desconhecido {text!r}; use 232, 485 ou 422")

    @property
    def short(self) -> str:
        return self.value.removeprefix("RS")


@dataclass(frozen=True)
class ProtocolProfile:
    """Everything the pipeline needs to know about the selected standard."""

    protocol: Protocol
    wires: int
    full_duplex: bool
    multidrop: bool
    #: Byte offsets that usually identify a command (address, function, ...).
    key_offsets: tuple[int, ...]
    #: Silence, in character times, that ends a frame. 3.5 is the Modbus RTU rule.
    idle_gap_chars: float
    #: Silence, in character times, above which a frame is assumed to start a
    #: new transaction rather than answer the previous one.
    turnaround_gap_chars: float
    #: Whether request/response direction can be guessed from timing alone.
    infer_direction: bool
    baud_candidates: tuple[int, ...]
    description: str
    hints: tuple[str, ...] = field(default_factory=tuple)
    #: Como ligar o analisador em paralelo ao barramento, sem perturba-lo.
    tap: tuple[str, ...] = field(default_factory=tuple)
    #: Quantos sentidos um unico adaptador consegue enxergar neste padrao.
    directions_per_adapter: int = 1

    @property
    def name(self) -> str:
        return self.protocol.value


_RS232 = ProtocolProfile(
    protocol=Protocol.RS232,
    wires=3,
    full_duplex=True,
    multidrop=False,
    key_offsets=(0,),
    idle_gap_chars=3.5,
    turnaround_gap_chars=20.0,
    infer_direction=False,
    baud_candidates=STANDARD_BAUDRATES,
    description="Ponto a ponto, full duplex, niveis +/-12 V (TX, RX, GND).",
    hints=(
        "Apenas dois equipamentos: nao ha campo de endereco na maioria dos protocolos.",
        "TX e RX sao fios separados; para ver os dois lados use dois adaptadores "
        "e duas sessoes de captura.",
        "Cabos longos (>15 m) costumam obrigar a baixar a velocidade.",
    ),
    tap=(
        "Ligue o RX do adaptador no fio que voce quer escutar e o GND no GND "
        "comum. O TX do adaptador fica desconectado.",
        "Um adaptador ve um sentido so. Para o dialogo completo use dois: um no "
        "TX do mestre, outro no TX do escravo.",
        "Nao ligue o TX do analisador em nada: dois transmissores no mesmo fio "
        "se anulam e derrubam a comunicacao real.",
    ),
    directions_per_adapter=1,
)

_RS485 = ProtocolProfile(
    protocol=Protocol.RS485,
    wires=2,
    full_duplex=False,
    multidrop=True,
    key_offsets=(0, 1),
    idle_gap_chars=3.5,
    turnaround_gap_chars=10.0,
    infer_direction=True,
    baud_candidates=STANDARD_BAUDRATES,
    description="Multiponto, half duplex, par trancado A/B (ate 32 nos por segmento).",
    hints=(
        "Os dois sentidos trafegam no mesmo par: pergunta e resposta aparecem "
        "intercaladas na mesma captura.",
        "O primeiro byte quase sempre e o endereco do escravo e o segundo a funcao.",
        "Silencio de 3,5 caracteres delimita o frame (regra do Modbus RTU).",
        "Se A e B estiverem invertidos a captura vira lixo constante: teste trocar.",
    ),
    tap=(
        "Ligue A no A e B no B do barramento, em paralelo, mais o GND de "
        "referencia. Um so adaptador ja ve os dois sentidos.",
        "NAO habilite o resistor de terminacao do adaptador: o barramento ja e "
        "terminado nas duas pontas, e um terceiro terminador carrega a linha.",
        "O adaptador nao pode acionar o DE (driver enable). O Serial Scan abre "
        "a porta com RTS e DTR desligados justamente por isso.",
        "Derive o mais curto possivel: um ramo longo ate o analisador cria "
        "reflexao no par.",
    ),
    directions_per_adapter=2,
)

_RS422 = ProtocolProfile(
    protocol=Protocol.RS422,
    wires=4,
    full_duplex=True,
    multidrop=True,
    key_offsets=(0, 1),
    idle_gap_chars=3.5,
    turnaround_gap_chars=15.0,
    infer_direction=True,
    baud_candidates=STANDARD_BAUDRATES,
    description="Um mestre e varios escravos, full duplex, dois pares diferenciais.",
    hints=(
        "Full duplex: o par do mestre e o par dos escravos sao independentes.",
        "Capturando um par so voce ve um sentido; capture os dois para o dialogo completo.",
        "O mestre e unico, entao o par de saida do mestre nunca tem colisao.",
    ),
    tap=(
        "Sao dois pares. Ligue o adaptador em paralelo no par que voce quer "
        "escutar (TX+/TX- do mestre, ou TX+/TX- dos escravos), mais o GND.",
        "Um adaptador por par: para ver pergunta e resposta juntas use dois "
        "adaptadores e duas sessoes.",
        "Nao habilite terminacao no ponto de derivacao: o par ja e terminado "
        "na ponta receptora.",
    ),
    directions_per_adapter=1,
)

PROFILES: dict[Protocol, ProtocolProfile] = {
    Protocol.RS232: _RS232,
    Protocol.RS485: _RS485,
    Protocol.RS422: _RS422,
}


def get_profile(protocol: Protocol | str) -> ProtocolProfile:
    if isinstance(protocol, str):
        protocol = Protocol.parse(protocol)
    return PROFILES[protocol]
