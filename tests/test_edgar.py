import httpx

from smartboi.edgar import (
    EIGHT_K_ITEMS,
    EdgarClient,
    FilingEvent,
    describe_8k_items,
    summarize_form4,
)

_FORM4_XML = """<?xml version="1.0"?>
<ownershipDocument>
  <issuer>
    <issuerTradingSymbol>UCTT</issuerTradingSymbol>
  </issuer>
  <reportingOwner>
    <reportingOwnerId>
      <rptOwnerName>Doe Jane</rptOwnerName>
    </reportingOwnerId>
    <reportingOwnerRelationship>
      <isDirector>0</isDirector>
      <isOfficer>1</isOfficer>
      <officerTitle>Chief Financial Officer</officerTitle>
    </reportingOwnerRelationship>
  </reportingOwner>
  <nonDerivativeTable>
    <nonDerivativeTransaction>
      <transactionDate><value>2026-07-20</value></transactionDate>
      <transactionCoding><transactionCode>P</transactionCode></transactionCoding>
      <transactionAmounts>
        <transactionShares><value>10000</value></transactionShares>
        <transactionPricePerShare><value>12.34</value></transactionPricePerShare>
        <transactionAcquiredDisposedCode><value>A</value></transactionAcquiredDisposedCode>
      </transactionAmounts>
      <postTransactionAmounts>
        <sharesOwnedFollowingTransaction><value>50000</value></sharesOwnedFollowingTransaction>
      </postTransactionAmounts>
    </nonDerivativeTransaction>
  </nonDerivativeTable>
</ownershipDocument>
"""


def test_summarize_form4_produces_readable_evidence():
    summary = summarize_form4(_FORM4_XML)
    assert "Doe Jane" in summary
    assert "Chief Financial Officer" in summary
    assert "open-market purchase" in summary
    assert "10,000 shares" in summary
    assert "$12.34" in summary
    assert "50,000 shares owned after" in summary
    assert "<" not in summary  # no XML leaks through as 'evidence'


def test_summarize_form4_rejects_non_form4_text():
    assert summarize_form4("<html><body>Some 8-K press release</body></html>") == ""
    assert summarize_form4("plain text, not xml at all") == ""


def test_summarize_form4_no_transactions_still_summarizes():
    xml = """<ownershipDocument>
      <reportingOwner>
        <reportingOwnerId><rptOwnerName>Doe John</rptOwnerName></reportingOwnerId>
        <reportingOwnerRelationship><isDirector>1</isDirector></reportingOwnerRelationship>
      </reportingOwner>
    </ownershipDocument>"""
    summary = summarize_form4(xml)
    assert "Doe John" in summary
    assert "director" in summary
    assert "no non-derivative transactions" in summary


def test_raw_document_url_strips_xsl_rendering_prefix():
    filing = FilingEvent(
        symbol="UCTT", cik10="0001275014", form="4", filing_date="2026-07-20",
        accession_number="0001275014-26-000001", primary_document="xslF345X05/form4.xml",
    )
    assert filing.raw_document_url.endswith("000127501426000001/form4.xml")
    assert "xslF345X05" not in filing.raw_document_url
    assert "xslF345X05" in filing.document_url


# --- Name-based ticker resolution (universe candidate backfill) ---

import json

from smartboi.edgar import EdgarClient, normalize_company_name


def test_normalize_company_name_strips_legal_suffixes():
    assert normalize_company_name("ASML Holding N.V.") == "asml"
    assert normalize_company_name("The Boeing Company") == "boeing"
    assert normalize_company_name("Applied Materials, Inc.") == "applied materials"


def test_normalize_company_name_handles_plain_names():
    assert normalize_company_name("ASML") == "asml"
    assert normalize_company_name("") == ""


async def _seed_cache(tmp_path, tickers_and_titles):
    cache_path = tmp_path / "cik_cache.json"
    ticker_map = {t.upper(): "0000000001" for t, _ in tickers_and_titles}
    name_map = {}
    for ticker, title in tickers_and_titles:
        name_map[normalize_company_name(title)] = ticker.upper()
    cache_path.write_text(json.dumps({
        "fetched_at": "2026-07-22T00:00:00+00:00",
        "map": ticker_map,
        "names": name_map,
    }))
    return cache_path


async def test_find_ticker_by_name_exact_match(tmp_path):
    cache_path = await _seed_cache(tmp_path, [("ASML", "ASML Holding N.V.")])
    client = EdgarClient("test test@example.com", cache_path)
    try:
        ticker = await client.find_ticker_by_name("ASML")
        assert ticker == "ASML"
    finally:
        await client.aclose()


async def test_find_ticker_by_name_prefix_match(tmp_path):
    cache_path = await _seed_cache(tmp_path, [("BA", "The Boeing Company")])
    client = EdgarClient("test test@example.com", cache_path)
    try:
        ticker = await client.find_ticker_by_name("Boeing")
        assert ticker == "BA"
    finally:
        await client.aclose()


