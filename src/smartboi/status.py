"""Status/analytics gathering for the dashboard -- pure reads of persisted
state (dossiers, graph, paper trade journal, signal log). No live IB/LLM
calls here, so the dashboard stays fast and never risks blocking on a slow
upstream API."""
from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path

from smartboi.config import strategy_key
from smartboi.dossier import SCORING_VERSION, Dossier, DossierStore
from smartboi.graph import RelationshipGraph


@dataclass
class PaperTradeStats:
    closed: int = 0
    wins: int = 0
    losses: int = 0
    timeouts: int = 0
    win_rate: float = 0.0
    # 95% Wilson interval around win_rate. A bare rate on a dozen trades
    # reads as fact when it is noise: at 13 closed the honest interval is
    # wide enough to still straddle the ~59% break-even, i.e. the record
    # cannot yet be called winning OR losing. Surfaced so the point estimate
    # is never presented alone (see _wilson_interval, and the dashboard card).
    win_rate_ci_low: float = 0.0
    win_rate_ci_high: float = 0.0
    avg_r: float = 0.0
    # Reported alongside net so the cost drag is visible rather than
    # implicit -- the gap between these two is the whole question of whether
    # a thin-edge strategy survives contact with real spreads.
    avg_r_gross: float = 0.0
    # Closed SHORTs whose borrow was never verifiable (small/unknown-cap
    # names are routinely hard-to-borrow -- see paper_journal.assumes_borrow)
    # and the avg R of everything else. borrow-assumed trades commingled
    # into one headline number would overstate what was actually executable.
    borrow_assumed: int = 0
    avg_r_clean: float = 0.0
    # Currency P&L overlay (see config's account model). `currency` is the ISO
    # code, `realized_pnl` sums the closed trades' currency_pnl, and `equity`
    # is the starting capital plus that realized P&L. Open positions' unrealized
    # P&L is marked live from the journal, not here, so equity stays "realized".
    currency: str = ""
    initial_capital: float = 0.0
    realized_pnl: float = 0.0
    equity: float = 0.0


@dataclass
class StrategyGeneration:
    """One strategy "generation" in the closed record -- a set of trades that
    share the same trade-governing config (see config.strategy_key). Kept
    apart so a new strategy's win rate is never pooled with an old, abandoned
    regime's, which would measure two strategies as one number. `legacy` marks
    trades opened before generation stamping existed (config unknown), grouped
    together rather than each masquerading as its own strategy; `is_current`
    marks the generation matching the live config -- the number that actually
    describes what the bot is doing now."""

    key: str
    label: str
    version_from: str
    version_to: str
    is_current: bool
    legacy: bool
    closed: int
    wins: int
    losses: int
    timeouts: int
    win_rate: float
    win_rate_ci_low: float
    win_rate_ci_high: float
    avg_r: float
    avg_r_gross: float
    realized_pnl: float


def gather_dossiers(store: DossierStore) -> list[dict]:
    rows = []
    for symbol in store.all_symbols():
        d = store.load(symbol)
        rows.append(
            {
                "symbol": d.symbol,
                "direction": d.direction,
                "confidence": round(d.confidence, 3),
                "magnitude": round(d.magnitude, 3),
                "horizon_days": d.horizon_days,
                "independent_source_count": d.independent_source_count,
                "status": d.status,
                "thesis_summary": d.thesis_summary,
                "evidence_count": len(d.evidence),
                "updated_at": d.updated_at,
                "signaled_at": d.signaled_at,
                "signaled_price": d.signaled_price,
                "mass_agree": round(d.mass_agree, 3),
                "mass_opposing": round(d.mass_opposing, 3),
            }
        )
    rows.sort(key=lambda r: (r["confidence"] * r["magnitude"]), reverse=True)
    return rows


