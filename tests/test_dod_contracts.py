"""DoD contract announcements. The safety-critical property is NAME MATCHING:
announcements use legal entity names, "Vertex" collides with Vertex
Pharmaceuticals, and a false match puts a defense award into an unrelated
company's thesis. Everything else here is secondary to that.
"""
from __future__ import annotations

from datetime import date

import httpx
import pytest

from smartboi.dod_contracts import (
    ANCHOR_VALUE_FLOOR_USD,
    BANNED_ALIASES,
    COMPANY_ALIASES,
    DodContractsClient,
    awards_from_page,
    business_days_back,
    html_to_text,
    match_symbols,
    parse_value_usd,
    split_announcements,
)

_UNIVERSE = {"LMT", "RTX", "NOC", "DCO", "V2X", "ATRO", "AIR", "TAYD", "KTOS"}

_DCO = (
    "Ducommun LaBarge Technologies Inc., Tulsa, Oklahoma, is awarded a "
    "$14,500,000 firm-fixed-price contract for structural assemblies in support "
    "of the F-15 program. Work will be performed in Tulsa, Oklahoma, and is "
    "expected to be completed by August 2028. Fiscal 2026 procurement funds in "
    "the amount of $14,500,000 will be obligated at time of award."
)
_LMT_SMALL = (
    "Lockheed Martin Corp., Fort Worth, Texas, is awarded a $12,000,000 "
    "modification to a previously awarded contract for sustainment services. "
    "Work will be performed in Fort Worth, Texas, and is expected to be "
    "completed by December 2027. Fiscal 2026 operations and maintenance funds "
    "will be obligated at time of award."
)
_LMT_BIG = (
    "Lockheed Martin Corp., Fort Worth, Texas, is awarded a $1,250,000,000 "
    "not-to-exceed undefinitized contract action for Lot 20 aircraft. Work will "
    "be performed in Fort Worth, Texas, and is expected to be completed by "
    "March 2030. Fiscal 2026 aircraft procurement funds will be obligated."
)


# --- Name matching: whole-word, hand-reviewed, never fuzzy ---

def test_a_legal_entity_name_resolves_to_its_ticker():
    assert match_symbols(_DCO, _UNIVERSE) == [("DCO", "Ducommun")]


def test_vertex_pharmaceuticals_is_not_a_defense_contractor():
    """The collision the alias table exists for. 'Vertex' alone is banned;
    only 'Vertex Aerospace' matches V2X."""
    text = ("Vertex Pharmaceuticals Inc., Boston, Massachusetts, was mentioned "
            "in an unrelated context that should never reach a defense dossier "
            "under any circumstances whatsoever in this system.")

    assert match_symbols(text, _UNIVERSE) == []


def test_vertex_aerospace_does_resolve_to_v2x():
    text = ("Vertex Aerospace LLC, Madison, Mississippi, is awarded a "
            "$250,000,000 contract for aircraft maintenance services in support "
            "of training operations across several installations nationwide.")

    assert match_symbols(text, _UNIVERSE) == [("V2X", "Vertex Aerospace")]


def test_matching_is_whole_word_so_air_does_not_match_aircraft():
    """Without word boundaries 'AIR' matches 'AIRCRAFT', and every single
    announcement in the corpus mentions aircraft."""
    text = ("Some Other Corp., Ohio, is awarded a $50,000,000 contract for "
            "aircraft components and airframe structures in support of ongoing "
            "sustainment across the fleet, with work performed in Ohio.")

    assert [s for s, _ in match_symbols(text, _UNIVERSE)] == []


def test_a_company_outside_the_live_universe_is_not_matched():
    """The table is global; the universe decides what is reachable."""
    assert match_symbols(_DCO, {"LMT"}) == []


def test_one_company_matches_once_even_when_named_repeatedly():
    text = _DCO + " Ducommun will perform the work. Ducommun is the prime."

    assert match_symbols(text, _UNIVERSE) == [("DCO", "Ducommun")]


def test_matching_is_case_insensitive():
    assert match_symbols(_DCO.upper(), _UNIVERSE) == [("DCO", "Ducommun")]


def test_no_banned_alias_is_in_the_live_table():
    """The banned list is in code rather than a comment precisely so that a
    future edit adding one fails here rather than in a live dossier."""
    live = {alias for aliases in COMPANY_ALIASES.values() for alias in aliases}

    assert live & set(BANNED_ALIASES) == set()


def test_every_alias_is_long_enough_to_be_a_safe_token():
    """A crude backstop, not the real defence.

    Whole-word matching plus the hand-reviewed banned list is what actually
    keeps 'Vertex' out. This only catches the case those two miss: a token so
    short that it is likely to be an acronym or a common word, where a human
    reviewer's eye slides right past it. Five is the floor because genuinely
    invented names can be that short (HEICO), and appending a corporate suffix
    to fix the length would BREAK matching rather than tighten it -- 'HEICO
    Corp' does not whole-word-match 'HEICO Corporation'."""
    for symbol, aliases in COMPANY_ALIASES.items():
        for alias in aliases:
            assert len(alias) >= 5, f"{symbol}: {alias!r} is too short to be unambiguous"


def test_a_corporate_suffix_would_break_matching_not_tighten_it():
    """Pins the reasoning above, because it is counterintuitive enough that
    someone will 'fix' an alias by appending Corp and quietly stop matching."""
    text = "HEICO Corporation, Hollywood, Florida, is awarded a $30,000,000 contract."

    assert match_symbols(text, {"HEI"}) == [("HEI", "HEICO")]


