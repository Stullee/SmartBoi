"""Candidate screening CLI, packaged so it ships wherever smartboi itself
is installed: `python -m smartboi.screen`.

Screens candidate tickers against the small/mid-cap thin-coverage bounds
this strategy actually needs. The universe drifts -- names that were
genuinely obscure when picked graduate into broad coverage, at which point
the information-diffusion lag the strategy trades no longer exists for them
(see universe.py's 2026-07 refresh note) -- so picking replacements means
checking live market-cap/analyst data, not guessing.

By default it mines every resolved-ticker entry already sitting in
universe_candidates.json (discovered by the engine's own relationship
extraction). Pass explicit tickers to screen ideas that were never
auto-discovered.

This lives in the PACKAGE rather than only in scripts/ on purpose: the Home
Assistant add-on installs the package straight from git (see
ha-addons/smartboi/Dockerfile) and never copies scripts/, so a
scripts-only screener is unreachable exactly where it's most needed --
inside the running deployment, which is the one place that already holds a
Finnhub key and a populated universe_candidates.json.

Usage:
    python -m smartboi.screen
    python -m smartboi.screen INTT ASYS CVU
    python -m smartboi.screen --max-cap 2000 --max-analysts 4

Output is a ranked table, not a decision -- whether a name belongs in an
ecosystem is an editorial call, the same reason universe_screen.py is
prune-only and discovered candidates are never auto-added.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
from pathlib import Path

from smartboi.config import load_settings
from smartboi.news import FinnhubClient
from smartboi.universe import spec_by_symbol
from smartboi.universe_screen import format_screening_report, guess_ecosystem, screen_candidate

log = logging.getLogger(__name__)

DEFAULT_MIN_CAP_MUSD = 75.0
# Tighter than the engine's live 5000M ceiling: that bound governs PRUNING an
# existing universe (where a borderline name is worth keeping until it
# clearly no longer fits), while this one governs ADDING, where there's no
# reason to start a new position anywhere near the ceiling it would soon
# graduate past.
DEFAULT_MAX_CAP_MUSD = 3000.0
# 10, not the 6 this started with: a live screen of 15 candidates failed 14 of
# them, clustered just above 6 (7,8,8,9,9,9,9,9,10), while everything that did
# clear 6 sat below the old $100M floor -- i.e. the old pair described a window
# that is close to empty in US small caps. It also makes this agree with
# universe.py, which already retained names at 9-10 analysts.
DEFAULT_MAX_ANALYSTS = 10

# Written by the Home Assistant supervisor from the add-on's configuration
# form (see ha-addons/smartboi/_addon_options.py, which loads it into the
# environment for the ENGINE process). A separate `docker exec` process --
# which is how this CLI gets run inside a deployment -- does not inherit
# that environment, so the key is read straight from the file instead.
_ADDON_OPTIONS_PATH = Path("/data/options.json")
# The add-on chdir's here before starting the engine, so this is where a
# deployed instance's data/ actually lives (see addon_entrypoint.py).
_ADDON_RUN_DIR = Path("/config/smartboi_run")


def _finnhub_key_from_addon_options() -> str:
    """The add-on's configured Finnhub key, or "" if not running inside one
    (or the file is unreadable/malformed -- a missing key is reported by the
    caller as a clear error, never a crash here)."""
    try:
        return str(json.loads(_ADDON_OPTIONS_PATH.read_text()).get("finnhub_api_key") or "")
    except (OSError, json.JSONDecodeError, AttributeError):
        return ""


def resolve_finnhub_key() -> str:
    """FINNHUB_API_KEY from the environment/.env as usual, falling back to
    the add-on's options.json -- see _ADDON_OPTIONS_PATH."""
    return load_settings().finnhub_api_key or _finnhub_key_from_addon_options()


def resolve_candidates_path(explicit: str | None) -> Path:
    """Where universe_candidates.json actually is: an explicit --candidates-file
    wins, then the relative path (correct when run from a checkout or from
    the add-on's own run directory), then the add-on run directory itself --
    so `docker exec`, which starts in /app rather than the run directory,
    still finds a deployed instance's candidates instead of reporting none."""
    if explicit:
        return Path(explicit)
    relative = Path("data/universe_candidates.json")
    if relative.exists():
        return relative
    return _ADDON_RUN_DIR / "data" / "universe_candidates.json"


def candidates_from_file(path: Path) -> list[tuple[str, list[str]]]:
    """Every resolved-ticker entry in universe_candidates.json, paired with
    its related_to list (used for the ecosystem guess). Ticker-less
    candidates are skipped -- there's nothing to screen without a ticker."""
    if not path.exists():
        return []
    try:
        raw = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        return []
    return [(e["ticker"], e.get("related_to", [])) for e in raw.values() if e.get("ticker")]


async def screen(
    tickers: list[str],
    candidates_path: Path,
    min_cap: float,
    max_cap: float,
    max_analysts: int,
) -> str:
    api_key = resolve_finnhub_key()
    if not api_key:
        raise SystemExit(
            "FINNHUB_API_KEY not set -- cannot screen without it. Set it in the environment/.env, "
            f"or run this inside the add-on where {_ADDON_OPTIONS_PATH} holds the configured key."
        )

    specs = spec_by_symbol(load_settings().universe)
    if tickers:
        candidates = [(t.upper(), []) for t in tickers]
    else:
        candidates = candidates_from_file(candidates_path)
        if not candidates:
            raise SystemExit(
                f"No resolved-ticker candidates in {candidates_path}. Pass tickers explicitly, "
                "e.g. `python -m smartboi.screen INTT ASYS CVU`."
            )

    finnhub = FinnhubClient(api_key)
    try:
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
    finally:
        await finnhub.aclose()
    return format_screening_report(results)


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="python -m smartboi.screen",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("tickers", nargs="*",
                        help="Specific tickers to screen (default: mine universe_candidates.json)")
    parser.add_argument("--min-cap", type=float, default=DEFAULT_MIN_CAP_MUSD, dest="min_cap",
                        help=f"Market cap floor, $M (default {DEFAULT_MIN_CAP_MUSD:.0f})")
    parser.add_argument("--max-cap", type=float, default=DEFAULT_MAX_CAP_MUSD, dest="max_cap",
                        help=f"Market cap ceiling, $M (default {DEFAULT_MAX_CAP_MUSD:.0f})")
    parser.add_argument("--max-analysts", type=int, default=DEFAULT_MAX_ANALYSTS, dest="max_analysts",
                        help=f"Analyst-count ceiling (default {DEFAULT_MAX_ANALYSTS})")
    parser.add_argument("--candidates-file", default=None, dest="candidates_file")
    args = parser.parse_args()

    # Finnhub's client scrubs its own key from error text (see news.py's
    # redact_token), but nothing else here should log at all -- keep output
    # to the report itself so it can be piped/pasted verbatim.
    logging.basicConfig(level=logging.WARNING, format="%(message)s")
    print(asyncio.run(screen(
        args.tickers,
        resolve_candidates_path(args.candidates_file),
        args.min_cap, args.max_cap, args.max_analysts,
    )))


if __name__ == "__main__":
    main()
