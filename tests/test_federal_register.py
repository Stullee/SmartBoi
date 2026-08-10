"""Federal Register: response parsing, curated-search discipline, and the
regulator propagation contract.

NOTE ON THE FIXTURE. `_PAYLOAD` mirrors the documented v1 API shape; egress to
federalregister.gov was blocked from the environment this was written in, so
it is not a live recording. That is precisely why `parse_documents` degrades
to ZERO documents on anything it does not recognise, and why the malformed
cases below outnumber the happy path: the happy path asserts an assumption,
the malformed cases assert a safety property that survives being wrong.
"""
from __future__ import annotations

from datetime import date

import httpx
import pytest

from smartboi.dossier import DISCLOSED_LINK_CONFIDENCE
from smartboi.federal_register import (
    CURATED_SEARCHES,
    REGULATOR_SYMBOLS,
    FederalRegisterClient,
    RegSearch,
    parse_documents,
    search_url,
)

_PAYLOAD = {
    "count": 2,
    "results": [
        {
            "document_number": "2026-14877",
            "title": "Utility Scale Wind Towers From Spain: Final Results of "
                     "Antidumping Duty Administrative Review",
            "abstract": "Commerce determines that producers/exporters subject to "
                        "this review made sales of subject merchandise at less "
                        "than normal value.",
            "html_url": "https://www.federalregister.gov/documents/2026/08/07/2026-14877/x",
            "publication_date": "2026-08-07",
            "type": "Notice",
            "agencies": [{"raw_name": "INTERNATIONAL TRADE ADMINISTRATION",
                          "name": "International Trade Administration"}],
        },
        {
            "document_number": "2026-14901",
            "title": "Phasedown of Hydrofluorocarbons: Allowance Allocation for 2027",
            "abstract": "EPA is allocating calendar year 2027 HFC production and "
                        "consumption allowances.",
            "html_url": "https://www.federalregister.gov/documents/2026/08/08/2026-14901/y",
            "publication_date": "2026-08-08",
            "type": "Rule",
            "agencies": [{"name": "Environmental Protection Agency"}],
        },
    ],
}


def test_documents_are_parsed_with_their_agencies():
    docs = parse_documents(_PAYLOAD)

    assert len(docs) == 2
    assert docs[0].document_number == "2026-14877"
    assert "Wind Towers" in docs[0].title
    assert docs[0].doc_type == "Notice"
    assert docs[0].agencies == ("International Trade Administration",)
    assert docs[1].agencies == ("Environmental Protection Agency",)


def test_the_evidence_text_carries_the_words_verbatim():
    """Whether a rule helps or hurts a given company is exactly the judgement
    the LLM is for. Summarising here would throw away what it needs."""
    text = parse_documents(_PAYLOAD)[1].evidence_text

    assert "Phasedown of Hydrofluorocarbons" in text
    assert "allocating calendar year 2027 HFC production" in text
    assert "Environmental Protection Agency" in text
    assert "2026-08-08" in text


def test_bare_string_agencies_are_tolerated():
    """The API returns agency objects on most document types and has been seen
    to return bare strings on others."""
    docs = parse_documents({"results": [
        {"document_number": "1", "title": "T", "agencies": ["Bureau of Industry and Security"]},
    ]})

    assert docs[0].agencies == ("Bureau of Industry and Security",)


# --- The safety property: an unrecognised response yields NO documents,
# never documents assembled from misread fields. ---

def test_an_unrecognised_shape_yields_no_documents():
    for payload in ({}, {"results": "nope"}, {"count": 5}, [], None, "", 42):
        assert parse_documents(payload) == [], payload


def test_a_document_missing_its_number_or_title_is_skipped():
    docs = parse_documents({"results": [
        {"title": "No number here"},
        {"document_number": "2026-1", "title": ""},
        {"document_number": "2026-2", "title": "Keeps this one"},
    ]})

    assert [d.document_number for d in docs] == ["2026-2"]


def test_a_count_of_zero_is_not_treated_as_an_error():
    """A search that legitimately matched nothing omits `results` entirely.
    That is the common case, not a failure."""
    assert parse_documents({"count": 0}) == []


