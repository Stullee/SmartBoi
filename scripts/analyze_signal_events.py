#!/usr/bin/env python3
"""How did the market move after each signal EPISODE fired -- split by what
the engine actually did with it (opened / drift-skipped / expired)? The
entry-timing guards' scorecard: a drift-skipped episode whose forward
return kept running is a trade the guard cost; one that mean-reverted is a
chase it saved. Joins logs/signals.jsonl (episode-keyed signal rows),
logs/decisions.jsonl (the decisions ledger -- signals.log_decision), and
logs/price_marks.jsonl.

Usage:
    python scripts/analyze_signal_events.py
    python scripts/analyze_signal_events.py --horizons 5,10,20

No network, no engine dependency -- pure offline analysis of the captured
logs, same design rules as analyze_forward_returns.py. Only as good as how
long capture has been running; episodes that fired before the decisions
ledger existed are reported as untracked rather than dropped."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from smartboi.event_study import format_event_study  # noqa: E402
from smartboi.forward_returns import price_marks_by_symbol  # noqa: E402


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
    parser.add_argument("--signals", default="logs/signals.jsonl")
    parser.add_argument("--decisions", default="logs/decisions.jsonl")
    parser.add_argument("--marks", default="logs/price_marks.jsonl")
    parser.add_argument("--horizons", default="5,20", help="Comma-separated forward-return windows in days")
    args = parser.parse_args()

    signal_rows = _read_jsonl(Path(args.signals))
    if not signal_rows:
        raise SystemExit(f"No rows in {args.signals} -- no signals have fired yet.")
    decision_rows = _read_jsonl(Path(args.decisions))
    marks = _read_jsonl(Path(args.marks))
    horizons = [int(h.strip()) for h in args.horizons.split(",") if h.strip()]
    print(format_event_study(signal_rows, decision_rows, price_marks_by_symbol(marks), horizons))


if __name__ == "__main__":
    main()
