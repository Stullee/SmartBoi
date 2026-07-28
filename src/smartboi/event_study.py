"""Signal-episode event study: how did the market move AFTER each signal
episode fired, split by what the engine actually did with it (opened a
paper trade / skipped on favorable drift / expired unopened)?

This is the analysis half of the decisions ledger (signals.log_decision;
rows in logs/decisions.jsonl) joined with logs/signals.jsonl (episode-keyed
signal rows) and logs/price_marks.jsonl (daily prices). It answers the one
question the paper-trade record alone cannot: whether the entry-timing
guards help or hurt. A drift-skipped episode whose forward return kept
running in the thesis direction is a trade the guard COST; one that
mean-reverted is a chase the guard SAVED. Until this existed those
episodes vanished into log lines and the guards were unfalsifiable.

Pure offline analysis: no network, no engine dependency, unit-testable on
synthetic rows -- same design rules as forward_returns.py, whose join and
statistics helpers this reuses."""
from __future__ import annotations

from datetime import date, timedelta

from smartboi.forward_returns import (
    _price_on_or_after,
    cluster_bootstrap_ci,
    effective_sample_count,
)

# Outcome precedence, terminal-most first: an episode that eventually
# opened is "opened" no matter how many drift-skips preceded it; one that
# expired without opening is "expired"; one with only drift-skip rows so
# far is still pending but observably drift-blocked. Episodes with no
# ledger rows at all predate the ledger or are still awaiting a decision.
_OUTCOME_PRECEDENCE = ("trade_opened", "signal_expired", "drift_skip")
OUTCOME_LABELS = {
    "trade_opened": "opened",
    "signal_expired": "expired unopened",
    "drift_skip": "drift-blocked (pending)",
    "untracked": "untracked (pre-ledger or pending)",
}


def collapse_episodes(signal_rows: list[dict]) -> list[dict]:
    """One row per signal EPISODE: the first logged row (earliest
    generated_at) of each (symbol, episode) group -- evaluation is
    status-blind, so one episode re-logs a row on every evidence item that
    keeps it above threshold, and counting re-logs as separate signals
    would overstate signal frequency by an order of magnitude. The FIRST
    row is the one that carries at-fire confidence/magnitude, which is
    what any "did firing predict the move" question must be scored
    against. Legacy rows with no episode key (logged before the key
    existed) fall back to their own generated_at -- uncollapsible, but
    each still yields one usable episode rather than being dropped."""
    by_key: dict[tuple[str, str], dict] = {}
    for row in signal_rows:
        symbol = row.get("symbol")
        generated_at = row.get("generated_at") or ""
        if not symbol or not generated_at:
            continue
        episode = row.get("episode") or generated_at
        key = (symbol, episode)
        existing = by_key.get(key)
        if existing is None:
            by_key[key] = {
                "symbol": symbol,
                "episode": episode,
                "direction": row.get("direction"),
                "confidence": row.get("confidence", 0.0),
                "magnitude": row.get("magnitude", 0.0),
                "fired_at": generated_at,
                "relog_count": 1,
            }
        else:
            existing["relog_count"] += 1
            if generated_at < existing["fired_at"]:
                existing.update(
                    fired_at=generated_at,
                    direction=row.get("direction"),
                    confidence=row.get("confidence", 0.0),
                    magnitude=row.get("magnitude", 0.0),
                )
    return sorted(by_key.values(), key=lambda e: (e["fired_at"], e["symbol"]))


