from smartboi.state import JsonState


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
