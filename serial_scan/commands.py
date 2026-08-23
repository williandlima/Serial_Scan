"""Identify, segregate and describe the commands seen on the bus.

Steps 4 and 5 of the project. A "command" here is a class of frames that share
the same identifying bytes: on a Modbus-style bus that is the slave address
plus the function code, on a simple point-to-point link it may be a single
opcode byte.

Two things make this more than a dictionary keyed on a byte prefix:

* **Which offsets identify a command is itself discovered.** The catalog can
  look at the traffic and propose the key offsets, then regroup everything
  already captured without losing the labels the operator typed.
* **Each command gets a field map.** Once several instances of the same
  command have been seen, comparing them byte by byte shows which positions
  never change (header, constants), which cycle through a small set (status,
  flags), which count up (sequence numbers) and which are free payload. That
  is what turns a hex dump into something an engineer can annotate.
"""

from __future__ import annotations

from collections import Counter, deque
from dataclasses import dataclass, field
from enum import Enum
from typing import Iterable, Sequence

from .framing import Direction, Frame


class FieldKind(str, Enum):
    CONSTANT = "constante"
    ENUM = "enumerado"
    COUNTER = "contador"
    VARIABLE = "variavel"
    CHECKSUM = "checksum"
    KEY = "chave"


@dataclass
class FieldStats:
    """What one byte offset looks like across every instance of a command."""

    offset: int
    kind: FieldKind
    values: Counter
    samples: int

    @property
    def distinct(self) -> int:
        return len(self.values)

    @property
    def constant_value(self) -> int | None:
        if len(self.values) == 1:
            return next(iter(self.values))
        return None

    def describe(self) -> str:
        if self.kind is FieldKind.CONSTANT:
            return f"0x{self.constant_value:02X}"
        if self.kind is FieldKind.KEY:
            return f"0x{self.constant_value:02X} (chave)" if self.constant_value is not None else "chave"
        if self.kind is FieldKind.CHECKSUM:
            return "checksum"
        if self.kind is FieldKind.ENUM:
            shown = ", ".join(f"0x{v:02X}" for v, _ in self.values.most_common(6))
            return f"{{{shown}}}"
        if self.kind is FieldKind.COUNTER:
            return "contador (+1)"
        lo = min(self.values)
        hi = max(self.values)
        return f"0x{lo:02X}..0x{hi:02X} ({self.distinct} valores)"


def signature_for(data: bytes, key_offsets: Sequence[int], use_length: bool) -> str:
    """Build the stable identifier of a frame's command class.

    Length is part of the identity by default: on most protocols a request and
    its response share the address and function code but differ in size, and
    keeping them apart is exactly the segregation the operator wants.
    """
    key = [f"{data[offset]:02X}" for offset in key_offsets if offset < len(data)]
    body = "-".join(key) if key else "??"
    return f"L{len(data)}:{body}" if use_length else body


