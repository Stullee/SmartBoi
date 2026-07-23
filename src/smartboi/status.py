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
    return {
        "edge_count": len(graph.relationships),
        "edges": [
            {
                "from": r.from_symbol,
                "to": r.to_symbol,
                "type": r.rel_type,
                "description": r.description,
                "confidence": r.confidence,
            }
            for r in graph.relationships
        ],
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
        stats.win_rate = stats.wins / stats.closed
        stats.avg_r = sum(r.get("r_multiple") or 0.0 for r in rows) / stats.closed
    return stats, rows[-20:]


def gather_recent_signals(log_path: Path, limit: int = 25) -> list[dict]:
    return _read_jsonl(log_path)[-limit:]


def gather_universe_candidates(candidates: dict, accepted: dict) -> list[dict]:
    """Companies outside the universe that filings disclosed relationships
    to (see engine._record_universe_candidate) -- most-corroborated first,
    for human review. Annotated with `accepted_as` when the dashboard's
    Accept button (or SYMBOLS/ANCHOR_SYMBOLS) has already added this
    ticker, so the UI can show its state instead of an Accept button."""
    rows = []
    for key, c in candidates.items():
        row = dict(c)
        row["accepted_as"] = accepted.get(row.get("ticker") or key)
        rows.append(row)
    rows.sort(key=lambda c: c.get("seen_count", 0), reverse=True)
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
