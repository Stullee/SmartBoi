"""Status/analytics gathering for the dashboard -- pure reads of persisted
state (dossiers, graph, paper trade journal, signal log). No live IB/LLM
calls here, so the dashboard stays fast and never risks blocking on a slow
upstream API."""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from smartboi.dossier import DossierStore
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


def gather_universe_candidates(candidates: dict) -> list[dict]:
    """Companies outside the universe that filings disclosed relationships
    to (see engine._record_universe_candidate) -- most-corroborated first,
    for human review; the dashboard shows them but nothing auto-adds them."""
    rows = list(candidates.values())
    rows.sort(key=lambda c: c.get("seen_count", 0), reverse=True)
    return rows
