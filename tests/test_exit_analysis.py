"""Exit-quality analysis over the closed paper-trade ledger. The fixtures are
real closed trades from the live deployment, so these tests double as a record
of what the tool found: a sub-1:1 realized payoff ratio, stops gapping through
on illiquid names, and nothing ever reaching its horizon."""
from __future__ import annotations

import pytest

from smartboi.exit_analysis import (
    exit_reasons,
    format_report,
    hold_to_horizon,
    net_pct,
    reward_risk,
    stop_integrity,
)


def _trades() -> list[dict]:
    # Six real closed trades: 2 wins / 4 losses, LONG and SHORT, spanning cost
    # buckets, with two clean stops (RDW, KLXE) and two that gapped through
    # (ULH, and LMB badly). Only the fields the analysis reads are kept.
    return [
        {"symbol": "BWEN", "direction": "LONG", "entry_price": 3.49, "stop_price": 3.2108,
         "opened_at": "2026-07-30T13:36:36+00:00", "closed_at": "2026-07-31T13:45:23+00:00",
         "horizon_days": 21, "status": "WIN", "exit_price": 4.0484,
         "r_multiple": 1.19, "r_multiple_gross": 2.0, "cost_bps_round_trip": 600.0},
        {"symbol": "PLPC", "direction": "LONG", "entry_price": 283.51, "stop_price": 260.8292,
         "opened_at": "2026-07-30T13:36:39+00:00", "closed_at": "2026-07-31T13:45:23+00:00",
         "horizon_days": 21, "status": "WIN", "exit_price": 328.8716,
         "r_multiple": 1.865, "r_multiple_gross": 2.0, "cost_bps_round_trip": 100.0},
        {"symbol": "ULH", "direction": "LONG", "entry_price": 14.72, "stop_price": 13.5424,
         "opened_at": "2026-07-30T13:36:46+00:00", "closed_at": "2026-07-31T13:45:23+00:00",
         "horizon_days": 21, "status": "LOSS", "exit_price": 13.38,
         "r_multiple": -1.496, "r_multiple_gross": -1.138, "cost_bps_round_trip": 300.0},
        {"symbol": "LMB", "direction": "LONG", "entry_price": 69.31, "stop_price": 63.7652,
         "opened_at": "2026-07-29T15:45:46+00:00", "closed_at": "2026-08-05T13:40:39+00:00",
         "horizon_days": 21, "status": "LOSS", "exit_price": 54.72,
         "r_multiple": -2.967, "r_multiple_gross": -2.631, "cost_bps_round_trip": 300.0},
        {"symbol": "RDW", "direction": "SHORT", "entry_price": 9.585, "stop_price": 10.3518,
         "opened_at": "2026-08-03T18:50:58+00:00", "closed_at": "2026-08-04T13:53:16+00:00",
         "horizon_days": 20, "status": "LOSS", "exit_price": 10.3518,
         "r_multiple": -1.13, "r_multiple_gross": -1.0, "cost_bps_round_trip": 100.0},
        {"symbol": "KLXE", "direction": "SHORT", "entry_price": 2.1, "stop_price": 2.268,
         "opened_at": "2026-07-30T13:36:38+00:00", "closed_at": "2026-08-06T15:37:25+00:00",
         "horizon_days": 16, "status": "LOSS", "exit_price": 2.268,
         "r_multiple": -1.78, "r_multiple_gross": -1.0, "cost_bps_round_trip": 600.0},
    ]


def test_exit_reasons_show_nothing_reaches_horizon():
    ex = exit_reasons(_trades())
    assert ex["n"] == 6
    assert ex["wins"] == 2 and ex["losses"] == 4
    # The headline finding: the grid, not the ~21d thesis window, ends every
    # trade -- zero TIMEOUTs, and most resolve inside a single day.
    assert ex["reached_horizon"] == 0
    assert ex["within_two_days"] == 4         # BWEN, PLPC, ULH, RDW resolved next session (~1 day)
    assert ex["max_hold_days"] == pytest.approx(7.0, abs=0.5)  # KLXE, the longest