def attach_outcomes(episodes: list[dict], decision_rows: list[dict]) -> list[dict]:
    """Adds `outcome` (see OUTCOME_LABELS), `decision_price`, and
    `decision_reason` to each episode from its decisions-ledger rows,
    matched on (symbol, episode). The most terminal event wins (see
    _OUTCOME_PRECEDENCE); the decision price/reason are taken from that
    winning event's row. `drift_skipped` is additionally set if ANY
    drift_skip row exists, so "opened, but only after a drift-block
    cleared" stays distinguishable from a clean open."""
    by_key: dict[tuple[str, str], list[dict]] = {}
    for row in decision_rows:
        symbol = row.get("symbol")
        episode = row.get("episode") or ""
        if not symbol or not episode:
            continue
        by_key.setdefault((symbol, episode), []).append(row)
    out = []
    for ep in episodes:
        rows = by_key.get((ep["symbol"], ep["episode"]), [])
        outcome = "untracked"
        decision_price = None
        decision_reason = ""
        for event in _OUTCOME_PRECEDENCE:
            matching = [r for r in rows if r.get("event") == event]
            if matching:
                outcome = event
                decision_price = matching[0].get("price")
                decision_reason = matching[0].get("reason") or ""
                break
        out.append({
            **ep,
            "outcome": outcome,
            "decision_price": decision_price,
            "decision_reason": decision_reason,
            "drift_skipped": any(r.get("event") == "drift_skip" for r in rows),
        })
    return out


def episode_forward_return(
    episode: dict,
    price_marks: dict[str, dict[str, float]],
    horizon_days: int,
    max_lookahead_days: int = 5,
) -> dict | None:
    """One episode -> its signed forward return over horizon_days from the
    first price mark on/after the FIRE date -- None when either endpoint
    has no mark within the lookahead (too recent, or marks stopped).
    Signed in the thesis direction, exactly like
    forward_returns.compute_forward_return, so positive always means "the
    signal was right so far"."""
    direction = episode.get("direction")
    if direction not in ("LONG", "SHORT"):
        return None
    marks = price_marks.get(episode["symbol"])
    if not marks:
        return None
    fired_date = (episode.get("fired_at") or "")[:10]
    if not fired_date:
        return None
    entry = _price_on_or_after(marks, fired_date, max_lookahead_days)
    if entry is None:
        return None
    entry_date, entry_price = entry
    if entry_price == 0:
        return None
    target_date = (date.fromisoformat(entry_date) + timedelta(days=horizon_days)).isoformat()
    exit_ = _price_on_or_after(marks, target_date, max_lookahead_days)
    if exit_ is None:
        return None
    _, exit_price = exit_
    raw_pct = (exit_price - entry_price) / entry_price * 100.0
    return {
        **episode,
        "entry_date": entry_date,
        "entry_price": entry_price,
        "horizon_days": horizon_days,
        "signed_return_pct": raw_pct if direction == "LONG" else -raw_pct,
    }


def _group_stats(rows: list[dict], horizon_days: int) -> dict:
    vals = [r["signed_return_pct"] for r in rows]
    by_symbol: dict[str, list[float]] = {}
    for r in rows:
        by_symbol.setdefault(r["symbol"], []).append(r["signed_return_pct"])
    joinable = [{"symbol": r["symbol"], "entry_date": r["entry_date"]} for r in rows]
    return {
        "count": len(vals),
        "mean_return_pct": sum(vals) / len(vals),
        "hit_rate": sum(1 for v in vals if v > 0) / len(vals),
        "n_symbols": len(by_symbol),
        "n_effective": effective_sample_count(joinable, horizon_days),
        "ci_90": cluster_bootstrap_ci(by_symbol),
    }


