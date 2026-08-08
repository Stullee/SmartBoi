from smartboi.universe import CompanySpec
from smartboi.universe_screen import (
    CandidateScreenResult,
    ScreenResult,
    format_screening_report,
    guess_ecosystem,
    recommend_candidate_type,
    screen_candidate,
    screen_universe,
)

from tests.fakes import FakeFinnhub


def _spec(symbol, ecosystem, signal_source_only=False):
    return CompanySpec(symbol, symbol, ecosystem, signal_source_only=signal_source_only)


# --- screen_universe (existing members) ---

async def test_screen_universe_flags_market_cap_out_of_range():
    finnhub = FakeFinnhub()
    finnhub.market_cap_by_symbol["UCTT"] = 10_000.0
    finnhub.analyst_count_by_symbol["UCTT"] = 3
    universe = [_spec("UCTT", "semi_equipment")]

    results = await screen_universe(universe, finnhub, min_market_cap_musd=100, max_market_cap_musd=3000, max_analyst_count=6)

    assert results == [ScreenResult("UCTT", False, "market cap $10000M outside [100M, 3000M]", 10_000.0, 3)]


async def test_screen_universe_flags_too_many_analysts():
    finnhub = FakeFinnhub()
    finnhub.market_cap_by_symbol["UCTT"] = 500.0
    finnhub.analyst_count_by_symbol["UCTT"] = 12
    universe = [_spec("UCTT", "semi_equipment")]

    results = await screen_universe(universe, finnhub, min_market_cap_musd=100, max_market_cap_musd=3000, max_analyst_count=6)

    assert results[0].still_fits is False
    assert "analysts" in results[0].reason


async def test_screen_universe_exempts_anchors_from_thin_coverage_bounds():
    """An anchor is deliberately large and heavily covered -- that's what
    makes its news worth propagating -- so a market cap far above the
    tradeable ceiling must not flag it."""
    finnhub = FakeFinnhub()
    finnhub.market_cap_by_symbol["AMAT"] = 180_000.0
    universe = [_spec("AMAT", "semi_equipment", signal_source_only=True)]

    results = await screen_universe(universe, finnhub, min_market_cap_musd=100, max_market_cap_musd=3000, max_analyst_count=6)

    assert results == [ScreenResult("AMAT", True, "anchor, live", 180_000.0, None, is_anchor=True)]


async def test_screen_universe_flags_anchor_with_no_market_data():
    """Anchors used to be skipped outright, so a dead one (delisted, or an
    OTC ADR line no source covers -- BMWYY/VLKAY/HYMTF, all confirmed live)
    was polled forever without anything ever noticing."""
    finnhub = FakeFinnhub()
    universe = [_spec("BMWYY", "auto_supply", signal_source_only=True)]

    results = await screen_universe(universe, finnhub, min_market_cap_musd=100, max_market_cap_musd=3000, max_analyst_count=6)

    assert results[0].still_fits is False
    assert results[0].is_anchor is True
    assert results[0].market_cap_musd is None


async def test_screen_universe_no_market_cap_data_fails():
    finnhub = FakeFinnhub()
    universe = [_spec("DEAD", "semi_equipment")]

    results = await screen_universe(universe, finnhub, min_market_cap_musd=100, max_market_cap_musd=3000, max_analyst_count=6)

    assert results[0].still_fits is False
    assert "no market cap data" in results[0].reason


async def test_screen_universe_within_bounds_fits():
    finnhub = FakeFinnhub()
    finnhub.market_cap_by_symbol["UCTT"] = 500.0
    finnhub.analyst_count_by_symbol["UCTT"] = 4
    universe = [_spec("UCTT", "semi_equipment")]

    results = await screen_universe(universe, finnhub, min_market_cap_musd=100, max_market_cap_musd=3000, max_analyst_count=6)

    assert results[0].still_fits is True


# --- recommend_candidate_type ---

def test_recommend_candidate_type_large_cap_is_anchor():
    rec, reason = recommend_candidate_type(50_000.0, 20, 100, 3000, 6)
    assert rec == "anchor"
    assert reason


def test_recommend_candidate_type_small_cap_is_tradeable():
    rec, reason = recommend_candidate_type(500.0, 3, 100, 3000, 6)
    assert rec == "tradeable"
    assert reason


