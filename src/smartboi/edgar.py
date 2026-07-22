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
import warnings
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import httpx
from bs4 import BeautifulSoup, XMLParsedAsHTMLWarning

log = logging.getLogger(__name__)

_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
_SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik10}.json"
# SEC's published fair-access guidance caps automated traffic at 10 req/sec;
# polling a few dozen symbols every hour never gets remotely close, but this
# still spaces requests out rather than bursting all of them at once.
_REQUEST_GAP_SEC = 0.3
# The ticker->CIK map gains new listings (IPOs, ticker changes) over time; a
# cache that never expires would leave those unresolvable forever ("no CIK
# found") on a long-lived deployment.
_CIK_CACHE_MAX_AGE = timedelta(days=7)

# Legal-entity suffixes stripped when matching a filing's free-text company
# name against SEC's registered title (see _normalize_company_name) --
# filing text rarely spells out a counterparty's full registered name
# ("ASML" vs "ASML Holding N.V."), so matching on the stripped core name is
# what makes the lookup useful at all.
_LEGAL_SUFFIXES = frozenset({
    "the", "inc", "incorporated", "corp", "corporation", "co", "company",
    "ltd", "limited", "llc", "lp", "plc", "holding", "holdings", "group",
    "ag", "sa", "nv", "se", "spa", "srl", "gmbh", "kk", "ab",
})

# Form 4 transaction codes worth spelling out for the dossier engine -- the
# raw code is kept alongside so nothing is lost for unmapped ones.
_FORM4_TRANSACTION_CODES = {
    "P": "open-market purchase",
    "S": "open-market sale",
    "A": "grant/award",
    "M": "option exercise",
    "F": "tax-withholding disposition",
    "G": "gift",
    "D": "disposition to issuer",
    "C": "conversion of derivative",
}


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
    def raw_document_url(self) -> str:
        """Like document_url, but with any XSL-rendering prefix stripped --
        Form 4 primaryDocument paths usually point at the human-readable
        rendering ("xslF345X05/form4.xml"); the same filename without the
        prefix is the raw XML the structured parser needs."""
        accession_nodash = self.accession_number.replace("-", "")
        raw_doc = re.sub(r"^xsl[^/]*/", "", self.primary_document)
        return f"https://www.sec.gov/Archives/edgar/data/{int(self.cik10)}/{accession_nodash}/{raw_doc}"

    @property
    def index_url(self) -> str:
        accession_nodash = self.accession_number.replace("-", "")
        return f"https://www.sec.gov/Archives/edgar/data/{int(self.cik10)}/{accession_nodash}/"


def _form4_value(node, tag: str) -> str:
    """Text of `<tag><value>x</value></tag>` (or bare `<tag>x</tag>`) under
    `node` -- html.parser lowercases tags, so `tag` must be lowercase."""
    found = node.find(tag)
    if found is None:
        return ""
    value = found.find("value")
    return (value or found).get_text(strip=True)


def _fmt_shares(raw: str) -> str:
    try:
        return f"{float(raw):,.0f}"
    except ValueError:
        return raw


def _normalize_company_name(name: str) -> str:
    """Lowercased, punctuation-stripped, legal-suffix-stripped core of a
    company name -- "ASML Holding N.V." and "ASML" both normalize toward
    "asml" (a leading/trailing suffix strip in each direction), which is
    what makes matching a filing's free-text counterparty name against
    SEC's registered title (see EdgarClient.find_ticker_by_name) actually
    useful instead of requiring an exact string match that rarely occurs."""
    # Periods are dropped outright (not turned into a space-separator) so
    # "N.V." collapses to "nv" -- a real legal-suffix token -- rather than
    # splitting into the two meaningless tokens "n" and "v".
    text = name.lower().replace(".", "")
    text = re.sub(r"[^\w\s]", " ", text)
    words = re.sub(r"\s+", " ", text).strip().split()
    while words and words[-1] in _LEGAL_SUFFIXES:
        words.pop()
    while words and words[0] in _LEGAL_SUFFIXES:
        words.pop(0)
    return " ".join(words)


