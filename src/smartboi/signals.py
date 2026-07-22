"""Evaluates dossiers for a signal crossing: confidence * magnitude above
threshold, corroborated by enough independent sources -- the trading
signal is accumulated evidence crossing a bar, not any single article (see
README point 3). Signals are always logged, whether or not a price feed is
configured to actually open a hypothetical position (see paper_journal.py
and prices.py) -- so the detection layer is fully exercisable, and its
output fully visible, before IB is wired in.

Deliberately status-blind: a dossier already SIGNALED (awaiting a price
feed, or with a hypothetical position already open) re-logs an updated
signal each time newly accepted evidence keeps it above threshold. The
SIGNALED status only gates opening a paper trade (engine.py), never
logging -- otherwise a no-price-feed deployment would log exactly one
signal per symbol, ever, since only a closing trade resets the status."""
from __future__ import annotations

import json
import logging
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

from smartboi.dossier import Dossier

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class SignalEvent:
    symbol: str
    direction: str
    confidence: float
    magnitude: float
    horizon_days: int
    independent_source_count: int
    thesis_summary: str
    generated_at: str


def evaluate(dossier: Dossier, confidence_threshold: float, min_independent_sources: int) -> SignalEvent | None:
    if dossier.direction == "NONE":
        return None
    if dossier.independent_source_count < min_independent_sources:
        return None
    if dossier.confidence * dossier.magnitude < confidence_threshold:
        return None
    return SignalEvent(
        symbol=dossier.symbol,
        direction=dossier.direction,
        confidence=dossier.confidence,
        magnitude=dossier.magnitude,
        horizon_days=dossier.horizon_days,
        independent_source_count=dossier.independent_source_count,
        thesis_summary=dossier.thesis_summary,
        generated_at=datetime.now(timezone.utc).isoformat(),
    )


def log_signal(log_path: Path, signal: SignalEvent) -> None:
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a") as f:
        f.write(json.dumps(asdict(signal)) + "\n")
    log.info(
        "[SIGNAL] %s %s confidence=%.2f magnitude=%.2f sources=%d: %s",
        signal.direction, signal.symbol, signal.confidence, signal.magnitude,
        signal.independent_source_count, signal.thesis_summary,
    )
