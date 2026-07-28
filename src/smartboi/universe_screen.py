"""Monthly universe auto-screen: rechecks every tradeable symbol against
market-cap and analyst-coverage bounds so the universe doesn't rot into
stale picks (acquired, delisted, graduated to broad analyst coverage) --
see README point 1 and the user-provided watchlist's own note that several
entries on published small-cap lists turn out to be years-old acquisitions.

Deliberately prune-only: it can flag/deactivate a symbol that no longer
fits, but it never adds new ones. Auto-adding would require judging whether
a newly-discovered ticker actually belongs in one of the four hand-curated
ecosystems (semi_equipment, defense_tier2, grid_datacenter, battery_storage)
-- a real editorial judgment call, not a threshold check, and getting it
wrong silently (adding an unrelated small-cap because it happened to clear
a market-cap/analyst-count bar) is a worse failure mode than a human having
to add new candidates by hand. A future version could propose additions
for human review rather than apply them automatically."""
from __future__ import annotations

import logging
from dataclasses import dataclass

from smartboi.news import FinnhubClient
from smartboi.universe import CompanySpec

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class ScreenResult:
    symbol: str
    still_fits: bool
    reason: str
    market_cap_musd: float | None
    analyst_count: int | None


def _fits_thin_coverage_bounds(
    market_cap_musd: float | None,
    analyst_count: int | None,
    min_market_cap_musd: float,
    max_market_cap_musd: float,
    max_analyst_count: int,
) -> tuple[bool, str]:
    """Shared bounds check behind both screen_universe (does an EXISTING
    member still fit) and screen_candidate (does a NEW candidate fit) --
    same thresholds, same reasoning, written once."""
    if market_cap_musd is None:
        return False, "no market cap data (possibly delisted/acquired, or just not covered)"
    if not (min_market_cap_musd <= market_cap_musd <= max_market_cap_musd):
        return False, f"market cap ${market_cap_musd:.0f}M outside [{min_market_cap_musd:.0f}M, {max_market_cap_musd:.0f}M]"
    if analyst_count is not None and analyst_count > max_analyst_count:
        return False, f"{analyst_count} analysts exceeds the thin-coverage bound of {max_analyst_count}"
    return True, "within bounds"


async def screen_universe(
    universe: list[CompanySpec],
    finnhub: FinnhubClient,
    min_market_cap_musd: float,
    max_market_cap_musd: float,
    max_analyst_count: int,
) -> list[ScreenResult]:
    """Anchors (signal_source_only=True) are skipped -- they're deliberately
    large/heavily-covered by design (that's what makes their news worth
    propagating), so the small/mid-cap/thin-coverage bounds don't apply to
    them."""
    results = []
    for company in universe:
        if company.signal_source_only:
            continue
        market_cap = await finnhub.market_cap_musd(company.symbol)
        analysts = await finnhub.analyst_count(company.symbol)
        fits, reason = _fits_thin_coverage_bounds(
            market_cap, analysts, min_market_cap_musd, max_market_cap_musd, max_analyst_count
        )
        results.append(ScreenResult(company.symbol, fits, reason, market_cap, analysts))

    dropped = [r for r in results if not r.still_fits]
    if dropped:
        log.warning(
            "Universe screen: %d symbol(s) no longer fit the small/mid-cap thin-coverage criteria: %s",
            len(dropped), ", ".join(f"{r.symbol} ({r.reason})" for r in dropped),
        )
    return results


