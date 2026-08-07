"""Exit-quality analysis over closed paper trades (logs/paper_trades.jsonl),
joined against logs/price_marks.jsonl for a hold-to-horizon counterfactual.

The win-rate headline says whether trades won; it says nothing about whether
the STOP/TARGET GRID was the right exit. This answers the questions it can't:

  - did trades close far before their horizon (is the grid, not the thesis
    window, deciding outcomes)?
  - is the realized reward:risk the 2:1 the 16/8 grid implies, or has it
    inverted after costs and gaps?
  - does the stop actually cap a loss at 1R, or do illiquid names gap
    straight through it?
  - how much do transaction costs eat, gross vs net?
  - the decision-relevant one: would holding to the horizon with no tight
    stop -- the academic slow-drift exit -- have beaten the grid on the
    trades captured so far?

Pure functions on already-loaded rows: no network, no engine, no LLM. The
price-mark join reuses forward_returns' primitives so this report and the
forward-return report can never disagree about how a date maps to a price."""
from __future__ import annotations

from datetime import date, datetime, timedelta

# Reused rather than re-implemented: a second copy of the on-or-after lookup
# would be a second place for the date->price mapping to drift. price marks
# are close-only and at most one per symbol per day (see engine.py's
# _run_daily_price_marks), which is all the counterfactual needs.
from smartboi.forward_returns import _price_on_or_after, price_marks_by_symbol  # noqa: F401 (re-exported for the tool wrapper)

# A loss whose GROSS r-multiple is at or below this gapped through the stop:
# a clean stop fills at exactly -1R gross, so anything materially past -1R is
# the price having jumped the level rather than the stop having held. The
# 0.05 slack keeps rounding noise from being reported as a gap.
_GAP_THRESHOLD_R = -1.05


def _closed(trades: list[dict]) -> list[dict]:
    """Only rows that actually closed. A defensive filter -- paper_trades.jsonl
    is the closed ledger, but a truncated final write (killed mid-append) can
    leave a row without an exit, and one such row must not divide-by-zero the
    whole report."""
    return [
        t for t in trades
        if t.get("status") in ("WIN", "LOSS", "TIMEOUT")
        and t.get("entry_price") and t.get("exit_price") is not None
    ]


def net_pct(entry: float, price: float, direction: str, cost_bps_round_trip: float) -> float:
    """Net-of-cost return on the entry notional, as a percent, signed in the
    thesis direction (a positive number always means "the thesis paid").

    Deliberately the SAME arithmetic as paper_journal._net_pnl -- charging
    cost on (entry+exit)/2 notional on both legs -- so the hold-to-horizon
    counterfactual is measured on identical terms to the actual exit and the
    two are directly comparable."""
    gross = (price - entry) if direction == "LONG" else (entry - price)
    cost = (entry + price) * (cost_bps_round_trip / 2.0) / 10_000.0
    return (gross - cost) / entry * 100.0


def holding_days(trade: dict) -> float | None:
    """Fractional days a trade was open. Fractional, not whole: 7 of a recent
    13-trade sample closed inside a single day, and rounding those to "0d" or
    "1d" would erase exactly the signal that the grid is resolving trades on
    day-one noise rather than the multi-week thesis."""
    opened, closed = trade.get("opened_at"), trade.get("closed_at")
    if not opened or not closed:
        return None
    try:
        delta = datetime.fromisoformat(closed) - datetime.fromisoformat(opened)
    except ValueError:
        return None
    return delta.total_seconds() / 86400.0


