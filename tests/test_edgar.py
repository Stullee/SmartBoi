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
