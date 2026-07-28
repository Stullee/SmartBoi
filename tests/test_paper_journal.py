from datetime import datetime, timedelta, timezone

import pytest

from smartboi.paper_journal import PaperTradeJournal


def _journal(tmp_path):
    return PaperTradeJournal(tmp_path / "logs" / "paper_trades.jsonl")


def test_open_long_computes_stop_and_target(tmp_path):
    journal = _journal(tmp_path)
    trade = journal.open(
        "UCTT", "LONG", entry_price=100.0, stop_loss_pct=8.0, take_profit_pct=16.0,
        horizon_days=30, thesis_summary="t", confidence=0.7, independent_source_count=2, citations=[],
    )
    assert trade.stop_price == pytest.approx(92.0)
    assert trade.target_price == pytest.approx(116.0)
    assert journal.has_open("UCTT")


def test_open_short_computes_stop_and_target(tmp_path):
    journal = _journal(tmp_path)
    trade = journal.open(
        "UCTT", "SHORT", entry_price=100.0, stop_loss_pct=8.0, take_profit_pct=16.0,
        horizon_days=30, thesis_summary="t", confidence=0.7, independent_source_count=2, citations=[],
    )
    assert trade.stop_price == pytest.approx(108.0)
    assert trade.target_price == pytest.approx(84.0)


def test_target_hit_closes_as_win(tmp_path):
    journal = _journal(tmp_path)
    journal.open("UCTT", "LONG", 100.0, 8.0, 16.0, 30, "t", 0.7, 2, [])
    journal.update("UCTT", 116.0)

    assert not journal.has_open("UCTT")
    lines = journal.log_path.read_text().strip().splitlines()
    assert len(lines) == 1
    import json
    closed = json.loads(lines[0])
    assert closed["status"] == "WIN"
    assert closed["r_multiple"] == 2.0  # (116-100)/(100-92)


def test_stop_hit_closes_as_loss(tmp_path):
    journal = _journal(tmp_path)
    journal.open("UCTT", "LONG", 100.0, 8.0, 16.0, 30, "t", 0.7, 2, [])
    journal.update("UCTT", 92.0)

    assert not journal.has_open("UCTT")


def test_timeout_closes_trade(tmp_path):
    journal = _journal(tmp_path)
    journal.open("UCTT", "LONG", 100.0, 8.0, 16.0, horizon_days=5, thesis_summary="t", confidence=0.7, independent_source_count=2, citations=[])
    future = datetime.now(timezone.utc) + timedelta(days=6)
    journal.update("UCTT", 101.0, now=future)

    assert not journal.has_open("UCTT")


def test_open_state_persists_across_instances(tmp_path):
    journal = _journal(tmp_path)
    journal.open("UCTT", "LONG", 100.0, 8.0, 16.0, 30, "t", 0.7, 2, [])

    reloaded = PaperTradeJournal(journal.log_path)
    assert reloaded.has_open("UCTT")
    assert reloaded.open_trades["UCTT"].entry_price == 100.0


def test_no_new_trade_while_one_is_open(tmp_path):
    journal = _journal(tmp_path)
    journal.open("UCTT", "LONG", 100.0, 8.0, 16.0, 30, "t", 0.7, 2, [])
    assert journal.has_open("UCTT")
    # Caller (engine.py) is responsible for checking has_open() before
    # calling open() again -- this test documents that expectation.


# --- Transaction costs. The closest published analogue to this strategy
# went from ~700% gross to ~50% at 25bp round-trip, and a survey of 204
# anomalies found ~93% post-publication decay AFTER costs (vs ~50% before),
# with the average anomaly netting 4bp/month. A paper record quoted gross is
# not evidence -- and this strategy's edge sits in exactly the small,
# thin-coverage names where spreads are worst.

def test_costs_are_charged_on_both_sides(tmp_path):
    journal = PaperTradeJournal(tmp_path / "t.jsonl")
    trade = journal.open("AAA", "LONG", 100.0, 10.0, 20.0, 21, "t", 0.8, 2, [],
                         cost_bps_round_trip=50.0)
    journal.update("AAA", 120.0)                      # take-profit

    # Gross: +20 on 10 of risk = 2.0R. Cost: 50bp over (100+120)/2 notional
    # per side => 0.25% * 220 = 0.55 -> 0.055R of drag.
    assert trade.r_multiple_gross == 2.0
    assert trade.r_multiple == pytest.approx(1.945, abs=1e-3)
    assert trade.r_multiple < trade.r_multiple_gross


def test_costs_apply_to_shorts_in_the_same_direction(tmp_path):
    """Cost is a drag regardless of side -- never a subsidy on a short."""
    journal = PaperTradeJournal(tmp_path / "t.jsonl")
    trade = journal.open("AAA", "SHORT", 100.0, 10.0, 20.0, 21, "t", 0.8, 2, [],
                         cost_bps_round_trip=50.0)
    journal.update("AAA", 80.0)                       # take-profit for a short
    assert trade.r_multiple < trade.r_multiple_gross


def test_a_marginal_winner_can_be_a_net_loser(tmp_path):
    """The whole point: costs decide marginal trades, which is where a
    thin-edge strategy lives."""
    journal = PaperTradeJournal(tmp_path / "t.jsonl")
    # Wide stop => small R per unit move, so cost drag dominates a small win.
    trade = journal.open("AAA", "LONG", 100.0, 50.0, 0.05, 21, "t", 0.8, 2, [],
                         cost_bps_round_trip=200.0)
    journal.update("AAA", 100.05)
    assert trade.r_multiple_gross > 0
    assert trade.r_multiple < 0


def test_unrealized_is_also_quoted_net(tmp_path):
    """An open book quoted gross systematically flatters itself."""
    journal = PaperTradeJournal(tmp_path / "t.jsonl")
    trade = journal.open("AAA", "LONG", 100.0, 10.0, 50.0, 21, "t", 0.8, 2, [],
                         cost_bps_round_trip=100.0)
    journal.update("AAA", 105.0)
    assert trade.status == "OPEN"
    assert trade.unrealized_r_multiple() < 0.5        # gross would be exactly 0.5


def test_zero_cost_reproduces_the_old_gross_behaviour(tmp_path):
    journal = PaperTradeJournal(tmp_path / "t.jsonl")
    trade = journal.open("AAA", "LONG", 100.0, 10.0, 20.0, 21, "t", 0.8, 2, [])
    journal.update("AAA", 120.0)
    assert trade.r_multiple == trade.r_multiple_gross == 2.0
