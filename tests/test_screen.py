"""The screening CLI lives in the package (smartboi.screen) rather than only
in scripts/ so it ships with the Home Assistant add-on, which installs the
package from git and never copies scripts/. These cover the two bits of
add-on-specific plumbing that make it usable there: finding the Finnhub key
without the engine's environment, and finding a deployed instance's
candidates file from a `docker exec` working directory."""
from __future__ import annotations

import json

from smartboi import screen


def test_candidates_from_file_skips_tickerless_entries(tmp_path):
    path = tmp_path / "universe_candidates.json"
    path.write_text(json.dumps({
        "AAA": {"ticker": "AAA", "related_to": ["DCO"]},
        "SOME PRIVATE CO": {"ticker": "", "related_to": ["DCO"]},
    }))
    assert screen.candidates_from_file(path) == [("AAA", ["DCO"])]


def test_candidates_from_file_tolerates_missing_or_malformed(tmp_path):
    assert screen.candidates_from_file(tmp_path / "nope.json") == []
    bad = tmp_path / "bad.json"
    bad.write_text("{not json")
    assert screen.candidates_from_file(bad) == []


def test_finnhub_key_falls_back_to_addon_options(tmp_path, monkeypatch):
    """A `docker exec` process inside the add-on doesn't inherit the engine
    process's environment (the supervisor's options are loaded into env by
    addon_entrypoint, not baked into the image), so the key has to be
    readable from options.json directly."""
    options = tmp_path / "options.json"
    options.write_text(json.dumps({"finnhub_api_key": "from-addon-options"}))
    monkeypatch.setattr(screen, "_ADDON_OPTIONS_PATH", options)
    assert screen._finnhub_key_from_addon_options() == "from-addon-options"


def test_finnhub_key_fallback_is_empty_when_not_in_an_addon(tmp_path, monkeypatch):
    monkeypatch.setattr(screen, "_ADDON_OPTIONS_PATH", tmp_path / "absent.json")
    assert screen._finnhub_key_from_addon_options() == ""


def test_candidates_path_prefers_explicit_then_relative_then_addon(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(screen, "_ADDON_RUN_DIR", tmp_path / "addon_run")

    assert screen.resolve_candidates_path("explicit.json") == __import__("pathlib").Path("explicit.json")

    # Nothing on disk -> falls through to the add-on run directory, so a
    # `docker exec` starting in /app still finds a deployment's candidates.
    assert screen.resolve_candidates_path(None) == tmp_path / "addon_run" / "data" / "universe_candidates.json"

    # A local checkout's data/ wins once it actually exists.
    local = tmp_path / "data"
    local.mkdir()
    (local / "universe_candidates.json").write_text("{}")
    assert str(screen.resolve_candidates_path(None)) == "data/universe_candidates.json"
