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
# Retry a transient SEC response with backoff before giving up, mirroring the
# Finnhub client. Without this a momentary SEC hiccup dropped that filing for
# the poll -- recovered next cycle (the fingerprint stays unregistered), but a
# first-ever run during an SEC blip had nothing cached to fall back on.
_MAX_ATTEMPTS = 3
# 429 rate-limit and 503 maintenance are the classic two. 403 and 500 are here
# because SEC's hosts do not answer overload uniformly: the full-text search
# host (efts.sec.gov) answers a rate-exceeded request with 403 rather than 429,
# and returns sporadic 500s under load. Both were previously passed straight to
# raise_for_status, so a rate-limited search looked identical to a permanent
# refusal and its results were silently dropped rather than retried.
#
# 403 is deliberately retried rather than treated as fatal. A genuine
# permanent 403 -- a malformed User-Agent, which SEC does enforce -- costs
# three attempts and then raises exactly as before, so the only price of
# treating it as transient is a few seconds on a request that was going to
# fail anyway. The reverse error, treating a throttle as permanent, silently
# loses data.
_RETRYABLE_STATUS = frozenset({403, 429, 500, 503})
# The ticker->CIK map gains new listings (IPOs, ticker changes) over time; a
# cache that never expires would leave those unresolvable forever ("no CIK
# found") on a long-lived deployment.
_CIK_CACHE_MAX_AGE = timedelta(days=7)

# Legal-entity suffixes stripped when matching a filing's free-text company
# name against SEC's registered title (see normalize_company_name) --
# filing text rarely spells out a counterparty's full registered name
# ("ASML" vs "ASML Holding N.V."), so matching on the stripped core name is
# what makes the lookup useful at all.
_LEGAL_SUFFIXES = frozenset({
    "the", "inc", "incorporated", "corp", "corporation", "co", "company",
    "ltd", "limited", "llc", "lp", "plc", "holding", "holdings", "group",
    "ag", "sa", "nv", "se", "spa", "srl", "gmbh", "kk", "ab",
})

