"""Unit tests for the signal-episode event study (event_study.py) and the
decisions ledger writer (signals.log_decision) -- synthetic rows only, no
engine, no network."""
from __future__ import annotations

import json

from smartboi.event_study import (
    attach_outcomes,
    collapse_episodes,
    episode_forward_return,
    format_event_study,
)
from smartboi.signals import log_decision


def _signal_row(symbol="UCTT", episode="2026-07-01T10:00:00+00:00", generated_at=None,
                direction="LONG", confidence=0.8, magnitude=0.9):
    return {
        "symbol": symbol, "direction": direction, "confidence": confidence,
        "magnitude": magnitude, "horizon_days": 20, "independent_source_count": 2,
        "thesis_summary": "t", "generated_at": generated_at or episode, "episode": episode,
    }


def test_collapse_episodes_groups_relogs_and_keeps_first_row():
    rows = [
        _signal_row(generated_at="2026-07-01T10:00:00+00:00", confidence=0.7),
        _signal_row(generated_at="2026-07-02T09:00:00+00:00", confidence=0.9),
        _signal_row(symbol="ICHR", episode="2026-07-03T08:00:00+00:00"),
    ]
    episodes = collapse_episodes(rows)
    assert len(episodes) == 2
    uctt = next(e for e in episodes if e["symbol"] == "UCTT")
    assert uctt["relog_count"] == 2
    # At-fire numbers, not the re-log's.
    assert uctt["confidence"] == 0.7
    assert uctt["fired_at"] == "2026-07-01T10:00:00+00:00"


def test_collapse_episodes_legacy_rows_without_episode_key():
    rows = [
        {**_signal_row(generated_at="2026-07-01T10:00:00+00:00"), "episode": ""},
        {**_signal_row(generated_at="2026-07-02T10:00:00+00:00"), "episode": ""},
    ]
    # Uncollapsible -- each pre-episode-key row stands alone rather than
    # being dropped or wrongly merged.
    assert len(collapse_episodes(rows)) == 2


def test_attach_outcomes_precedence_and_drift_flag():
    episodes = collapse_episodes([_signal_row()])
    decisions = [
        {"event": "drift_skip", "symbol": "UCTT", "episode": "2026-07-01T10:00:00+00:00",
         "price": 105.0, "reason": "drifted", "at": "2026-07-02T00:00:00+00:00"},
        {"event": "trade_opened", "symbol": "UCTT", "episode": "2026-07-01T10:00:00+00:00",
         "price": 101.0, "reason": "", "at": "2026-07-03T00:00:00+00:00"},
    ]
    (ep,) = attach_outcomes(episodes, decisions)
    # Opened wins over the earlier drift-skip, but the drift history stays visible.
    assert ep["outcome"] == "trade_opened"
    assert ep["decision_price"] == 101.0
    assert ep["drift_skipped"] is True


def test_attach_outcomes_untracked_when_no_ledger_rows():
    (ep,) = attach_outcomes(collapse_episodes([_signal_row()]), [])
    assert ep["outcome"] == "untracked"
    assert ep["decision_price"] is None


def test_episode_forward_return_signed_in_thesis_direction():
    (ep,) = attach_outcomes(collapse_episodes([_signal_row(direction="SHORT")]), [])
    marks = {"UCTT": {"2026-07-01": 100.0, "2026-07-06": 90.0}}
    r = episode_forward_return(ep, marks, horizon_days=5)
    assert r is not None
    # SHORT and the price fell 10% -> +10 signed (thesis right).
    assert round(r["signed_return_pct"], 2) == 10.0


def test_episode_forward_return_none_when_unjoinable():
    (ep,) = attach_outcomes(collapse_episodes([_signal_row()]), [])
    assert episode_forward_return(ep, {}, horizon_days=5) is None
    # Entry exists but nothing at/after the exit date within the lookahead.
    marks = {"UCTT": {"2026-07-01": 100.0}}
    assert episode_forward_return(ep, marks, horizon_days=5) is None


def test_format_event_study_smoke_and_guard_verdict():
    signals = [
        _signal_row(symbol="OPEN1", episode="2026-07-01T00:00:00+00:00"),
        _signal_row(symbol="SKIP1", episode="2026-07-01T00:00:00+00:00"),
    ]
    decisions = [
        {"event": "trade_opened", "symbol": "OPEN1", "episode": "2026-07-01T00:00:00+00:00",
         "price": 100.0, "reason": "", "at": "2026-07-01T01:00:00+00:00"},
        {"event": "drift_skip", "symbol": "SKIP1", "episode": "2026-07-01T00:00:00+00:00",
         "price": 106.0, "reason": "drifted 6%", "at": "2026-07-01T01:00:00+00:00"},
    ]
    marks = {
        "OPEN1": {"2026-07-01": 100.0, "2026-07-06": 102.0},
        "SKIP1": {"2026-07-01": 100.0, "2026-07-06": 110.0},
    }
    report = format_event_study(signals, decisions, marks, horizons=(5,))
    assert "2 episode(s)" in report
    assert "opened" in report
    assert "drift-blocked" in report
    # The skipped one kept running (+10 vs +2) -> the guard-cost warning fires.
    assert "KEPT GOING" in report


def test_format_event_study_empty_inputs():
    assert "No signals logged" in format_event_study([], [], {}, horizons=(5,))


def test_log_decision_appends_joinable_rows(tmp_path):
    path = tmp_path / "decisions.jsonl"
    log_decision(path, "drift_skip", "UCTT", "LONG", "2026-07-01T10:00:00+00:00",
                 price=105.0, reason="drifted 6.0% favorably from 99.00")
    log_decision(path, "signal_expired", "UCTT", "LONG", "2026-07-01T10:00:00+00:00")
    rows = [json.loads(line) for line in path.read_text().splitlines()]
    assert [r["event"] for r in rows] == ["drift_skip", "signal_expired"]
    assert rows[0]["price"] == 105.0
    assert rows[1]["price"] is None
    assert all(r["episode"] == "2026-07-01T10:00:00+00:00" for r in rows)
    assert all(r["at"] for r in rows)
