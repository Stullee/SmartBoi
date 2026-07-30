import json
from datetime import datetime, timedelta, timezone

import pytest

import smartboi.market_hours
import smartboi.paper_journal
from smartboi.market_hours import MARKET_TZ
from smartboi.paper_journal import PaperTrade, PaperTradeJournal


def _next_session(days: int = 1) -> datetime:
    """A mark timestamp on a later session, DURING regular trading hours.

    update() refuses to resolve a stop or target unless both hold: the entry
    was on an earlier date, and the market is currently open. The second is
    not belt-and-braces -- the daily bar does not roll to a new session until
    the next US open, so a bar fetched at 02:00 UTC is still the entry
    session's, including prints from before the position existed.

    17:00 UTC is 13:00 ET in summer / 12:00 ET in winter, inside the session
    either way, and the weekday walk keeps it off Saturday and Sunday. Pinned
    rather than derived from the wall clock so the suite gives the same answer
    at any hour.
    """
    stamp = (datetime.now(timezone.utc) + timedelta(days=days)).replace(
        hour=17, minute=0, second=0, microsecond=0
    )
    while stamp.astimezone(MARKET_TZ).weekday() >= 5:
        stamp += timedelta(days=1)
    return stamp


@pytest.fixture
def real_session_clock(monkeypatch):
    """Undo conftest's suite-wide "market is always open" pin.

    That pin exists so the ~15 close-path tests don't depend on the hour the
    suite runs at. The three tests below are the exception: they are ABOUT the
    predicate, so they need the real one."""
    monkeypatch.setattr(
        smartboi.paper_journal,
        "is_regular_trading_hours",
        smartboi.market_hours.is_regular_trading_hours,
    )


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
    journal.update("UCTT", 116.0, now=_next_session())

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
    journal.update("UCTT", 92.0, now=_next_session())

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
    journal.update("AAA", 120.0, now=_next_session())                      # take-profit

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
    journal.update("AAA", 80.0, now=_next_session())                       # take-profit for a short
    assert trade.r_multiple < trade.r_multiple_gross


def test_a_marginal_winner_can_be_a_net_loser(tmp_path):
    """The whole point: costs decide marginal trades, which is where a
    thin-edge strategy lives."""
    journal = PaperTradeJournal(tmp_path / "t.jsonl")
    # Wide stop => small R per unit move, so cost drag dominates a small win.
    trade = journal.open("AAA", "LONG", 100.0, 50.0, 0.05, 21, "t", 0.8, 2, [],
                         cost_bps_round_trip=200.0)
    journal.update("AAA", 100.05, now=_next_session())
    assert trade.r_multiple_gross > 0
    assert trade.r_multiple < 0


def test_unrealized_is_also_quoted_net(tmp_path):
    """An open book quoted gross systematically flatters itself."""
    journal = PaperTradeJournal(tmp_path / "t.jsonl")
    trade = journal.open("AAA", "LONG", 100.0, 10.0, 50.0, 21, "t", 0.8, 2, [],
                         cost_bps_round_trip=100.0)
    journal.update("AAA", 105.0, now=_next_session())
    assert trade.status == "OPEN"
    assert trade.unrealized_r_multiple() < 0.5        # gross would be exactly 0.5


def test_zero_cost_reproduces_the_old_gross_behaviour(tmp_path):
    journal = PaperTradeJournal(tmp_path / "t.jsonl")
    trade = journal.open("AAA", "LONG", 100.0, 10.0, 20.0, 21, "t", 0.8, 2, [])
    journal.update("AAA", 120.0, now=_next_session())
    assert trade.r_multiple == trade.r_multiple_gross == 2.0


# --- Regression: stops/targets are evaluated against the day's intraday
# extremes when available -- a stock that breached the stop midday and
# recovered by the close is a real stop-out, and close-only evaluation
# silently erased exactly those losses. ---