async def test_find_ticker_by_name_no_match_returns_none(tmp_path):
    cache_path = await _seed_cache(tmp_path, [("ASML", "ASML Holding N.V.")])
    client = EdgarClient("test test@example.com", cache_path)
    try:
        ticker = await client.find_ticker_by_name("Some Private Company LLC")
        assert ticker is None
    finally:
        await client.aclose()


# --- Head+tail truncation ---

from smartboi.edgar import _truncate_head_tail


def test_truncate_head_tail_noop_when_already_fits():
    assert _truncate_head_tail("short text", 1000) == "short text"


def test_truncate_head_tail_keeps_front_and_back():
    text = "HEAD" + ("x" * 1000) + "TAIL"
    result = _truncate_head_tail(text, 100)
    assert result.startswith("HEAD")
    assert result.endswith("TAIL")
    assert "[...document middle omitted...]" in result
    assert len(result) <= 100


def test_truncate_head_tail_respects_budget_ratio():
    text = "A" * 10000
    result = _truncate_head_tail(text, 150)
    marker = " [...document middle omitted...] "
    head, _, tail = result.partition(marker)
    # 2:1 head:tail split of the budget.
    assert len(head) > len(tail)
    assert len(result) <= 150


# --- Verifying a resolved ticker actually IS the disclosed company ---
# The guard behind auto-accepting a candidate as tradeable: confirmed live,
# a filing describing a partnership with *Advantest* ended up recorded
# against ATRO (Astronics), an unrelated aerospace company.

class _StubbedNameMap(EdgarClient):
    def __init__(self, name_map):
        self._name_map = name_map

    async def _ticker_map(self):
        return {}, self._name_map


async def test_name_matches_ticker_accepts_the_registered_name():
    client = _StubbedNameMap({"astronics": "ATRO"})
    assert await client.name_matches_ticker("Astronics Corporation", "ATRO") is True


async def test_name_matches_ticker_allows_a_prefix_in_either_direction():
    """Filing text rarely spells out a registered name, so a short form has
    to match a longer registered title and vice versa."""
    # Registered title longer than the disclosed name.
    client = _StubbedNameMap({"asml holding": "ASML"})
    assert await client.name_matches_ticker("ASML", "ASML") is True
    # Disclosed name longer than the registered title (legal suffixes like
    # "Holding"/"N.V." are stripped by normalization, so this ends up exact).
    client = _StubbedNameMap({"asml": "ASML"})
    assert await client.name_matches_ticker("ASML Holding N.V.", "ASML") is True


async def test_name_matches_ticker_rejects_a_different_company():
    """Advantest is not Astronics -- this is the case that must return False."""
    client = _StubbedNameMap({"astronics": "ATRO"})
    assert await client.name_matches_ticker("Advantest", "ATRO") is False


async def test_name_matches_ticker_rejects_an_unknown_ticker():
    """Unknown is not a match: a caller gating an automatic action on this
    must treat "not in SEC's map" as "don't"."""
    client = _StubbedNameMap({"astronics": "ATRO"})
    assert await client.name_matches_ticker("Some Company", "NOPE") is False


async def test_name_matches_ticker_rejects_an_empty_name():
    client = _StubbedNameMap({"astronics": "ATRO"})
    assert await client.name_matches_ticker("", "ATRO") is False


# --- 8-K item codes: the cheapest high-signal metadata EDGAR offers, and it
# was being discarded. "Item 1.01 Entry into a Material Definitive
# Agreement" is a contract win; "Item 5.02 Departure of Directors" is
# usually noise. Telling the dossier engine which it is costs nothing --
# the submissions endpoint already returns it. ---

def test_describe_8k_items_expands_known_codes():
    described = describe_8k_items("1.01,9.01")
    assert "Material Definitive Agreement" in described
    assert "Item 9.01" in described


def test_describe_8k_items_passes_unknown_codes_through():
    assert describe_8k_items("1.01,6.66") == (
        f"Item 1.01: {EIGHT_K_ITEMS['1.01']}; Item 6.66"
    )


def test_describe_8k_items_is_empty_for_no_items():
    assert describe_8k_items("") == ""
    assert describe_8k_items(None) == ""


def test_filing_event_items_default_to_empty():
    filing = FilingEvent("DCO", "0000029669", "8-K", "2026-07-28", "0000029669-26-000012", "d8k.htm")
    assert filing.items == ""


# --- Evidence exhibits: an 8-K's PRIMARY document is a cover page reading
# "a press release is attached as Exhibit 99.1". Fetching only that handed
# the dossier engine a filing that says news exists and not one word of what
# it said. These tests pin the exhibit resolution that fixes it. ---