def test_no_alias_is_claimed_by_two_tickers():
    seen: dict[str, str] = {}
    for symbol, aliases in COMPANY_ALIASES.items():
        for alias in aliases:
            assert alias not in seen, f"{alias!r} claimed by both {seen.get(alias)} and {symbol}"
            seen[alias] = symbol


# --- Award value: the FIRST figure, not the largest ---

def test_the_award_value_is_the_first_figure_not_the_largest():
    """Announcements carry several figures -- the award, obligated funds, then
    often a ceiling. Taking the maximum would systematically read an IDIQ
    ceiling as new revenue."""
    text = ("Acme is awarded a $14,500,000 contract with a ceiling of "
            "$900,000,000 over five years.")

    assert parse_value_usd(text) == 14_500_000.0


def test_a_paragraph_with_no_figure_yields_none():
    assert parse_value_usd("No dollar amount appears in this sentence.") is None
    assert parse_value_usd("") is None


def test_billions_parse():
    assert parse_value_usd(_LMT_BIG) == 1_250_000_000.0


# --- The anchor value floor ---

def test_a_routine_anchor_award_is_filtered_out():
    """LMT/RTX/NOC/GD/BA appear most business days; without a floor their
    routine awards dominate the propagation budget while saying nothing a
    thesis can use."""
    awards = awards_from_page(f"{_LMT_SMALL}\n\n{_DCO}", _UNIVERSE, "2026-08-07",
                              anchors={"LMT"})

    assert [a.symbol for a in awards] == ["DCO"]


def test_a_large_anchor_award_clears_the_floor():
    awards = awards_from_page(_LMT_BIG, _UNIVERSE, "2026-08-07", anchors={"LMT"})

    assert [a.symbol for a in awards] == ["LMT"]
    assert awards[0].value_usd == 1_250_000_000.0


def test_the_floor_never_applies_to_a_tradeable():
    """A $14.5M award to a $90M-cap company is material to it in a way the
    same award to Lockheed is not."""
    awards = awards_from_page(_DCO, _UNIVERSE, "2026-08-07", anchors={"LMT"})

    assert [a.symbol for a in awards] == ["DCO"]
    assert awards[0].value_usd < ANCHOR_VALUE_FLOOR_USD


def test_the_announcement_text_is_passed_through_verbatim():
    """Many 'awards' are IDIQ ceilings or modifications rather than new
    revenue, and the difference lives in the wording. Summarising here would
    destroy exactly what the skeptic needs to catch it."""
    award = awards_from_page(_LMT_BIG, _UNIVERSE, "2026-08-07")[0]

    assert "not-to-exceed undefinitized contract action" in award.evidence_text
    assert "2026-08-07" in award.evidence_text
    assert award.matched_alias == "Lockheed Martin"


# --- Page splitting and HTML extraction ---

def test_service_headings_and_boilerplate_are_dropped():
    page = f"ARMY\n\nNAVY\n\n{_DCO}\n\nAIR FORCE\n\n{_LMT_BIG}"

    assert len(split_announcements(page)) == 2


def test_html_extraction_drops_scripts_and_styles():
    html = ("<html><head><style>.x{color:red}</style>"
            "<script>var leak='SHOULD_NOT_APPEAR';</script></head>"
            f"<body><p>{_DCO}</p></body></html>")

    text = html_to_text(html)

    assert "SHOULD_NOT_APPEAR" not in text
    assert "Ducommun LaBarge" in text


def test_a_malformed_page_yields_no_text_rather_than_garbage():
    assert html_to_text("") == ""


# --- Fetching ---

@pytest.mark.asyncio
async def test_a_missing_day_page_is_not_an_error():
    """Federal holidays simply have no page. That is the normal case."""
    client = DodContractsClient(httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(404))))

    assert await client.fetch_day(date(2026, 8, 7)) == ""


@pytest.mark.asyncio
async def test_a_weekend_is_never_requested():
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(200, text="<html><body>x</body></html>")

    client = DodContractsClient(httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    assert await client.fetch_day(date(2026, 8, 8)) == ""   # Saturday
    assert seen == []


@pytest.mark.asyncio
async def test_a_network_error_yields_no_text_and_does_not_raise():
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("no route to host")

    client = DodContractsClient(httpx.AsyncClient(transport=httpx.MockTransport(boom)))

    assert await client.fetch_day(date(2026, 8, 7)) == ""


def test_business_days_back_skips_weekends():
    days = business_days_back(3, today=date(2026, 8, 10))   # a Monday

    assert days == [date(2026, 8, 10), date(2026, 8, 7), date(2026, 8, 6)]


def test_dod_ingestion_is_off_by_default_because_war_gov_blocks_automation():
    """Pins the 2026-08-10 finding so it cannot be quietly undone.

    Every HTML path on war.gov returns 403 from Akamai's bot manager -- the
    listing AND individual articles -- and the only open endpoint, the RSS
    feed, carries a fixed boilerplate description with no award text and no
    company names. There is no route to the data short of defeating a bot
    manager, and every substitute (USASpending / FPDS / SAM) sits behind DoD's
    90-day hold, which is ~6x past evidence_is_stale's floor.

    Turning this back on without a working fetch route just resumes 12 failed
    requests a day. See the module docstring for the full transcript."""
    from smartboi.config import Settings

    assert Settings(_env_file=None).enable_dod_contracts is False


def test_the_parsing_layer_is_kept_working_for_the_day_a_route_exists():
    """The reason this module survives being switched off: the expensive part
    is the hand-reviewed alias table and the award semantics, not the fetch.
    If war.gov ever opens an automated route this is a fetch-layer change."""
    awards = awards_from_page(_DCO, _UNIVERSE, "2026-08-07")

    assert [a.symbol for a in awards] == ["DCO"]
    assert awards[0].value_usd == 14_500_000.0
