#!/usr/bin/env python3
"""Check the would-be trades against real market data: did the lagged
course adjustment actually happen?

Every other analysis in this repo scores the forward record against
`logs/price_marks.jsonl` -- the engine's own daily marks. Those cannot be
backfilled, so on a young deployment the pre-signal window does not exist
and the post-signal window is mostly holes. This script fetches REAL
historical daily bars for the symbols the strategy signalled on, aligns
them in event time around each signal, and reports where the move actually
happened: before the signal, during the signal session, or in the days
after it -- the last of which is the premise the whole strategy rests on.

    python scripts/backtest_trades.py
    python scripts/backtest_trades.py --near 5 --far 20
    python scripts/backtest_trades.py --provider tiingo --tiingo-token $TIINGO_API_KEY
    python scripts/backtest_trades.py --offline           # re-run from the bar cache, no network

Bars are cached under `data/bars/`, so the first run costs one request per
symbol and every re-run costs none. Read-only: this script fetches price
history and writes a cache, nothing else.

The default provider (Stooq) needs no API key and no account."""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from smartboi.backtest import (  # noqa: E402
    DEFAULT_POST_DAYS,
    DEFAULT_PRE_DAYS,
    ENTRY_TOLERANCE_PCT,
    KINDS,
    NEAR_DRIFT_DAYS,
    Series,
    analyse,
    format_report,
    load_would_be_trades,
)
from smartboi.bars import PROVIDERS, BarClient, window_bounds  # noqa: E402
from smartboi.config import load_settings  # noqa: E402
from smartboi.universe import spec_by_symbol  # noqa: E402


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--logs", default="logs", help="Directory holding the runtime .jsonl logs")
    parser.add_argument("--cache-dir", default="data/bars", help="Where fetched bars are cached")
    parser.add_argument("--provider", default="stooq", choices=PROVIDERS)
    parser.add_argument("--tiingo-token", default="", help="Token for --provider tiingo")
    parser.add_argument("--offline", action="store_true",
                        help="Never hit the network -- run from cached bars only")
    parser.add_argument("--pre", type=int, default=DEFAULT_PRE_DAYS,
                        help="Sessions of pre-signal history to include")
    parser.add_argument("--near", type=int, default=NEAR_DRIFT_DAYS,
                        help="End of the near-drift segment, in sessions after the signal")
    parser.add_argument("--far", type=int, default=DEFAULT_POST_DAYS,
                        help="End of the far-drift segment, in sessions after the signal")
    parser.add_argument("--benchmark", default="ecosystem", choices=("ecosystem", "market", "none"),
                        help="What to measure abnormal returns against")
    parser.add_argument("--market-symbol", default="IWM",
                        help="Benchmark used with --benchmark market, and as the fallback when an "
                             "ecosystem has no other priced member")
    parser.add_argument("--kinds", default=",".join(KINDS),
                        help=f"Which would-be trades to include ({', '.join(KINDS)})")
    parser.add_argument("--entry-tolerance", type=float, default=ENTRY_TOLERANCE_PCT,
                        help="%% outside the real session range before a recorded entry is flagged")
    parser.add_argument("--json", dest="as_json", action="store_true",
                        help="Emit the joined rows as JSON instead of the text report")
    return parser.parse_args()


async def _run(args: argparse.Namespace) -> int:
    log_dir = Path(args.logs)
    wanted_kinds = {k.strip() for k in args.kinds.split(",") if k.strip()}
    trades = [t for t in load_would_be_trades(log_dir) if t.kind in wanted_kinds]
    if not trades:
        print(f"No would-be trades in {log_dir}/ -- nothing to check. "
              f"(Looked for paper_trades.jsonl, open_paper_trades.json, signals.jsonl, decisions.jsonl.)")
        return 1

    settings = load_settings()
    specs = spec_by_symbol(settings.universe)
    ecosystem_by_symbol = {symbol: spec.ecosystem for symbol, spec in specs.items()}

    # Fetch the traded names, every symbol that could serve as a peer
    # benchmark, and the market proxy. Peers matter as much as subjects: a
    # benchmark built only from names that happened to signal is not a
    # sector, it is a selection of the sector on the thing being measured.
    subjects = sorted({t.symbol for t in trades})
    needed = set(subjects) | set(specs)
    if args.benchmark != "none":
        needed.add(args.market_symbol.upper())
    start_date, end_date = window_bounds([t.event_at[:10] for t in trades], args.pre, args.far)

    async with BarClient(
        cache_dir=Path(args.cache_dir),
        provider=args.provider,
        tiingo_token=args.tiingo_token,
        offline=args.offline,
    ) as client:
        print(f"Fetching daily bars for {len(needed)} symbol(s), {start_date} .. {end_date} "
              f"(provider: {args.provider}{', offline' if args.offline else ''}, cache: {args.cache_dir}) ...",
              file=sys.stderr)
        bars_by_symbol = await client.bars_for_all(sorted(needed), start_date, end_date)
        failures = dict(client.failures)

    series_by_symbol = {symbol: Series(bars) for symbol, bars in bars_by_symbol.items() if bars}
    if failures:
        print(f"{len(failures)} symbol(s) had no bars: "
              + ", ".join(f"{s} ({why})" for s, why in sorted(failures.items())[:8])
              + (" ..." if len(failures) > 8 else ""), file=sys.stderr)

    joined = analyse(
        trades, series_by_symbol, ecosystem_by_symbol,
        benchmark_mode=args.benchmark, market_symbol=args.market_symbol,
        pre_days=args.pre, far_days=args.far, entry_tolerance_pct=args.entry_tolerance,
    )
    if args.as_json:
        # Event-time offsets are ints; JSON object keys must be strings.
        print(json.dumps({
            "trades": len(trades),
            **joined,
            "paths": [
                {**p, **{curve: {str(k): v for k, v in p[curve].items()}
                         for curve in ("raw", "abnormal", "as_traded")}}
                for p in joined["paths"]
            ],
            "unpriced": sorted(set(joined["unpriced"])),
        }, indent=2))
        return 0

    print(format_report(
        trades, joined["paths"], joined["replays"], joined["reconciliations"],
        key=joined["key"], pre_days=args.pre, near_days=args.near, far_days=args.far,
        benchmark_label=joined["benchmark_label"], unpriced=joined["unpriced"],
    ))
    return 0


def main() -> None:
    raise SystemExit(asyncio.run(_run(_parse_args())))


if __name__ == "__main__":
    main()
