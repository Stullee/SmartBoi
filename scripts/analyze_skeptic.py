#!/usr/bin/env python3
"""Does the adversarial skeptic pass earn its 2x per-item cost? Pure offline
analysis of already-captured state -- the accepted evidence records in
data/dossiers/*.json (each stores the updater's PRE-skeptic
proposed_confidence/proposed_magnitude next to the post-skeptic numbers) and
logs/skeptic_refutations.jsonl (one row per skeptic-refuted item). No network,
no engine dependency.

Usage:
    python scripts/analyze_skeptic.py
    python scripts/analyze_skeptic.py --dossiers data/dossiers --refutations logs/skeptic_refutations.jsonl

Reports the refutation rate, the up/down/unchanged re-scaling distribution, and
mean confidence/magnitude deltas -- overall, split by direct vs propagated
evidence, and split by skeptic model -- the inputs to deciding whether the
trade-gating skeptic belongs on a cheaper or pricier model tier."""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from smartboi.dossier import DossierStore  # noqa: E402
from smartboi.skeptic_report import analyze_skeptic_effect, format_skeptic_report  # noqa: E402


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
    parser.add_argument("--dossiers", default="data/dossiers")
    parser.add_argument("--refutations", default="logs/skeptic_refutations.jsonl")
    args = parser.parse_args()

    store = DossierStore(Path(args.dossiers))
    accepted = []
    for symbol in store.all_symbols():
        for e in store.load(symbol).evidence:
            accepted.append({
                "is_propagated": e.is_propagated,
                "model": e.reviewed_by_model or "unknown",
                "proposed_confidence": e.proposed_confidence,
                "proposed_magnitude": e.proposed_magnitude,
                "confidence": e.confidence,
                "magnitude": e.magnitude,
            })

    refutations = _read_jsonl(Path(args.refutations))
    print(format_skeptic_report(analyze_skeptic_effect(accepted, refutations)))


if __name__ == "__main__":
    main()