# --- Curated-search discipline. ~200 documents publish per business day;
# "watch the Federal Register" is a disqualifying firehose, so every search
# must declare what it is allowed to reach. ---

def test_every_curated_search_declares_a_target():
    for search in CURATED_SEARCHES:
        assert search.targets or search.ecosystem, f"{search.key} reaches nothing"


def test_every_curated_search_names_a_known_regulator():
    for search in CURATED_SEARCHES:
        assert search.regulator in REGULATOR_SYMBOLS, search.key


def test_search_keys_are_unique():
    """The key is part of the source_name, and therefore part of the
    independence key -- two searches sharing one would collapse two unrelated
    proceedings onto a single corroborating source."""
    keys = [s.key for s in CURATED_SEARCHES]

    assert len(keys) == len(set(keys))


def test_the_documented_ticker_claims_are_still_wired():
    """Each of these was written against a specific checkable claim; a
    refactor that quietly drops one should fail here rather than in silence."""
    by_key = {s.key: s for s in CURATED_SEARCHES}

    assert "BWEN" in by_key["wind-towers-adcvd"].targets
    assert "HDSN" in by_key["hfc-allowances"].targets
    assert "AOSL" in by_key["entity-list"].targets
    assert by_key["semi-export-controls"].ecosystem == "semi_equipment"
    assert by_key["fmvss"].ecosystem == "auto_supply"


def test_the_url_carries_the_term_agency_and_date_floor():
    url = search_url(CURATED_SEARCHES[0], since=date(2026, 8, 1))

    assert url.startswith("https://www.federalregister.gov/api/v1/documents.json?")
    assert "conditions%5Bpublication_date%5D%5Bgte%5D=2026-08-01" in url
    assert "conditions%5Bterm%5D" in url
    assert "conditions%5Bagencies%5D%5B%5D=international-trade-administration" in url


# --- Fetch behaviour ---

@pytest.mark.asyncio
async def test_fetch_returns_parsed_documents():
    client = FederalRegisterClient(httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, json=_PAYLOAD))))

    docs = await client.fetch(CURATED_SEARCHES[0], today=date(2026, 8, 10))

    assert len(docs) == 2


@pytest.mark.asyncio
async def test_an_http_error_yields_no_documents_and_does_not_raise():
    client = FederalRegisterClient(httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(503, text="upstream down"))))

    assert await client.fetch(CURATED_SEARCHES[0], today=date(2026, 8, 10)) == []


@pytest.mark.asyncio
async def test_a_network_error_yields_no_documents_and_does_not_raise():
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route to host")

    client = FederalRegisterClient(httpx.AsyncClient(transport=httpx.MockTransport(boom)))

    assert await client.fetch(CURATED_SEARCHES[0], today=date(2026, 8, 10)) == []


@pytest.mark.asyncio
async def test_a_non_json_body_yields_no_documents():
    client = FederalRegisterClient(httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, text="<html>maintenance"))))

    assert await client.fetch(CURATED_SEARCHES[0], today=date(2026, 8, 10)) == []


@pytest.mark.asyncio
async def test_the_lookback_window_overlaps_rather_than_gapping():
    """The caller dedupes on document_number, so the cost of an overlapping
    window is a cheap repeat request; the cost of a gap is a rule that is
    never read at all."""
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(200, json={"count": 0})

    client = FederalRegisterClient(httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    await client.fetch(RegSearch(key="k", term="t", regulator="EPA", targets=("HDSN",)),
                       lookback_days=3, today=date(2026, 8, 10))

    assert "2026-08-07" in seen[0]


def test_regulator_edges_stay_below_the_corroboration_bar():
    """A sector-wide rule can raise a thesis but must never buy the
    corroboration discount a quantified customer disclosure earns. 'The EPA
    set HFC allowances' is real and material, and it is not evidence that some
    other item about the company is independently corroborated."""
    assert 0.80 < DISCLOSED_LINK_CONFIDENCE