@dataclass
class CommandEntry:
    """One identified command, with its statistics and the operator's label."""

    signature: str
    key_offsets: tuple[int, ...]
    key_bytes: bytes
    length: int
    first_seen: float
    last_seen: float
    count: int = 0
    label: str = ""
    notes: str = ""
    directions: Counter = field(default_factory=Counter)
    checksum_ok: int = 0
    checksum_bad: int = 0
    #: Bounded history, used for the field map and for the detail view.
    samples: deque[bytes] = field(default_factory=lambda: deque(maxlen=64))
    #: Set for the frame that first revealed this command.
    first_frame_index: int = 0

    @property
    def display_name(self) -> str:
        return self.label or self.signature

    @property
    def key_hex(self) -> str:
        return " ".join(f"{b:02X}" for b in self.key_bytes)

    @property
    def is_labelled(self) -> bool:
        return bool(self.label.strip())

    @property
    def checksum_ratio(self) -> float | None:
        total = self.checksum_ok + self.checksum_bad
        return self.checksum_ok / total if total else None

    @property
    def direction(self) -> Direction:
        if not self.directions:
            return Direction.UNKNOWN
        return self.directions.most_common(1)[0][0]

    def observe(self, frame: Frame) -> None:
        self.count += 1
        self.last_seen = frame.time_end
        self.samples.append(frame.data)
        self.directions[frame.direction] += 1
        if frame.checksum_ok is True:
            self.checksum_ok += 1
        elif frame.checksum_ok is False:
            self.checksum_bad += 1

    def rate_per_minute(self, window: float) -> float:
        return self.count / window * 60.0 if window > 0 else 0.0

    def field_map(self, checksum_width: int = 0) -> list[FieldStats]:
        """Classify every byte offset from the samples collected so far.

        With a single sample nothing can be classified: every byte would look
        constant. The map therefore only becomes meaningful from the second
        instance onwards, and says so by reporting VARIABLE for nothing.
        """
        if not self.samples:
            return []
        width = min(len(s) for s in self.samples)
        checksum_from = width - checksum_width if checksum_width else width
        stats: list[FieldStats] = []
        for offset in range(width):
            values = Counter(sample[offset] for sample in self.samples)
            kind = _classify(
                offset=offset,
                values=values,
                samples=len(self.samples),
                key_offsets=self.key_offsets,
                checksum_from=checksum_from,
                sequence=[sample[offset] for sample in self.samples],
            )
            stats.append(
                FieldStats(offset=offset, kind=kind, values=values, samples=len(self.samples))
            )
        return stats

    def to_dict(self) -> dict:
        return {
            "signature": self.signature,
            "label": self.label,
            "notes": self.notes,
            "count": self.count,
            "length": self.length,
            "key": self.key_hex,
            "direction": self.direction.value,
            "first_seen": self.first_seen,
            "last_seen": self.last_seen,
            "checksum_ok": self.checksum_ok,
            "checksum_bad": self.checksum_bad,
            "last_sample": self.samples[-1].hex(" ").upper() if self.samples else "",
        }


def _classify(
    offset: int,
    values: Counter,
    samples: int,
    key_offsets: Sequence[int],
    checksum_from: int,
    sequence: Sequence[int],
) -> FieldKind:
    if offset >= checksum_from:
        return FieldKind.CHECKSUM
    if offset in key_offsets:
        return FieldKind.KEY
    distinct = len(values)
    if distinct == 1:
        return FieldKind.CONSTANT
    if samples >= 4 and _looks_like_counter(sequence):
        return FieldKind.COUNTER
    if distinct <= 6 and distinct <= max(2, samples * 0.4):
        return FieldKind.ENUM
    return FieldKind.VARIABLE


def _looks_like_counter(sequence: Sequence[int], tolerance: float = 0.7) -> bool:
    """True when consecutive samples mostly increase by one, modulo 256."""
    if len(sequence) < 4:
        return False
    steps = [
        (following - previous) % 256
        for previous, following in zip(sequence, sequence[1:])
    ]
    return sum(1 for step in steps if step == 1) / len(steps) >= tolerance


# ---------------------------------------------------------------------------
# The catalog
# ---------------------------------------------------------------------------


@dataclass
class Observation:
    """Result of showing one frame to the catalog."""

    entry: CommandEntry
    is_new: bool