def test_intraday_stop_breach_closes_as_loss_even_if_close_recovered(tmp_path):
    journal = _journal(tmp_path)
    journal.open("UCTT", "LONG", 100.0, 8.0, 16.0, 30, "t", 0.7, 2, [])
    # Day: low 90 (through the 92 stop), close back at 99.
    journal.update("UCTT", 99.0, high=101.0, low=90.0, now=_next_session())

    assert not journal.has_open("UCTT")
    import json
    closed = json.loads(journal.log_path.read_text().strip().splitlines()[0])
    assert closed["status"] == "LOSS"
    assert closed["exit_price"] == 92.0  # fills at the stop, not the recovered close


def test_intraday_stop_fill_is_never_better_than_the_close(tmp_path):
    journal = _journal(tmp_path)
    journal.open("UCTT", "LONG", 100.0, 8.0, 16.0, 30, "t", 0.7, 2, [])
    # Gap down: closes at 85, below the 92 stop -- a resting stop can't
    # have filled at 92 on a gap through it; the close is the honest fill.
    journal.update("UCTT", 85.0, high=88.0, low=84.0, now=_next_session())

    import json
    closed = json.loads(journal.log_path.read_text().strip().splitlines()[0])
    assert closed["status"] == "LOSS"
    assert closed["exit_price"] == 85.0


def test_intraday_target_touch_closes_as_win_at_target_price(tmp_path):
    journal = _journal(tmp_path)
    journal.open("UCTT", "LONG", 100.0, 8.0, 16.0, 30, "t", 0.7, 2, [])
    journal.update("UCTT", 110.0, high=117.0, low=105.0, now=_next_session())  # touched 116 target intraday

    import json
    closed = json.loads(journal.log_path.read_text().strip().splitlines()[0])
    assert closed["status"] == "WIN"
    assert closed["exit_price"] == pytest.approx(116.0)  # a limit fills at its level, not the day's high


def test_stop_wins_when_both_levels_traded_in_one_bar(tmp_path):
    journal = _journal(tmp_path)
    journal.open("UCTT", "LONG", 100.0, 8.0, 16.0, 30, "t", 0.7, 2, [])
    # Wild bar: both the 92 stop and 116 target traded. With no intraday
    # sequencing available, assuming the loss is the conservative choice.
    journal.update("UCTT", 100.0, high=118.0, low=91.0, now=_next_session())

    import json
    closed = json.loads(journal.log_path.read_text().strip().splitlines()[0])
    assert closed["status"] == "LOSS"


def test_short_intraday_stop_breach(tmp_path):
    journal = _journal(tmp_path)
    journal.open("UCTT", "SHORT", 100.0, 8.0, 16.0, 30, "t", 0.7, 2, [])
    # SHORT stop at 108: high spiked to 109 intraday, closed back at 101.
    journal.update("UCTT", 101.0, high=109.0, low=99.0, now=_next_session())

    import json
    closed = json.loads(journal.log_path.read_text().strip().splitlines()[0])
    assert closed["status"] == "LOSS"
    assert closed["exit_price"] == 108.0


def test_close_only_update_behaves_as_before(tmp_path):
    journal = _journal(tmp_path)
    journal.open("UCTT", "LONG", 100.0, 8.0, 16.0, 30, "t", 0.7, 2, [])
    journal.update("UCTT", 100.5, now=_next_session())  # no high/low -- old behavior, stays open
    assert journal.has_open("UCTT")


# --- Regression: a crash between the closed-log append and the open-state
# rewrite left the trade in BOTH files; on restart it was closed a second
# time at a different price, double-counting it in every statistic. ---

