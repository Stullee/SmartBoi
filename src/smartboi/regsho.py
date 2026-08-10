"""Reg SHO threshold securities list -- the borrow observable.

FINRA/Nasdaq publish, once per settlement day, the list of securities with
persistent failures to deliver (five consecutive settlement days at 10,000+
shares and 0.5% of shares outstanding, SEC Rule 203(b)(3)). Appearing on it
is the closest thing to a public, free, daily statement that a name is
genuinely hard to borrow.

Why this exists: paper_journal.assumes_borrow currently answers "could a real
account have located shares for this SHORT" with a market-cap PROXY -- below
$500M, assume not. That proxy is a guess standing in for something directly
observable, and it is wrong in both directions: plenty of sub-$500M names
borrow freely all day, and a squeezed mid-cap on the threshold list does not.
The dashboard splits its R statistics with and without the borrow assumption,
so the split is only as meaningful as the flag driving it.

Free, no auth, no key. One plain-text file per day, a few hundred rows.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta, timezone

import httpx

log = logging.getLogger(__name__)

# Nasdaq publishes the consolidated file covering all listing venues, so one
# fetch covers NYSE/AMEX/Nasdaq names alike.
_URL_TEMPLATE = "https://www.nasdaqtrader.com/dynamic/symdir/regsho/nasdaqth{yyyymmdd}.txt"

# The file is published per SETTLEMENT day and lags: a request for today
# frequently 404s well into the session. Walking back a few days and using the
# most recent file that exists is correct rather than lax -- threshold status
# is persistent by construction (it takes five consecutive fail days to get on
# the list, and five clean ones to come off), so a two-day-old file is a good
# answer and no file at all is not.
_MAX_LOOKBACK_DAYS = 6

_TIMEOUT_SEC = 20.0


class RegShoClient:
    """Fetches and caches one day's threshold list.

    Deliberately fail-open on the fetch and fail-CLOSED on the meaning: a
    fetch that does not resolve leaves the list EMPTY, and an empty list makes
    is_threshold() return False for everything, which sends the caller back to
    the market-cap proxy rather than silently declaring every short borrowable.
    """

    def __init__(self, client: httpx.AsyncClient | None = None) -> None:
        self._client = client
        self._owns_client = client is None
        self._symbols: set[str] = set()
        self._as_of: str = ""

    async def _get(self, url: str) -> httpx.Response | None:
        client = self._client
        if client is None:
            client = httpx.AsyncClient(timeout=_TIMEOUT_SEC)
            self._client = client
        try:
            response = await client.get(url)
        except Exception:  # noqa: BLE001 - a missing borrow observable is a no-op, never a crash
            log.exception("Reg SHO fetch failed for %s", url)
            return None
        if response.status_code == 404:
            return None  # not published for that day -- walk further back
        if response.status_code >= 400:
            log.warning("[REGSHO] HTTP %d for %s -- body starts: %s",
                        response.status_code, url, response.text[:160])
            return None
        return response

    @staticmethod
    def _parse(text: str) -> set[str]:
        """Pipe-delimited, one header row, one trailing total row.

        Columns: Date|Symbol|SecurityName|Market|RegSHOThresholdFlag|... The
        symbol is column 1. Rows that do not split into at least two fields
        (the header, the trailing record count) are skipped rather than
        parsed, so a format tweak degrades to fewer symbols rather than to
        garbage symbols."""
        symbols: set[str] = set()
        for line in text.splitlines():
            parts = line.split("|")
            if len(parts) < 3:
                continue
            symbol = parts[1].strip().upper()
            # The header row's second field is literally "Symbol"; a real
            # ticker is alphanumeric with optional dot/dash class suffix.
            if not symbol or symbol == "SYMBOL":
                continue
            if not all(c.isalnum() or c in ".-" for c in symbol):
                continue
            symbols.add(symbol)
        return symbols

    async def refresh(self, today: date | None = None) -> bool:
        """Loads the most recent published list. True when one was found.

        Keeps the previous list on failure: threshold status is persistent, so
        yesterday's answer is far better than no answer, and the alternative
        (clearing it) would silently flip every short back to the cap proxy on
        one bad fetch."""
        today = today or datetime.now(timezone.utc).date()
        # What was actually tried, so a failure is diagnosable from the log.
        # Without this the only output was "not found in the last 6 days",
        # which cannot distinguish a wrong URL from a blocked host from a file
        # that genuinely is not published -- three problems with three
        # different fixes. Confirmed live: 48 consecutive failures said
        # nothing about which URL or which status.
        attempts: list[str] = []
        for back in range(_MAX_LOOKBACK_DAYS):
            day = today - timedelta(days=back)
            if day.weekday() >= 5:
                continue  # no settlement file on weekends
            url = _URL_TEMPLATE.format(yyyymmdd=day.strftime("%Y%m%d"))
            response = await self._get(url)
            if response is None:
                attempts.append(f"{url} -> no response/404")
                continue
            symbols = self._parse(response.text)
            if not symbols:
                # A 200 that parses to nothing is the most misleading failure
                # of the three: the host is up, the URL is right, and the
                # FORMAT changed. The body prefix is what says so.
                attempts.append(
                    f"{url} -> HTTP 200 but 0 symbols parsed; body starts: "
                    f"{response.text[:160]!r}"
                )
                continue
            self._symbols = symbols
            self._as_of = day.isoformat()
            log.info("[REGSHO] Loaded %d threshold securities as of %s (%s).",
                     len(symbols), self._as_of, url)
            return True
        log.warning(
            "[REGSHO] No threshold list found in the last %d day(s) -- keeping the previous list "
            "(%d symbol(s), as of %s). Borrow flags fall back to the market-cap proxy. Tried:\n  %s",
            _MAX_LOOKBACK_DAYS, len(self._symbols), self._as_of or "never",
            "\n  ".join(attempts) or "(no business days in range)",
        )
        return False

    def is_threshold(self, symbol: str) -> bool:
        return symbol.upper() in self._symbols

    @property
    def as_of(self) -> str:
        return self._as_of

    @property
    def count(self) -> int:
        return len(self._symbols)

    async def aclose(self) -> None:
        if self._owns_client and self._client is not None:
            await self._client.aclose()
            self._client = None
