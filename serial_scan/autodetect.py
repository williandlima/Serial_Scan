"""Automatic identification of the frame settings (step 2 of the project).

Two strategies, both ending in the same ranked report:

``scan``
    Open the port at each candidate configuration in turn, capture for a
    moment, and score the bytes that come out (:mod:`serial_scan.scoring`).
    Works with any USB-serial adapter. Cost: it needs the bus to be talking
    during every probe, so a slow poll loop makes the scan slow.

``oversample``
    Open the port at a much higher rate than the line, treat every received
    byte as 8 samples of the line level, and decode in software
    (:mod:`serial_scan.uart`). One capture is enough to measure the bit time
    *and* to try every framing hypothesis on identical data, which makes it
    both faster and more conclusive. It needs an adapter that tolerates the
    high rate, and it cannot see the line while it is idle-high only.

The report always keeps the runners-up, because "9600 8N1 at 92% and 9600 8E1
at 88%" is information the operator needs, not a detail to hide.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Sequence

from .framing import Frame, GapHistogram, TimedByte, split_frames
from .portconfig import (
    COMMON_FRAME_FORMATS,
    PARITY_EVEN,
    PARITY_NONE,
    PARITY_ODD,
    STANDARD_BAUDRATES,
    SerialConfig,
)
from .protocols import Protocol, ProtocolProfile, get_profile
from .scoring import StreamScore, score_frames
from .sources import ByteSource
from .uart import BaudEstimate, DecodeResult, decode_samples, estimate_baudrate, parity_bit


@dataclass
class Candidate:
    """One configuration hypothesis and how well it explained the capture."""

    config: SerialConfig
    score: StreamScore
    frames: list[Frame] = field(default_factory=list)
    decode: DecodeResult | None = None
    #: Inter-byte gaps observed in the raw capture, before framing.
    gaps: GapHistogram = field(default_factory=GapHistogram)
    #: Additive corrections applied after scoring (parity disambiguation, ...).
    adjustment: float = 0.0
    notes: list[str] = field(default_factory=list)

    @property
    def confidence(self) -> float:
        base = self.score.total
        if self.decode is not None:
            # Framing and parity errors are hard evidence, unlike the
            # statistical metrics: weigh them heavily.
            base *= (1.0 - self.decode.error_rate) ** 2
        return max(0.0, min(1.0, base + self.adjustment))

    @property
    def percent(self) -> int:
        return int(round(self.confidence * 100))

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<Candidate {self.config.label} {self.percent}%>"


#: Preference order used to pick a representative among configurations that
#: yield exactly the same data bytes. Fewer stop bits first, because a
#: receiver set to 1 stop bit reads a 2-stop-bit line perfectly while the
#: reverse fails; then no parity, which is both the most common setting and
#: the one that never rejects a character.
_PARITY_PREFERENCE = {PARITY_NONE: 0, PARITY_EVEN: 1, PARITY_ODD: 2, "M": 3, "S": 4}


def _canonical_rank(config: SerialConfig) -> tuple:
    return (
        config.stopbits,
        _PARITY_PREFERENCE.get(config.parity, 9),
        -config.bytesize,
    )


@dataclass
class DetectionReport:
    """Ranked hypotheses plus everything needed to explain the verdict."""

    method: str
    protocol: Protocol
    candidates: list[Candidate] = field(default_factory=list)
    baud_estimate: BaudEstimate | None = None
    idle_gap: float = 0.0
    elapsed: float = 0.0
    notes: list[str] = field(default_factory=list)
    #: Configurations that decode to the same bytes as the winner. They are
    #: not competitors: choosing between them does not change the payload.
    equivalent: list[SerialConfig] = field(default_factory=list)

    @property
    def best(self) -> Candidate | None:
        return self.candidates[0] if self.candidates else None

    @property
    def config(self) -> SerialConfig | None:
        return self.best.config if self.best else None

    @property
    def confidence(self) -> float:
        return self.best.confidence if self.best else 0.0

    def accepts(self, config: SerialConfig) -> bool:
        """True when ``config`` is the winner or one of its equivalents."""
        return self.config == config or config in self.equivalent

    @property
    def rival(self) -> Candidate | None:
        """The best candidate that is *not* equivalent to the winner."""
        if not self.best:
            return None
        for candidate in self.candidates[1:]:
            if candidate.config not in self.equivalent:
                return candidate
        return None

    @property
    def is_conclusive(self) -> bool:
        """True when the winner is strong and clearly ahead of any real rival.

        Configurations that produce identical bytes are not rivals, so a tie
        between 8E1 and 8O1 does not make the detection inconclusive: it only
        means the parity bit itself could not be observed.
        """
        if not self.best or self.best.confidence < 0.55:
            return False
        rival = self.rival
        if rival is None:
            return True
        return (self.best.confidence - rival.confidence) >= 0.08

    def summary(self) -> str:
        if not self.best:
            return "Nenhuma configuracao plausivel encontrada."
        verdict = "identificada" if self.is_conclusive else "provavel (sem certeza)"
        text = f"Configuracao {verdict}: {self.best.config.label} ({self.best.percent}%)"
        if self.equivalent:
            alternatives = ", ".join(c.frame_format for c in self.equivalent)
            text += f" [bytes identicos em: {alternatives}]"
        return text

    def table(self, limit: int = 8) -> str:
        lines = [f"{'config':<16} {'conf':>5}  {'frames':>6}  {'checksum':<20} detalhes"]
        for candidate in self.candidates[:limit]:
            checksum = candidate.score.checksum
            checksum_text = checksum.name if checksum else "-"
            parts = " ".join(
                f"{key[:4]}={value:.2f}" for key, value in candidate.score.parts.items()
            )
            lines.append(
                f"{candidate.config.label:<16} {candidate.percent:>4}%  "
                f"{candidate.score.frames:>6}  {checksum_text:<20} {parts}"
            )
        return "\n".join(lines)


# ---------------------------------------------------------------------------
# Equivalence between configurations
# ---------------------------------------------------------------------------


def _fingerprint(candidate: Candidate) -> frozenset[bytes]:
    """The set of distinct frames a candidate produced."""
    return frozenset(f.data for f in candidate.frames if f.data)


def _same_bytes(a: Candidate, b: Candidate, threshold: float = 0.8) -> bool:
    """Whether two candidates decoded the line into the same content.

    Exact equality is not usable in the scan strategy, where every candidate
    listens to a *different* slice of time and therefore sees a different
    number of frames. What does hold, on a repetitive bus, is that equivalent
    configurations observe the same *set* of distinct frames.

    The overlap coefficient is used rather than the Jaccard index because the
    two windows are not the same length: one probe routinely catches a couple
    of frames the other missed, and that should not count against the match.
    Measured separation is wide — byte-identical formats land around 0.85-1.0,
    genuinely different configurations at 0.0 — so the threshold is not
    delicate.
    """
    left, right = _fingerprint(a), _fingerprint(b)
    if not left or not right:
        return False
    return len(left & right) / min(len(left), len(right)) >= threshold


def _group_equivalents(candidates: list[Candidate]) -> list[SerialConfig]:
    """Collapse the winner's byte-identical twins and elect a representative.

    Returns the configurations judged equivalent to the (possibly re-elected)
    winner, and reorders ``candidates`` in place so that the representative
    comes first.
    """
    if len(candidates) < 2:
        return []
    winner = candidates[0]

    def refuted(candidate: Candidate) -> bool:
        """Hard evidence beats byte-level equivalence.

        The oversampling strategy *can* see the parity bit, so an 8O1
        hypothesis on an 8E1 line shows parity errors on every character even
        though the eight data bits are identical. Such a candidate is not an
        equivalent, it is disproved, and must not win the election.
        """
        if candidate.decode is None or winner.decode is None:
            return False
        return candidate.decode.error_rate > winner.decode.error_rate + 0.02

    twins = [
        c for c in candidates[1:] if not refuted(c) and _same_bytes(winner, c)
    ]
    if not twins:
        return []

    group = [winner, *twins]
    representative = min(group, key=lambda c: _canonical_rank(c.config))
    if representative is not winner:
        candidates.remove(representative)
        candidates.insert(0, representative)
    return [c.config for c in group if c is not representative]


def _note_equivalents(report: DetectionReport) -> None:
    """Explain an equivalence class in the report notes."""
    if not report.equivalent or report.config is None:
        return
    alternatives = ", ".join(config.frame_format for config in report.equivalent)
    report.notes.append(
        f"{report.config.frame_format} foi escolhido entre formatos que geram "
        f"exatamente os mesmos bytes ({alternatives}). A diferenca esta no bit de "
        f"paridade ou no numero de stop bits, que nao aparece nos dados lidos; "
        f"para ler o barramento qualquer um deles serve."
    )


# ---------------------------------------------------------------------------
# Parity disambiguation
# ---------------------------------------------------------------------------


@dataclass
class ParityHint:
    parity: str
    ratio: float
    tested: int

    @property
    def strong(self) -> bool:
        return self.tested >= 16 and self.ratio >= 0.98


def infer_seven_bit_parity(data: bytes) -> ParityHint | None:
    """Detect 7-bit-plus-parity data captured as 8N1.

    A receiver set to 8N1 reading a 7E1 line produces no framing error at all:
    it simply latches the parity bit as the most significant data bit. So if
    bit 7 of (almost) every byte equals the even parity of the lower seven
    bits, the line is really 7E1 and not 8N1. Same for odd.

    Random 8-bit data satisfies either test about half the time, hence the
    strict ratio and the minimum sample count.
    """
    if len(data) < 8:
        return None
    even_hits = 0
    odd_hits = 0
    for byte in data:
        low = byte & 0x7F
        msb = (byte >> 7) & 1
        if msb == parity_bit(low, 7, PARITY_EVEN):
            even_hits += 1
        if msb == parity_bit(low, 7, PARITY_ODD):
            odd_hits += 1
    if even_hits >= odd_hits:
        return ParityHint(PARITY_EVEN, even_hits / len(data), len(data))
    return ParityHint(PARITY_ODD, odd_hits / len(data), len(data))


def _apply_parity_disambiguation(candidates: list[Candidate], notes: list[str]) -> None:
    """Break the 8N1 / 7E1 tie using the parity-in-the-MSB test.

    Only meaningful for candidates that decoded 8 data bits with no parity,
    since those are the ones that would silently absorb a parity bit.
    """
    reference = next(
        (
            c
            for c in candidates
            if c.config.bytesize == 8
            and c.config.parity == PARITY_NONE
            and c.score.bytes_seen >= 16
        ),
        None,
    )
    if reference is None:
        return
    data = b"".join(f.data for f in reference.frames)
    hint = infer_seven_bit_parity(data)
    if hint is None or not hint.strong:
        return

    notes.append(
        f"Bit 7 dos bytes segue paridade {hint.parity} em {hint.ratio:.0%} do fluxo: "
        f"a linha e 7{hint.parity}1, nao 8N1."
    )
    for candidate in candidates:
        if candidate.config.bytesize == 7 and candidate.config.parity == hint.parity:
            candidate.adjustment += 0.15
            candidate.notes.append("Confirmado pelo teste de paridade no bit 7.")
        elif candidate.config.bytesize == 8 and candidate.config.parity == PARITY_NONE:
            candidate.adjustment -= 0.15
            candidate.notes.append(
                "Rejeitado: o bit 7 carrega paridade, logo sao 7 bits de dados."
            )
    candidates.sort(key=lambda c: c.confidence, reverse=True)


# ---------------------------------------------------------------------------
# Strategy 1: candidate scan against a live source
# ---------------------------------------------------------------------------


def scan_candidates(
    source: ByteSource,
    protocol: Protocol | str = Protocol.RS485,
    dwell: float = 0.6,
    baudrates: Sequence[int] | None = None,
    frame_formats: Sequence[tuple[int, str, float]] | None = None,
    min_frames: int = 3,
    early_stop_confidence: float = 0.85,
    refine_top: int = 3,
    progress: object = None,
) -> DetectionReport:
    """Probe configurations on a live source, baud rate first.

    Trying every baud rate against every frame format would mean ~190 probes;
    at a realistic ``dwell`` that is minutes of scanning. Two observations cut
    it down to a few dozen:

    1. A wrong *baud rate* destroys the byte stream, while a wrong *format*
       usually only perturbs it. So the baud rate can be found first, probing
       each rate once with the most common format (8N1).
    2. Only the few best-scoring rates deserve a full format sweep.

    ``dwell`` is how long each candidate is listened to; it must cover a
    couple of bus transactions, so a slow poll loop wants a larger value.
    ``early_stop_confidence`` ends the first phase as soon as a rate is
    convincing enough, which is the common case since the usual rates are
    tried first.
    """
    profile = get_profile(protocol)
    if not source.supports_reconfigure:
        raise ValueError(
            f"{source.name} nao permite trocar a configuracao; "
            f"use analyse_capture() em um arquivo ja gravado."
        )

    bauds = tuple(baudrates) if baudrates else profile.baud_candidates
    formats = tuple(frame_formats) if frame_formats else COMMON_FRAME_FORMATS
    # Phase 1 cannot probe with 8N1 alone: reading an 8E1 line as 8N1 puts the
    # parity bit where the stop bit belongs, so the receiver throws framing
    # errors and mangles the stream even at the *correct* baud rate. Covering
    # the parity space costs three probes per rate and avoids missing the
    # right rate entirely.
    probe_formats = tuple(
        dict.fromkeys(
            fmt for fmt in ((8, PARITY_NONE, 1.0), (8, PARITY_EVEN, 1.0), (7, PARITY_EVEN, 1.0))
            if fmt in formats
        )
    ) or (formats[0],)

    started = time.monotonic()
    original = source.config
    results: dict[SerialConfig, Candidate] = {}
    notes: list[str] = []
    probes = 0

    def probe(config: SerialConfig) -> Candidate:
        nonlocal probes
        source.reconfigure(config)
        source.drain()
        items = _read_for(source, dwell)
        candidate = analyse_bytes(items, config, profile, min_frames=min_frames)
        results[config] = candidate
        probes += 1
        if callable(progress):
            progress(candidate, probes)
        return candidate

    try:
        # Phase 1 - find the baud rate.
        found = False
        for baud in bauds:
            for fmt in probe_formats:
                candidate = probe(SerialConfig(baud, *fmt))
                if candidate.confidence >= early_stop_confidence:
                    notes.append(
                        f"Busca de velocidade interrompida em {baud} bps: "
                        f"{candidate.percent}% de confianca ja com "
                        f"{candidate.config.frame_format}."
                    )
                    found = True
                    break
            if found:
                break

        # Phase 2 - sweep the frame formats on the most promising rates.
        ranked_bauds = sorted(
            results.values(), key=lambda c: c.confidence, reverse=True
        )[: max(1, refine_top)]
        for leader in ranked_bauds:
            for fmt in formats:
                config = SerialConfig(leader.config.baudrate, *fmt)
                if config not in results:
                    probe(config)
    finally:
        source.reconfigure(original)

    candidates = sorted(results.values(), key=lambda c: c.confidence, reverse=True)
    _apply_parity_disambiguation(candidates, notes)
    equivalent = _group_equivalents(candidates)
    notes.insert(0, f"{probes} configuracoes testadas em duas fases.")

    report = DetectionReport(
        method="scan",
        protocol=profile.protocol,
        candidates=candidates,
        elapsed=time.monotonic() - started,
        notes=notes,
        equivalent=equivalent,
    )
    _note_equivalents(report)
    if report.best:
        report.idle_gap = _suggest_idle_gap(report.best, profile)
    if not report.is_conclusive:
        report.notes.append(
            "Resultado nao conclusivo: aumente o tempo por candidato (dwell) ou "
            "verifique se o barramento esta realmente trafegando."
        )
    return report


def _read_for(source: ByteSource, duration: float, slice_size: float = 0.05) -> list[TimedByte]:
    out: list[TimedByte] = []
    remaining = duration
    while remaining > 0:
        chunk = min(slice_size, remaining)
        out.extend(source.read(chunk))
        remaining -= chunk
    return out


def analyse_bytes(
    items: Sequence[TimedByte],
    config: SerialConfig,
    profile: ProtocolProfile,
    min_frames: int = 3,
) -> Candidate:
    """Frame and score one capture under an assumed configuration."""
    idle_gap = profile.idle_gap_chars * config.char_time
    frames = split_frames(items, idle_gap)
    score = score_frames(frames, key_offsets=profile.key_offsets, min_frames=min_frames)
    # Keep the raw inter-byte gaps: they are the only place where the
    # "inside a frame" and "between frames" populations are both visible, and
    # they are destroyed by the split above.
    gaps = GapHistogram()
    for previous, following in zip(items, items[1:]):
        gaps.add(following.time - previous.time)
    return Candidate(config=config, score=score, frames=frames, gaps=gaps)


# ---------------------------------------------------------------------------
# Strategy 2: oversampled line capture
# ---------------------------------------------------------------------------


def analyse_samples(
    samples: Sequence[int],
    sample_rate: float,
    protocol: Protocol | str = Protocol.RS485,
    baudrates: Sequence[int] | None = None,
    frame_formats: Sequence[tuple[int, str, float]] | None = None,
    min_frames: int = 3,
) -> DetectionReport:
    """Identify the configuration from a line-level sample train.

    The bit time is measured first, which collapses the search space from
    "every baud rate times every format" down to "every format at the one
    measured baud rate". When the measurement does not snap to a standard
    rate, the neighbouring standard rates are tried instead of giving up.
    """
    profile = get_profile(protocol)
    started = time.monotonic()
    notes: list[str] = []

    estimate = estimate_baudrate(samples, sample_rate)
    if estimate is None:
        return DetectionReport(
            method="oversample",
            protocol=profile.protocol,
            elapsed=time.monotonic() - started,
            notes=["Sinal curto ou sem transicoes suficientes para medir o bit time."],
        )

    if baudrates is not None:
        bauds: tuple[int, ...] = tuple(baudrates)
    elif estimate.snapped is not None:
        bauds = (estimate.snapped,)
        notes.append(
            f"Bit time medido: {estimate.measured:,.0f} bps -> {estimate.snapped} bps "
            f"(erro de ajuste {estimate.fit_error:.4f})."
        )
    else:
        # No standard rate within tolerance: try the closest few rather than
        # reporting failure, the measurement may be slightly off.
        ordered = sorted(STANDARD_BAUDRATES, key=lambda b: abs(b - estimate.measured))
        bauds = tuple(ordered[:3])
        notes.append(
            f"Bit time medido ({estimate.measured:,.0f} bps) nao casa com nenhum "
            f"padrao; testando {', '.join(str(b) for b in bauds)}."
        )

    formats = tuple(frame_formats) if frame_formats else COMMON_FRAME_FORMATS
    candidates: list[Candidate] = []
    for baud in bauds:
        for bytesize, parity, stopbits in formats:
            config = SerialConfig(baud, bytesize, parity, stopbits)
            decoded = decode_samples(samples, sample_rate, config)
            items = [TimedByte(time=c.time, value=c.value & 0xFF) for c in decoded.chars]
            candidate = analyse_bytes(items, config, profile, min_frames=min_frames)
            candidate.decode = decoded
            if decoded.chars:
                if decoded.framing_errors:
                    candidate.notes.append(
                        f"{decoded.framing_errors}/{len(decoded.chars)} erros de "
                        f"enquadramento (stop bit invalido)."
                    )
                if decoded.parity_errors:
                    candidate.notes.append(
                        f"{decoded.parity_errors}/{len(decoded.chars)} erros de paridade."
                    )
            candidates.append(candidate)

    candidates.sort(key=lambda c: c.confidence, reverse=True)
    _apply_parity_disambiguation(candidates, notes)
    equivalent = _group_equivalents(candidates)

    report = DetectionReport(
        method="oversample",
        protocol=profile.protocol,
        candidates=candidates,
        baud_estimate=estimate,
        elapsed=time.monotonic() - started,
        notes=notes,
        equivalent=equivalent,
    )
    _note_equivalents(report)
    if report.best:
        report.idle_gap = _suggest_idle_gap(report.best, profile)
    return report


def _suggest_idle_gap(candidate: Candidate, profile: ProtocolProfile) -> float:
    """Prefer the silence the traffic actually shows over the protocol default.

    Measured on the raw inter-byte gaps, which are bimodal: one character time
    inside a frame, several between frames. Gaps between *already split*
    frames cannot be used - the split removed the short population, so the
    widest jump left would be the request-to-response turnaround and the
    threshold would swallow the responses.
    """
    return candidate.gaps.suggest_idle_gap(
        candidate.config.char_time, floor_chars=1.5
    )


def analyse_capture(
    items: Sequence[TimedByte],
    protocol: Protocol | str = Protocol.RS485,
    config: SerialConfig | None = None,
    min_frames: int = 3,
) -> DetectionReport:
    """Score an already-decoded capture (a replayed file).

    The bytes were fixed by whatever configuration recorded them, so this
    cannot re-derive the baud rate. What it *can* do is confirm the recording
    is coherent, choose the framing gap from the observed timing, and detect
    the checksum.
    """
    profile = get_profile(protocol)
    config = config or SerialConfig(9600)
    candidate = analyse_bytes(items, config, profile, min_frames=min_frames)
    report = DetectionReport(
        method="capture",
        protocol=profile.protocol,
        candidates=[candidate],
        notes=[
            "Arquivo ja decodificado: baud rate e formato vem do cabecalho da "
            "gravacao, nao foram remedidos."
        ],
    )
    report.idle_gap = _suggest_idle_gap(candidate, profile)
    return report