def test_restart_drops_open_trade_already_present_in_closed_log(tmp_path):
    journal = _journal(tmp_path)
    trade = journal.open("UCTT", "LONG", 100.0, 8.0, 16.0, 30, "t", 0.7, 2, [])
    journal.update("UCTT", 116.0, now=_next_session())  # closes as WIN
    assert not journal.has_open("UCTT")

    # Simulate the crash window: re-inject the closed trade into the
    # open-state file as if _write_open_state never ran.
    import json
    from dataclasses import asdict
    stale = asdict(trade)
    stale["status"] = "OPEN"
    stale["closed_at"] = None
    stale["exit_price"] = None
    journal.open_state_path.write_text(json.dumps({"UCTT": stale}))

    reloaded = PaperTradeJournal(journal.log_path)
    assert not reloaded.has_open("UCTT")  # self-healed, no second close possible


# --- Market-cap transaction-cost buckets + borrow flag ---

from smartboi.paper_journal import (
    assumes_borrow,
    cost_bps_per_side_for_cap,
    trade_economics,
)


def test_cost_buckets_follow_market_cap():
    assert cost_bps_per_side_for_cap(5000.0, floor_bps_per_side=25.0) == 50.0
    assert cost_bps_per_side_for_cap(1000.0, floor_bps_per_side=25.0) == 50.0
    assert cost_bps_per_side_for_cap(600.0, floor_bps_per_side=25.0) == 150.0
    assert cost_bps_per_side_for_cap(120.0, floor_bps_per_side=25.0) == 300.0


def test_unknown_cap_gets_middle_bucket_not_cheapest():
    assert cost_bps_per_side_for_cap(None, floor_bps_per_side=25.0) == 150.0
    assert cost_bps_per_side_for_cap(0.0, floor_bps_per_side=25.0) == 150.0


def test_configured_floor_is_a_floor_not_a_ceiling():
    # A user who raised the flat setting above a bucket keeps their number.
    assert cost_bps_per_side_for_cap(5000.0, floor_bps_per_side=80.0) == 80.0
    # But the flat setting can never buy sub-bucket costs on a small cap.
    assert cost_bps_per_side_for_cap(120.0, floor_bps_per_side=25.0) == 300.0


def test_assumes_borrow_only_for_small_or_unknown_shorts():
    assert assumes_borrow("SHORT", 120.0)
    assert assumes_borrow("SHORT", None)
    assert not assumes_borrow("SHORT", 800.0)
    assert not assumes_borrow("LONG", 120.0)
    assert not assumes_borrow("LONG", None)


def test_retail_profile_is_cheaper_in_every_bucket():
    for cap in (5000.0, 600.0, 120.0):
        retail = cost_bps_per_side_for_cap(cap, 0.0, "retail")
        institutional = cost_bps_per_side_for_cap(cap, 0.0, "institutional")
        assert retail < institutional, cap
    # Unknown cap still takes the middle bucket of whichever table is in use.
    assert cost_bps_per_side_for_cap(None, 0.0, "retail") == 35.0


def test_unknown_profile_falls_back_to_the_expensive_table():
    # A typo'd setting must never make trades look cheaper than they are.
    assert cost_bps_per_side_for_cap(120.0, 0.0, "reatil") == 300.0
    assert cost_bps_per_side_for_cap(120.0, 0.0, "") == 300.0


def test_the_configured_floor_still_applies_under_the_retail_table():
    assert cost_bps_per_side_for_cap(5000.0, floor_bps_per_side=25.0, profile="retail") == 25.0


# --- What the stop/target grid is actually worth after costs ---


def test_costless_grid_matches_its_nominal_reward_to_risk():
    econ = trade_economics(8.0, 16.0, cost_bps_round_trip=0.0)
    assert econ.r_win == 2.0
    assert econ.r_loss == -1.0
    # 2:1 breaks even at one win in three.
    assert econ.breakeven_win_rate == pytest.approx(1 / 3, abs=1e-3)
    assert econ.cost_share_of_risk == 0.0


