"""Corporate-action detection: the split that fabricates a maximal win.

Nothing in this system adjusts for splits, and with -50%/+100% bands any
split of 2:1 or more carries the price outside the band on the first print
after the ex-date. A 1-for-10 reverse split therefore books a maximal WIN
on a position that did not move, and sub-$1 compliance reverse splits are
routine in exactly this universe.

The hard part is not detection, it is not over-detecting: a small cap
doubling on a contract award is the event this whole system exists to
catch, and voiding those would trade one bias for another.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from smartboi.corporate_actions import classify_price_jump
from smartboi.forward_returns import (
    compute_forward_return, price_marks_by_symbol, window_has_corporate_action,
)
from smartboi.paper_journal import PaperTradeJournal


# --- the classifier ---

@pytest.mark.parametrize("ref,cur,label", [
    (2.00, 20.00, "1-for-10 reverse split"),
    (2.00, 18.80, "1-for-10 reverse split"),   # on a -6% day
    (5.00, 10.00, "1-for-2 reverse split"),
    (40.0, 20.00, "2-for-1 forward split"),
    (0.50, 10.00, "1-for-20 reverse split"),
    (30.0, 10.00, "3-for-1 forward split"),
])
def test_split_ratios_are_recognised(ref, cur, label):
    jump = classify_price_jump(ref, cur)
    assert jump is not None and jump.is_split_like
    assert jump.likely_action == label


@pytest.mark.parametrize("ref,cur", [
    (10.0, 10.90),   # +9%, ordinary
    (10.0, 7.00),    # -30%, bad news
    (10.0, 18.70),   # +87%, below the floor
    (10.0, 5.20),    # -48%, just inside the floor
])
def test_ordinary_moves_are_not_flagged_at_all(ref, cur):
    assert classify_price_jump(ref, cur) is None


@pytest.mark.parametrize("ref,cur", [
    (10.0, 23.10),   # +131% buyout pop -- big, but not near a split ratio
    (10.0, 4.50),    # -55% collapse
])
def test_large_arbitrary_moves_are_real_returns_not_splits(ref, cur):
    """The bias that matters. A thinly-covered small cap doubling on news is
    the event the strategy is built to catch; calling it a split would
    remove exactly the winners the record needs."""
    jump = classify_price_jump(ref, cur)
    assert jump is not None
    assert not jump.is_split_like


def test_absurd_moves_are_actions_whatever_the_ratio():
    """Past 4x there is no competing explanation, so an odd ratio (a stale
    reference, a 1-for-8 on a heavy day) must not slip through as a
    'real return'."""
    jump = classify_price_jump(1.00, 37.30)
    assert jump is not None and jump.is_split_like


def test_missing_or_nonsense_prices_are_never_a_jump():
    for ref, cur in [(None, 10.0), (10.0, None), (0.0, 10.0), (10.0, 0.0), (-1.0, 10.0)]:
        assert classify_price_jump(ref, cur) is None


# --- the trade path ---

def _open_trade(tmp_path, monkeypatch):
    import smartboi.paper_journal as pj
    monkeypatch.setattr(pj, "is_regular_trading_hours", lambda now=None: True)
    journal = PaperTradeJournal(log_path=tmp_path / "paper_trades.jsonl")
    opened = datetime(2026, 8, 3, 14, 0, tzinfo=timezone.utc)
    journal.open(symbol="NVX", direction="LONG", entry_price=2.00,
                 stop_loss_pct=50.0, take_profit_pct=100.0, horizon_days=21,
                 thesis_summary="t", confidence=0.8, independent_source_count=2,
                 citations=[])
    # Backdated: the journal deliberately never resolves a stop or target on
    # the ENTRY session, so every close happens on a later one.
    journal.open_trades["NVX"].opened_at = opened.isoformat()
    return journal, opened


def test_a_reverse_split_does_not_book_a_fabricated_win(tmp_path, monkeypatch):
    """The finding, end to end. Entry 2.00, target 4.00. A 1-for-10 reverse
    split prints 20.00, which clears the target ten times over on a
    position that did not move."""
    journal, opened = _open_trade(tmp_path, monkeypatch)
    later = opened + timedelta(days=3)

    journal.update("NVX", 20.00, now=later, high=20.40, low=19.60)

    assert journal.has_open("NVX"), "must not resolve a target against a split print"
    trade = journal.open_trades["NVX"]
    assert trade.price_discontinuity_ratio == pytest.approx(10.0)
    assert "1-for-10 reverse split" in trade.price_discontinuity_note


def test_a_flagged_trade_closes_void_with_no_pnl(tmp_path, monkeypatch):
    journal, opened = _open_trade(tmp_path, monkeypatch)
    journal.update("NVX", 20.00, now=opened + timedelta(days=3), high=20.4, low=19.6)
    journal.expire_past_horizon(now=opened + timedelta(days=30))

    assert not journal.has_open("NVX")
    import json
    row = json.loads((tmp_path / "paper_trades.jsonl").read_text().splitlines()[-1])
    assert row["status"] == "VOID"
    assert row["r_multiple"] is None
    assert row["exit_price"] is None
    assert row["price_discontinuity_ratio"] == pytest.approx(10.0)


def test_a_flagged_trade_never_resolves_again_even_if_the_price_returns(tmp_path, monkeypatch):
    """Once the series has broken, later prints are not comparable to the
    frozen levels either -- the flag is sticky on purpose."""
    journal, opened = _open_trade(tmp_path, monkeypatch)
    journal.update("NVX", 20.00, now=opened + timedelta(days=3), high=20.4, low=19.6)
    journal.update("NVX", 0.50, now=opened + timedelta(days=4), high=0.55, low=0.45)

    assert journal.has_open("NVX"), "a post-split stop print must not resolve either"


def test_a_genuine_move_to_target_still_banks_a_win(tmp_path, monkeypatch):
    """The guard must not eat real winners. Marked up across sessions, as a
    real position is."""
    journal, opened = _open_trade(tmp_path, monkeypatch)
    journal.update("NVX", 3.00, now=opened + timedelta(days=2), high=3.05, low=2.90)
    journal.update("NVX", 4.10, now=opened + timedelta(days=3), high=4.15, low=3.95)

    assert not journal.has_open("NVX")
    import json
    row = json.loads((tmp_path / "paper_trades.jsonl").read_text().splitlines()[-1])
    assert row["status"] == "WIN"
    assert row["r_multiple"] is not None


# --- the panel ---

def test_a_split_inside_the_window_makes_the_return_unjoinable():
    """One 1-for-10 reverse split produces a +900% observation. This panel
    is the dataset that reaches significance first, so a single unfiltered
    split can decide the answer on its own."""
    marks = price_marks_by_symbol([
        {"session_date": "2026-08-03", "symbol": "NVX", "price": 2.00, "marked_at": "x"},
        {"session_date": "2026-08-04", "symbol": "NVX", "price": 1.95, "marked_at": "x"},
        {"session_date": "2026-08-05", "symbol": "NVX", "price": 19.50, "marked_at": "x"},
    ])
    row = compute_forward_return(
        {"symbol": "NVX", "direction": "LONG", "score": 0.8, "session_date": "2026-08-03"},
        marks, horizon_days=2,
    )
    assert row is None, "a +900% split must never enter the panel as a return"


def test_a_clean_window_still_joins():
    marks = price_marks_by_symbol([
        {"session_date": "2026-08-03", "symbol": "NVX", "price": 2.00, "marked_at": "x"},
        {"session_date": "2026-08-04", "symbol": "NVX", "price": 2.10, "marked_at": "x"},
        {"session_date": "2026-08-05", "symbol": "NVX", "price": 2.40, "marked_at": "x"},
    ])
    row = compute_forward_return(
        {"symbol": "NVX", "direction": "LONG", "score": 0.8, "session_date": "2026-08-03"},
        marks, horizon_days=2,
    )
    assert row is not None and row["signed_return_pct"] == pytest.approx(20.0)


def test_a_large_genuine_move_still_joins():
    """+131% in one session is a real return and belongs in the panel."""
    marks = price_marks_by_symbol([
        {"session_date": "2026-08-03", "symbol": "NVX", "price": 10.0, "marked_at": "x"},
        {"session_date": "2026-08-04", "symbol": "NVX", "price": 23.1, "marked_at": "x"},
    ])
    assert not window_has_corporate_action(marks["NVX"], "2026-08-03", "2026-08-04")


def test_the_check_is_scoped_to_the_window():
    """A split AFTER the exit must not disqualify a window that closed
    cleanly before it."""
    marks = price_marks_by_symbol([
        {"session_date": "2026-08-03", "symbol": "NVX", "price": 2.00, "marked_at": "x"},
        {"session_date": "2026-08-04", "symbol": "NVX", "price": 2.20, "marked_at": "x"},
        {"session_date": "2026-08-09", "symbol": "NVX", "price": 22.0, "marked_at": "x"},
    ])
    assert not window_has_corporate_action(marks["NVX"], "2026-08-03", "2026-08-04")
    assert window_has_corporate_action(marks["NVX"], "2026-08-03", "2026-08-09")


def test_the_close_alert_survives_a_void_trade(tmp_path, monkeypatch):
    """A VOID trade carries no exit price and no R multiple. The close-alert
    text formatted both with ':.2f', which raises TypeError on None -- inside
    the marking loop, i.e. it would have taken down the pass that closes
    every other trade."""
    from smartboi.engine import Engine

    journal, opened = _open_trade(tmp_path, monkeypatch)
    journal.update("NVX", 20.00, now=opened + timedelta(days=3), high=20.4, low=19.6)
    (closed,) = journal.expire_past_horizon(now=opened + timedelta(days=30))

    body = Engine._closed_trade_alert_body(closed)
    assert "VOIDED" in body
    assert "1-for-10 reverse split" in body
    assert "None" not in body


def test_void_trades_are_excluded_from_the_win_rate(tmp_path, monkeypatch):
    """A voided trade is not a loss. Left in the denominator it would count
    as a non-win and quietly deflate the record."""
    import json
    from smartboi.status import gather_paper_trade_stats

    log = tmp_path / "paper_trades.jsonl"
    rows = [
        {"symbol": "A", "status": "WIN", "r_multiple": 1.9, "r_multiple_gross": 2.0},
        {"symbol": "B", "status": "LOSS", "r_multiple": -1.0, "r_multiple_gross": -1.0},
        {"symbol": "C", "status": "VOID", "r_multiple": None, "r_multiple_gross": None},
    ]
    log.write_text("\n".join(json.dumps(r) for r in rows))

    stats, tail = gather_paper_trade_stats(log)

    assert stats.closed == 2, "VOID must not sit in the denominator"
    assert stats.voided == 1
    assert stats.win_rate == pytest.approx(0.5)
    assert len(tail) == 3, "...but the operator should still see it happened"