def gather_coverage(universe, graph: RelationshipGraph, store: DossierStore) -> dict:
    """How much of the tradeable universe is actually being covered -- the
    "are we there yet" readout.

    Three numbers, each answering a different question:

    - dossiers vs tradeables: how many trade targets have any accumulated
      thesis at all. This is the headline: a dossier count far below the
      tradeable count means most of the universe is dark, not that the
      market is quiet.
    - CONNECTED tradeables: how many have at least one graph edge, i.e. can
      ever receive propagated evidence from an anchor. An unconnected
      tradeable can only ever build a dossier from its own direct news --
      and these are selected for thin coverage, so in practice it will not.
    - LIVE anchors: how many anchors have an edge to a tradeable. An anchor
      is never its own analysis target, so one without such an edge is
      inert by construction: its news resolves to zero targets and is
      discarded unread, no matter how much of it there is.

    Measured live 2026-07-29, before the edge-promotion fix: 16 dossiers
    against 48 tradeables, 20 of 48 tradeables connected, and 26 of 130
    anchors live. Those three numbers together explain the whole gap
    between ingestion volume and signal output, which is why they belong on
    the dashboard rather than in a log line."""
    tradeables = [c.symbol for c in universe if not c.signal_source_only]
    anchors = [c.symbol for c in universe if c.signal_source_only]
    tradeable_set = set(tradeables)

    linked: dict[str, set[str]] = {}
    for r in graph.relationships:
        linked.setdefault(r.from_symbol, set()).add(r.to_symbol)
        linked.setdefault(r.to_symbol, set()).add(r.from_symbol)

    with_dossier = {s for s in store.all_symbols() if s in tradeable_set}
    connected = [s for s in tradeables if linked.get(s)]
    # An anchor-to-anchor edge yields no analysis target either, so "live"
    # means specifically "linked to something tradeable".
    live_anchors = [a for a in anchors if linked.get(a, set()) & tradeable_set]

    return {
        "tradeables": len(tradeables),
        "anchors": len(anchors),
        "tradeables_with_dossier": len(with_dossier),
        "tradeables_connected": len(connected),
        "tradeables_unconnected": sorted(tradeable_set - set(connected)),
        "anchors_live": len(live_anchors),
        "anchors_inert": sorted(set(anchors) - set(live_anchors)),
    }


def gather_graph_stats(graph: RelationshipGraph, universe=None, store=None) -> dict:
    """Relationships grouped by the filer (`from_symbol` -- "the company the
    evidence is about", see graph.py's Relationship) instead of one flat
    row per edge -- a company with several disclosed counterparties reads
    as one group ("FORM: customer of X, supplier to Y, ...") rather than
    being scattered across a table sorted by insertion order. Each group's
    own relationships are strongest-confidence first.

    Also returns flat `nodes`/`edges` for the interactive graph panel. A
    node's `kind` comes from the universe (anchors are signal_source_only),
    and its `dir`/`score` from any dossier it has -- so the viz can colour a
    tradeable by its live thesis and size it by conviction, exactly like the
    approved mockup. Both optional: called without them (as several tests do)
    it still returns the tables, just with empty node/edge lists."""
    by_symbol: dict[str, list[dict]] = {}
    for r in graph.relationships:
        by_symbol.setdefault(r.from_symbol, []).append(
            {
                "counterparty": r.to_symbol,
                "type": r.rel_type,
                "confidence": r.confidence,
                "description": r.description,
            }
        )
    groups = [
        {"symbol": symbol, "relationships": sorted(rels, key=lambda x: x["confidence"], reverse=True)}
        for symbol, rels in sorted(by_symbol.items())
    ]

    kind_by: dict[str, str] = {}
    if universe is not None:
        for c in universe:
            kind_by[c.symbol] = "anchor" if c.signal_source_only else "tradeable"
    dossier_syms = set(store.all_symbols()) if store is not None else set()

    symbols: set[str] = set()
    edges: list[list] = []
    for r in graph.relationships:
        symbols.add(r.from_symbol)
        symbols.add(r.to_symbol)
        edges.append([r.from_symbol, r.to_symbol, r.rel_type, round(r.confidence, 3)])

    nodes = []
    for s in sorted(symbols):
        direction = score = None
        if s in dossier_syms:
            d = store.load(s)
            direction = d.direction
            score = round(d.confidence * d.magnitude, 3)
        nodes.append({"id": s, "kind": kind_by.get(s, "external"), "dir": direction, "score": score})

    return {
        "edge_count": len(graph.relationships),
        "by_symbol": groups,
        "nodes": nodes,
        "edges": edges,
    }


def _read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    rows = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return rows