def test_microcap_costs_turn_a_nominal_two_to_one_into_a_coin_flip():
    # 300bp/side institutional bucket -> 600bp round trip.
    econ = trade_economics(8.0, 16.0, cost_bps_round_trip=600.0)
    assert econ.r_win == pytest.approx(1.19, abs=0.01)
    assert econ.r_loss == pytest.approx(-1.72, abs=0.01)
    # The whole point of the function: nowhere near the nominal 33%.
    assert econ.breakeven_win_rate == pytest.approx(0.591, abs=0.005)
    # Cost alone eats most of a risk unit on a tight stop.
    assert econ.cost_share_of_risk == pytest.approx(0.72, abs=0.01)


def test_costs_land_on_both_legs_so_they_worsen_wins_and_losses_alike():
    cheap = trade_economics(8.0, 16.0, cost_bps_round_trip=100.0)
    dear = trade_economics(8.0, 16.0, cost_bps_round_trip=600.0)
    assert dear.r_win < cheap.r_win
    assert dear.r_loss < cheap.r_loss  # more negative, not merely smaller
    assert dear.breakeven_win_rate > cheap.breakeven_win_rate


def test_shorts_are_costed_on_their_own_geometry():
    # A short's target is BELOW the entry, so its exit notional -- and hence
    # its cost -- differs from the mirrored long. Same ballpark, not equal.
    long_econ = trade_economics(8.0, 16.0, 600.0, "LONG")
    short_econ = trade_economics(8.0, 16.0, 600.0, "SHORT")
    assert short_econ.r_win != long_econ.r_win
    assert short_econ.breakeven_win_rate == pytest.approx(0.576, abs=0.005)


def test_a_grid_that_cannot_break_even_reports_so_rather_than_over_one():
    # Cost exceeds the entire target move: the win leg is negative, so no
    # hit rate saves it. Must not report a fraction above 1.
    econ = trade_economics(8.0, 4.0, cost_bps_round_trip=1200.0)
    assert econ.r_win < 0
    assert econ.breakeven_win_rate == 1.0


def test_economics_are_scale_invariant_in_the_entry_price():
    # Cost and P&L are both linear in price, so the answer must not depend
    # on the notional the calculation happened to pick.
    assert trade_economics(8.0, 16.0, 600.0) == trade_economics(8.0, 16.0, 600.0)
    # And it agrees with an actual journal trade's realized R at the stop.
    journal_r = PaperTrade(
        symbol="X", direction="LONG", entry_price=15.05, stop_price=15.05 * 0.92,
        target_price=15.05 * 1.16, opened_at="2026-07-29T00:00:00+00:00",
        horizon_days=20, thesis_summary="t", confidence=0.8,
        independent_source_count=2, cost_bps_round_trip=600.0,
    )
    risk = 15.05 - 15.05 * 0.92
    at_stop = journal_r._net_pnl(15.05 * 0.92) / risk
    assert at_stop == pytest.approx(trade_economics(8.0, 16.0, 600.0).r_loss, abs=0.005)


def test_open_records_cap_and_borrow_flag(tmp_path):
    journal = PaperTradeJournal(tmp_path / "paper_trades.jsonl")
    trade = journal.open(
        "UCTT", "SHORT", 100.0, stop_loss_pct=8.0, take_profit_pct=16.0,
        horizon_days=20, thesis_summary="t", confidence=0.8,
        independent_source_count=2, citations=[],
        cost_bps_round_trip=600.0, market_cap_musd=120.0,
    )
    assert trade.market_cap_musd == 120.0
    assert trade.assumes_borrow is True

    # And survives the open-state snapshot round trip.
    reloaded = PaperTradeJournal(tmp_path / "paper_trades.jsonl")
    assert reloaded.open_trades["UCTT"].assumes_borrow is True
    assert reloaded.open_trades["UCTT"].market_cap_musd == 120.0


# --- A trade nothing can price must still hit its horizon.
#
# `update` is the only other close path and it needs a price, so a symbol no
# source could price (delisted, halted, unqualifiable at IB, absent from
# Finnhub's free tier) produced a position that never stopped out, never took
# profit and never timed out -- open forever, its dossier pinned at SIGNALED
# so no fresh signal could replace it, its P&L excluded from every statistic.

