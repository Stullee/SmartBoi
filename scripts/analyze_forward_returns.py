#!/usr/bin/env python3
"""Does confidence*magnitude actually predict forward returns? P1 Phase B
of the work order -- automates the hand analysis by joining
logs/dossier_snapshots.jsonl (status.py's snapshot_dossier, one row per
dossier per day) against logs/price_marks.jsonl (engine.py's
_run_daily_price_marks, one price per tradeable symbol per day). No
network, no engine dependency -- pure offline analysis of already-captured
logs.

Usage:
    python scripts/analyze_forward_returns.py
    python scripts/analyze_forward_returns.py --horizons 5,10,20
    python scripts/analyze_forward_returns.py --snapshots logs/dossier_snapshots.jsonl --marks logs/price_marks.jsonl

For each horizon (default 5 and 20 trading-ish days), reports:
  - mean forward return by score bucket (<0.2, 0.2-0.35, 0.35-0.5, >=0.5)
    and whether it's monotonic
  - correlation between score and signed forward return
  - overall hit-rate (% of theses where the direction was right)
  - a per-symbol breakdown
  - a benchmark-relative variant (return minus the symbol's own ecosystem's
    mean return over the same window) to separate alpha from sector beta

Forward data can't be backfilled -- this is only as good as how long the
daily snapshot/price-mark capture has actually been running."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from smartboi.config import load_settings  # noqa: E402
from smartboi.forward_returns import compute_forward_return, format_report, price_marks_by_symbol  # noqa: E402
from smartboi.universe import spec_by_symbol  # noqa: E402


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


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--snapshots", default="logs/dossier_snapshots.jsonl")
    parser.add_argument("--marks", default="logs/price_marks.jsonl")
    parser.add_argument("--horizons", default="5,20", help="Comma-separated forward-return windows in days")
    args = parser.parse_args()

    snapshots = _read_jsonl(Path(args.snapshots))
    marks = _read_jsonl(Path(args.marks))
    if not snapshots:
        raise SystemExit(f"No rows in {args.snapshots} -- nothing to analyze yet.")
    if not marks:
        raise SystemExit(f"No rows in {args.marks} -- nothing to join against yet.")

    price_marks = price_marks_by_symbol(marks)
    settings = load_settings()
    specs = spec_by_symbol(settings.universe)
    ecosystem_by_symbol = {symbol: spec.ecosystem for symbol, spec in specs.items()}

    horizons = [int(h.strip()) for h in args.horizons.split(",") if h.strip()]
    for horizon_days in horizons:
        joined = [
            r for r in (compute_forward_return(s, price_marks, horizon_days) for s in snapshots)
            if r is not None
        ]
        print(format_report(horizon_days, joined, ecosystem_by_symbol))
        print()


if __name__ == "__main__":
    main()