def _wilson_interval(successes: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """95% Wilson score interval for a binomial proportion (default z=1.96).

    Chosen over the textbook Wald (p +/- z*sqrt(p(1-p)/n)) interval because
    Wald degenerates exactly where this record lives: it collapses to zero
    width at 0 or 100% wins and can run past [0, 1] at small n. Wilson stays
    inside [0, 1] and stays honest at the dozen-trade counts the paper
    journal actually has. n <= 0 -> (0, 0)."""
    if n <= 0:
        return (0.0, 0.0)
    p = successes / n
    denom = 1.0 + z * z / n
    center = (p + z * z / (2 * n)) / denom
    margin = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return (max(0.0, center - margin), min(1.0, center + margin))


def gather_paper_trade_stats(
    log_path: Path, initial_capital: float = 0.0, currency: str = ""
) -> tuple[PaperTradeStats, list[dict]]:
    rows = _read_jsonl(log_path)
    stats = PaperTradeStats(closed=len(rows), currency=currency, initial_capital=initial_capital)
    if rows:
        stats.wins = sum(1 for r in rows if r.get("status") == "WIN")
        stats.losses = sum(1 for r in rows if r.get("status") == "LOSS")
        stats.timeouts = sum(1 for r in rows if r.get("status") == "TIMEOUT")
        gross = [r.get("r_multiple_gross") for r in rows if r.get("r_multiple_gross") is not None]
        stats.avg_r_gross = round(sum(gross) / len(gross), 3) if gross else 0.0
        stats.win_rate = stats.wins / stats.closed
        # A win is a WIN against every closed trade (timeouts included, as in
        # win_rate above), so the interval is over the same successes/trials.
        low, high = _wilson_interval(stats.wins, stats.closed)
        stats.win_rate_ci_low = round(low, 4)
        stats.win_rate_ci_high = round(high, 4)
        stats.avg_r = sum(r.get("r_multiple") or 0.0 for r in rows) / stats.closed
        stats.borrow_assumed = sum(1 for r in rows if r.get("assumes_borrow"))
        clean = [r.get("r_multiple") or 0.0 for r in rows if not r.get("assumes_borrow")]
        stats.avg_r_clean = round(sum(clean) / len(clean), 3) if clean else 0.0
        pnls = [r.get("currency_pnl") for r in rows if r.get("currency_pnl") is not None]
        stats.realized_pnl = round(sum(pnls), 2) if pnls else 0.0
    stats.equity = round(initial_capital + stats.realized_pnl, 2)
    return stats, rows[-20:]


def _generation_stats(
    key: str, rows: list[dict], current_key: str | None, current_signature: dict | None
) -> StrategyGeneration:
    legacy = key == ""
    is_current = bool(current_key) and key == current_key and not legacy
    closed = len(rows)
    wins = sum(1 for r in rows if r.get("status") == "WIN")
    losses = sum(1 for r in rows if r.get("status") == "LOSS")
    timeouts = sum(1 for r in rows if r.get("status") == "TIMEOUT")
    win_rate = wins / closed if closed else 0.0
    low, high = _wilson_interval(wins, closed)
    avg_r = (sum(r.get("r_multiple") or 0.0 for r in rows) / closed) if closed else 0.0
    gross_vals = [r.get("r_multiple_gross") for r in rows if r.get("r_multiple_gross") is not None]
    avg_r_gross = round(sum(gross_vals) / len(gross_vals), 3) if gross_vals else 0.0
    pnls = [r.get("currency_pnl") for r in rows if r.get("currency_pnl") is not None]
    realized = round(sum(pnls), 2) if pnls else 0.0

    # Label + version range: for the LIVE generation prefer the current
    # signature, so it reads correctly ("hold-to-horizon, since v0.43") even
    # with zero closed trades yet; otherwise read them off the stamped rows.
    stamped = [r.get("strategy") for r in rows if r.get("strategy")]
    versions = sorted(s.get("version", "") for s in stamped if s.get("version"))
    if legacy:
        label = "legacy (pre-tracking)"
    elif is_current and current_signature:
        label = current_signature.get("label") or "current strategy"
    elif stamped:
        label = stamped[-1].get("label") or "strategy"
    else:
        label = "strategy"
    version_from = versions[0] if versions else (
        (current_signature or {}).get("version", "") if is_current else ""
    )
    version_to = versions[-1] if versions else version_from

    return StrategyGeneration(
        key=key, label=label, version_from=version_from, version_to=version_to,
        is_current=is_current, legacy=legacy, closed=closed, wins=wins, losses=losses,
        timeouts=timeouts, win_rate=round(win_rate, 4),
        win_rate_ci_low=round(low, 4), win_rate_ci_high=round(high, 4),
        avg_r=round(avg_r, 3), avg_r_gross=avg_r_gross, realized_pnl=realized,
    )


def gather_strategy_generations(
    log_path: Path, current_signature: dict | None = None
) -> list[StrategyGeneration]:
    """Segments the closed paper-trade record by strategy generation, so the
    dashboard can show the CURRENT strategy's forward performance apart from
    the trades taken under an old config (institutional costs, tight stops,
    etc.). The current generation is always included even with zero closed
    trades yet -- that "0W-0L, still measuring" state is the honest headline
    right after a strategy change, and the whole point of the split. Ordered
    current first, then other generations by trade count, legacy last."""
    rows = _read_jsonl(log_path)
    current_key = strategy_key(current_signature) if current_signature else None

    groups: dict[str, list[dict]] = {}
    for r in rows:
        groups.setdefault(strategy_key(r.get("strategy")), []).append(r)
    # The live strategy always gets a row, even before it has closed a trade.
    if current_key:
        groups.setdefault(current_key, [])

    gens = [
        _generation_stats(key, grp, current_key, current_signature)
        for key, grp in groups.items()
    ]
    gens.sort(key=lambda g: (not g.is_current, g.legacy, -g.closed))
    return gens


def gather_recent_signals(log_path: Path, limit: int = 25) -> list[dict]:
    return _read_jsonl(log_path)[-limit:]


def _is_addable(c: dict) -> bool:
    return bool(c.get("ticker")) and not c.get("accepted_as")


def gather_universe_candidates(candidates: dict, accepted: dict) -> list[dict]:
    """Companies outside the universe that filings disclosed relationships
    to (see engine._record_universe_candidate), for human review.
    Annotated with `accepted_as` when the dashboard's Accept button (or
    SYMBOLS/ANCHOR_SYMBOLS) has already added this ticker, so the UI can
    show its state instead of an Accept button. Sorted addable candidates
    first (a resolved ticker, not yet accepted -- there's actually
    something to click), then everything else -- both groups
    most-corroborated first -- so the entries that need a decision aren't
    buried under a long tail of unresolved/already-added ones."""
    rows = []
    for key, c in candidates.items():
        row = dict(c)
        entry = accepted.get(row.get("ticker") or key)
        # Entries were originally a bare "tradeable"/"anchor" string and are
        # now a {"as", "source"} dict recording whether a human or the engine
        # accepted it (see engine.accept_candidate). Both shapes are
        # flattened here so the UI renders a string either way rather than
        # "[object Object]" for anything written since the upgrade.
        if isinstance(entry, dict):
            row["accepted_as"] = entry.get("as")
            row["accepted_source"] = entry.get("source", "manual")
        else:
            row["accepted_as"] = entry
            row["accepted_source"] = "manual" if entry else None
        rows.append(row)
    rows.sort(key=lambda c: (0 if _is_addable(c) else 1, -c.get("seen_count", 0)))
    return rows


def snapshot_dossier(d: Dossier, snapshotted_at: str) -> dict:
    """One row for the daily dossier-snapshot log (logs/dossier_snapshots.jsonl,
    see engine.py's _run_daily_snapshot) -- the raw material for eventually
    validating whether confidence*magnitude predicts forward returns,
    joined by symbol/date against logs/price_marks.jsonl. Captures every
    dossier once a day regardless of whether anything actually changed
    that day, so the resulting time series has no gaps to explain away --
    forward data can't be backfilled once a day is missed, so this is
    deliberately unconditional rather than only logging on a change."""
    return {
        "snapshotted_at": snapshotted_at,
        # Which scoring logic produced these numbers (see
        # dossier.SCORING_VERSION). Without it a change to how magnitude or
        # confidence is aggregated silently mixes incomparable rows into one
        # forward-return series; with it the analysis can split at the
        # boundary. Forward data can't be backfilled and old rows must never
        # be re-scored with new logic, so the split is the only honest option.
        "scoring_version": SCORING_VERSION,
        "symbol": d.symbol,
        "direction": d.direction,
        "confidence": round(d.confidence, 4),
        "magnitude": round(d.magnitude, 4),
        "score": round(d.confidence * d.magnitude, 4),
        "independent_source_count": d.independent_source_count,
        "status": d.status,
    }


def gather_usage(snapshot) -> dict:
    # Per-category spend breakdown, so the dashboard can show WHERE the day's
    # budget went (extraction / dossier / synthesis / research), not just the
    # total -- the split is the thing that reveals a category starving another
    # (the reason budget_reserve_synthesis exists; see config.py / usage.py).
    # Every category is present, zeros included, so the bar segments stay
    # stable across refreshes instead of appearing and disappearing.
    from smartboi.usage import CATEGORIES

    by_category = {}
    for cat in CATEGORIES:
        usd, calls = snapshot.by_category.get(cat, (0.0, 0))
        by_category[cat] = {"usd": round(usd, 4), "calls": calls}
    return {
        "date": snapshot.date,
        "calls": snapshot.calls,
        "input_tokens": snapshot.input_tokens,
        "output_tokens": snapshot.output_tokens,
        "daily_call_budget": snapshot.daily_call_budget,
        # Estimated spend, not just call volume: per-call cost now spans
        # more than an order of magnitude across the configurable models,
        # so a call count on its own no longer tells an operator what the
        # day is costing (see usage.py).
        "usd_spent": round(snapshot.usd_spent, 2),
        "daily_usd_budget": snapshot.daily_usd_budget,
        "by_category": by_category,
    }
