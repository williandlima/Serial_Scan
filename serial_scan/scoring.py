"""Score how plausible a decoded byte stream is.

When the analyser cannot see individual bits (the usual case with an ordinary
USB-serial adapter) it has to judge a candidate configuration from the bytes
that came out of it. A *wrong* baud rate does not produce random-looking data:
it produces a very recognisable mess, and that is what these metrics measure.

Symptoms of a wrong configuration
---------------------------------
* Byte boundaries drift, so ``0x00`` and ``0xFF`` pile up (the receiver keeps
  latching onto runs of the same level).
* The set of distinct frame lengths explodes, because framing is now random.
* No checksum validates.
* The same command never repeats byte-for-byte, even though real buses are
  extremely repetitive.

Each metric returns 0..1 and the weighted mean is the confidence reported to
the operator. Weights are deliberately conservative: structure and checksum
dominate, cosmetic hints such as "looks like ASCII" only break ties.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import Sequence

from .checksums import ChecksumMatch, checksum_confidence, detect_checksum
from .framing import Frame

#: Structural sanity: necessary for a configuration to be plausible, but not
#: sufficient. A repeating poll loop decoded at the *wrong* baud rate produces
#: repeating garbage, so these metrics stay high even when everything is wrong.
STRUCTURE_WEIGHTS: dict[str, float] = {
    "repetition": 0.40,
    "length_consistency": 0.30,
    "byte_health": 0.30,
}

#: Structure alone can never exceed this fraction of the final confidence.
#: Passing it requires *evidence* that the bytes actually mean something: a
#: checksum that validates, or a stream that is genuinely readable text.
EVIDENCE_FLOOR = 0.35


@dataclass
class StreamScore:
    """Result of scoring one candidate configuration."""

    total: float
    parts: dict[str, float] = field(default_factory=dict)
    frames: int = 0
    bytes_seen: int = 0
    checksum: ChecksumMatch | None = None
    notes: list[str] = field(default_factory=list)

    @property
    def percent(self) -> int:
        return int(round(self.total * 100))

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<StreamScore {self.percent}% frames={self.frames}>"


def byte_health(data: bytes) -> float:
    """Penalise the ``0x00`` / ``0xFF`` pile-up typical of a wrong baud rate.

    Real payloads do contain zeros, so the penalty only bites past 40% of the
    stream, and it looks at long *runs* too: eight consecutive ``0xFF`` is a
    stuck line, not data.
    """
    if not data:
        return 0.0
    counts = Counter(data)
    degenerate = (counts[0x00] + counts[0xFF]) / len(data)
    score = 1.0
    if degenerate > 0.4:
        score -= min(0.8, (degenerate - 0.4) / 0.5)

    longest_run = 1
    run = 1
    for previous, current in zip(data, data[1:]):
        if current == previous and current in (0x00, 0xFF):
            run += 1
            longest_run = max(longest_run, run)
        else:
            run = 1
    if longest_run >= 8:
        score -= min(0.4, (longest_run - 7) * 0.05)

    # A stream using only a handful of distinct values at a wrong baud rate is
    # suspicious; at the right one it is normal for short captures, so this is
    # only applied to reasonably long streams.
    if len(data) >= 64 and len(counts) <= 3:
        score -= 0.2
    return max(0.0, min(1.0, score))


def printable_ratio(data: bytes) -> float:
    """Fraction of bytes that are printable ASCII, tab, CR or LF."""
    if not data:
        return 0.0
    good = sum(1 for b in data if 0x20 <= b < 0x7F or b in (0x09, 0x0A, 0x0D))
    return good / len(data)


def printable_score(data: bytes) -> float:
    """Reward text protocols without punishing binary ones.

    An almost-entirely-printable stream is strong evidence of a correct
    configuration. A binary stream sits around 40% printable by chance, so
    anything in that region scores a neutral 0.5 instead of a penalty.
    """
    ratio = printable_ratio(data)
    if ratio >= 0.95:
        return 1.0
    if ratio <= 0.6:
        return 0.5
    return 0.5 + (ratio - 0.6) * (0.5 / 0.35)


def length_consistency(frames: Sequence[Frame]) -> float:
    """How concentrated the frame lengths are.

    A bus running one protocol shows a handful of distinct frame lengths. A
    misconfigured capture shows nearly as many lengths as frames.
    """
    if len(frames) < 2:
        return 0.0
    lengths = Counter(len(f) for f in frames)
    distinct_ratio = len(lengths) / len(frames)
    top_share = lengths.most_common(1)[0][1] / len(frames)
    return max(0.0, min(1.0, 0.5 * (1.0 - distinct_ratio) + 0.5 * top_share))


def repetition(frames: Sequence[Frame], key_offsets: Sequence[int] = (0, 1)) -> float:
    """How repetitive the traffic is, at the frame and at the header level.

    Two independent signals are combined: identical frames (a poll loop
    re-sending the exact same request) and identical headers (same address and
    function, different payload). Either one is hard to obtain by accident.
    """
    if len(frames) < 2:
        return 0.0

    exact = Counter(f.data for f in frames)
    repeated_exact = sum(count for count in exact.values() if count > 1) / len(frames)

    headers: Counter[bytes] = Counter()
    for frame in frames:
        if len(frame.data) > max(key_offsets, default=0):
            headers[bytes(frame.data[offset] for offset in key_offsets)] += 1
    header_score = 0.0
    if headers:
        repeated_headers = sum(c for c in headers.values() if c > 1) / len(frames)
        # Few distinct headers over many frames is exactly what a poll loop
        # looks like.
        concentration = 1.0 - min(1.0, len(headers) / len(frames))
        header_score = 0.5 * repeated_headers + 0.5 * concentration

    return max(0.0, min(1.0, 0.5 * repeated_exact + 0.5 * header_score))


def text_evidence(data: bytes) -> float:
    """Evidence that the stream is readable text rather than noise.

    Text protocols (Modbus ASCII, NMEA, AT commands, scale and printer
    protocols) often carry no checksum at all, so readability is the only
    proof available that the configuration is right. The bar is high on
    purpose: garbage decoded at the wrong baud rate lands near 40% printable,
    never near 99%.
    """
    ratio = printable_ratio(data)
    if ratio >= 0.99:
        return 1.0
    if ratio <= 0.85:
        return 0.0
    return (ratio - 0.85) / 0.14


def score_frames(
    frames: Sequence[Frame],
    key_offsets: Sequence[int] = (0, 1),
    min_frames: int = 3,
) -> StreamScore:
    """Combine every metric into a single confidence figure.

    Structure and evidence are combined multiplicatively rather than as a
    weighted sum. The reason is empirical: a poll loop sampled at a wrong baud
    rate scores *perfectly* on repetition and length consistency, because the
    same wrong decoding repeats just as faithfully as the right one. Only
    evidence that the bytes carry meaning separates the two.
    """
    data = b"".join(f.data for f in frames)
    notes: list[str] = []

    match = detect_checksum([f.data for f in frames], min_frames=min_frames)
    checksum_part = checksum_confidence(match)
    text_part = text_evidence(data)
    parts = {
        "checksum": checksum_part,
        "repetition": repetition(frames, key_offsets),
        "length_consistency": length_consistency(frames),
        "byte_health": byte_health(data),
        "printable": printable_score(data),
    }
    structure = sum(parts[name] * weight for name, weight in STRUCTURE_WEIGHTS.items())
    evidence = max(checksum_part, text_part)
    parts["evidence"] = evidence
    total = structure * (EVIDENCE_FLOOR + (1.0 - EVIDENCE_FLOOR) * evidence)

    # Very short captures cannot support a confident verdict, whatever the
    # metrics say. Scale the answer down instead of pretending otherwise.
    if len(frames) < min_frames:
        total *= 0.5
        notes.append(
            f"Apenas {len(frames)} frame(s) capturado(s): confianca reduzida, "
            f"capture por mais tempo."
        )
    if match is not None:
        notes.append(
            f"Checksum {match.name} confere em {match.valid}/{match.tested} frames."
        )
    elif text_part >= 0.5:
        notes.append(
            f"Sem checksum, mas {printable_ratio(data):.0%} do fluxo e texto legivel."
        )
    else:
        notes.append(
            "Sem evidencia de conteudo valido (nenhum checksum confere e o "
            "fluxo nao e texto): confianca limitada."
        )
    if parts["byte_health"] < 0.5:
        notes.append("Excesso de 0x00/0xFF: sintoma classico de baud rate errado.")

    return StreamScore(
        total=max(0.0, min(1.0, total)),
        parts=parts,
        frames=len(frames),
        bytes_seen=len(data),
        checksum=match,
        notes=notes,
    )
