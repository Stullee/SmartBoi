"""Operator tools -- the analyses that used to require a terminal, exposed
as plain awaitable/callable functions so the dashboard can run them from a
button (see webapp.py's /api/tools/*) instead of anyone needing shell access
to the Home Assistant host.

That matters more than convenience: the HA host's terminal add-ons are an
awkward and unreliable place to run anything, and the two things an operator
most needs between deployments -- "does this candidate ticker actually screen
thin enough to trade?" and "is confidence*magnitude predicting forward
returns yet?" -- are exactly the things that shouldn't require it.

Both are strictly read-only with respect to the strategy: screening performs
Finnhub lookups and returns a report, forward-return analysis reads two
append-only log files. Neither mutates a dossier, the graph, the universe, or
the paper journal, and neither can place an order (nothing in this codebase
can -- see prices.py/paper_journal.py)."""
from __future__ import annotations

import json
import logging
from pathlib import Path

from smartboi.forward_returns import (
    compute_forward_return,
    dedup_snapshots,
    format_report,
    price_marks_by_symbol,
)
from smartboi.screen import candidates_from_file, resolve_candidates_path
from smartboi.universe import CompanySpec, spec_by_symbol
from smartboi.universe_screen import format_screening_report, guess_ecosystem, screen_candidate

log = logging.getLogger(__name__)

DEFAULT_HORIZONS = (5, 20)
# A screening pass costs two Finnhub calls per ticker, and FinnhubClient
# spaces requests ~1.1s apart to stay inside the free tier's 60/min -- so a
# request is roughly 2.2s per ticker. Capped so one dashboard click can't
# monopolise the shared client for many minutes while the engine's own news
# polling waits behind it.
MAX_TICKERS_PER_RUN = 40


def read_jsonl(path: Path) -> list[dict]:
    """Tolerant JSONL read: a malformed line is skipped rather than aborting
    the whole analysis. These are append-only logs written by a long-running
    process, so a torn final line after an unclean shutdown is possible and
    must not make the report unavailable."""
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


async def run_screen(
    finnhub,
    universe: list[CompanySpec],
    tickers: list[str],
    min_cap: float,
    max_cap: float,
    max_analysts: int,
    candidates_file: str | None = None,
) -> str:
    """Screens `tickers` (or every resolved-ticker universe candidate when
    empty) against the thin-coverage bounds, returning the formatted report.

    Takes the CALLER'S FinnhubClient rather than constructing one: the
    engine's client carries the process's request-spacing state (see
    news.py's _throttled_get), so sharing it keeps a dashboard-triggered
    screen inside the same 60/min free-tier budget the engine's own polling
    is already pacing against. A second client would have its own timer and
    the two together would 429."""
    if finnhub is None:
        return ("News ingestion is disabled, so there's no Finnhub client to screen with. "
                "Set FINNHUB_API_KEY (and enable news ingestion) first.")

    specs = spec_by_symbol(universe)
    if tickers:
        candidates = [(t, []) for t in tickers]
    else:
        candidates = candidates_from_file(resolve_candidates_path(candidates_file))
        if not candidates:
            return ("No resolved-ticker candidates discovered yet -- nothing to screen. "
                    "Enter tickers explicitly to screen names the extraction pipeline hasn't surfaced.")

    truncated = len(candidates) - MAX_TICKERS_PER_RUN
    candidates = candidates[:MAX_TICKERS_PER_RUN]

    results = []
    for symbol, related_to in candidates:
        market_cap = await finnhub.market_cap_musd(symbol)
        analysts = await finnhub.analyst_count(symbol)
        results.append(
            screen_candidate(
                symbol, market_cap, analysts, guess_ecosystem(related_to, specs),
                min_cap, max_cap, max_analysts,
            )
        )
    report = format_screening_report(results)
    if truncated > 0:
        # Never silently drop work -- an operator reading a table of 40 has
        # no way to tell it was capped otherwise.
        report += (f"\n\n({truncated} further candidate(s) not screened this run -- capped at "
                   f"{MAX_TICKERS_PER_RUN} per run to stay inside Finnhub's rate limit. Re-run to continue.)")
    return report


def run_forward_returns(
    log_dir: str | Path,
    universe: list[CompanySpec],
    horizons: tuple[int, ...] | list[int] = DEFAULT_HORIZONS,
) -> str:
    """The "does score predict forward returns" report over every captured
    dossier snapshot, for each horizon -- the analysis half of the
    forward-validation capture (see engine.py's _run_daily_snapshot /
    _run_daily_price_marks). Pure file reads; no network, no LLM."""
    log_dir = Path(log_dir)
    raw_snapshots = read_jsonl(log_dir / "dossier_snapshots.jsonl")
    marks = read_jsonl(log_dir / "price_marks.jsonl")
    if not raw_snapshots:
        return "No dossier snapshots captured yet -- nothing to analyze. These accrue once a day."
    if not marks:
        return ("No price marks captured yet -- nothing to join against. These need ENABLE_IB_PRICE_FEED "
                "and a reachable IB Gateway, and accrue once a day.")

    snapshots = dedup_snapshots(raw_snapshots)
    lines = []
    dropped = len(raw_snapshots) - len(snapshots)
    if dropped:
        lines.append(
            f"Dropped {dropped} duplicate snapshot row(s) (restart artifacts) -- "
            f"{len(snapshots)} distinct (symbol, date) observations remain.\n"
        )

    price_marks = price_marks_by_symbol(marks)
    ecosystem_by_symbol = {symbol: spec.ecosystem for symbol, spec in spec_by_symbol(universe).items()}
    for horizon_days in horizons:
        joined = [
            r for r in (compute_forward_return(s, price_marks, horizon_days) for s in snapshots)
            if r is not None
        ]
        lines.append(format_report(horizon_days, joined, price_marks, ecosystem_by_symbol))
        lines.append("")
    return "\n".join(lines)
