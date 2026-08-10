"""EDGAR full-text search -- inverting the disclosure asymmetry.

WHY THIS EXISTS. Every graph edge comes from filing text, and filing text is
one-directional in a way that structurally starves this strategy. A small
company's 10-K names its big CUSTOMERS, because customer concentration is a
disclosable risk. A giant's 10-K does not enumerate its small suppliers -- no
rule requires it and no supplier is material enough to mention. So the
direction the strategy needs ("which thinly-covered names move when this
anchor moves") is exactly the direction filings do not disclose.

`research.py` attacks that with web search. This attacks it with EDGAR
itself, and from the other end: instead of reading the anchor's filings and
hoping a supplier is named, it asks **which other filers name our anchor**.
A supplier disclosing "Applied Materials accounted for 22% of net sales" is
making exactly the disclosure the anchor never would -- and full-text search
is the only mechanism that finds it.

WHAT IT DELIBERATELY DOES NOT DO -- and this is the argument, not a dodge.
It produces **candidates only**, never evidence:

- If the filer is already in the universe, `_poll_edgar` has already fetched
  that 10-K and run extraction on it. A hit adds nothing.
- If the filer is NOT in the universe, there is no dossier to write to.

So a hit is a lead about WHERE TO LOOK, and it routes to
`research.merge_into_candidates` exactly like a web-researched supplier does,
inheriting ticker resolution, the market-cap screen and the dashboard's
Accept button. Once accepted, the symbol's own filings are backfilled and the
edge is created from a primary source. Research decides where to look; EDGAR
still decides what is true.

It also never touches `seen_count`. That counter gates auto-accept as a TRADE
TARGET at `auto_accept_min_seen_count`, and it is meant to count independent
filing DISCLOSURES of a relationship -- not sightings of a company name in a
search index.

ON THE QUERY. EFTS has no proximity operator: quoted phrases and implicit AND
only. So the anchor name and the concentration language are ANDed at the
document level, which over-matches (a 10-K can name Applied Materials in one
section and say "of our net sales" in a completely unrelated one). The
proximity test is done locally instead, over the fetched text, in
`concentration_context`. Document-level AND narrows 500,000 filings to a
handful; the local pass decides which of the handful is real.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass

log = logging.getLogger(__name__)

_SEARCH_URL = "https://efts.sec.gov/LATEST/search-index"

# Phrases a filer uses when disclosing revenue concentration on a named
# counterparty. These are the sentences worth finding: they are quantified,
# primary, and name the exact relationship the anchor's own filings omit.
CONCENTRATION_PHRASES = (
    "accounted for",
    "of our net sales",
    "of net revenue",
    "of total revenue",
    "of our revenues",
    "our largest customer",
    "a significant customer",
)

# How close the anchor name must sit to concentration language for a hit to
# count, in characters. EFTS matched them anywhere in the document; this is
# the proximity operator it does not have. ~300 chars is a long sentence or a
# short paragraph -- wide enough for "Applied Materials, our largest customer,
# accounted for 22% of net sales in fiscal 2025", narrow enough to reject a
# name in Item 1 and a revenue sentence forty pages later.
PROXIMITY_CHARS = 300

# Hits to consider per anchor query. The interesting disclosures cluster at
# the top of the relevance ranking, and every hit past this costs a document
# fetch to proximity-test.
MAX_HITS_PER_QUERY = 10


@dataclass(frozen=True)
class SearchHit:
    """One EFTS document hit, deduped on accession number.

    EFTS returns one hit per DOCUMENT, not per filing, so a 10-K whose
    main body and an exhibit both match arrives twice with the same
    `adsh`. Deduping on it is what stops one filing counting as two leads."""

    adsh: str            # accession number, e.g. "0000320193-23-000106"
    cik: str
    company: str         # display name as EDGAR renders it, e.g. "ACME CORP (ACME)"
    form: str
    filing_date: str
    # The matched document's filename, taken from the "<adsh>:<document>"
    # _id. Carried because the archive URL needs it and nothing else in the
    # response supplies it -- without it a hit can be deduped and reported
    # but never FETCHED, which is the whole second half of the pass.
    document: str = ""

    @property
    def ticker(self) -> str:
        """The ticker EDGAR appends to a display name, when it appends one.

        Display names look like "ACME CORP (ACME)" or, for a filer with no
        listed ticker, just "ACME CORP". Returns "" for the latter rather
        than guessing -- an unresolved candidate still gets ticker resolution
        downstream, and a WRONG ticker is the ATRO/Advantest failure mode."""
        match = re.search(r"\(([A-Z][A-Z0-9.\-]{0,6})\)\s*$", self.company.strip())
        return match.group(1) if match else ""

    @property
    def name(self) -> str:
        return re.sub(r"\s*\([A-Z][A-Z0-9.\-]{0,6}\)\s*$", "", self.company).strip()


def build_query(anchor_name: str) -> str:
    """The EFTS `q` for one anchor: the company name as a quoted phrase.

    Deliberately just the name. Adding the concentration phrases to the query
    ANDs them at DOCUMENT level, which does not mean what it looks like -- it
    admits any 10-K that mentions the anchor anywhere and uses the phrase
    anywhere -- while also excluding filers who disclose the same fact in
    different words. The narrowing that actually matters is the local
    proximity pass, so the query stays broad and cheap and the filtering
    happens where it can be done correctly."""
    return f'"{anchor_name}"'


def parse_hits(payload: object) -> list[SearchHit]:
    """EFTS's Elasticsearch-shaped response -> deduped hits.

    Defensive to the point of paranoia, on purpose. This response schema is
    not versioned, not documented as a public API contract, and can change
    without notice. Every field is read with a guard and any hit that does
    not yield an accession number is skipped, so a schema change degrades to
    ZERO CANDIDATES rather than to candidates built from misread fields. That
    asymmetry is the whole point: a missing lead costs nothing, while a
    garbage lead carrying a wrong ticker is the ATRO/Advantest failure mode
    pointed straight at the universe."""
    if isinstance(payload, (str, bytes)):
        try:
            payload = json.loads(payload)
        except (ValueError, TypeError):
            log.warning("EDGAR full-text search returned unparseable JSON -- no candidates.")
            return []
    if not isinstance(payload, dict):
        return []
    hits = payload.get("hits")
    if isinstance(hits, dict):
        hits = hits.get("hits")
    if not isinstance(hits, list):
        log.warning("EDGAR full-text search response had no hits list -- no candidates.")
        return []

    out: list[SearchHit] = []
    seen: set[str] = set()
    for raw in hits:
        if not isinstance(raw, dict):
            continue
        source = raw.get("_source")
        source = source if isinstance(source, dict) else {}
        # The accession is in _source on current responses, and recoverable
        # from the "<adsh>:<document>" _id when it is not.
        adsh = str(source.get("adsh") or "").strip()
        if not adsh:
            adsh = str(raw.get("_id") or "").split(":")[0].strip()
        if not adsh or adsh in seen:
            continue
        seen.add(adsh)
        raw_id = str(raw.get("_id") or "")
        out.append(SearchHit(
            adsh=adsh,
            cik=_first_str(source.get("ciks")),
            company=_first_str(source.get("display_names")),
            form=str(source.get("file_type") or source.get("root_form") or "").strip(),
            filing_date=str(source.get("file_date") or "").strip(),
            document=raw_id.split(":", 1)[1].strip() if ":" in raw_id else "",
        ))
    return out


def _first_str(value: object) -> str:
    """EFTS reports ciks/display_names as LISTS -- one filing can have several
    filers. The first is the primary one."""
    if isinstance(value, list):
        return str(value[0]).strip() if value else ""
    return str(value or "").strip()


def concentration_context(text: str, anchor_name: str,
                          proximity_chars: int = PROXIMITY_CHARS) -> str:
    """The sentence-ish window where the anchor name sits within
    `proximity_chars` of revenue-concentration language, or "".

    This is the proximity operator EFTS does not have. Document-level AND
    narrows the corpus to a handful; this decides which of the handful is a
    real disclosure rather than two unrelated mentions in one long filing.

    Returns the surrounding text verbatim rather than a verdict, so the
    operator reading the candidate sees the actual sentence -- an IDIQ
    ceiling, a historical figure, or a genuine concentration disclosure read
    very differently, and only the raw words distinguish them."""
    if not text or not anchor_name:
        return ""
    haystack = text.lower()
    needle = anchor_name.lower()
    for match in re.finditer(re.escape(needle), haystack):
        start = max(0, match.start() - proximity_chars)
        end = min(len(text), match.end() + proximity_chars)
        window = haystack[start:end]
        if any(phrase in window for phrase in CONCENTRATION_PHRASES):
            return text[start:end].strip()
    return ""


def search_url(anchor_name: str, forms: str = "10-K", date_from: str = "") -> str:
    """The GET URL for one anchor query.

    Scoped to 10-K by default: the concentration disclosure this looks for is
    an annual-report item, and leaving forms unscoped pulls in every 8-K that
    mentions the anchor in passing -- which is precisely the low-value bulk
    the local proximity pass would then have to fetch and reject one document
    at a time."""
    from urllib.parse import urlencode

    params = {"q": build_query(anchor_name), "forms": forms}
    if date_from:
        params["dateRange"] = "custom"
        params["startdt"] = date_from
    return f"{_SEARCH_URL}?{urlencode(params)}"
