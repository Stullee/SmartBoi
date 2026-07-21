"""Finnhub ingestion: company news (free tier) as one evidence source for
the dossier engine -- see edgar.py for the other (SEC filings). Also used
for the universe auto-screen's market-cap/analyst-coverage checks
(universe_screen.py), since it's the one data provider already wired in
and both needs are covered by its free tier."""
from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone

import httpx

log = logging.getLogger(__name__)

_NEWS_URL = "https://finnhub.io/api/v1/company-news"
_PROFILE_URL = "https://finnhub.io/api/v1/stock/profile2"
_RECOMMENDATION_URL = "https://finnhub.io/api/v1/stock/recommendation"


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

    async def recent_news(self, symbol: str, from_date: str, to_date: str) -> list[NewsArticle]:
        """`from_date`/`to_date` are YYYY-MM-DD. Finnhub's free tier covers
        US-listed common stock only -- fine for this universe."""
        try:
            response = await self._client.get(
                _NEWS_URL,
                params={"symbol": symbol, "from": from_date, "to": to_date, "token": self._api_key},
            )
            response.raise_for_status()
        except httpx.HTTPError as exc:
            log.warning("%s: Finnhub news fetch failed: %s", symbol, exc)
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
            response = await self._client.get(_PROFILE_URL, params={"symbol": symbol, "token": self._api_key})
            response.raise_for_status()
        except httpx.HTTPError as exc:
            log.warning("%s: Finnhub profile fetch failed: %s", symbol, exc)
            return None
        return response.json().get("marketCapitalization")  # already millions USD per Finnhub's docs

    async def analyst_count(self, symbol: str) -> int | None:
        """Most recent month's total analyst recommendation count across
        all rating buckets -- a proxy for "how thinly covered is this"."""
        try:
            response = await self._client.get(_RECOMMENDATION_URL, params={"symbol": symbol, "token": self._api_key})
            response.raise_for_status()
        except httpx.HTTPError as exc:
            log.warning("%s: Finnhub recommendation-trend fetch failed: %s", symbol, exc)
            return None
        rows = response.json()
        if not rows:
            return None
        latest = rows[0]
        return sum(latest.get(k, 0) for k in ("strongBuy", "buy", "hold", "sell", "strongSell"))

    async def aclose(self) -> None:
        await self._client.aclose()
