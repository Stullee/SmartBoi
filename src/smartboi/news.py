"""Finnhub ingestion: company news (free tier) as one evidence source for
the dossier engine -- see edgar.py for the other (SEC filings). Also used
for the universe auto-screen's market-cap/analyst-coverage checks
(universe_screen.py), since it's the one data provider already wired in
and both needs are covered by its free tier."""
from __future__ import annotations

import asyncio
import logging
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone

import httpx

from smartboi.edgar import normalize_company_name

log = logging.getLogger(__name__)

_NEWS_URL = "https://finnhub.io/api/v1/company-news"
_PROFILE_URL = "https://finnhub.io/api/v1/stock/profile2"
_RECOMMENDATION_URL = "https://finnhub.io/api/v1/stock/recommendation"
_SEARCH_URL = "https://finnhub.io/api/v1/search"

# Finnhub's free tier allows 60 requests/minute -- a ~40-symbol universe
# polled as a burst blows through that partway in and 429s the rest of the
# list every single poll, systematically starving whichever symbols sort
# last. Spacing requests just under the budget keeps a full pass legal.
_REQUEST_GAP_SEC = 1.1
_MAX_ATTEMPTS = 3
# Consecutive plan-rejections of /search before it's treated as unavailable
# rather than as a per-query failure. Small, because the signal is
# unambiguous -- a plan either includes the endpoint or rejects everything.
_SEARCH_UNAVAILABLE_AFTER = 3


def _is_plan_rejection(exc: Exception) -> bool:
    """A 422/401/403 from Finnhub means "this plan/request isn't allowed",
    not "no result for that query" -- an absent company answers 200 with an
    empty result list."""
    response = getattr(exc, "response", None)
    return getattr(response, "status_code", None) in (401, 403, 422)

# Finnhub puts the API key in the query string, so any logged exception
# containing the request URL (httpx includes it) would leak the key into
# the add-on log -- scrub it before it reaches a log line.
_TOKEN_RE = re.compile(r"token=[^&\s'\"]+")


def redact_token(text: object) -> str:
    return _TOKEN_RE.sub("token=REDACTED", str(text))


@dataclass(frozen=True)
class NewsArticle:
    symbol: str
    headline: str
    summary: str
    source: str
    url: str
    published_at: str  # ISO datetime, UTC


def _best_search_match(results: list[dict], normalized_query: str) -> str | None:
    """First /search result whose own name plausibly matches the query --
    Finnhub's relevance ranking on an unconstrained free-text query can't
    be trusted blindly, so this is a real filter, not a formality. Skips
    anything without a plain US ticker (a "." in the symbol means a
    foreign-exchange listing, e.g. "005930.KS") or that isn't a common
    stock (ETFs, indices, OTC junk that shows up in the same search)."""
    for row in results:
        symbol = row.get("symbol") or ""
        if not symbol or "." in symbol or row.get("type") != "Common Stock":
            continue
        normalized_result = normalize_company_name(row.get("description") or "")
        if not normalized_result:
            continue
        if (
            normalized_result == normalized_query
            or normalized_result.startswith(normalized_query + " ")
            or normalized_query.startswith(normalized_result + " ")
        ):
            return symbol.upper()
    return None


def _epoch_to_iso(epoch: int | None) -> str:
    if not epoch:
        return ""
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat()