# US state (and DC) abbreviations, for SEC's state-of-incorporation marker --
# see strip_state_of_incorporation.
_STATE_OF_INCORPORATION = frozenset({
    "ak", "al", "ar", "az", "ca", "co", "ct", "dc", "de", "del", "fl", "ga",
    "hi", "ia", "id", "il", "in", "ks", "ky", "la", "ma", "md", "me", "mi",
    "mn", "mo", "ms", "mt", "nc", "nd", "ne", "nh", "nj", "nm", "nv", "ny",
    "oh", "ok", "or", "pa", "ri", "sc", "sd", "tn", "tx", "ut", "va", "vt",
    "wa", "wi", "wv", "wy",
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


# What an 8-K's item codes MEAN, spelled out for the dossier engine.
#
# The submissions endpoint already returns these per filing (recent["items"],
# e.g. "1.01,9.01") and they were being thrown away. They are the cheapest
# high-signal metadata EDGAR offers: they say what KIND of event a filing
# reports before a single byte of the document is fetched, and the
# difference between "Item 1.01 Entry into a Material Definitive Agreement"
# (a contract win -- exactly the catalyst this system exists to trade) and
# "Item 5.02 Departure of Directors" (usually noise) is the difference
# between a thesis and a wasted LLM call.
#
# Only the codes that carry real directional signal are spelled out; an
# unmapped code is still passed through verbatim so nothing is silently lost.
EIGHT_K_ITEMS = {
    "1.01": "Entry into a Material Definitive Agreement (a new contract, order, supply agreement or partnership)",
    "1.02": "Termination of a Material Definitive Agreement (a contract or customer relationship ending)",
    "1.03": "Bankruptcy or Receivership",
    "1.05": "Material Cybersecurity Incident",
    "2.01": "Completion of Acquisition or Disposition of Assets",
    "2.02": "Results of Operations and Financial Condition (earnings release, and any guidance in it)",
    "2.03": "Creation of a Material Direct Financial Obligation (new debt)",
    "2.04": "Triggering Event Accelerating a Financial Obligation (a covenant breach or acceleration)",
    "2.05": "Costs Associated with Exit or Disposal Activities (restructuring)",
    "2.06": "Material Impairment (a write-down)",
    "3.01": "Notice of Delisting or Failure to Satisfy a Listing Rule",
    "3.02": "Unregistered Sales of Equity Securities (dilution)",
    "4.01": "Changes in Registrant's Certifying Accountant (an auditor change)",
    "4.02": "Non-Reliance on Previously Issued Financial Statements (a restatement)",
    "5.01": "Changes in Control of Registrant",
    "5.02": "Departure or Election of Directors or Principal Officers",
    "7.01": "Regulation FD Disclosure (usually the company's own press release, attached as an exhibit)",
    "8.01": "Other Events (usually a press release the company chose to file: product launches, contract awards, operational updates)",
    "9.01": "Financial Statements and Exhibits (the exhibit list -- the substance is in the attached exhibit)",
}

# Exhibit document types whose content is the actual news. EX-99.x is where
# a company files its own press release: the product launch, the contract
# award, the earnings release with the guidance in it. EX-10.x is the text
# of the material agreement itself when Item 1.01 is reported.
#
# This is the single most consequential thing EDGAR ingestion was missing.
# An 8-K's PRIMARY document is a cover page -- checkbox boilerplate plus one
# sentence reading "On [date] the Company issued a press release... a copy is
# attached hereto as Exhibit 99.1 and is incorporated herein by reference."
# Fetching only that (which is what fetch_text did) handed the dossier engine
# a filing that says a press release exists and not one word of what it said,
# so the updater correctly judged it "not new information" and the catalyst
# was discarded. Every product release, contract award and guidance revision
# this system's universe announced went into the bin this way.
_EVIDENCE_EXHIBIT_PREFIXES = ("EX-99", "EX-10")
# Filename fallback for when the directory listing carries no usable type
# field -- EDGAR filenames for these exhibits are overwhelmingly of the form
# "ex991.htm" / "ex-99_1.htm" / "dco-ex991_6.htm".
_EXHIBIT_NAME_RE = re.compile(r"ex[-_]?(99|10)[-_.]?\d*", re.IGNORECASE)
_TEXTUAL_EXTENSIONS = (".htm", ".html", ".txt")


def describe_8k_items(items: str) -> str:
    """Human/LLM-readable expansion of an 8-K's comma-separated item codes.
    Unmapped codes pass through verbatim rather than being dropped."""
    codes = [c.strip() for c in (items or "").split(",") if c.strip()]
    if not codes:
        return ""
    return "; ".join(f"Item {c}: {EIGHT_K_ITEMS[c]}" if c in EIGHT_K_ITEMS else f"Item {c}" for c in codes)


@dataclass(frozen=True)
class FilingEvent:
    symbol: str
    cik10: str
    form: str
    filing_date: str
    accession_number: str
    primary_document: str
    # Comma-separated 8-K item codes as EDGAR reports them ("1.01,9.01").
    # Empty for every other form. Defaulted so existing construction sites
    # (and tests) that don't care keep working unchanged.
    items: str = ""

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

    def document_url_for(self, filename: str) -> str:
        return f"{self.index_url}{filename}"


def _truncate_head_tail(text: str, max_chars: int) -> str:
    """Head + tail truncation instead of a flat prefix: most of the budget
    from the front, a meaningful slice from the back, joined with a marker
    showing where the middle was dropped. Used for filing text where the
    highest-value content (financial statement notes) often sits near the
    end of a long document -- see EdgarClient.fetch_text. A no-op (returns
    text unchanged) when it already fits."""
    if len(text) <= max_chars:
        return text
    marker = " [...document middle omitted...] "
    head_chars = (max_chars - len(marker)) * 2 // 3
    tail_chars = max_chars - len(marker) - head_chars
    return text[:head_chars] + marker + text[-tail_chars:]


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


def normalize_company_name(name: str) -> str:
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


def strip_state_of_incorporation(normalized: str) -> str:
    """An already-normalized SEC title with its state-of-incorporation marker
    removed -- "danaher corp de" -> "danaher corp".

    SEC's registered titles carry the state of incorporation as a trailing
    "/DE/" ("DANAHER CORP /DE/", "DIODES INC /DEL/", "TRACTOR SUPPLY CO /DE/").
    normalize_company_name drops the slashes, leaving a bare "de"/"del" token
    that no filing's free-text name ever contains -- so the two sides can never
    match, however correct the pair is. Live, that refused seven verified pairs
    outright (BAC CLB DHR DIOD DVN IEX TSCO), all of which become EXACT matches
    once the marker is gone.

    Only stripped where it FOLLOWS a legal-form token, because that is the only
    shape SEC emits. The guard matters: "in", "co", "de", "or", "ok", "hi" and
    "me" are all real words as well as state codes, and an unconditional strip
    would quietly truncate a company genuinely named for one.

    Deliberately a separate function rather than a change to
    normalize_company_name, which builds the name INDEX. Folding it in there
    collapses "INDEPENDENT BANK CORP /MI/" and "INDEPENDENT BANK CORP" onto one
    key -- measured against the live filer list, two such collisions (IBCP/INDB
    and CIA/CIZN) -- and a collision DROPS a ticker from the index, which is the
    wrong-ticker outcome this whole guard exists to prevent. Applied at
    comparison time it can only ever ADD a match, never lose an entry."""
    words = normalized.split()
    if len(words) >= 2 and words[-1] in _STATE_OF_INCORPORATION and words[-2] in _LEGAL_SUFFIXES:
        words.pop()
    return " ".join(words)


def boilerplate_only_prefix(a: str, b: str) -> bool:
    """Whether two already-normalized names differ ONLY by corporate-form
    boilerplate -- "asml" vs "asml holding" -- as opposed to by words that
    identify a different company or a different KIND OF INSTRUMENT.

    Both callers below used to accept any token-prefix in either direction,
    which is far too generous once normalize_company_name has stripped the
    legal suffixes. Because it strips them, a name that survives as a single
    token is a bare brand word, and a bare brand word prefix-matches every
    registered title that happens to start with it. Confirmed live, and this
    is the doctrine's own named worst outcome (a ticker pointing at an
    unrelated company):

        "PGIM, Inc."  -> "pgim"  -> matched "pgim high yield bond fund"
                                    -> GHY, a closed-end BOND FUND, accepted
                                       as a tradeable equity

    and it is the same mechanism behind the "Vertex" collision dod_contracts.py
    warns about ("vertex" would match "vertex aerospace" as readily as Vertex
    Pharmaceuticals).

    The rule: one name must be a whole-token prefix of the other, AND every
    remaining token must be corporate-form boilerplate. "high yield bond
    fund" and "aerospace" fail it, because those words are doing identifying
    work. This can only ever REFUSE a match the old code accepted -- it never
    creates one.

    Worth being precise about what that leaves, because it is stricter than
    it first looks. Both callers pass names that have ALREADY been through
    normalize_company_name, which strips leading and trailing legal suffixes
    -- so neither side can normally still END in boilerplate, and the branch
    is left firing only on interior boilerplate and on exact equality. In
    particular "ASML" vs "ASML Holding N.V." keeps resolving because both
    normalize to "asml" and match EXACTLY, not because the prefix allowance
    rescues it. That is the intended end state: near-exact matching against
    SEC's registered titles, with Finnhub's fuzzy /search as the deliberate
    place where brand-vs-legal-name mismatches get handled instead.

    The cost is accepted rather than overlooked. name_matches_ticker gates
    auto-accept, and a refusal there is durable (engine.py records
    auto_accept_blocked and reads it back on later passes), so a legitimate
    pair this refuses needs a human click on the dashboard -- accept_candidate
    does not consult this function. That is the correct direction for a guard
    whose own docstring says unknown means "don't": a candidate waiting for a
    human is recoverable, a trade fired against the wrong company is not."""
    a_words, b_words = a.split(), b.split()
    if not a_words or not b_words:
        return False
    shorter, longer = sorted((a_words, b_words), key=len)
    if longer[:len(shorter)] != shorter:
        return False
    return all(word in _LEGAL_SUFFIXES for word in longer[len(shorter):])


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
        response: httpx.Response | None = None
        for attempt in range(_MAX_ATTEMPTS):
            now = time.monotonic()
            wait = _REQUEST_GAP_SEC - (now - self._last_request)
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_request = time.monotonic()
            response = await self._client.get(url)
            if response.status_code not in _RETRYABLE_STATUS:
                response.raise_for_status()
                return response
            retry_after = float(response.headers.get("Retry-After") or 0)
            delay = max(retry_after, 2.0 * (attempt + 1))
            log.warning("SEC EDGAR returned %d -- backing off %.0fs.", response.status_code, delay)
            await asyncio.sleep(delay)
        response.raise_for_status()  # still retryable after N attempts -> raise to the caller
        return response

    async def full_text_search(self, anchor_name: str, forms: str = "10-K",
                               date_from: str = "") -> list:
        """Which OTHER filers name this anchor -- see edgar_search.py for why
        that is the only mechanism that inverts the disclosure asymmetry.

        Returns deduped SearchHits, or [] on any failure. Never raises: this
        feeds candidate discovery, which is a nice-to-have, and a search
        outage must not be able to interrupt a poll that is doing real work.

        Uses the same throttle and retry set as every other SEC call. The
        retry set matters more here than anywhere else -- efts.sec.gov answers
        rate-exceeded with 403 rather than 429, so before _RETRYABLE_STATUS
        included it a throttled search was indistinguishable from a permanent
        refusal and its hits were silently dropped."""
        from smartboi.edgar_search import parse_hits, search_url

        try:
            response = await self._throttled_get(search_url(anchor_name, forms, date_from))
        except Exception:  # noqa: BLE001 - a failed lead search is never worth a raise
            log.exception("EDGAR full-text search failed for %r", anchor_name)
            return []
        try:
            return parse_hits(response.json())
        except ValueError:
            log.warning("EDGAR full-text search returned a non-JSON body for %r.", anchor_name)
            return []

    def filing_from_hit(self, hit, symbol: str = "") -> FilingEvent:
        """A SearchHit -> the FilingEvent shape fetch_text already knows how
        to read, so the proximity pass reuses the existing document fetch
        rather than growing a second one.

        `primary_document` MUST carry the hit's document filename. document_url
        is built as <archives>/<cik>/<accession>/<primary_document>, so an
        empty one produces a directory URL that always 404s -- which silently
        made the proximity pass unable to fetch anything at all, and therefore
        made the whole search yield zero candidates no matter what EFTS
        returned. That is exactly why SearchHit carries `document`."""
        return FilingEvent(
            symbol=symbol or hit.ticker or hit.name,
            cik10=hit.cik,
            form=hit.form,
            filing_date=hit.filing_date,
            accession_number=hit.adsh,
            primary_document=hit.document,
        )

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
                normalized = normalize_company_name(row.get("title", ""))
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
        normalized = normalize_company_name(company_name)
        if not normalized:
            return None
        if normalized in name_map:
            return name_map[normalized]
        # Exact-on-the-stripped-title before any prefix allowance: an exact
        # match is the strongest evidence available, and taking it first stops
        # a weaker prefix hit on an unrelated filer from winning the race
        # through a dict whose order is arbitrary.
        for title, ticker in name_map.items():
            if strip_state_of_incorporation(title) == normalized:
                return ticker
        for title, ticker in name_map.items():
            if boilerplate_only_prefix(title, normalized):
                return ticker
            if boilerplate_only_prefix(strip_state_of_incorporation(title), normalized):
                return ticker
        return None

    async def live_tickers(self) -> set[str] | None:
        """Every ticker SEC currently lists a registered filer for, or None if
        the map could not be loaded.

        None is not an empty set, and the distinction is the whole point: a
        caller using this to spot delisted symbols would, on an unreachable
        endpoint, otherwise conclude that EVERY symbol is dead and propose
        emptying the universe. Reuses the same cached company_tickers.json the
        CIK lookups already fetch, so this costs no extra request."""
        try:
            cik_by_ticker, _ = await self._ticker_map()
        except Exception as exc:  # noqa: BLE001 - unknown must never read as "all dead"
            log.warning("Could not load SEC's ticker map for the liveness check: %s", exc)
            return None
        return set(cik_by_ticker) or None

    async def name_matches_ticker(self, company_name: str, ticker: str) -> bool:
        """Whether `ticker`'s registered SEC filer name is actually the same
        company as the free-text `company_name` a filing named.

        This is the guard against acting on a WRONG ticker. Confirmed live:
        a PDF Solutions filing describing a partnership with *Advantest* (a
        Japanese test-equipment maker, not a US filer) ended up recorded
        against ATRO -- Astronics, an unrelated aerospace company. A
        counterparty ticker can be wrong whether the model supplied it
        directly or find_ticker_by_name resolved it, so verification is done
        against SEC's own registered title rather than by trusting either
        source. Matching is on the normalized core name (see
        normalize_company_name), with a prefix allowance in both directions
        so "ASML" still matches the registered "ASML Holding N.V." -- but
        only where the difference is pure corporate-form boilerplate, since
        an unrestricted prefix match is itself a source of wrong tickers
        (see boilerplate_only_prefix).

        Returns False when the ticker isn't in SEC's map at all -- unknown
        is not a match, and a caller gating an automatic action on this
        should treat it as "don't"."""
        _, name_map = await self._ticker_map()
        normalized = normalize_company_name(company_name)
        if not normalized:
            return False
        ticker = ticker.upper()
        registered = [title for title, mapped in name_map.items() if mapped == ticker]
        for title in registered:
            # Each registered title is tried both as SEC wrote it and with the
            # state-of-incorporation marker removed. The stripped form is only
            # ever MORE permissive by exactly that marker, so this cannot admit
            # a pair the boilerplate rule would otherwise refuse on identifying
            # words -- "total" still fails against "total return securities
            # fund", which has no marker to strip.
            for candidate in {title, strip_state_of_incorporation(title)}:
                if candidate == normalized:
                    return True
                if boilerplate_only_prefix(candidate, normalized):
                    return True
        return False

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
        # `items` is zip-padded rather than zipped directly: EDGAR populates
        # it only for 8-Ks, and on some submissions payloads the list is
        # shorter than the others. A bare zip() would silently TRUNCATE the
        # whole filing list to the length of the shortest column and drop
        # real filings, so the columns are indexed instead.
        forms_col = recent.get("form", [])
        dates_col = recent.get("filingDate", [])
        accessions_col = recent.get("accessionNumber", [])
        docs_col = recent.get("primaryDocument", [])
        items_col = recent.get("items", [])
        for i in range(min(len(forms_col), len(dates_col), len(accessions_col), len(docs_col))):
            form, filing_date = forms_col[i], dates_col[i]
            if form not in forms or filing_date < since_date:
                continue
            events.append(FilingEvent(
                symbol, cik10, form, filing_date, accessions_col[i], docs_col[i],
                items=items_col[i] if i < len(items_col) else "",
            ))
        return events

    async def latest_filing_result(self, symbol: str, form: str) -> tuple[FilingEvent | None, str]:
        """`(filing, outcome)` where outcome is one of:

          "ok"          -- found it
          "absent"      -- this filer genuinely has no filing of this form in
                           the recent block (foreign issuer, fresh IPO). The
                           only PERMANENT answer of the three failures.
          "no_cik"      -- the ticker did not resolve to a CIK
          "fetch_error" -- EDGAR was unreachable or errored

        The distinction exists because the caller writes a permanent "nothing
        to extract, and never will be" marker on a miss, and the three cases
        are not the same claim at all. A ticker that fails to resolve, or an
        EDGAR outage, was being recorded as a structural impossibility --
        forever, on the first attempt. Live consequence: 24 live-universe
        symbols carrying that marker, including a cached CIK for XOM
        (0002115436) that is not Exxon's (0000034088), so Exxon's own 10-K had
        never been read while the state file said there was nothing to read."""
        cik10 = await self.cik_for(symbol)
        if cik10 is None:
            log.warning("%s: no CIK found in EDGAR's ticker map -- cannot backfill.", symbol)
            return None, "no_cik"
        try:
            response = await self._throttled_get(_SUBMISSIONS_URL.format(cik10=cik10))
        except httpx.HTTPError as exc:
            log.warning("%s: EDGAR submissions fetch failed: %s", symbol, exc)
            return None, "fetch_error"
        recent = response.json().get("filings", {}).get("recent", {})
        for filing_form, filing_date, accession, primary_doc in zip(
            recent.get("form", []),
            recent.get("filingDate", []),
            recent.get("accessionNumber", []),
            recent.get("primaryDocument", []),
        ):
            if filing_form == form:
                return FilingEvent(symbol, cik10, filing_form, filing_date, accession, primary_doc), "ok"
        return None, "absent"

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

    async def evidence_exhibits(self, filing: FilingEvent, limit: int = 3) -> list[str]:
        """Filenames of the substantive exhibits attached to this filing --
        the press releases (EX-99.x) and material agreements (EX-10.x) whose
        content is the actual news. See _EVIDENCE_EXHIBIT_PREFIXES.

        Read from the accession's `index.json` directory listing, which costs
        one small request and is available for every filing. Resolution is
        deliberately belt-and-braces: EDGAR's listing carries a `type` field
        holding the EDGAR document type ("EX-99.1", "8-K", "GRAPHIC"), but
        it is not guaranteed present on every historical accession, so a
        filename pattern (ex991.htm, ex-99_1.htm, abc-ex991_6.htm) is used as
        a fallback. Both paths require a textual extension, so the logos and
        XBRL sidecars in the same folder are never fetched.

        Bounded by `limit` because an 8-K can attach a dozen exhibits and the
        per-evidence path truncates to a few thousand characters anyway --
        past the first few there is nothing left to spend on them. Never
        raises: an unavailable index degrades to "no exhibits", i.e. exactly
        the primary-document-only behaviour this replaced."""
        try:
            response = await self._throttled_get(f"{filing.index_url}index.json")
            items = response.json().get("directory", {}).get("item", [])
        except (httpx.HTTPError, ValueError, AttributeError) as exc:
            log.warning("%s: could not list exhibits for %s: %s",
                        filing.symbol, filing.accession_number, exc)
            return []

        primary = filing.primary_document.rsplit("/", 1)[-1].lower()
        found: list[str] = []
        for item in items:
            name = (item.get("name") or "") if isinstance(item, dict) else ""
            if not name.lower().endswith(_TEXTUAL_EXTENSIONS) or name.lower() == primary:
                continue
            doc_type = str(item.get("type") or "").upper()
            typed = doc_type.startswith(_EVIDENCE_EXHIBIT_PREFIXES)
            # The filename fallback only applies when `type` gave no opinion
            # at all -- when EDGAR HAS typed the document and typed it as
            # something else, that is authoritative and a coincidental
            # filename match must not override it.
            named = not doc_type and bool(_EXHIBIT_NAME_RE.search(name))
            if typed or named:
                found.append(name)
            if len(found) >= limit:
                break
        return found

    async def fetch_evidence_text(self, filing: FilingEvent) -> str:
        """Evidence-ready text for a filing.

        Form 4s get a structured summary of the insider transactions (the raw
        filing is XML -- useless noise as LLM 'evidence').

        Everything else gets the primary document PLUS its substantive
        exhibits, and for an 8-K a plain-English expansion of its item codes
        on top (see EIGHT_K_ITEMS / describe_8k_items). Ordering is gated on
        form type. For an 8-K the exhibit is put FIRST, because it is the news
        and the primary document is a cover page: _process_filing re-truncates
        this to a few thousand head-weighted characters, so anything behind the
        boilerplate would be cut off. This is the difference between the dossier
        engine reading "the Company issued a press release, attached as Exhibit
        99.1" and reading the press release. But for a 10-K/10-Q/424B5/SC 13D
        the primary document IS the substance (MD&A, customer-concentration
        notes, the shelf terms), so it leads and the exhibits follow -- putting
        a routine EX-10 contract first and labeling the 10-K a "cover document"
        pushed the filing's own disclosures into the truncated tail."""
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

        primary_text = await self.fetch_text(filing)
        sections: list[str] = []

        item_description = describe_8k_items(filing.items) if filing.form.startswith("8-K") else ""

        exhibit_sections: list[str] = []
        for name in await self.evidence_exhibits(filing):
            exhibit_text = await self.fetch_document_text(filing.document_url_for(name))
            if exhibit_text:
                exhibit_sections.append(f"--- Attached exhibit ({name}) ---\n{exhibit_text}")

        # No exhibits and no item expansion -> unchanged: primary document only.
        if not exhibit_sections and not item_description:
            return primary_text

        if item_description:
            sections.append(f"This 8-K reports: {item_description}")
        if filing.form.startswith("8-K"):
            # 8-K: the exhibit is the news; the primary is a cover page, so it
            # trails (and can fall into the truncated tail without loss).
            sections.extend(exhibit_sections)
            if primary_text:
                sections.append(f"--- Filing cover document ---\n{primary_text}")
        else:
            # 10-K/10-Q/424B5/SC 13D: the primary IS the substance -- it leads
            # so its front matter survives head+tail truncation, exhibits after.
            if primary_text:
                sections.append(f"--- Filing document ({filing.form}) ---\n{primary_text}")
            sections.extend(exhibit_sections)

        if not sections:
            return primary_text
        return "\n\n".join(sections)

    async def fetch_text(self, filing: FilingEvent, max_chars: int = 150_000) -> str:
        """Plain-text extraction of a filing's primary document, truncated --
        a hard cap keeps one filing from dominating an extraction call's
        context/cost regardless of the source document's actual length.

        150k chars (~35-40k tokens) rather than a tighter cap: a modern
        10-K's extracted text commonly runs 300k-800k chars. Even so, taken
        from the FRONT alone this still cuts off well before the notes to
        financial statements where the quantitative customer-concentration
        disclosures relationship extraction actually wants ("Customer X
        accounted for 22% of revenue") usually sit, for anything much past
        150k chars -- so this takes HEAD + TAIL instead of a flat prefix:
        most of the budget from the front (Item 1 business description,
        MD&A) and a meaningful slice from the back (financial statement
        notes), joined with a marker showing where the middle was dropped.
        This only costs more on the relatively rare extraction path
        (10-K/10-Q, annual/quarterly, plus the one-time backfill): the
        per-evidence dossier-update path (engine.py) re-truncates to 4000
        chars regardless of what this returns, so raising this does NOT
        increase the high-frequency per-article cost the daily LLM call
        budget is guarding against."""
        return await self.fetch_document_text(filing.document_url, max_chars=max_chars)

    async def fetch_document_text(self, url: str, max_chars: int = 150_000) -> str:
        """Plain-text extraction of ONE document in a filing's folder, by
        URL. Split out from fetch_text so exhibits (see evidence_exhibits)
        go through exactly the same HTML-stripping and truncation as the
        primary document, rather than a parallel implementation that could
        drift from it."""
        try:
            response = await self._throttled_get(url)
        except httpx.HTTPError as exc:
            log.warning("Could not fetch filing document %s: %s", url, exc)
            return ""
        content_type = response.headers.get("content-type", "")
        if "html" in content_type or url.lower().endswith((".htm", ".html")):
            soup = BeautifulSoup(response.text, "html.parser")
            text = soup.get_text(separator=" ")
        else:
            text = response.text
        text = re.sub(r"\s+", " ", text).strip()
        return _truncate_head_tail(text, max_chars)

    async def aclose(self) -> None:
        await self._client.aclose()