def summarize_form4(xml_text: str) -> str:
    """Compact, human/LLM-readable summary of a Form 4's non-derivative
    transactions ("insider X (CFO) open-market purchase of 10,000 shares at
    $12.34..."). Returns "" when the text isn't parseable as Form 4 XML --
    the caller then falls back to plain text extraction rather than feeding
    raw XML to the dossier engine as 'evidence'."""
    with warnings.catch_warnings():
        # html.parser on XML is deliberate (lenient, and avoids an lxml
        # dependency) -- it just lowercases tags, which _form4_value expects.
        warnings.simplefilter("ignore", XMLParsedAsHTMLWarning)
        soup = BeautifulSoup(xml_text, "html.parser")
    owner = _form4_value(soup, "rptownername")
    if not owner:
        return ""

    roles = []
    relationship = soup.find("reportingownerrelationship")
    if relationship is not None:
        if _form4_value(relationship, "isdirector") in ("1", "true"):
            roles.append("director")
        officer_title = _form4_value(relationship, "officertitle")
        if officer_title:
            roles.append(officer_title)
        elif _form4_value(relationship, "isofficer") in ("1", "true"):
            roles.append("officer")
        if _form4_value(relationship, "istenpercentowner") in ("1", "true"):
            roles.append("10% owner")
    role = ", ".join(roles) or "insider"

    lines = []
    for txn in soup.find_all("nonderivativetransaction"):
        code = _form4_value(txn, "transactioncode")
        desc = _FORM4_TRANSACTION_CODES.get(code, f"transaction code {code}")
        shares = _fmt_shares(_form4_value(txn, "transactionshares"))
        price = _form4_value(txn, "transactionpricepershare")
        date = _form4_value(txn, "transactiondate")
        owned_after = _fmt_shares(_form4_value(txn, "sharesownedfollowingtransaction"))
        line = f"{date}: {desc} of {shares} shares"
        if price:
            line += f" at ${price}"
        if owned_after:
            line += f" ({owned_after} shares owned after)"
        lines.append(line)

    if not lines:
        return (
            f"Form 4 insider filing by {owner} ({role}): no non-derivative "
            "transactions reported (derivative/option activity only)."
        )
    return f"Form 4 insider transactions by {owner} ({role}): " + "; ".join(lines)


