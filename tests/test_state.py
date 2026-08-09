import json

from smartboi.state import JsonState, atomic_write_json, quarantine_corrupt_file


def test_set_then_get_round_trips(tmp_path):
    state = JsonState(tmp_path / "state.json")
    state.set("AAPL", {"seen_count": 3})
    assert state.get("AAPL") == {"seen_count": 3}


def test_get_missing_key_returns_default(tmp_path):
    state = JsonState(tmp_path / "state.json")
    assert state.get("MISSING") is None
    assert state.get("MISSING", "fallback") == "fallback"


def test_delete_removes_the_key(tmp_path):
    state = JsonState(tmp_path / "state.json")
    state.set("AAPL", {"seen_count": 3})
    state.delete("AAPL")
    assert state.get("AAPL") is None


def test_delete_missing_key_is_a_noop(tmp_path):
    state = JsonState(tmp_path / "state.json")
    state.delete("MISSING")  # must not raise
    assert state.data == {}


def test_state_survives_reload_after_delete(tmp_path):
    path = tmp_path / "state.json"
    state = JsonState(path)
    state.set("AAPL", 1)
    state.set("MSFT", 2)
    state.delete("AAPL")
    reloaded = JsonState(path)
    assert reloaded.data == {"MSFT": 2}


def test_update_sets_several_keys_at_once(tmp_path):
    path = tmp_path / "state.json"
    state = JsonState(path)
    state.set("KEEP", 1)
    state.update({"A": 2, "B": 3})
    assert JsonState(path).data == {"KEEP": 1, "A": 2, "B": 3}


def test_a_corrupt_state_file_is_quarantined_not_silently_wiped(tmp_path):
    """A corrupt file must be renamed aside (recoverable) and NOT overwritten
    by the next save -- a silent wipe of e.g. periodic_pass_state re-fires
    every daily pass, and of accepted_candidates reverts the whole universe."""
    path = tmp_path / "state.json"
    path.write_text("{ this is not valid json")

    state = JsonState(path)  # must not raise

    assert state.data == {}  # started fresh
    quarantined = list(tmp_path.glob("state.json.corrupt-*"))
    assert len(quarantined) == 1
    assert quarantined[0].read_text() == "{ this is not valid json"  # original preserved
    # And the next save writes a clean file rather than clobbering the original.
    state.set("A", 1)
    assert JsonState(path).data == {"A": 1}


def test_quarantine_is_a_noop_when_the_file_is_gone(tmp_path):
    """Best-effort recovery must never raise on top of the corruption it is
    handling (e.g. the file was already removed under it)."""
    quarantine_corrupt_file(tmp_path / "does_not_exist.json", ValueError("boom"))  # must not raise


def test_atomic_write_json_round_trips(tmp_path):
    path = tmp_path / "nested" / "out.json"
    atomic_write_json(path, {"x": [1, 2, 3]}, indent=2)
    assert json.loads(path.read_text()) == {"x": [1, 2, 3]}
    assert not path.with_suffix(".tmp").exists()  # tmp renamed away, not left behind