class _FakeResponse:
    def __init__(self, payload=None, text="", headers=None):
        self._payload = payload
        self.text = text
        self.headers = headers or {}

    def json(self):
        if self._payload is None:
            raise ValueError("not json")
        return self._payload


class _FakeEdgar(EdgarClient):
    """EdgarClient with the network replaced by a url -> response dict."""

    def __init__(self, responses, tmp_path):
        super().__init__("SmartBoi test test@example.com", tmp_path / "cik.json")
        self.responses = responses
        self.requested = []

    async def _throttled_get(self, url):
        self.requested.append(url)
        if url not in self.responses:
            raise httpx.HTTPError(f"no canned response for {url}")
        return self.responses[url]


def _index(items):
    return _FakeResponse(payload={"directory": {"item": items}})


_FILING = FilingEvent(
    "DCO", "0000029669", "8-K", "2026-07-28", "0000029669-26-000012", "d8k.htm", items="1.01,9.01",
)
_BASE = _FILING.index_url


async def test_evidence_exhibits_finds_press_release_by_type(tmp_path):
    client = _FakeEdgar({
        f"{_BASE}index.json": _index([
            {"name": "d8k.htm", "type": "8-K"},
            {"name": "logo.jpg", "type": "GRAPHIC"},
            {"name": "d919283dex991.htm", "type": "EX-99.1"},
        ]),
    }, tmp_path)
    assert await client.evidence_exhibits(_FILING) == ["d919283dex991.htm"]


async def test_evidence_exhibits_falls_back_to_the_filename_when_untyped(tmp_path):
    client = _FakeEdgar({
        f"{_BASE}index.json": _index([
            {"name": "d8k.htm"},
            {"name": "ex-99_1.htm"},
        ]),
    }, tmp_path)
    assert await client.evidence_exhibits(_FILING) == ["ex-99_1.htm"]


async def test_a_declared_type_beats_a_coincidental_filename_match(tmp_path):
    """An EDGAR-typed document is authoritative -- a filename that happens to
    contain "ex99" must not smuggle in something typed as an XBRL sidecar."""
    client = _FakeEdgar({
        f"{_BASE}index.json": _index([
            {"name": "dco-ex991_htm.xml", "type": "XML"},
            {"name": "R99.htm", "type": "GRAPHIC"},
        ]),
    }, tmp_path)
    assert await client.evidence_exhibits(_FILING) == []


async def test_evidence_exhibits_skips_the_primary_document_and_binaries(tmp_path):
    client = _FakeEdgar({
        f"{_BASE}index.json": _index([
            {"name": "d8k.htm", "type": "EX-99.1"},   # same file as primaryDocument
            {"name": "ex991.pdf", "type": "EX-99.1"},  # not textual
        ]),
    }, tmp_path)
    assert await client.evidence_exhibits(_FILING) == []


async def test_evidence_exhibits_degrade_to_empty_when_the_index_is_unavailable(tmp_path):
    client = _FakeEdgar({}, tmp_path)  # index.json raises
    assert await client.evidence_exhibits(_FILING) == []


async def test_fetch_evidence_text_leads_with_the_press_release(tmp_path):
    client = _FakeEdgar({
        f"{_BASE}d8k.htm": _FakeResponse(
            text="<html>Item 1.01. On July 28 2026 the Company issued a press release, "
                 "attached as Exhibit 99.1 and incorporated by reference.</html>",
            headers={"content-type": "text/html"},
        ),
        f"{_BASE}index.json": _index([{"name": "dex991.htm", "type": "EX-99.1"}]),
        f"{_BASE}dex991.htm": _FakeResponse(
            text="<html>Ducommun awarded $150 million contract by Raytheon for missile "
                 "guidance assemblies, deliveries beginning Q1 2027.</html>",
            headers={"content-type": "text/html"},
        ),
    }, tmp_path)

    text = await client.fetch_evidence_text(_FILING)

    # The item codes are spelled out for the model...
    assert "Material Definitive Agreement" in text
    # ...the press release is present at all (it never used to be)...
    assert "$150 million contract" in text
    # ...and it comes BEFORE the cover page, because _process_filing
    # truncates this head-weighted.
    assert text.index("$150 million contract") < text.index("incorporated by reference")


async def test_fetch_evidence_text_is_unchanged_when_there_are_no_exhibits(tmp_path):
    client = _FakeEdgar({
        f"{_BASE}d8k.htm": _FakeResponse(text="cover page only", headers={"content-type": "text/plain"}),
        f"{_BASE}index.json": _index([{"name": "d8k.htm", "type": "8-K"}]),
    }, tmp_path)
    filing = FilingEvent("DCO", "0000029669", "10-Q", "2026-07-28", "0000029669-26-000012", "d8k.htm")
    assert await client.fetch_evidence_text(filing) == "cover page only"