class FinnhubClient:
    def __init__(self, api_key: str):
        self._api_key = api_key
        self._client = httpx.AsyncClient(timeout=15.0)
        self._last_request = 0.0
        # See search_ticker_by_name: /search isn't on every Finnhub plan, and
        # an excluded plan rejects every query rather than saying so once.
        self._search_422s = 0
        self._search_unavailable = False

    async def _throttled_get(self, url: str, params: dict) -> httpx.Response:
        response: httpx.Response | None = None
        for attempt in range(_MAX_ATTEMPTS):
            now = time.monotonic()
            wait = _REQUEST_GAP_SEC - (now - self._last_request)
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_request = time.monotonic()
            response = await self._client.get(url, params={**params, "token": self._api_key})
            if response.status_code != 429:
                response.raise_for_status()
                return response
            retry_after = float(response.headers.get("Retry-After") or 0)
            delay = max(retry_after, 20.0 * (attempt + 1))
            log.warning("Finnhub rate limit hit -- backing off %.0fs.", delay)
            await asyncio.sleep(delay)
        response.raise_for_status()  # still 429 after retries -> raise to the caller
        return response

    async def recent_news(self, symbol: str, from_date: str, to_date: str) -> list[NewsArticle]:
        """`from_date`/`to_date` are YYYY-MM-DD. Finnhub's free tier covers
        US-listed common stock only -- fine for this universe."""
        try:
            response = await self._throttled_get(
                _NEWS_URL, {"symbol": symbol, "from": from_date, "to": to_date}
            )
        except httpx.HTTPError as exc:
            log.warning("%s: Finnhub news fetch failed: %s", symbol, redact_token(exc))
            return []
        articles = []
        for row in response.json():
            headline = row.get("headline") or ""
            if not headline:
                continue
            articles.append(
                NewsArticle(
                    symbol=symbol,
                    headline=headline,
                    summary=row.get("summary") or "",
                    source=row.get("source") or "unknown",
                    url=row.get("url") or "",
                    published_at=_epoch_to_iso(row.get("datetime")),
                )
            )
        return articles

    async def search_ticker_by_name(self, company_name: str) -> str | None:
        """Fuzzy company-name -> US ticker fallback for when EdgarClient.
        find_ticker_by_name's exact/prefix match against SEC's own
        registered filer TITLE misses -- e.g. brand-name-vs-legal-name
        mismatches ("Google" vs "Alphabet Inc") or common abbreviations
        ("IBM" vs "International Business Machines Corp") that a strict
        SEC-title match can't catch. Finnhub's /search is the same fuzzy,
        brand-aware lookup its own autocomplete uses -- free tier, no extra
        integration or cost beyond one more throttled request."""
        if self._search_unavailable:
            return None
        normalized_query = normalize_company_name(company_name)
        if not normalized_query:
            return None
        try:
            response = await self._throttled_get(_SEARCH_URL, {"q": company_name})
        except httpx.HTTPError as exc:
            # /search is not included in every Finnhub plan, and an excluded
            # plan answers 422 for every query rather than saying so once.
            # Confirmed live: every search failed this way, including
            # unambiguous names ("Eastman Kodak Company"), while news and
            # profile calls on the same key kept working. Left unchecked that
            # is one guaranteed-to-fail, rate-limited request per unresolved
            # candidate per day -- ~two minutes of the shared 60/min budget
            # spent achieving nothing, plus a warning apiece burying real
            # problems. So repeated 422s trip a breaker for the process.
            if _is_plan_rejection(exc):
                self._search_422s += 1
                if self._search_422s >= _SEARCH_UNAVAILABLE_AFTER:
                    self._search_unavailable = True
                    log.warning(
                        "Finnhub ticker search rejected %d requests in a row -- it is not available on this "
                        "API plan. Disabling it for this process; EDGAR's own filer-name lookup still runs, "
                        "so candidates resolve slightly less often but nothing else changes.",
                        self._search_422s,
                    )
                    return None
            log.warning("Ticker search failed for %r: %s", company_name, redact_token(exc))
            return None
        self._search_422s = 0
        return _best_search_match(response.json().get("result", []), normalized_query)

    async def market_cap_musd(self, symbol: str) -> float | None:
        try:
            response = await self._throttled_get(_PROFILE_URL, {"symbol": symbol})
        except httpx.HTTPError as exc:
            log.warning("%s: Finnhub profile fetch failed: %s", symbol, redact_token(exc))
            return None
        return response.json().get("marketCapitalization")  # already millions USD per Finnhub's docs

    async def analyst_count(self, symbol: str) -> int | None:
        """Most recent month's total analyst recommendation count across
        all rating buckets -- a proxy for "how thinly covered is this"."""
        try:
            response = await self._throttled_get(_RECOMMENDATION_URL, {"symbol": symbol})
        except httpx.HTTPError as exc:
            log.warning("%s: Finnhub recommendation-trend fetch failed: %s", symbol, redact_token(exc))
            return None
        rows = response.json()
        if not rows:
            return None
        latest = rows[0]
        return sum(latest.get(k, 0) for k in ("strongBuy", "buy", "hold", "sell", "strongSell"))

    async def aclose(self) -> None:
        await self._client.aclose()
