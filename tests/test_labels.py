"""Persistence of the operator's labels (step 5)."""

from __future__ import annotations

import json

import pytest

from serial_scan.labels import LabelRecord, LabelStore


@pytest.fixture
def store(tmp_path) -> LabelStore:
    return LabelStore(tmp_path / "rotulos.json")


def test_set_and_read_back(store: LabelStore) -> None:
    store.set_label("RS485", "L8:01-03", "  Leitura de temperatura ")
    store.set_notes("RS485", "L8:01-03", "sensor da caldeira")

    record = store.get("RS485", "L8:01-03")
    assert record is not None
    assert record.label == "Leitura de temperatura"
    assert record.notes == "sensor da caldeira"
    assert store.dirty


def test_survives_a_save_and_load_cycle(tmp_path) -> None:
    path = tmp_path / "rotulos.json"
    first = LabelStore(path)
    first.set_label("RS485", "L8:01-03", "Leitura")
    first.set_label("RS232", "L4:AA", "Ping")
    first.save()
    assert not first.dirty

    second = LabelStore(path).load()
    assert second.labels_for("RS485") == {"L8:01-03": "Leitura"}
    assert second.labels_for("RS232") == {"L4:AA": "Ping"}


def test_protocols_are_kept_apart(store: LabelStore) -> None:
    store.set_label("RS485", "L8:01-03", "Modbus")
    store.set_label("RS232", "L8:01-03", "Balanca")
    assert store.get("RS485", "L8:01-03").label == "Modbus"
    assert store.get("RS232", "L8:01-03").label == "Balanca"
    assert len(store) == 2


def test_empty_records_are_not_written(tmp_path) -> None:
    path = tmp_path / "rotulos.json"
    store = LabelStore(path)
    store.set_label("RS485", "L8:01-03", "Leitura")
    store.set_label("RS485", "L8:02-03", "")
    store.save()

    written = json.loads(path.read_text(encoding="utf-8"))
    assert list(written["protocols"]["RS485"]) == ["L8:01-03"]


def test_remove(store: LabelStore) -> None:
    store.set_label("RS485", "L8:01-03", "Leitura")
    store.remove("RS485", "L8:01-03")
    assert store.get("RS485", "L8:01-03") is None
    # Removing something that is not there is a no-op, not an error.
    store.remove("RS485", "nope")


def test_missing_file_loads_as_empty(tmp_path) -> None:
    store = LabelStore(tmp_path / "nao-existe.json").load()
    assert len(store) == 0


def test_corrupt_file_is_reported_clearly(tmp_path) -> None:
    path = tmp_path / "rotulos.json"
    path.write_text("{isto nao e json", encoding="utf-8")
    with pytest.raises(ValueError, match="invalido"):
        LabelStore(path).load()


def test_accepts_a_hand_written_shorthand(tmp_path) -> None:
    """A human editing the file by hand should not need the nested form."""
    path = tmp_path / "rotulos.json"
    path.write_text(
        json.dumps({"protocols": {"RS485": {"L8:01-03": "Leitura"}}}), encoding="utf-8"
    )
    store = LabelStore(path).load()
    assert store.get("RS485", "L8:01-03").label == "Leitura"


def test_unknown_keys_are_preserved(tmp_path) -> None:
    path = tmp_path / "rotulos.json"
    path.write_text(
        json.dumps(
            {"protocols": {"RS485": {"L8:01-03": {"label": "Leitura", "cor": "verde"}}}}
        ),
        encoding="utf-8",
    )
    store = LabelStore(path).load()
    assert store.get("RS485", "L8:01-03").extra == {"cor": "verde"}

    store.save()
    reread = json.loads(path.read_text(encoding="utf-8"))
    assert reread["protocols"]["RS485"]["L8:01-03"]["cor"] == "verde"


def test_save_creates_the_parent_directory(tmp_path) -> None:
    path = tmp_path / "sub" / "dir" / "rotulos.json"
    store = LabelStore(path)
    store.set_label("RS485", "L8:01-03", "Leitura")
    store.save()
    assert path.exists()


def test_save_leaves_no_temporary_files(tmp_path) -> None:
    path = tmp_path / "rotulos.json"
    store = LabelStore(path)
    store.set_label("RS485", "L8:01-03", "Leitura")
    store.save()
    store.save()
    assert [p.name for p in tmp_path.iterdir()] == ["rotulos.json"]


def test_label_record_from_various_shapes() -> None:
    assert LabelRecord.from_dict("texto").label == "texto"
    assert LabelRecord.from_dict({"label": "x", "notes": "y"}).notes == "y"
    assert LabelRecord.from_dict(None).label == ""
    assert LabelRecord.from_dict(42).label == ""


def test_accents_are_kept_readable(tmp_path) -> None:
    path = tmp_path / "rotulos.json"
    store = LabelStore(path)
    store.set_label("RS485", "L8:01-03", "Leitura de pressão")
    store.save()
    assert "pressão" in path.read_text(encoding="utf-8")
    assert LabelStore(path).load().get("RS485", "L8:01-03").label == "Leitura de pressão"