class EdgarClient:
    def __init__(self, user_agent: str, cache_path: Path):
        headers = {"User-Agent": user_agent, "Accept-Encoding": "gzip, deflate"}
        self._client = httpx.AsyncClient(headers=headers, timeout=20.0)
        self._cache_path = cache_path
        self._cik_by_ticker: dict[str, str] | None = None
        self._ticker_by_name: dict[str, str] | None = None
        self._map_loaded_at: datetime | None = None
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

    async def _ticker_map(self) -> tuple[dict[str, str], dict[str, str]]:
        """Returns (ticker -> CIK10, normalized_name -> ticker) -- both
        built from the same cached company_tickers.json fetch, so the
        name index used for find_ticker_by_name costs nothing extra."""
        now = datetime.now(timezone.utc)
        if self._cik_by_ticker is not None and self._map_loaded_at is not None:
            if now - self._map_loaded_at < _CIK_CACHE_MAX_AGE:
                return self._cik_by_ticker, self._ticker_by_name

        cached: dict | None = None
        cache_fresh = False
        if self._cache_path.exists():
            try:
                raw = json.loads(self._cache_path.read_text())
                if isinstance(raw, dict) and "map" in raw:
                    cached = raw
                    fetched_at = raw.get("fetched_at", "")
                    cache_fresh = fetched_at >= (now - _CIK_CACHE_MAX_AGE).isoformat()
                else:
                    cached = {"map": raw, "names": {}}  # legacy flat format, no names yet -> refresh
            except (json.JSONDecodeError, OSError):
                pass

        if cached is not None and cache_fresh:
            self._cik_by_ticker = cached["map"]
            self._ticker_by_name = cached.get("names", {})
            self._map_loaded_at = now
            return self._cik_by_ticker, self._ticker_by_name

        try:
            response = await self._throttled_get(_TICKERS_URL)
            raw = response.json()
            ticker_map = {row["ticker"].upper(): str(row["cik_str"]).zfill(10) for row in raw.values()}
            name_map = {}
            for row in raw.values():
                normalized = _normalize_company_name(row.get("title", ""))
                if normalized:
                    name_map.setdefault(normalized, row["ticker"].upper())
            self._cache_path.parent.mkdir(parents=True, exist_ok=True)
            self._cache_path.write_text(
                json.dumps({"fetched_at": now.isoformat(), "map": ticker_map, "names": name_map})
            )
        except (httpx.HTTPError, KeyError, ValueError) as exc:
            if cached is None:
                raise
            log.warning("Could not refresh EDGAR's ticker map (%s) -- using the stale cached copy.", exc)
            ticker_map, name_map = cached["map"], cached.get("names", {})
        self._cik_by_ticker = ticker_map
        self._ticker_by_name = name_map
        self._map_loaded_at = now
        return ticker_map, name_map

    async def cik_for(self, symbol: str) -> str | None:
        ticker_map, _ = await self._ticker_map()
        return ticker_map.get(symbol.upper())

    async def find_ticker_by_name(self, company_name: str) -> str | None:
        """Best-effort match of a free-text company name (as written in a
        filing, e.g. "ASML") against SEC's registered filer titles (e.g.
        "ASML Holding N.V.") -- used to backfill a ticker for relationship-
        extraction candidates the model didn't recognize (see engine.py's
        _record_universe_candidate), using the same cached
        company_tickers.json already fetched for CIK lookups, so this costs
        no extra request. Best-effort and US-listed-SEC-filer only: private
        companies, foreign private issuers that don't file with the SEC,
        and generic entity descriptions never resolve, which is correct --
        there is no ticker to find."""
        _, name_map = await self._ticker_map()
        normalized = _normalize_company_name(company_name)
        if not normalized:
            return None
        if normalized in name_map:
            return name_map[normalized]
        for title, ticker in name_map.items():
            if title.startswith(normalized + " ") or normalized.startswith(title + " "):
                return ticker
        return None

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

    async def latest_filing(self, symbol: str, form: str) -> FilingEvent | None:
        """The most recent filing of `form` for `symbol`, REGARDLESS of age
        (within the submissions endpoint's ~1000-filing recent block) --
        used by the one-time relationship backfill (engine.py), which needs
        last year's 10-K, not just whatever landed inside the rolling
        poll lookback window."""
        cik10 = await self.cik_for(symbol)
        if cik10 is None:
            log.warning("%s: no CIK found in EDGAR's ticker map -- cannot backfill.", symbol)
            return None
        try:
            response = await self._throttled_get(_SUBMISSIONS_URL.format(cik10=cik10))
        except httpx.HTTPError as exc:
            log.warning("%s: EDGAR submissions fetch failed: %s", symbol, exc)
            return None
        recent = response.json().get("filings", {}).get("recent", {})
        # The recent block is newest-first, so the first match is the latest.
        for filing_form, filing_date, accession, primary_doc in zip(
            recent.get("form", []),
            recent.get("filingDate", []),
            recent.get("accessionNumber", []),
            recent.get("primaryDocument", []),
        ):
            if filing_form == form:
                return FilingEvent(symbol, cik10, filing_form, filing_date, accession, primary_doc)
        return None

    async def fetch_evidence_text(self, filing: FilingEvent) -> str:
        """Evidence-ready text for a filing. Form 4s get a structured
        summary of the insider transactions (the raw filing is XML --
        useless noise as LLM 'evidence'); everything else gets plain-text
        extraction of the primary document (see fetch_text)."""
        if filing.form == "4":
            try:
                response = await self._throttled_get(filing.raw_document_url)
            except httpx.HTTPError as exc:
                log.warning("%s: could not fetch Form 4 XML %s: %s", filing.symbol, filing.raw_document_url, exc)
                return ""
            summary = summarize_form4(response.text)
            if summary:
                return summary
            log.warning("%s: Form 4 %s did not parse -- falling back to plain text.", filing.symbol, filing.accession_number)
        return await self.fetch_text(filing)

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