def _open_trade(journal, symbol="FORM", horizon_days=20):
    return journal.open(
        symbol=symbol, direction="LONG", entry_price=10.0, stop_loss_pct=8.0,
        take_profit_pct=16.0, horizon_days=horizon_days, thesis_summary="t",
        confidence=0.8, independent_source_count=2, citations=[],
    )


def test_a_trade_past_its_horizon_closes_without_a_price(tmp_path):
    journal = PaperTradeJournal(tmp_path / "paper_trades.jsonl")
    trade = _open_trade(journal)
    opened = datetime.fromisoformat(trade.opened_at)

    expired = journal.expire_past_horizon(now=opened + timedelta(days=21))

    assert [t.symbol for t in expired] == ["FORM"]
    assert not journal.has_open("FORM")
    assert expired[0].status == "TIMEOUT"


def test_it_exits_at_the_last_mark_when_one_exists(tmp_path):
    journal = PaperTradeJournal(tmp_path / "paper_trades.jsonl")
    trade = _open_trade(journal)
    journal.update("FORM", 10.5, now=_next_session())  # a mark landed, then the feed went dark
    opened = datetime.fromisoformat(trade.opened_at)

    journal.expire_past_horizon(now=opened + timedelta(days=21))

    closed = [json.loads(line) for line in journal.log_path.read_text().splitlines()]
    assert closed[-1]["exit_price"] == 10.5


def test_it_exits_flat_rather_than_inventing_a_price(tmp_path):
    """A flat row is honest about having learned nothing."""
    journal = PaperTradeJournal(tmp_path / "paper_trades.jsonl")
    trade = _open_trade(journal)
    opened = datetime.fromisoformat(trade.opened_at)

    journal.expire_past_horizon(now=opened + timedelta(days=21))

    closed = [json.loads(line) for line in journal.log_path.read_text().splitlines()]
    assert closed[-1]["exit_price"] == 10.0  # the entry price
    assert closed[-1]["r_multiple_gross"] == 0.0


def test_a_trade_inside_its_horizon_is_left_alone(tmp_path):
    journal = PaperTradeJournal(tmp_path / "paper_trades.jsonl")
    trade = _open_trade(journal)
    opened = datetime.fromisoformat(trade.opened_at)

    assert journal.expire_past_horizon(now=opened + timedelta(days=19)) == []
    assert journal.has_open("FORM")


# --- Audit round 2, finding #1: a trade could be opened and spuriously
# resolved on the SAME poll, against price action from before it existed.
# engine._mark_and_execute opens in its first loop and then marks every open
# trade -- the new one included -- a few lines later in the same call. ---


def test_a_stop_inside_the_entry_session_range_does_not_close_the_trade(tmp_path):
    """The exact shape of the bug: the entry-day bar's low is through the
    stop because the stock swung that far BEFORE the entry. Resolving it
    books a loss on a position that did not exist at that price."""
    journal = _journal(tmp_path)
    journal.open("UCTT", "LONG", 100.0, 8.0, 16.0, 30, "t", 0.7, 2, [])
    journal.update("UCTT", 99.0, high=101.0, low=90.0)  # entry session, no now=

    assert journal.has_open("UCTT")
    assert journal.open_trades["UCTT"].last_price == 99.0  # still marked for display


def test_a_target_inside_the_entry_session_range_does_not_close_it_either(tmp_path):
    """Symmetric, and it matters: silently taking the free wins would bias
    the ledger the other way and be much harder to notice."""
    journal = _journal(tmp_path)
    journal.open("UCTT", "LONG", 100.0, 8.0, 16.0, 30, "t", 0.7, 2, [])
    journal.update("UCTT", 110.0, high=120.0, low=105.0)

    assert journal.has_open("UCTT")