def format_event_study(
    signal_rows: list[dict],
    decision_rows: list[dict],
    price_marks: dict[str, dict[str, float]],
    horizons: tuple[int, ...] | list[int] = (5, 20),
    max_lookahead_days: int = 5,
) -> str:
    """The full plain-text report: episode counts by outcome, then per
    horizon the signed forward return of each outcome group -- the
    opened-vs-drift-skipped comparison is the entry-timing guards'
    scorecard. Same honesty rules as forward_returns.format_report:
    joined-of-attempted accounting, N_eff, symbol-clustered CIs, and a
    plain "not enough data yet" instead of empty tables."""
    lines = ["=== Signal-episode event study ==="]
    if not signal_rows:
        lines.append("No signals logged yet -- nothing to study.")
        return "\n".join(lines)

    episodes = attach_outcomes(collapse_episodes(signal_rows), decision_rows)
    total_relogs = sum(e["relog_count"] for e in episodes)
    lines.append(
        f"{len(episodes)} episode(s) collapsed from {total_relogs} signal row(s) "
        f"across {len({e['symbol'] for e in episodes})} symbol(s)"
    )

    lines.append("")
    lines.append("-- Episodes by outcome --")
    by_outcome: dict[str, list[dict]] = {}
    for e in episodes:
        by_outcome.setdefault(e["outcome"], []).append(e)
    for outcome in (*_OUTCOME_PRECEDENCE, "untracked"):
        group = by_outcome.get(outcome)
        if not group:
            continue
        note = ""
        if outcome == "trade_opened":
            after_drift = sum(1 for e in group if e["drift_skipped"])
            if after_drift:
                note = f" ({after_drift} opened only after a drift-block cleared)"
        lines.append(f"  {OUTCOME_LABELS[outcome]:34} {len(group)}{note}")
    if by_outcome.get("untracked"):
        lines.append(
            "  (untracked = fired before the decisions ledger existed, or still awaiting a decision "
            "-- their forward returns below cover ALL fired signals regardless)"
        )

    for horizon_days in horizons:
        lines.append("")
        lines.append(f"-- Forward return from signal fire, {horizon_days}-day horizon (signed in thesis direction) --")
        joined_by_outcome: dict[str, list[dict]] = {}
        attempted = 0
        joined_total = 0
        for e in episodes:
            attempted += 1
            r = episode_forward_return(e, price_marks, horizon_days, max_lookahead_days)
            if r is None:
                continue
            joined_total += 1
            joined_by_outcome.setdefault(e["outcome"], []).append(r)
            joined_by_outcome.setdefault("__all__", []).append(r)
        lines.append(
            f"{joined_total} of {attempted} episode(s) joined "
            f"(rest: too recent for this horizon, or no price marks near fire/exit dates)"
        )
        if not joined_total:
            lines.append("Not enough data yet for this horizon.")
            continue
        lines.append(f"  {'Group':<34}{'N':<5}{'Syms':<6}{'N_eff':<7}{'Mean %':<9}{'90% CI':<20}{'Hit'}")
        for outcome in ("__all__", *_OUTCOME_PRECEDENCE, "untracked"):
            group = joined_by_outcome.get(outcome)
            if not group:
                continue
            s = _group_stats(group, horizon_days)
            ci = s["ci_90"]
            ci_text = f"[{ci[0]:+.2f}, {ci[1]:+.2f}]" if ci else "n/a (1 sym)"
            label = "all fired signals" if outcome == "__all__" else OUTCOME_LABELS[outcome]
            lines.append(
                f"  {label:<34}{s['count']:<5}{s['n_symbols']:<6}{s['n_effective']:<7}"
                f"{s['mean_return_pct']:<+9.2f}{ci_text:<20}{s['hit_rate'] * 100:.0f}%"
            )
        opened = joined_by_outcome.get("trade_opened")
        skipped = joined_by_outcome.get("signal_expired", []) + joined_by_outcome.get("drift_skip", [])
        drift_only = [r for r in skipped if r.get("drift_skipped")]
        if opened and drift_only:
            gap = _group_stats(drift_only, horizon_days)["mean_return_pct"] - _group_stats(opened, horizon_days)["mean_return_pct"]
            lines.append(
                f"  Drift-blocked episodes vs opened ones: {gap:+.2f}pp mean forward return -- "
                + ("the guard is skipping moves that KEPT GOING (it may be costing trades)."
                   if gap > 0 else
                   "the guard is skipping moves that did not continue (it is doing its job).")
            )
    return "\n".join(lines)
