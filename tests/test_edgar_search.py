"""EDGAR full-text search: response parsing, the local proximity pass, and
the candidate-only discipline.

NOTE ON THE FIXTURE. `_PAYLOAD` below mirrors EFTS's Elasticsearch-shaped
response as documented and as the public EDGAR search UI consumes it. It is
NOT a recording of a live call -- egress to efts.sec.gov was blocked from the
environment this was written in. That is exactly why `parse_hits` is written
to degrade to zero candidates on any unexpected shape rather than to trust
its own assumptions, and why the tests below spend more effort on the
malformed cases than on the happy path: the happy path asserts against an
assumption, while the malformed cases assert a SAFETY PROPERTY that holds
however the real schema differs.
"""
from __future__ import annotations

from smartboi.edgar_search import (
    PROXIMITY_CHARS,
    SearchHit,
    build_query,
    concentration_context,
    parse_hits,
    search_url,
)

_PAYLOAD = {
    "took": 12,
    "hits": {
        "total": {"value": 3, "relation": "eq"},
        "hits": [
            {
                "_id": "0001050915-25-000012:ichr-20250101.htm",
                "_source": {
                    "ciks": ["0001050915"],
                    "display_names": ["ICHOR HOLDINGS, LTD. (ICHR)"],
                    "file_type": "10-K",
                    "file_date": "2025-02-14",
                    "adsh": "0001050915-25-000012",
                },
            },
            {
                # Same filing, second matching document -- EFTS returns one
                # hit per DOCUMENT, not per filing.
                "_id": "0001050915-25-000012:ex-21.htm",
                "_source": {
                    "ciks": ["0001050915"],
                    "display_names": ["ICHOR HOLDINGS, LTD. (ICHR)"],
                    "file_type": "10-K",
                    "file_date": "2025-02-14",
                    "adsh": "0001050915-25-000012",
                },
            },
            {
                "_id": "0000914712-25-000031:ucap-20250630.htm",
                "_source": {
                    "ciks": ["0000914712"],
                    "display_names": ["PRIVATE FILER CORP"],   # no listed ticker
                    "file_type": "10-K",
                    "file_date": "2025-08-01",
                    "adsh": "0000914712-25-000031",
                },
            },
        ],
    },
}


def test_hits_are_deduped_on_accession_not_document():
    """One filing matching in two documents is ONE lead. Counting it twice
    would inflate the apparent independent support for a candidate."""
    hits = parse_hits(_PAYLOAD)

    assert len(hits) == 2
    assert [h.adsh for h in hits] == ["0001050915-25-000012", "0000914712-25-000031"]


def test_the_ticker_is_read_from_the_display_name_when_present():
    hits = parse_hits(_PAYLOAD)

    assert hits[0].ticker == "ICHR"
    assert hits[0].name == "ICHOR HOLDINGS, LTD."
    assert hits[0].form == "10-K"
    assert hits[0].cik == "0001050915"


def test_a_filer_with_no_listed_ticker_yields_no_ticker_rather_than_a_guess():
    """A wrong ticker is the ATRO/Advantest misresolution failure mode aimed
    straight at the universe. An empty one still gets resolution downstream."""
    hits = parse_hits(_PAYLOAD)

    assert hits[1].ticker == ""
    assert hits[1].name == "PRIVATE FILER CORP"


def test_the_accession_is_recovered_from_the_id_when_the_field_is_absent():
    payload = {"hits": {"hits": [
        {"_id": "0000320193-23-000106:aapl-20230930.htm",
         "_source": {"display_names": ["APPLE INC. (AAPL)"], "file_type": "10-K"}},
    ]}}

    hits = parse_hits(payload)

    assert len(hits) == 1
    assert hits[0].adsh == "0000320193-23-000106"


# --- The safety property. These assert what holds however the real schema
# differs from the fixture above: a response this code does not understand
# produces NO candidates, never candidates built from misread fields. ---

def test_an_unrecognised_shape_yields_no_candidates():
    for payload in (
        {},                                  # empty
        {"hits": {}},                        # no inner list
        {"hits": {"hits": "not-a-list"}},    # wrong type
        {"error": "rate exceeded"},          # an error body
        [],                                  # a list at the top level
        None,
        "",
    ):
        assert parse_hits(payload) == [], payload


def test_unparseable_json_yields_no_candidates():
    assert parse_hits("{not json at all") == []
    assert parse_hits(b"\x00\x01binary") == []


def test_a_hit_with_no_recoverable_accession_is_skipped_not_guessed():
    payload = {"hits": {"hits": [
        {"_source": {"display_names": ["MYSTERY CORP (XYZ)"]}},   # no _id, no adsh
        {"_id": "0000320193-23-000106:x.htm", "_source": {}},
    ]}}

    hits = parse_hits(payload)

    assert len(hits) == 1
    assert hits[0].adsh == "0000320193-23-000106"