def test_the_same_breach_resolves_normally_on_the_next_session(tmp_path):
    """Deferred, not discarded -- the guard must not make a trade unclosable."""
    journal = _journal(tmp_path)
    journal.open("UCTT", "LONG", 100.0, 8.0, 16.0, 30, "t", 0.7, 2, [])
    journal.update("UCTT", 99.0, high=101.0, low=90.0)
    assert journal.has_open("UCTT")

    journal.update("UCTT", 99.0, high=101.0, low=90.0, now=_next_session())
    assert not journal.has_open("UCTT")
    closed = json.loads(journal.log_path.read_text().strip().splitlines()[-1])
    assert closed["status"] == "LOSS"


def test_the_entry_session_mark_still_persists(tmp_path):
    """last_price has to survive a restart even on the entry day, or the
    dashboard shows an open position with no mark until the next session."""
    journal = _journal(tmp_path)
    journal.open("UCTT", "LONG", 100.0, 8.0, 16.0, 30, "t", 0.7, 2, [])
    journal.update("UCTT", 97.5, high=101.0, low=90.0)

    reloaded = PaperTradeJournal(journal.log_path)
    assert reloaded.open_trades["UCTT"].last_price == 97.5


def test_resolution_is_refused_overnight_even_on_a_later_calendar_date(tmp_path, real_session_clock):
    """The bug in the first version of this guard: the UTC date rolls at
    00:00, but the daily BAR does not roll until the next US open (~13:30
    UTC). A date-only check therefore resolved against a bar that was still
    the entry session's, for thirteen and a half hours every night."""
    journal = _journal(tmp_path)
    journal.open("UCTT", "LONG", 100.0, 8.0, 16.0, 30, "t", 0.7, 2, [])
    opened = datetime.fromisoformat(journal.open_trades["UCTT"].opened_at)

    # Next calendar day, 02:00 UTC = 22:00 ET the evening before. The date has
    # advanced; the session has not.
    overnight = (opened + timedelta(days=1)).replace(hour=2, minute=0, second=0, microsecond=0)
    journal.update("UCTT", 99.0, high=101.0, low=90.0, now=overnight)

    assert journal.has_open("UCTT")
    assert journal.open_trades["UCTT"].last_price == 99.0


def test_a_friday_entry_is_not_resolved_across_the_whole_weekend(tmp_path, real_session_clock):
    """A Friday entry was exposed from Saturday 00:00 UTC until Monday 13:30
    -- the longest window, against the stalest bar."""
    journal = _journal(tmp_path)
    journal.open("UCTT", "LONG", 100.0, 8.0, 16.0, 30, "t", 0.7, 2, [])
    opened = datetime.fromisoformat(journal.open_trades["UCTT"].opened_at)
    # Walk to the next Saturday, mid-day UTC.
    saturday = opened + timedelta(days=1)
    while saturday.astimezone(MARKET_TZ).weekday() != 5:
        saturday += timedelta(days=1)
    for day in (saturday, saturday + timedelta(days=1)):
        journal.update("UCTT", 99.0, high=101.0, low=90.0,
                       now=day.replace(hour=15, minute=0, second=0, microsecond=0))
        assert journal.has_open("UCTT"), day


def test_a_timeout_still_closes_outside_the_session(tmp_path):
    """The guard must not make a position unclosable. A horizon timeout does
    not depend on an intraday range, so it is not affected by the stale-bar
    problem and must still fire -- otherwise a trade whose horizon lapses on a
    Friday sits open all weekend for no reason."""
    journal = _journal(tmp_path)
    journal.open("UCTT", "LONG", 100.0, 8.0, 16.0, horizon_days=5,
                 thesis_summary="t", confidence=0.7, independent_source_count=2, citations=[])
    past_horizon = datetime.now(timezone.utc) + timedelta(days=6)

    assert journal.expire_past_horizon(now=past_horizon)
    assert not journal.has_open("UCTT")
