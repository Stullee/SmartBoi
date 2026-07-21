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

        if market_cap is None:
            results.append(ScreenResult(company.symbol, False, "no market cap data (possibly delisted/acquired)", None, analysts))
            continue
        if not (min_market_cap_musd <= market_cap <= max_market_cap_musd):
            results.append(ScreenResult(
                company.symbol, False,
                f"market cap ${market_cap:.0f}M outside [{min_market_cap_musd:.0f}M, {max_market_cap_musd:.0f}M]",
                market_cap, analysts,
            ))
            continue
        if analysts is not None and analysts > max_analyst_count:
            results.append(ScreenResult(
                company.symbol, False,
                f"{analysts} analysts exceeds the thin-coverage bound of {max_analyst_count}",
                market_cap, analysts,
            ))
            continue
        results.append(ScreenResult(company.symbol, True, "within bounds", market_cap, analysts))

    dropped = [r for r in results if not r.still_fits]
    if dropped:
        log.warning(
            "Universe screen: %d symbol(s) no longer fit the small/mid-cap thin-coverage criteria: %s",
            len(dropped), ", ".join(f"{r.symbol} ({r.reason})" for r in dropped),
        )
    return results
