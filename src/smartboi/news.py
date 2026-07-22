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

log = logging.getLogger(__name__)

_NEWS_URL = "https://finnhub.io/api/v1/company-news"
_PROFILE_URL = "https://finnhub.io/api/v1/stock/profile2"
_RECOMMENDATION_URL = "https://finnhub.io/api/v1/stock/recommendation"

# Finnhub's free tier allows 60 requests/minute -- a ~40-symbol universe
# polled as a burst blows through that partway in and 429s the rest of the
# list every single poll, systematically starving whichever symbols sort
# last. Spacing requests just under the budget keeps a full pass legal.
_REQUEST_GAP_SEC = 1.1
_MAX_ATTEMPTS = 3

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


def _epoch_to_iso(epoch: int | None) -> str:
    if not epoch:
        return ""
    return datetime.fromtimestamp(epoch, tz=timezone.utc).isoformat()


class FinnhubClient:
    def __init__(self, api_key: str):
        self._api_key = api_key
        self._client = httpx.AsyncClient(timeout=15.0)
        self._last_request = 0.0

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