class CommandCatalog:
    """Segregates frames into commands and keeps the operator's labels."""

    def __init__(
        self,
        key_offsets: Sequence[int] = (0, 1),
        use_length: bool = True,
        max_samples: int = 64,
        history: int = 4000,
    ) -> None:
        self.key_offsets = tuple(key_offsets)
        self.use_length = use_length
        self.max_samples = max_samples
        self.entries: dict[str, CommandEntry] = {}
        #: Labels survive regrouping and reloading, so they live apart from
        #: the entries themselves.
        self.labels: dict[str, str] = {}
        self.notes: dict[str, str] = {}
        #: Retained frames, so the catalog can be rebuilt under new key
        #: offsets without re-capturing.
        self._history: deque[Frame] = deque(maxlen=history)
        self.checksum_width = 0

    # -- ingestion --------------------------------------------------------

    def observe(self, frame: Frame) -> Observation:
        """Classify one frame, creating the command entry if it is new."""
        signature = signature_for(frame.data, self.key_offsets, self.use_length)
        frame.signature = signature
        self._history.append(frame)

        entry = self.entries.get(signature)
        is_new = entry is None
        if entry is None:
            key_bytes = bytes(
                frame.data[offset]
                for offset in self.key_offsets
                if offset < len(frame.data)
            )
            entry = CommandEntry(
                signature=signature,
                key_offsets=self.key_offsets,
                key_bytes=key_bytes,
                length=len(frame.data),
                first_seen=frame.time_start,
                last_seen=frame.time_end,
                label=self.labels.get(signature, ""),
                notes=self.notes.get(signature, ""),
                first_frame_index=frame.index,
            )
            entry.samples = deque(maxlen=self.max_samples)
            self.entries[signature] = entry
        entry.observe(frame)
        return Observation(entry=entry, is_new=is_new)

    def observe_many(self, frames: Iterable[Frame]) -> list[Observation]:
        return [self.observe(frame) for frame in frames]

    # -- labelling (step 5) ----------------------------------------------

    def set_label(self, signature: str, label: str) -> None:
        label = label.strip()
        self.labels[signature] = label
        if signature in self.entries:
            self.entries[signature].label = label

    def set_notes(self, signature: str, notes: str) -> None:
        self.notes[signature] = notes
        if signature in self.entries:
            self.entries[signature].notes = notes

    def apply_labels(self, labels: dict[str, str], notes: dict[str, str] | None = None) -> None:
        """Bulk-apply labels loaded from disk."""
        for signature, label in labels.items():
            self.set_label(signature, label)
        for signature, text in (notes or {}).items():
            self.set_notes(signature, text)

    # -- views ------------------------------------------------------------

    @property
    def total_frames(self) -> int:
        return sum(entry.count for entry in self.entries.values())

    def sorted_entries(self, by: str = "count") -> list[CommandEntry]:
        entries = list(self.entries.values())
        if by == "count":
            return sorted(entries, key=lambda e: (-e.count, e.signature))
        if by == "recent":
            return sorted(entries, key=lambda e: -e.last_seen)
        if by == "first":
            return sorted(entries, key=lambda e: e.first_seen)
        return sorted(entries, key=lambda e: e.signature)

    @property
    def unlabelled(self) -> list[CommandEntry]:
        return [entry for entry in self.entries.values() if not entry.is_labelled]

    def history(self) -> list[Frame]:
        return list(self._history)

    # -- key offset discovery --------------------------------------------

    def suggest_key_offsets(
        self, max_offset: int = 4, max_distinct: int = 16, max_keys: int = 2
    ) -> tuple[int, ...]:
        """Propose which byte offsets actually identify a command.

        The signal is diversity. A command identifier takes a handful of
        distinct values across the whole capture: one value means a constant
        header byte (it identifies nothing), while dozens of values mean
        payload. Offsets landing in between are the addresses and function
        codes.

        The ceiling on "a handful" is relative to how much traffic was seen:
        twelve distinct values over twelve frames is a sequence number, not a
        function code, and keying on it would give one command per frame.

        Only the first ``max_keys`` such offsets are kept. Beyond that the
        catalog starts splitting one command into a group per parameter value,
        which is the opposite of the segregation being asked for.
        """
        frames = [f.data for f in self._history if f.data]
        if len(frames) < 4:
            return self.key_offsets
        width = min(max_offset, max(len(f) for f in frames))
        ceiling = min(max_distinct, max(2, int(len(frames) * 0.25)))
        chosen: list[int] = []
        for offset in range(width):
            values = {f[offset] for f in frames if offset < len(f)}
            if 2 <= len(values) <= ceiling:
                chosen.append(offset)
        if not chosen:
            # Everything in the header is constant: the traffic is a single
            # command, or the identifier sits further in. Offset 0 is the
            # least surprising answer.
            return (0,)
        return tuple(chosen[:max_keys])

    def regroup(self, key_offsets: Sequence[int], use_length: bool | None = None) -> None:
        """Rebuild the whole catalog under new key offsets.

        Labels are keyed on the signature, so a regroup that produces the same
        signature keeps its label; the others stay in :attr:`labels` and are
        reattached if that signature ever comes back.
        """
        self.key_offsets = tuple(key_offsets)
        if use_length is not None:
            self.use_length = use_length
        retained = list(self._history)
        self.entries.clear()
        self._history.clear()
        for frame in retained:
            self.observe(frame)

    def autotune(self) -> bool:
        """Adopt the suggested key offsets if they differ. Returns True if so."""
        suggested = self.suggest_key_offsets()
        if suggested and suggested != self.key_offsets:
            self.regroup(suggested)
            return True
        return False

    # -- export -----------------------------------------------------------

    def to_dict(self) -> dict:
        return {
            "key_offsets": list(self.key_offsets),
            "use_length": self.use_length,
            "commands": [entry.to_dict() for entry in self.sorted_entries()],
        }