def test_recommend_candidate_type_no_data_is_unknown():
    rec, reason = recommend_candidate_type(None, None, 100, 3000, 6)
    assert rec == "unknown"


# --- screen_candidate ---

def test_screen_candidate_fits():
    result = screen_candidate("ZZZZ", 500.0, 3, "semi_equipment", 100, 3000, 6)
    assert result == CandidateScreenResult("ZZZZ", True, "within bounds", 500.0, 3, "semi_equipment")


def test_screen_candidate_excluded_reports_reason():
    result = screen_candidate("ZZZZ", 50_000.0, 20, "?", 100, 3000, 6)
    assert result.fits is False
    assert "3000M" in result.reason


# --- guess_ecosystem ---

def test_guess_ecosystem_uses_first_related_companys_ecosystem():
    specs = {"FORM": _spec("FORM", "semi_equipment"), "DCO": _spec("DCO", "defense_tier2")}
    assert guess_ecosystem(["FORM", "DCO"], specs) == "semi_equipment"


def test_guess_ecosystem_skips_unknown_related_symbols():
    specs = {"DCO": _spec("DCO", "defense_tier2")}
    assert guess_ecosystem(["UNKNOWN", "DCO"], specs) == "defense_tier2"


def test_guess_ecosystem_defaults_to_unknown():
    assert guess_ecosystem([], {}) == "?"
    assert guess_ecosystem(["NOPE"], {}) == "?"


# --- format_screening_report ---

def test_format_screening_report_ranks_thinnest_coverage_first():
    results = [
        CandidateScreenResult("BIGGER", True, "within bounds", 2000.0, 5, "semi_equipment"),
        CandidateScreenResult("THINNEST", True, "within bounds", 500.0, 1, "defense_tier2"),
        CandidateScreenResult("EXCLUDED", False, "market cap too high", 50_000.0, 20, "?"),
    ]
    report = format_screening_report(results)
    assert report.index("THINNEST") < report.index("BIGGER")
    assert "EXCLUDED" in report
    assert "market cap too high" in report


def test_format_screening_report_handles_no_fitting_candidates():
    results = [CandidateScreenResult("NOPE", False, "too big", 50_000.0, 20, "?")]
    report = format_screening_report(results)
    assert "0 fit the bounds" in report
    assert "NOPE" in report


# --- lookup failure is not a verdict ---

async def test_a_failed_market_data_lookup_is_not_a_failed_screen():
    """The whole point of the lookup_failed split. A transient Finnhub
    error used to be indistinguishable from "Finnhub says this ticker has
    no market cap", and _prune_dead_symbols deletes on the latter."""
    finnhub = FakeFinnhub()
    finnhub.market_cap_lookup_fails.add("UCTT")
    universe = [_spec("UCTT", "semi_equipment")]

    (result,) = await screen_universe(
        universe, finnhub, min_market_cap_musd=100, max_market_cap_musd=3000, max_analyst_count=6
    )

    assert result.lookup_failed is True
    assert result.still_fits is True, "an unscreened symbol has not failed a screen"
    assert result.market_cap_musd is None


async def test_a_failed_lookup_on_an_anchor_is_not_a_dead_anchor():
    finnhub = FakeFinnhub()
    finnhub.market_cap_lookup_fails.add("ASML")
    universe = [_spec("ASML", "semi_equipment", signal_source_only=True)]

    (result,) = await screen_universe(
        universe, finnhub, min_market_cap_musd=100, max_market_cap_musd=3000, max_analyst_count=6
    )

    assert result.lookup_failed is True
    assert result.still_fits is True
    assert result.is_anchor is True


async def test_a_genuine_no_data_answer_is_still_reported_as_dead():
    """The guard must not swing so far that real delistings stop being
    detected -- that was the original problem it is fixing."""
    finnhub = FakeFinnhub()  # no entry at all => Finnhub answered with nothing
    universe = [_spec("DEADCO", "semi_equipment")]

    (result,) = await screen_universe(
        universe, finnhub, min_market_cap_musd=100, max_market_cap_musd=3000, max_analyst_count=6
    )

    assert result.lookup_failed is False
    assert result.still_fits is False
    assert result.market_cap_musd is None
