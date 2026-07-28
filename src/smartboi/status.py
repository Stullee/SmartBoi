"""Status/analytics gathering for the dashboard -- pure reads of persisted
state (dossiers, graph, paper trade journal, signal log). No live IB/LLM
calls here, so the dashboard stays fast and never risks blocking on a slow
upstream API."""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from smartboi.dossier import Dossier, DossierStore
from smartboi.graph import RelationshipGraph


@dataclass
class PaperTradeStats:
    closed: int = 0
    wins: int = 0
    losses: int = 0
    timeouts: int = 0
    win_rate: float = 0.0
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


def gather_graph_stats(graph: RelationshipGraph) -> dict:
    """Relationships grouped by the filer (`from_symbol` -- "the company the
    evidence is about", see graph.py's Relationship) instead of one flat
    row per edge -- a company with several disclosed counterparties reads
    as one group ("FORM: customer of X, supplier to Y, ...") rather than
    being scattered across a table sorted by insertion order. Each group's
    own relationships are strongest-confidence first."""
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
    return {
        "edge_count": len(graph.relationships),
        "by_symbol": groups,
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


def gather_paper_trade_stats(log_path: Path) -> tuple[PaperTradeStats, list[dict]]:
    rows = _read_jsonl(log_path)
    stats = PaperTradeStats(closed=len(rows))
    if rows:
        stats.wins = sum(1 for r in rows if r.get("status") == "WIN")
        stats.losses = sum(1 for r in rows if r.get("status") == "LOSS")
        stats.timeouts = sum(1 for r in rows if r.get("status") == "TIMEOUT")
        gross = [r.get("r_multiple_gross") for r in rows if r.get("r_multiple_gross") is not None]
        stats.avg_r_gross = round(sum(gross) / len(gross), 3) if gross else 0.0
        stats.win_rate = stats.wins / stats.closed
        stats.avg_r = sum(r.get("r_multiple") or 0.0 for r in rows) / stats.closed
        stats.borrow_assumed = sum(1 for r in rows if r.get("assumes_borrow"))
        clean = [r.get("r_multiple") or 0.0 for r in rows if not r.get("assumes_borrow")]
        stats.avg_r_clean = round(sum(clean) / len(clean), 3) if clean else 0.0
    return stats, rows[-20:]


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
        "symbol": d.symbol,
        "direction": d.direction,
        "confidence": round(d.confidence, 4),
        "magnitude": round(d.magnitude, 4),
        "score": round(d.confidence * d.magnitude, 4),
        "independent_source_count": d.independent_source_count,
        "status": d.status,
    }


def gather_usage(snapshot) -> dict:
    return {
        "date": snapshot.date,
        "calls": snapshot.calls,
        "input_tokens": snapshot.input_tokens,
        "output_tokens": snapshot.output_tokens,
        "daily_call_budget": snapshot.daily_call_budget,
    }
