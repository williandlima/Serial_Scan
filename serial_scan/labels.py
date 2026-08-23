"""Persist the labels the operator attaches to each identified command.

Step 5 of the project. Labels are the part of the analysis that cannot be
recomputed: everything else in this package is derived from the capture, but
"this is the command that opens valve 3" only exists because a person typed
it. So it is stored separately, in a plain JSON file that survives regrouping,
reconfiguration and new capture sessions.

The file is keyed by protocol so that one project directory can hold the
labels of several buses without collisions.
"""

from __future__ import annotations

import json
import os
import tempfile
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

DEFAULT_FILENAME = "serial_scan_labels.json"
FORMAT_VERSION = 1


@dataclass
class LabelRecord:
    label: str = ""
    notes: str = ""
    #: Free-form extras (colour, category, ...) so the format can grow.
    extra: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        data: dict[str, Any] = {"label": self.label}
        if self.notes:
            data["notes"] = self.notes
        data.update(self.extra)
        return data

    @classmethod
    def from_dict(cls, data: Any) -> "LabelRecord":
        # Tolerate the simplest possible hand-written file: {"L8:01-03": "Leitura"}
        if isinstance(data, str):
            return cls(label=data)
        if not isinstance(data, dict):
            return cls()
        extra = {k: v for k, v in data.items() if k not in ("label", "notes")}
        return cls(
            label=str(data.get("label", "")),
            notes=str(data.get("notes", "")),
            extra=extra,
        )


class LabelStore:
    """A JSON-backed label dictionary, one section per protocol."""

    def __init__(self, path: str | Path | None = None) -> None:
        self.path = Path(path) if path else Path.cwd() / DEFAULT_FILENAME
        self.sections: dict[str, dict[str, LabelRecord]] = {}
        self._dirty = False

    # -- persistence ------------------------------------------------------

    def load(self) -> "LabelStore":
        if not self.path.exists():
            return self
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            raise ValueError(f"arquivo de rotulos invalido ({self.path}): {exc}") from exc
        sections = raw.get("protocols", raw) if isinstance(raw, dict) else {}
        for protocol, entries in sections.items():
            if not isinstance(entries, dict):
                continue
            self.sections[protocol] = {
                signature: LabelRecord.from_dict(value)
                for signature, value in entries.items()
            }
        self._dirty = False
        return self

    def save(self) -> None:
        """Write atomically: a half-written label file would lose real work."""
        payload = {
            "version": FORMAT_VERSION,
            "protocols": {
                protocol: {
                    signature: record.to_dict()
                    for signature, record in sorted(entries.items())
                    if record.label or record.notes or record.extra
                }
                for protocol, entries in sorted(self.sections.items())
            },
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        handle = tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=self.path.parent,
            prefix=self.path.name,
            suffix=".tmp",
            delete=False,
        )
        try:
            with handle:
                json.dump(payload, handle, indent=2, ensure_ascii=False)
                handle.write("\n")
            os.replace(handle.name, self.path)
        except BaseException:
            Path(handle.name).unlink(missing_ok=True)
            raise
        self._dirty = False

    @property
    def dirty(self) -> bool:
        return self._dirty

    # -- access -----------------------------------------------------------

    def section(self, protocol: str) -> dict[str, LabelRecord]:
        return self.sections.setdefault(str(protocol), {})

    def get(self, protocol: str, signature: str) -> LabelRecord | None:
        return self.section(protocol).get(signature)

    def set_label(self, protocol: str, signature: str, label: str) -> None:
        record = self.section(protocol).setdefault(signature, LabelRecord())
        record.label = label.strip()
        self._dirty = True

    def set_notes(self, protocol: str, signature: str, notes: str) -> None:
        record = self.section(protocol).setdefault(signature, LabelRecord())
        record.notes = notes.strip()
        self._dirty = True

    def remove(self, protocol: str, signature: str) -> None:
        if self.section(protocol).pop(signature, None) is not None:
            self._dirty = True

    def labels_for(self, protocol: str) -> dict[str, str]:
        return {
            signature: record.label
            for signature, record in self.section(protocol).items()
            if record.label
        }

    def notes_for(self, protocol: str) -> dict[str, str]:
        return {
            signature: record.notes
            for signature, record in self.section(protocol).items()
            if record.notes
        }

    def __len__(self) -> int:
        return sum(len(entries) for entries in self.sections.values())