def recommend_candidate_type(
    market_cap_musd: float | None,
    analyst_count: int | None,
    min_market_cap_musd: float,
    max_market_cap_musd: float,
    max_analyst_count: int,
) -> tuple[str, str]:
    """Suggests "tradeable" or "anchor" for a resolved universe candidate --
    a "which Accept button" hint on the dashboard, not a guarantee. Reuses
    the exact same small/mid-cap thin-coverage bounds screen_universe
    applies to existing members: a big, heavily-covered name is exactly
    the profile this system treats as a news SOURCE (an anchor, never a
    trade target); a small/mid-cap, thinly-covered name is exactly what it
    looks for as a trade target. Returns ("unknown", reason) rather than
    guessing when there isn't enough data to judge."""
    if market_cap_musd is None:
        return "unknown", "no market cap data available"
    if market_cap_musd > max_market_cap_musd:
        return "anchor", f"market cap ${market_cap_musd:.0f}M exceeds the tradeable ceiling of ${max_market_cap_musd:.0f}M"
    if analyst_count is not None and analyst_count > max_analyst_count:
        return "anchor", f"{analyst_count} analysts already cover it -- past the thin-coverage bound of {max_analyst_count}"
    if market_cap_musd < min_market_cap_musd:
        return (
            "tradeable",
            f"market cap ${market_cap_musd:.0f}M is below the usual floor of ${min_market_cap_musd:.0f}M -- "
            "still tradeable, just smaller than the typical pick",
        )
    analyst_note = f"{analyst_count} analysts" if analyst_count is not None else "unknown analyst coverage"
    return "tradeable", f"market cap ${market_cap_musd:.0f}M and {analyst_note} fit the small/mid-cap thin-coverage profile"


@dataclass(frozen=True)
class CandidateScreenResult:
    symbol: str
    fits: bool
    reason: str
    market_cap_musd: float | None
    analyst_count: int | None
    ecosystem_guess: str


def screen_candidate(
    symbol: str,
    market_cap_musd: float | None,
    analyst_count: int | None,
    ecosystem_guess: str,
    min_market_cap_musd: float,
    max_market_cap_musd: float,
    max_analyst_count: int,
) -> CandidateScreenResult:
    """Pure fits/doesn't-fit check for a brand-new candidate joining the
    tradeable universe -- same bounds check as screen_universe, but takes
    already-fetched market data instead of making its own Finnhub calls,
    so a caller (see scripts/screen_candidates.py) can batch the fetch and
    this stays a plain, network-free function to test."""
    fits, reason = _fits_thin_coverage_bounds(
        market_cap_musd, analyst_count, min_market_cap_musd, max_market_cap_musd, max_analyst_count
    )
    return CandidateScreenResult(symbol, fits, reason, market_cap_musd, analyst_count, ecosystem_guess)


def guess_ecosystem(related_to: list[str], specs: dict) -> str:
    """A not-yet-classified candidate's ecosystem, guessed from the first
    already-classified company it was discovered in relation to -- e.g. a
    candidate disclosed as a customer of FORM (semi_equipment) is
    plausibly semi_equipment too. A cheap deterministic heuristic, never
    authoritative -- the owner makes the real editorial call on where (or
    whether) a candidate belongs."""
    for symbol in related_to:
        spec = specs.get(symbol)
        if spec is not None:
            return spec.ecosystem
    return "?"


def format_screening_report(results: list[CandidateScreenResult]) -> str:
    """Plain-text ranked table for a human to review -- thinnest coverage
    first (fewest analysts, then smallest cap), since that IS the edge
    thesis (README point 1: synthesis wins where news isn't already
    priced in). Excluded candidates are listed with their reason rather
    than silently dropped, so nothing looks like it was never screened."""
    fitting = sorted(
        (r for r in results if r.fits),
        key=lambda r: (r.analyst_count if r.analyst_count is not None else 999, r.market_cap_musd or 0),
    )
    excluded = [r for r in results if not r.fits]
    lines = [f"{'Symbol':<8}{'Cap ($M)':<12}{'Analysts':<10}{'Ecosystem':<16}", "-" * 46]
    for r in fitting:
        analysts = r.analyst_count if r.analyst_count is not None else "?"
        lines.append(f"{r.symbol:<8}{r.market_cap_musd:<12.0f}{str(analysts):<10}{r.ecosystem_guess:<16}")
    lines.append("")
    lines.append(f"{len(fitting)} fit the bounds, {len(excluded)} excluded:" if excluded else f"{len(fitting)} fit the bounds.")
    for r in excluded:
        lines.append(f"  {r.symbol}: {r.reason}")
    return "\n".join(lines)
