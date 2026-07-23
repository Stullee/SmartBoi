from smartboi.edgar import FilingEvent, summarize_form4

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
