"""Corrupt-file and durability paths.

None of this was covered before. Every store had a hand-rolled loader with
an `except` clause that turned an unreadable file into an empty object, and
no test ever wrote a bad file to see what happened -- so the behaviour that
mattered most on the worst day was the only behaviour never exercised.

The central assertion in most of these is not "it did not crash". It is
that THE BYTES SURVIVE: a store that silently starts empty and then
overwrites the unreadable file on its next save has destroyed the data, and
that is indistinguishable from working correctly until someone looks for
the evidence months later.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

from smartboi import persist
from smartboi.dedup import DedupIndex
from smartboi.dossier import Dossier, DossierStore, EvidenceRecord
from smartboi.graph import RelationshipGraph, Relationship
from smartboi.paper_journal import PaperTradeJournal
from smartboi.state import JsonState


@pytest.fixture(autouse=True)
def _clear_quarantine_log():
    persist.quarantine_events.clear()
    yield
    persist.quarantine_events.clear()


def _corrupt_siblings(path: Path) -> list[Path]:
    return sorted(path.parent.glob(f"{path.name}.corrupt-*"))


# --- atomic_write_json ------------------------------------------------

def test_atomic_write_round_trips_and_leaves_no_temp_file(tmp_path):
    target = tmp_path / "nested" / "state.json"
    persist.atomic_write_json(target, {"a": 1})
    assert json.loads(target.read_text()) == {"a": 1}
    assert list(tmp_path.rglob("*.tmp")) == []


def test_atomic_write_temp_name_cannot_masquerade_as_a_dossier(tmp_path):
    """DossierStore.all_symbols globs *.json. The temp file must never
    match it, or a crash mid-write would leave a half-written file that
    the next decay pass loads as a real symbol."""
    target = tmp_path / "AAPL.json"
    persist.atomic_write_json(target, {"symbol": "AAPL"})
    # with_suffix('.tmp') would have produced AAPL.tmp; with_name produces
    # AAPL.json.tmp. Neither ends in .json, which is the property we need.
    assert not any(p.name.endswith(".json") and p.name != "AAPL.json" for p in tmp_path.iterdir())


def test_atomic_write_preserves_the_old_file_when_serialisation_fails(tmp_path):
    target = tmp_path / "state.json"
    persist.atomic_write_json(target, {"good": 1})
    with pytest.raises(TypeError):
        persist.atomic_write_json(target, {"bad": object()})
    assert json.loads(target.read_text()) == {"good": 1}


# --- read_json --------------------------------------------------------

def test_missing_file_is_not_an_error_and_records_nothing(tmp_path):
    assert persist.read_json(tmp_path / "nope.json") is None
    assert persist.quarantine_events == []


def test_invalid_json_is_quarantined_with_the_bytes_intact(tmp_path):
    path = tmp_path / "graph.json"
    path.write_text('[{"from_symbol": "AAP')  # truncated mid-write
    assert persist.read_json(path, expect=list) is None
    assert not path.exists(), "the unreadable file must be moved out of the way"
    (preserved,) = _corrupt_siblings(path)
    assert preserved.read_text() == '[{"from_symbol": "AAP'
    assert len(persist.quarantine_events) == 1
    assert persist.quarantine_events[0].bytes_preserved == 21


def test_wrong_top_level_type_is_corruption_not_a_crash(tmp_path):
    """A dict where a list belongs used to raise AttributeError out of a
    constructor, past main's KeyboardInterrupt-only handler, killing the
    process -- and with boot: manual it stayed dead."""
    path = tmp_path / "state.json"
    path.write_text('["not", "a", "dict"]')
    assert persist.read_json(path, expect=dict) is None
    assert len(_corrupt_siblings(path)) == 1


def test_invalid_utf8_is_quarantined_rather_than_raised(tmp_path):
    """UnicodeDecodeError is a ValueError but NOT a JSONDecodeError, so
    every original loader let it escape. Every persisted byte is ASCII
    today only because json.dumps defaults to ensure_ascii=True."""
    path = tmp_path / "dedup.json"
    path.write_bytes(b'{"fp": "\xff\xfe not utf-8"}')
    assert persist.read_json(path, expect=dict) is None
    assert len(_corrupt_siblings(path)) == 1


def test_an_io_error_never_quarantines(tmp_path):
    """A full disk or a briefly locked file says nothing about the
    CONTENT. Moving the file on an I/O error would turn a transient
    problem into permanent damage."""
    path = tmp_path / "state.json"
    path.write_text('{"a": 1}')
    path.chmod(0o000)
    try:
        if persist.read_json(path) is None:  # skip if running as root, where chmod is advisory
            assert path.exists()
            assert persist.quarantine_events == []
    finally:
        path.chmod(0o644)


def test_repeated_corruption_never_overwrites_an_earlier_quarantine(tmp_path):
    """A crash loop can hit the same bad file several times within one
    second, and the timestamp only has second resolution. Quarantining on
    top of a quarantine is the same data loss, just slower."""
    path = tmp_path / "state.json"
    for marker in ("first", "second", "third"):
        path.write_text("{" + marker)
        assert persist.read_json(path) is None

    preserved = sorted(p.read_text() for p in _corrupt_siblings(path))
    assert preserved == ["{first", "{second", "{third"]
    assert len(persist.quarantine_events) == 3


# --- the stores, end to end -------------------------------------------

def test_corrupt_state_file_starts_empty_and_keeps_the_bytes(tmp_path):
    path = tmp_path / "periodic.json"
    path.write_text("{truncated")
    state = JsonState(path)
    assert state.data == {}
    state.set("k", "v")  # the write that used to destroy the evidence
    assert json.loads(path.read_text()) == {"k": "v"}
    assert len(_corrupt_siblings(path)) == 1


def test_state_file_holding_a_list_does_not_crash_on_get(tmp_path):
    path = tmp_path / "periodic.json"
    path.write_text("[1, 2, 3]")
    assert JsonState(path).get("anything") is None


def test_corrupt_graph_keeps_the_edges_for_recovery(tmp_path):
    path = tmp_path / "graph.json"
    good = [Relationship("FORM", "INTC", "customer", "d", "s", 0.9)]
    RelationshipGraph(path=path, relationships=list(good))._save()
    original = path.read_text()
    path.write_text(original[: len(original) // 2])  # truncate

    graph = RelationshipGraph(path=path)
    assert graph.relationships == []
    (preserved,) = _corrupt_siblings(path)
    assert preserved.read_text() == original[: len(original) // 2]


def test_graph_rows_that_are_not_relationships_are_quarantined(tmp_path):
    path = tmp_path / "graph.json"
    path.write_text('[{"nonsense": true}]')
    assert RelationshipGraph(path=path).relationships == []
    assert len(_corrupt_siblings(path)) == 1


def test_corrupt_dossier_does_not_take_down_the_other_symbols(tmp_path):
    store = DossierStore(dir_path=tmp_path)
    store.save(Dossier(symbol="GOOD", evidence=[EvidenceRecord(
        evidence_id="e1", source_type="news", source_name="s", url="u",
        headline="h", published_at="2026-01-01T00:00:00+00:00",
        origin_symbol="GOOD", is_propagated=False, relationship_note="",
        direction="LONG", magnitude=0.5, confidence=0.5, horizon_days=5,
        reasoning="r", skeptic_note="",
    )]))
    (tmp_path / "BAD.json").write_text("{not json")

    assert store.load("BAD").evidence == []          # empty, not an exception
    assert len(store.load("GOOD").evidence) == 1     # unaffected
    assert len(_corrupt_siblings(tmp_path / "BAD.json")) == 1


def test_dossier_with_unknown_fields_is_quarantined_not_wiped(tmp_path):
    """A rollback to a version predating a field used to be a silent
    total loss for that symbol."""
    store = DossierStore(dir_path=tmp_path)
    (tmp_path / "X.json").write_text('{"symbol": "X", "field_from_the_future": 1}')
    assert store.load("X").symbol == "X"
    assert len(_corrupt_siblings(tmp_path / "X.json")) == 1


def test_corrupt_dedup_index_does_not_crash_and_keeps_bytes(tmp_path):
    path = tmp_path / "dedup.json"
    path.write_text('["a list, not a dict"]')
    index = DedupIndex(path=path)
    assert not index.is_duplicate("anything")
    assert len(_corrupt_siblings(path)) == 1


def test_corrupt_open_trades_preserves_the_in_flight_positions(tmp_path):
    """The highest-value state in the tree: losing it drops open trades out
    of the record AND lets the same symbol re-enter as a duplicate."""
    path = tmp_path / "open_paper_trades.json"
    path.write_text('{"FORM": {"unexpected_field": 1}}')
    journal = PaperTradeJournal(log_path=tmp_path / "paper_trades.jsonl")
    assert journal.open_trades == {}
    assert len(_corrupt_siblings(path)) == 1
