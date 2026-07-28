#!/usr/bin/env python3
"""Screens candidate tickers against the small/mid-cap thin-coverage
bounds this strategy actually needs -- P1(a) of the universe refresh: the
current DEFAULT_UNIVERSE has drifted (most tradeable names graduated to
broad analyst coverage), and picking replacements by hand means guessing
at market caps. This queries live data instead.

By default, mines every resolved-ticker entry already sitting in
data/universe_candidates.json (discovered by the engine's own relationship
extraction -- see README's "Bring your own universe"). Pass explicit
tickers on the command line to screen ideas that were never auto-discovered.

Usage:
    python scripts/screen_candidates.py
    python scripts/screen_candidates.py AEIS CEVA POWI
    python scripts/screen_candidates.py --min-cap 50 --max-cap 2000 --max-analysts 4

Requires FINNHUB_API_KEY (reads the same .env / environment smartboi.config
does). Output is a ranked table, not a decision -- see README's "Bring your
own universe": the final tradeable list is an editorial call, not a
threshold check."""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

# Lets this run straight from a checkout without a prior `pip install -e .`
# -- same src-layout the package itself uses, harmless if already installed.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from smartboi.config import load_settings  # noqa: E402
from smartboi.news import FinnhubClient  # noqa: E402
from smartboi.universe import spec_by_symbol  # noqa: E402
from smartboi.universe_screen import format_screening_report, guess_ecosystem, screen_candidate  # noqa: E402

DEFAULT_MIN_CAP_MUSD = 100.0
DEFAULT_MAX_CAP_MUSD = 3000.0  # tighter than the live 5000M ceiling -- see the work order's P1 rationale
DEFAULT_MAX_ANALYSTS = 6


def _candidates_from_file(path: Path) -> list[tuple[str, list[str]]]:
    """Every resolved-ticker entry in universe_candidates.json, paired with
    its related_to list (used for the ecosystem guess). Ticker-less
    candidates are skipped -- there's nothing to screen without a ticker."""
    if not path.exists():
        return []
    raw = json.loads(path.read_text())
    return [(e["ticker"], e.get("related_to", [])) for e in raw.values() if e.get("ticker")]


async def _run(args: argparse.Namespace) -> str:
    settings = load_settings()
    if not settings.finnhub_api_key:
        raise SystemExit("FINNHUB_API_KEY not set -- cannot screen without it.")

    specs = spec_by_symbol(settings.universe)
    if args.tickers:
        candidates = [(t.upper(), []) for t in args.tickers]
    else:
        candidates = _candidates_from_file(Path(args.candidates_file))
        if not candidates:
            raise SystemExit(f"No resolved-ticker candidates in {args.candidates_file}.")

    finnhub = FinnhubClient(settings.finnhub_api_key)
    try:
        results = []
        for symbol, related_to in candidates:
            market_cap = await finnhub.market_cap_musd(symbol)
            analysts = await finnhub.analyst_count(symbol)
            ecosystem = guess_ecosystem(related_to, specs)
            results.append(
                screen_candidate(
                    symbol, market_cap, analysts, ecosystem,
                    args.min_cap, args.max_cap, args.max_analysts,
                )
            )
    finally:
        await finnhub.aclose()
    return format_screening_report(results)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("tickers", nargs="*", help="Specific tickers to screen (default: mine data/universe_candidates.json)")
    parser.add_argument("--min-cap", type=float, default=DEFAULT_MIN_CAP_MUSD, dest="min_cap", help="Market cap floor, $M")
    parser.add_argument("--max-cap", type=float, default=DEFAULT_MAX_CAP_MUSD, dest="max_cap", help="Market cap ceiling, $M")
    parser.add_argument("--max-analysts", type=int, default=DEFAULT_MAX_ANALYSTS, dest="max_analysts", help="Analyst-count ceiling")
    parser.add_argument("--candidates-file", default="data/universe_candidates.json")
    args = parser.parse_args()
    print(asyncio.run(_run(args)))


if __name__ == "__main__":
    main()