def test_a_json_string_body_is_parsed():
    import json

    assert len(parse_hits(json.dumps(_PAYLOAD))) == 2


# --- The local proximity pass: EFTS has no proximity operator, so
# document-level AND over-matches and this is what narrows it. ---

def test_a_real_concentration_disclosure_is_found():
    text = (
        "Item 1. Business. We design fluid delivery subsystems. "
        "Applied Materials, our largest customer, accounted for 22% of net sales "
        "in fiscal 2025."
    )

    context = concentration_context(text, "Applied Materials")

    assert "accounted for 22% of net sales" in context


def test_two_unrelated_mentions_in_one_filing_are_rejected():
    """This is the case document-level AND admits and the whole reason the
    local pass exists: the anchor named in one section, revenue language in
    another, forty pages apart."""
    text = (
        "Our products are qualified at Applied Materials."
        + " filler." * 400
        + " Our top three customers accounted for 48% of total revenue."
    )

    assert concentration_context(text, "Applied Materials") == ""


def test_the_match_is_case_insensitive_but_the_context_is_verbatim():
    """The operator has to see the actual sentence: an IDIQ ceiling, a
    historical figure and a live concentration disclosure read very
    differently, and only the raw words distinguish them."""
    text = "APPLIED MATERIALS accounted for 22% of our net sales."

    context = concentration_context(text, "Applied Materials")

    assert "APPLIED MATERIALS" in context, "verbatim, not lowercased"


def test_a_later_mention_is_found_when_the_first_is_bare():
    """The first occurrence of a name is often a boilerplate list. Scanning
    only it would miss the disclosure entirely."""
    text = (
        "Competitors include Lam Research and Applied Materials."
        + " filler." * 200
        + " Applied Materials accounted for 31% of our revenues in 2025."
    )

    assert "31% of our revenues" in concentration_context(text, "Applied Materials")


def test_empty_inputs_are_a_no_op():
    assert concentration_context("", "Applied Materials") == ""
    assert concentration_context("some text", "") == ""


def test_the_proximity_window_is_configurable_and_bounded():
    text = "Applied Materials" + " x" * 200 + " accounted for 22% of net sales."

    assert concentration_context(text, "Applied Materials", proximity_chars=50) == ""
    assert concentration_context(text, "Applied Materials", proximity_chars=600) != ""


# --- Query construction ---

def test_the_query_is_the_bare_quoted_name():
    """Adding the concentration phrases would AND them at DOCUMENT level --
    which admits any filing that mentions the anchor anywhere and uses the
    phrase anywhere, while excluding filers who word it differently. The
    narrowing that works is local."""
    assert build_query("Applied Materials") == '"Applied Materials"'


def test_the_search_url_is_scoped_to_annual_reports_by_default():
    url = search_url("Applied Materials")

    assert url.startswith("https://efts.sec.gov/LATEST/search-index?")
    assert "forms=10-K" in url
    assert "%22Applied+Materials%22" in url or "%22Applied%20Materials%22" in url


def test_a_date_floor_is_passed_through_when_given():
    url = search_url("Applied Materials", date_from="2025-01-01")

    assert "startdt=2025-01-01" in url
    assert "dateRange=custom" in url


def test_the_proximity_default_is_a_paragraph_not_a_document():
    assert 100 <= PROXIMITY_CHARS <= 600


def test_a_search_hit_is_frozen():
    """Hits flow into candidate merging; a mutable one invites a caller to
    'fix up' a ticker in place, which is how a wrong ticker gets laundered
    into looking like EDGAR said it."""
    import dataclasses
    import pytest

    hit = SearchHit(adsh="x", cik="y", company="Z CORP (Z)", form="10-K", filing_date="2025-01-01")  # not an age: the test asserts the dataclass is frozen
    with pytest.raises(dataclasses.FrozenInstanceError):
        hit.adsh = "other"  # type: ignore[misc]


def test_a_hit_carries_the_document_filename_needed_to_fetch_it():
    """Regression, found live. The archive URL is
    <archives>/<cik>/<accession>/<primary_document>, so a hit without its
    document filename builds a DIRECTORY url that always 404s -- which made
    the proximity pass unable to fetch anything and therefore made the whole
    search yield zero candidates no matter what EFTS returned."""
    hits = parse_hits(_PAYLOAD)

    assert hits[0].document == "ichr-20250101.htm"
    assert hits[1].document == "ucap-20250630.htm"


def test_a_filing_built_from_a_hit_has_a_fetchable_url():
    from smartboi.edgar import EdgarClient

    hit = parse_hits(_PAYLOAD)[0]
    filing = EdgarClient.filing_from_hit(None, hit)   # pure mapping, no client state

    assert filing.primary_document == "ichr-20250101.htm"
    assert filing.document_url.endswith("/1050915/000105091525000012/ichr-20250101.htm")
    assert not filing.document_url.endswith("/")