def test_reward_risk_ratio_has_inverted_below_one():
    rr = reward_risk(_trades())
    assert rr["avg_win_r"] == pytest.approx(1.5275, abs=1e-3)
    assert rr["avg_loss_r"] == pytest.approx(-1.8433, abs=1e-3)
    # The 16/8 grid implies 2:1; realized is under 1:1 -- each loss bigger
    # than each win.
    assert rr["reward_risk_ratio"] < 1.0
    # Gross is far less bad than net: costs are the difference-maker, and the
    # drag is a positive number of R per trade.
    assert rr["gross_expectancy_r"] > rr["net_expectancy_r"]
    assert rr["cost_drag_r"] == pytest.approx(0.425, abs=0.01)


def test_stop_integrity_flags_gap_throughs():
    si = stop_integrity(_trades())
    assert si["losers"] == 4
    # ULH (-1.138) and LMB (-2.631) gapped past -1R gross; RDW/KLXE stopped
    # cleanly at -1.0.
    assert si["gapped_through"] == 2
    assert si["worst_symbol"] == "LMB"
    assert si["worst_gross_r"] == pytest.approx(-2.631)


def test_net_pct_matches_the_journal_cost_model():
    # gross move 16 on a 100-entry, cost charged on (100+116)/2 notional at
    # 100bp round trip = 1.08 -> 14.92% net.
    assert net_pct(100.0, 116.0, "LONG", 100.0) == pytest.approx(14.92, abs=1e-6)
    # SHORT: a 100->84 fall is a +16 gross gain; cost is charged on the lower
    # (100+84)/2 notional = 0.92, so net is 15.08 (a hair better than the LONG,
    # whose higher exit notional costs more).
    assert net_pct(100.0, 84.0, "SHORT", 100.0) == pytest.approx(15.08, abs=1e-6)


def test_hold_to_horizon_counterfactual_and_coverage():
    trades = [
        # Capped winner: exited at +8 but the mark at its horizon is far higher.
        {"symbol": "ZZZ", "direction": "LONG", "entry_price": 100.0, "stop_price": 92.0,
         "opened_at": "2026-07-01T14:00:00+00:00", "closed_at": "2026-07-02T14:00:00+00:00",
         "horizon_days": 20, "status": "WIN", "exit_price": 108.0,
         "r_multiple": 1.0, "r_multiple_gross": 1.0, "cost_bps_round_trip": 100.0},
        # No price mark near its horizon -> must be omitted, not counted as 0.
        {"symbol": "QQQ", "direction": "LONG", "entry_price": 50.0, "stop_price": 46.0,
         "opened_at": "2026-07-01T14:00:00+00:00", "closed_at": "2026-07-02T14:00:00+00:00",
         "horizon_days": 20, "status": "LOSS", "exit_price": 46.0,
         "r_multiple": -1.0, "r_multiple_gross": -1.0, "cost_bps_round_trip": 100.0},
    ]
    # ZZZ opened 2026-07-01, horizon 20d -> 2026-07-21; the stock ran to 130.
    marks = {"ZZZ": {"2026-07-21": 130.0}}
    rows = hold_to_horizon(trades, marks)

    assert len(rows) == 1 and rows[0]["symbol"] == "ZZZ"   # QQQ omitted for lack of a mark
    r = rows[0]
    assert r["actual_net_pct"] == pytest.approx(6.96, abs=1e-2)     # +8 gross, 1.04 cost
    assert r["horizon_net_pct"] == pytest.approx(28.85, abs=1e-2)   # +30 gross, 1.15 cost
    assert r["delta_pp"] > 0                                        # holding would have helped this capped winner


def test_format_report_handles_an_empty_ledger():
    assert "No closed paper trades yet" in format_report([], {})


def test_format_report_renders_the_key_sections():
    report = format_report(_trades(), {})
    assert "Exit analysis (6 closed trade(s))" in report
    assert "reached horizon (TIMEOUT) : 0" in report
    assert "Realized reward : risk" in report
    assert "Stop integrity" in report
    # No price marks were supplied, so the counterfactual reports itself as
    # not-yet-joinable rather than crashing or fabricating numbers.
    assert "0 of 6 trades joinable yet" in report
