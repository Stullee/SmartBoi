"""SEC EDGAR ingestion: company filings (8-K material events, 10-K customer/
supplier disclosures, Form 4 insider transactions) as evidence for the
dossier engine -- see README point 4 ("read what nobody parses"). Uses
EDGAR's public JSON APIs directly, no third-party data vendor and no API
key, just a compliant User-Agent (SEC blocks/throttles requests that don't
identify a real contact -- see config.py's edgar_user_agent)."""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from dataclasses import dataclass
from pathlib import Path

import httpx
from bs4 import BeautifulSoup

log = logging.getLogger(__name__)

_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
_SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik10}.json"
# SEC's published fair-access guidance caps automated traffic at 10 req/sec;
# polling a few dozen symbols every hour never gets remotely close, but this
# still spaces requests out rather than bursting all of them at once.
_REQUEST_GAP_SEC = 0.3


@dataclass(frozen=True)
class FilingEvent:
    symbol: str
    cik10: str
    form: str
    filing_date: str
    accession_number: str
    primary_document: str

    @property
    def document_url(self) -> str:
        accession_nodash = self.accession_number.replace("-", "")
        return (
            f"https://www.sec.gov/Archives/edgar/data/{int(self.cik10)}/"
            f"{accession_nodash}/{self.primary_document}"
        )

    @property
    def index_url(self) -> str:
        accession_nodash = self.accession_number.replace("-", "")
        return f"https://www.sec.gov/Archives/edgar/data/{int(self.cik10)}/{accession_nodash}/"


class EdgarClient:
    def __init__(self, user_agent: str, cache_path: Path):
        headers = {"User-Agent": user_agent, "Accept-Encoding": "gzip, deflate"}
        self._client = httpx.AsyncClient(headers=headers, timeout=20.0)
        self._cache_path = cache_path
        self._cik_by_ticker: dict[str, str] | None = None
        self._last_request = 0.0

    async def _throttled_get(self, url: str) -> httpx.Response:
        now = time.monotonic()
        wait = _REQUEST_GAP_SEC - (now - self._last_request)
        if wait > 0:
            await asyncio.sleep(wait)
        self._last_request = time.monotonic()
        response = await self._client.get(url)
        response.raise_for_status()
        return response

    async def _ticker_map(self) -> dict[str, str]:
        if self._cik_by_ticker is not None:
            return self._cik_by_ticker
        if self._cache_path.exists():
            try:
                self._cik_by_ticker = json.loads(self._cache_path.read_text())
                return self._cik_by_ticker
            except (json.JSONDecodeError, OSError):
                pass
        response = await self._throttled_get(_TICKERS_URL)
        raw = response.json()
        mapping = {row["ticker"].upper(): str(row["cik_str"]).zfill(10) for row in raw.values()}
        self._cache_path.parent.mkdir(parents=True, exist_ok=True)
        self._cache_path.write_text(json.dumps(mapping))
        self._cik_by_ticker = mapping
        return mapping

    async def cik_for(self, symbol: str) -> str | None:
        mapping = await self._ticker_map()
        return mapping.get(symbol.upper())

    async def recent_filings(self, symbol: str, forms: set[str], since_date: str) -> list[FilingEvent]:
        """Filings for `symbol` on/after `since_date` (YYYY-MM-DD), restricted
        to `forms`. Only looks at the submissions endpoint's `recent` block
        (the last ~1000 filings) -- plenty for a poll loop that runs hourly
        and tracks its own cursor (see engine.py); a company with a filing
        gap longer than that would need the paginated older-filings files,
        not implemented here."""
        cik10 = await self.cik_for(symbol)
        if cik10 is None:
            log.warning("%s: no CIK found in EDGAR's ticker map -- skipping filings.", symbol)
            return []
        try:
            response = await self._throttled_get(_SUBMISSIONS_URL.format(cik10=cik10))
        except httpx.HTTPError as exc:
            log.warning("%s: EDGAR submissions fetch failed: %s", symbol, exc)
            return []
        data = response.json()
        recent = data.get("filings", {}).get("recent", {})
        events = []
        for form, filing_date, accession, primary_doc in zip(
            recent.get("form", []),
            recent.get("filingDate", []),
            recent.get("accessionNumber", []),
            recent.get("primaryDocument", []),
        ):
            if form not in forms or filing_date < since_date:
                continue
            events.append(FilingEvent(symbol, cik10, form, filing_date, accession, primary_doc))
        return events

    async def fetch_text(self, filing: FilingEvent, max_chars: int = 40_000) -> str:
        """Plain-text extraction of a filing's primary document, truncated --
        LLM extraction only needs the substantive body (customer/supplier
        mentions, item descriptions), not exhibits/boilerplate, and a hard
        cap keeps one filing from dominating an extraction call's context/
        cost regardless of the source document's actual length."""
        try:
            response = await self._throttled_get(filing.document_url)
        except httpx.HTTPError as exc:
            log.warning("%s: could not fetch filing document %s: %s", filing.symbol, filing.document_url, exc)
            return ""
        content_type = response.headers.get("content-type", "")
        if "html" in content_type or filing.primary_document.endswith((".htm", ".html")):
            soup = BeautifulSoup(response.text, "html.parser")
            text = soup.get_text(separator=" ")
        else:
            text = response.text
        text = re.sub(r"\s+", " ", text).strip()
        return text[:max_chars]

    async def aclose(self) -> None:
        await self._client.aclose()