def exit_reasons(trades: list[dict]) -> dict:
    """How trades ended and how long they were held. `reached_horizon` is the
    TIMEOUT count -- the only exit that is the thesis window rather than the
    grid firing early."""
    closed = _closed(trades)
    holds = [d for d in (holding_days(t) for t in closed) if d is not None]
    holds.sort()
    horizons = [t.get("horizon_days") for t in closed if t.get("horizon_days")]
    # "Within two days" rather than "one": the entry-session guard defers
    # resolution to the next session, so the FASTEST a trade can close is
    # ~1 day, and a next-session stop-out lands at ~1.0-1.3 calendar days.
    # Two days captures "resolved almost immediately" without a sub-day
    # boundary that a few minutes of clock drift would flip.
    within_two_days = sum(1 for d in holds if d <= 2.0)
    return {
        "n": len(closed),
        "wins": sum(1 for t in closed if t.get("status") == "WIN"),
        "losses": sum(1 for t in closed if t.get("status") == "LOSS"),
        "reached_horizon": sum(1 for t in closed if t.get("status") == "TIMEOUT"),
        "median_hold_days": holds[len(holds) // 2] if holds else None,
        "max_hold_days": holds[-1] if holds else None,
        "within_two_days": within_two_days,
        "typical_horizon_days": max(set(horizons), key=horizons.count) if horizons else None,
    }


def reward_risk(trades: list[dict]) -> dict:
    """Realized reward:risk and the expectancy decomposition. The single most
    important comparison here is avg-win vs avg-loss: the 16/8 grid LOOKS 2:1,
    and costs+gaps can invert it to below 1:1 while the nominal grid is
    unchanged. Net vs gross expectancy isolates how much of the result is the
    strategy and how much is the transaction-cost tax."""
    closed = _closed(trades)
    wins = [t["r_multiple"] for t in closed if t.get("status") == "WIN" and t.get("r_multiple") is not None]
    losses = [t["r_multiple"] for t in closed if t.get("status") == "LOSS" and t.get("r_multiple") is not None]
    net = [t["r_multiple"] for t in closed if t.get("r_multiple") is not None]
    gross = [t["r_multiple_gross"] for t in closed if t.get("r_multiple_gross") is not None]
    avg_win = sum(wins) / len(wins) if wins else None
    avg_loss = sum(losses) / len(losses) if losses else None
    return {
        "avg_win_r": avg_win,
        "avg_loss_r": avg_loss,
        # |win| / |loss|: the realized payoff ratio. < 1 means each loss is
        # bigger than each win -- fatal at any hit rate near 50%.
        "reward_risk_ratio": (abs(avg_win) / abs(avg_loss)) if avg_win is not None and avg_loss else None,
        "net_expectancy_r": sum(net) / len(net) if net else None,
        "gross_expectancy_r": sum(gross) / len(gross) if gross else None,
        "cost_drag_r": (sum(gross) / len(gross) - sum(net) / len(net)) if net and gross else None,
    }


def stop_integrity(trades: list[dict]) -> dict:
    """Whether the stop actually bounded losses at 1R. A clean stop fills at
    -1R gross; a loss worse than that gross gapped THROUGH the stop -- the
    price jumped the level, so the fill (and the loss) is worse than the risk
    the position was sized for. This is the difference between "downside
    capped at 1R" and "downside is a fat left tail"."""
    losers = [t for t in _closed(trades) if t.get("status") == "LOSS" and t.get("r_multiple_gross") is not None]
    gapped = [t for t in losers if t["r_multiple_gross"] <= _GAP_THRESHOLD_R]
    overshoots = [-1.0 - t["r_multiple_gross"] for t in gapped]  # how far past -1R gross, as a positive R
    worst = min(gapped, key=lambda t: t["r_multiple_gross"]) if gapped else None
    return {
        "losers": len(losers),
        "gapped_through": len(gapped),
        "avg_overshoot_r": (sum(overshoots) / len(overshoots)) if overshoots else None,
        "worst_symbol": worst.get("symbol") if worst else None,
        "worst_gross_r": worst.get("r_multiple_gross") if worst else None,
        "worst_net_r": worst.get("r_multiple") if worst else None,
    }


def hold_to_horizon(
    trades: list[dict],
    price_marks: dict[str, dict[str, float]],
    max_lookahead_days: int = 5,
) -> list[dict]:
    """Per closed trade: the ACTUAL net return vs the counterfactual of having
    ignored the stop/target and exited at the horizon price instead -- the
    exit the slow-drift thesis actually calls for. Both legs use net_pct with
    the trade's own cost bucket, so they are directly comparable.

    Only trades whose horizon has both elapsed AND been priced are returned;
    a too-recent trade (horizon in the future) or one whose price marks
    stopped is omitted rather than counted as a zero -- reporting joined-of-
    total keeps that censoring visible. `delta_pp` > 0 means holding to the
    horizon would have beaten the grid exit for that trade."""
    out = []
    for t in trades:
        if t.get("status") not in ("WIN", "LOSS", "TIMEOUT"):
            continue
        entry, exit_price = t.get("entry_price"), t.get("exit_price")
        direction, opened = t.get("direction"), t.get("opened_at")
        horizon_days, cost_bps = t.get("horizon_days"), t.get("cost_bps_round_trip", 0.0) or 0.0
        if not entry or exit_price is None or direction not in ("LONG", "SHORT") or not opened or not horizon_days:
            continue
        marks = price_marks.get(t.get("symbol"))
        if not marks:
            continue
        try:
            horizon_date = (datetime.fromisoformat(opened).date() + timedelta(days=int(horizon_days))).isoformat()
        except ValueError:
            continue
        found = _price_on_or_after(marks, horizon_date, max_lookahead_days)
        if found is None:
            continue
        _, horizon_price = found
        actual = net_pct(entry, exit_price, direction, cost_bps)
        held = net_pct(entry, horizon_price, direction, cost_bps)
        out.append({
            "symbol": t.get("symbol"),
            "status": t.get("status"),
            "actual_net_pct": actual,
            "horizon_net_pct": held,
            "delta_pp": held - actual,
        })
    return out


def _fmt(value: float | None, suffix: str = "", places: int = 2) -> str:
    return "n/a" if value is None else f"{value:.{places}f}{suffix}"


def format_report(trades: list[dict], price_marks: dict[str, dict[str, float]]) -> str:
    """The full exit-quality report as plain text (renders in the dashboard's
    tool panel). Leads with the exit-reason and reward:risk facts, which need
    no price marks, then the hold-to-horizon counterfactual, which does."""
    closed = _closed(trades)
    if not closed:
        return ("No closed paper trades yet -- exit analysis starts meaning something once trades "
                "close (logs/paper_trades.jsonl). Open trades are excluded; only realized exits count.")

    ex = exit_reasons(trades)
    rr = reward_risk(trades)
    si = stop_integrity(trades)
    lines = [f"=== Exit analysis ({ex['n']} closed trade(s)) ==="]

    lines.append("")
    lines.append("-- Exit reason & holding period --")
    lines.append(f"  hit target (WIN)          : {ex['wins']}")
    lines.append(f"  hit stop  (LOSS)          : {ex['losses']}")
    lines.append(f"  reached horizon (TIMEOUT) : {ex['reached_horizon']}")
    lines.append(f"  holding period            : median {_fmt(ex['median_hold_days'], 'd', 1)}, "
                 f"max {_fmt(ex['max_hold_days'], 'd', 1)} (typical horizon ~{ex['typical_horizon_days']}d)")
    if ex["n"]:
        pct = ex["within_two_days"] / ex["n"] * 100
        lines.append(f"  closed within 2 days      : {ex['within_two_days']} of {ex['n']} ({pct:.0f}%)")
    if ex["reached_horizon"] == 0 and ex["n"]:
        lines.append("  ^^ nothing reached its horizon -- the stop/target grid, not the thesis window, is")
        lines.append("     deciding every trade. A slow-drift thesis is not being given time to play out.")

    lines.append("")
    lines.append("-- Realized reward : risk --")
    ratio = rr["reward_risk_ratio"]
    lines.append(f"  avg win  {_fmt(rr['avg_win_r'], 'R')}   avg loss  {_fmt(rr['avg_loss_r'], 'R')}   "
                 f"ratio {_fmt(ratio, ' : 1') if ratio is not None else 'n/a'}")
    if ratio is not None and ratio < 1.0:
        lines.append("  ^^ each loss is BIGGER than each win -- the 16/8 grid implies 2:1, this is under 1:1.")
    lines.append(f"  net expectancy   : {_fmt(rr['net_expectancy_r'], 'R')} / trade")
    lines.append(f"  gross expectancy : {_fmt(rr['gross_expectancy_r'], 'R')} / trade   (before costs)")
    lines.append(f"  cost drag        : {_fmt(rr['cost_drag_r'], 'R')} / trade   (net = gross minus this)")

    lines.append("")
    lines.append("-- Stop integrity (did the stop cap losses at 1R?) --")
    lines.append(f"  losers gapping THROUGH the stop : {si['gapped_through']} of {si['losers']} "
                 "(gross loss worse than -1R)")
    if si["gapped_through"]:
        lines.append(f"  avg overshoot past -1R (gross)  : -{_fmt(si['avg_overshoot_r'], 'R')}")
        lines.append(f"  worst                           : {si['worst_symbol']} "
                     f"{_fmt(si['worst_gross_r'], 'R')} gross / {_fmt(si['worst_net_r'], 'R')} net")
        lines.append("  ^^ on illiquid names the price jumps the stop -- downside is NOT bounded at 1R.")

    lines.append("")
    lines.append("-- Hold-to-horizon counterfactual (would 'no stop, exit at the horizon' have done better?) --")
    rows = hold_to_horizon(trades, price_marks)
    if not rows:
        lines.append(f"  0 of {ex['n']} trades joinable yet -- horizons not elapsed, or price marks stopped.")
        lines.append("  This is the section that answers the strategy question; it fills in as horizons pass.")
        return "\n".join(lines)

    actual_mean = sum(r["actual_net_pct"] for r in rows) / len(rows)
    held_mean = sum(r["horizon_net_pct"] for r in rows) / len(rows)
    helped = sum(1 for r in rows if r["delta_pp"] > 0)
    win_rows = [r for r in rows if r["status"] == "WIN"]
    loss_rows = [r for r in rows if r["status"] == "LOSS"]
    lines.append(f"  joined {len(rows)} of {ex['n']} closed trade(s) (rest: horizon not yet priced)")
    lines.append(f"  actual exits    : mean {actual_mean:+.2f}% net / trade")
    lines.append(f"  held to horizon : mean {held_mean:+.2f}% net / trade")
    lines.append(f"  difference      : {held_mean - actual_mean:+.2f} pp  "
                 f"({'holding would have HELPED' if held_mean > actual_mean else 'holding would have HURT'} on this sample)")
    lines.append(f"  trades improved by holding : {helped} of {len(rows)}")
    if win_rows:
        w_helped = sum(1 for r in win_rows if r["delta_pp"] > 0)
        lines.append(f"    winners better held (capped upside)  : {w_helped} of {len(win_rows)}")
    if loss_rows:
        l_helped = sum(1 for r in loss_rows if r["delta_pp"] > 0)
        lines.append(f"    losers better held (whipsawed out)   : {l_helped} of {len(loss_rows)}")
    lines.append("  ^^ backward-looking, close-only marks, small sample -- directional, not proof.")

    lines.append("")
    lines.append("-- Per-trade: actual vs held-to-horizon (biggest gap from holding first) --")
    lines.append(f"  {'Symbol':<8}{'Status':<8}{'Actual %':<11}{'Horizon %':<12}{'Delta pp':<10}")
    for r in sorted(rows, key=lambda r: r["delta_pp"], reverse=True):
        lines.append(f"  {r['symbol']:<8}{r['status']:<8}{r['actual_net_pct']:<+11.2f}"
                     f"{r['horizon_net_pct']:<+12.2f}{r['delta_pp']:<+10.2f}")
    return "\n".join(lines)
