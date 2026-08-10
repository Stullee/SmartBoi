"""Auto-accepting discovered universe candidates (engine._auto_accept_candidates).

The engine already resolves a candidate's ticker, fetches its market cap and
analyst count, and computes a tradeable-vs-anchor recommendation; accepting by
hand applied exactly that recommendation. These cover the guards that make
acting on it automatically safe -- above all that a TRADEABLE is never added on
an unverified ticker, which is the confirmed-live Advantest->ATRO failure."""
from __future__ import annotations

import asyncio

import pytest

from smartboi.config import Settings
from smartboi.engine import Engine
from smartboi.universe import spec_by_symbol

from tests.fakes import FakeEdgarClient, FakeFinnhub


def _candidate(name="Some Supplier Inc", ticker="ZZZZ", recommended_as="tradeable", seen_count=3):
    return {
        "name": name, "ticker": ticker, "related_to": ["DCO"], "rel_types": ["supplier"],
        "description": "disclosed supplier", "sources": ["https://sec.gov/x"],
        "seen_count": seen_count, "first_seen_at": "2026-07-01T00:00:00+00:00",
        "recommended_as": recommended_as, "recommendation_reason": "fits the profile",
    }


@pytest.fixture
def engine(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    settings = Settings(
        _env_file=None, symbols="DCO", anchor_symbols="RTX",
        enable_dashboard=False, enable_universe_autoscreen=False,
        # Anchor auto-accept now ships OFF (the connectivity gate makes it safe
        # to re-enable, but off is the safe default). This file exercises the
        # accept path, so turn it on explicitly.
        auto_accept_anchors=True,
    )
    e = Engine(settings)
    e.edgar_client = FakeEdgarClient()
    e.finnhub = FakeFinnhub()
    return e


# --- The core behaviour ---

async def test_anchor_recommendation_is_auto_accepted(engine):
    """Anchors are held to the liberal bar: they can never become a trade,
    so the downside is wasted LLM spend and the upside is a live
    propagation source."""
    entry = _candidate(recommended_as="anchor", seen_count=1)
    entry["pending_edges"] = _pending()  # disclosed by DCO (a tradeable) -> lands connected
    engine.candidates.set("ZZZZ", entry)
    await engine._auto_accept_candidates()
    assert engine.spec_by_symbol["ZZZZ"].signal_source_only is True


async def test_tradeable_recommendation_is_auto_accepted_when_verified(engine):
    engine.candidates.set("ZZZZ", _candidate())
    await engine._auto_accept_candidates()
    assert "ZZZZ" in engine.spec_by_symbol
    assert engine.spec_by_symbol["ZZZZ"].signal_source_only is False


async def test_auto_accept_is_recorded_with_its_source(engine):
    """Persisted as {"as", "source", "ecosystem"} so an auto-add is
    distinguishable from one a human chose (auditable and undoable), and so
    the ecosystem accept_candidate classified it into survives a restart --
    it used not to, and _apply_accepted_candidates rebuilt every accepted
    symbol into the inert "accepted" bucket on startup."""
    engine.candidates.set("ZZZZ", _candidate())
    await engine._auto_accept_candidates()
    assert engine.accepted_candidates.get("ZZZZ") == {
        "as": "tradeable", "source": "auto", "ecosystem": "custom",
    }


# --- The guards ---

async def test_tradeable_is_blocked_when_the_ticker_name_does_not_verify(engine):
    """The Advantest->ATRO case: a filing named one company, the ticker
    resolved to an unrelated one. An anchor mistake is cheap; auto-adding a
    WRONG company as a trade target is not."""
    engine.edgar_client.name_matches = False
    engine.candidates.set("ZZZZ", _candidate())
    await engine._auto_accept_candidates()
    assert "ZZZZ" not in engine.spec_by_symbol
    # ...and the reason is surfaced rather than the candidate looking stuck.
    assert "does not match" in engine.candidates.get("ZZZZ")["auto_accept_blocked"]


async def test_anchor_is_not_blocked_by_an_unverified_name(engine):
    """The name check gates TRADEABLES only -- anchor auto-accept stays liberal
    on the name (its only bar is connectivity to a tradeable)."""
    engine.edgar_client.name_matches = False
    entry = _candidate(recommended_as="anchor")
    entry["pending_edges"] = _pending()  # connected, so it reaches the anchor gate
    engine.candidates.set("ZZZZ", entry)
    await engine._auto_accept_candidates()
    assert engine.spec_by_symbol["ZZZZ"].signal_source_only is True


async def test_tradeable_needs_repeat_disclosure(engine):
    engine.candidates.set("ZZZZ", _candidate(seen_count=1))
    await engine._auto_accept_candidates()
    assert "ZZZZ" not in engine.spec_by_symbol


async def test_unknown_recommendation_is_never_auto_accepted(engine):
    """"unknown" means there was no market data to judge on."""
    engine.candidates.set("ZZZZ", _candidate(recommended_as="unknown"))
    await engine._auto_accept_candidates()
    assert "ZZZZ" not in engine.spec_by_symbol


async def test_candidate_without_a_ticker_is_skipped(engine):
    engine.candidates.set("SOME PRIVATE CO", _candidate(ticker=""))
    await engine._auto_accept_candidates()
    assert len(engine.spec_by_symbol) == 2  # DCO + RTX only


async def test_daily_cap_bounds_how_many_can_be_added(engine):
    """One filing naming a long list of counterparties must not be able to
    flood the universe in a single pass."""
    engine.settings.auto_accept_max_per_day = 2
    for i in range(5):
        engine.candidates.set(f"ZZZ{i}", _candidate(ticker=f"ZZZ{i}"))
    await engine._auto_accept_candidates()
    assert sum(1 for s in engine.spec_by_symbol if s.startswith("ZZZ")) == 2


async def test_daily_cap_survives_a_restart(engine, tmp_path, monkeypatch):
    """Process-local counting would let a restart loop reset the budget."""
    engine.settings.auto_accept_max_per_day = 1
    for i in range(3):
        engine.candidates.set(f"ZZZ{i}", _candidate(ticker=f"ZZZ{i}"))
    await engine._auto_accept_candidates()

    restarted = Engine(engine.settings)
    restarted.edgar_client = FakeEdgarClient()
    restarted.finnhub = FakeFinnhub()
    await restarted._auto_accept_candidates()
    assert sum(1 for s in restarted.spec_by_symbol if s.startswith("ZZZ")) == 1


async def test_disabled_by_config(engine):
    engine.settings.enable_auto_accept_candidates = False
    engine.candidates.set("ZZZZ", _candidate())
    await engine._auto_accept_candidates()
    assert "ZZZZ" not in engine.spec_by_symbol


async def test_tradeables_can_be_disabled_independently_of_anchors(engine):
    engine.settings.auto_accept_tradeables = False
    engine.candidates.set("TRAD", _candidate(ticker="TRAD"))
    anch = _candidate(ticker="ANCH", recommended_as="anchor")
    anch["pending_edges"] = _pending()  # connected, so anchor auto-accept takes it
    engine.candidates.set("ANCH", anch)
    await engine._auto_accept_candidates()
    assert "TRAD" not in engine.spec_by_symbol
    assert "ANCH" in engine.spec_by_symbol


# --- Persisted-format compatibility ---

def test_legacy_string_entries_still_load(engine, tmp_path, monkeypatch):
    """accepted_candidates.json entries were originally a bare
    "tradeable"/"anchor" string; a deployment upgrading into this release
    must keep its existing accepted symbols."""
    engine.accepted_candidates.set("OLDT", "tradeable")
    engine.accepted_candidates.set("OLDA", "anchor")

    restarted = Engine(engine.settings)
    assert restarted.spec_by_symbol["OLDT"].signal_source_only is False
    assert restarted.spec_by_symbol["OLDA"].signal_source_only is True


# --- A recommendation is only meaningful relative to the bounds that
# produced it, and those move (the 2026-07 recalibration went 100/6 ->
# 75/10). A stale one silently keeps auto-accept on superseded thresholds.

async def test_recommendation_is_recomputed_when_the_bounds_change(engine):
    engine.finnhub.market_cap_musd = lambda symbol: asyncio.sleep(0, result=800.0)
    engine.finnhub.analyst_count = lambda symbol: asyncio.sleep(0, result=8)
    # As recorded before the recalibration: 8 analysts was > the old bound of 6.
    entry = _candidate(recommended_as="anchor")
    entry["recommendation_bounds"] = [100.0, 5000.0, 6.0]
    engine.candidates.set("ZZZZ", entry)

    await engine._run_candidate_ticker_recheck()

    # 8 analysts is inside the current bound of 10, so it's a trade target now.
    assert engine.candidates.get("ZZZZ")["recommended_as"] == "tradeable"


async def test_recommendation_is_not_recomputed_when_bounds_are_unchanged(engine):
    """Re-fetching market data for every candidate on every daily recheck
    would be pure waste -- two Finnhub calls each, against a 60/min budget
    shared with the engine's own polling."""
    calls = []

    async def counting_market_cap(symbol):
        calls.append(symbol)
        return 800.0

    engine.finnhub.market_cap_musd = counting_market_cap
    engine.finnhub.analyst_count = lambda symbol: asyncio.sleep(0, result=8)
    entry = _candidate(recommended_as="tradeable")
    entry["recommendation_bounds"] = [
        engine.settings.universe_min_market_cap_musd,
        engine.settings.universe_max_market_cap_musd,
        float(engine.settings.universe_max_analyst_count),
    ]
    engine.candidates.set("ZZZZ", entry)

    await engine._run_candidate_ticker_recheck()
    assert calls == []


async def test_a_candidate_recorded_before_bounds_existed_is_refreshed(engine):
    """Entries written before recommendation_bounds was stored at all have
    no marker, so they must be treated as stale rather than trusted."""
    engine.finnhub.market_cap_musd = lambda symbol: asyncio.sleep(0, result=800.0)
    engine.finnhub.analyst_count = lambda symbol: asyncio.sleep(0, result=8)
    engine.candidates.set("ZZZZ", _candidate(recommended_as="anchor"))  # no bounds key

    await engine._run_candidate_ticker_recheck()

    assert engine.candidates.get("ZZZZ")["recommended_as"] == "tradeable"


# --- A large, heavily-covered name must never become a trade target,
# however it was requested. Confirmed live: a deployment accumulated ten
# mega/large caps as TRADEABLE through the dashboard's own button, including
# a $323B pharma, which then accrued dossiers and LLM spend.

async def test_accepting_an_anchor_recommendation_as_tradeable_is_refused(engine):
    entry = _candidate(name="Merck", ticker="MRK", recommended_as="anchor")
    entry["recommendation_reason"] = "market cap $322954M exceeds the tradeable ceiling"
    engine.candidates.set("MRK", entry)

    with pytest.raises(ValueError, match="does not screen as a trade target"):
        engine.accept_candidate("MRK", "tradeable")
    assert "MRK" not in engine.spec_by_symbol


async def test_the_same_name_can_still_be_accepted_as_an_anchor(engine):
    engine.candidates.set("MRK", _candidate(name="Merck", ticker="MRK", recommended_as="anchor"))
    spec = engine.accept_candidate("MRK", "anchor")
    assert spec.signal_source_only is True


# --- Recovering from a polluted universe ---

async def test_reset_removes_runtime_additions_and_archives_their_dossiers(engine, tmp_path):
    from smartboi.dossier import Dossier

    engine.candidates.set("ZZZZ", _candidate())
    await engine._auto_accept_candidates()
    assert "ZZZZ" in engine.spec_by_symbol
    engine.dossiers.save(Dossier(symbol="ZZZZ"))

    result = engine.reset_accepted_candidates()

    assert result["removed"] == ["ZZZZ"]
    assert "ZZZZ" not in engine.spec_by_symbol
    assert engine.accepted_candidates.data == {}
    # Archived, not deleted -- the accumulated evidence is real history.
    assert not (engine.dossiers.dir_path / "ZZZZ.json").exists()
    assert (tmp_path / "data" / "dossiers_archived" / "ZZZZ.json").exists()
    # The curated universe survives untouched.
    assert "DCO" in engine.spec_by_symbol


# --- Dead tickers: the screen used to only LOG them, so a delisted or
# uncovered symbol kept costing an EDGAR poll, a news poll and any resulting
# LLM calls forever while being incapable of ever being priced or traded.
# Confirmed live: a literal "NULL", plus BMWYY/VLKAY/HYMTF sitting as anchors
# the screen skipped entirely. ---

async def test_dead_runtime_accepted_symbols_are_pruned_automatically(engine, tmp_path):
    from smartboi.dossier import Dossier
    from smartboi.universe_screen import ScreenResult

    engine.candidates.set("ZZZZ", _candidate())
    await engine._auto_accept_candidates()
    engine.dossiers.save(Dossier(symbol="ZZZZ"))
    assert "ZZZZ" in engine.spec_by_symbol

    pruned = engine._prune_dead_symbols([ScreenResult("ZZZZ", False, "no market cap data", None, None)])

    assert pruned == ["ZZZZ"]
    assert "ZZZZ" not in engine.spec_by_symbol
    assert "ZZZZ" not in engine.accepted_candidates.data
    assert (tmp_path / "data" / "dossiers_archived" / "ZZZZ.json").exists()


def test_a_dead_curated_symbol_is_reported_not_silently_removed(engine):
    """A curated symbol is a deliberate human choice. Deleting it here would
    be un-undoable from the dashboard and would fight the operator's own
    list on every screen -- so it is surfaced for a human instead."""
    from smartboi.universe_screen import ScreenResult

    pruned = engine._prune_dead_symbols([ScreenResult("DCO", False, "no market cap data", None, None)])

    assert pruned == []
    assert "DCO" in engine.spec_by_symbol
    assert engine.universe_screen_state.get("curated_no_market_data") == ["DCO"]


async def test_a_symbol_edgar_does_not_know_is_dropped_on_the_next_poll(engine, tmp_path):
    """Stronger and faster than the monthly market-cap screen: a foreign ADR
    line has a perfectly good market cap and files nothing with the SEC, so
    the screen would never catch it. Confirmed live: BMWYY/VLKAY/HYMTF plus
    a literal "NULL" logged a CIK warning once an hour, forever."""
    from smartboi.dossier import Dossier

    bmw = _candidate(ticker="BMWYY", recommended_as="anchor")
    bmw["pending_edges"] = _pending()  # connected, so anchor auto-accept takes it
    engine.candidates.set("BMWYY", bmw)
    await engine._auto_accept_candidates()
    assert "BMWYY" in engine.spec_by_symbol
    engine.dossiers.save(Dossier(symbol="BMWYY"))

    # EDGAR's ticker map has no CIK for it.
    engine.edgar_client.cik_for = lambda symbol: _none()

    assert await engine._is_unknown_to_edgar("BMWYY") is True
    assert "BMWYY" not in engine.spec_by_symbol
    assert "BMWYY" not in engine.accepted_candidates.data
    assert (tmp_path / "data" / "dossiers_archived" / "BMWYY.json").exists()


async def test_a_curated_symbol_unknown_to_edgar_is_reported_not_removed(engine):
    engine.edgar_client.cik_for = lambda symbol: _none()

    assert await engine._is_unknown_to_edgar("DCO") is True
    assert "DCO" in engine.spec_by_symbol
    assert engine.universe_screen_state.get("curated_unknown_to_edgar") == ["DCO"]


async def test_a_symbol_edgar_knows_is_polled_normally(engine):
    assert await engine._is_unknown_to_edgar("DCO") is False
    assert "DCO" in engine.spec_by_symbol


async def _none():
    return None


def test_a_live_symbol_is_never_pruned(engine):
    from smartboi.universe_screen import ScreenResult

    pruned = engine._prune_dead_symbols([
        ScreenResult("DCO", False, "market cap $9000M outside [75M, 5000M]", 9000.0, 4),
    ])

    assert pruned == []
    assert "DCO" in engine.spec_by_symbol


async def test_reset_leaves_discovered_candidates_alone(engine):
    """Candidates are facts discovered from filings -- clearing them would
    just make the engine rediscover the same names and re-pay for the
    ticker and market-data lookups."""
    engine.candidates.set("ZZZZ", _candidate())
    await engine._auto_accept_candidates()
    engine.reset_accepted_candidates()
    assert "ZZZZ" in engine.candidates.data


async def test_paper_trades_never_open_on_a_non_tradeable_symbol(engine):
    """A dossier FILE outlives universe membership: a symbol demoted to
    anchor keeps its file, and a stale SIGNALED one must not open a trade on
    something the system is no longer allowed to trade."""
    from smartboi.dossier import Dossier
    from tests.fakes import FakePriceFeed

    engine.price_feed = FakePriceFeed(prices={"RTX": 100.0})
    signaled = Dossier(symbol="RTX", direction="LONG", confidence=0.9, magnitude=0.9,
                       status="SIGNALED", signaled_at="2026-07-28T00:00:00+00:00")
    engine.dossiers.save(signaled)          # RTX is an ANCHOR in this fixture

    await engine._mark_and_execute()

    assert not engine.journal.has_open("RTX")


# --- Self-healing: acceptance used to be a one-way door, so a wrong type
# stayed wrong. Confirmed live: ten mega/large caps held as TRADEABLE, every
# one of which the engine itself recommended as an anchor.

async def test_a_tradeable_that_screens_as_an_anchor_is_demoted_automatically(engine):
    entry = _candidate(name="Merck", ticker="MRK", recommended_as="anchor")
    entry["recommendation_reason"] = "market cap $322954M exceeds the tradeable ceiling"
    engine.candidates.set("MRK", entry)
    # Wrongly accepted before the guard existed.
    engine.accepted_candidates.set("MRK", {"as": "tradeable", "source": "manual"})
    engine.universe = list(engine.settings.universe)
    engine._apply_accepted_candidates()
    from smartboi.universe import spec_by_symbol as _sbs
    engine.spec_by_symbol = _sbs(engine.universe)
    assert engine.spec_by_symbol["MRK"].signal_source_only is False

    engine._reconcile_accepted_types()

    assert engine.spec_by_symbol["MRK"].signal_source_only is True
    assert engine.accepted_candidates.get("MRK")["as"] == "anchor"


async def test_reconciliation_leaves_a_correctly_typed_symbol_alone(engine):
    engine.candidates.set("ZZZZ", _candidate(recommended_as="tradeable"))
    await engine._auto_accept_candidates()
    before = dict(engine.accepted_candidates.data)
    engine._reconcile_accepted_types()
    assert engine.accepted_candidates.data == before


async def test_reconciliation_ignores_symbols_with_no_recommendation(engine):
    """"unknown" or missing means there was no market data to judge on --
    not a reason to change anything."""
    engine.accepted_candidates.set("ZZZZ", {"as": "tradeable", "source": "manual"})
    engine.candidates.set("ZZZZ", _candidate(recommended_as="unknown"))
    engine._reconcile_accepted_types()
    assert engine.accepted_candidates.get("ZZZZ")["as"] == "tradeable"


# --- Not every discovered name is worth a Finnhub search ---

def test_unsearchable_names_are_skipped():
    for name in ("State and federal government agencies", "Major electric utilities",
                 "Internal Revenue Service (IRS)", "",
                 "Secretariat of the Federal Revenue Bureau of Brazil"):
        assert Engine._is_unsearchable(name) is True


def test_real_company_names_are_still_searched():
    for name in ("Advantest", "SolarEdge Technologies Inc.", "Eastman Kodak Company"):
        assert Engine._is_unsearchable(name) is False


# --- Edge promotion: accepting a candidate must write the relationship that
# DISCOVERED it into the graph. Without this the disclosure was discarded --
# _extract_relationships records the candidate without adding an edge, the
# filer is marked backfilled and never re-extracted, and backfill skips
# anchors entirely. Confirmed live: 237 candidates discovered, 40 accepted,
# and a graph of only 61 edges, with accepted anchors (ENTG, GEHC, DUK,
# LDOS, STM, SEDG, NVMI) carrying no edge at all. ---

def _pending(from_symbol="DCO", rel_type="customer", confidence=0.9):
    return [{
        "from_symbol": from_symbol, "rel_type": rel_type,
        "description": f"{from_symbol} discloses this counterparty as a major customer",
        "confidence": confidence, "source": "https://sec.gov/x",
    }]


def test_accepting_a_candidate_writes_its_discovered_relationship(engine):
    entry = _candidate(ticker="ZZZZ", recommended_as="anchor")
    entry["pending_edges"] = _pending()
    engine.candidates.set("ZZZZ", entry)

    engine.accept_candidate("ZZZZ", "anchor", source="auto")

    edges = [(r.from_symbol, r.to_symbol, r.rel_type) for r in engine.graph.relationships]
    assert ("DCO", "ZZZZ", "customer") in edges


def test_a_promoted_anchor_can_actually_reach_a_dossier(engine):
    """The point of the edge: an anchor is never its own analysis target, so
    with no edge to a tradeable its news resolves to zero targets and is
    discarded without an LLM call. This is the end-to-end assertion --
    without the stashed relationship an accepted anchor is inert, with it
    the anchor is immediately worth polling."""
    # Ecosystem fallback off: this isolates the DISCLOSED-EDGE path, which
    # is what promotion is about. The fallback has its own coverage.
    engine.settings.enable_ecosystem_propagation = False
    engine.candidates.set("INRT", _candidate(ticker="INRT", recommended_as="anchor"))
    engine.accept_candidate("INRT", "anchor", source="auto")
    assert engine._can_produce_evidence("INRT") is False  # unconnected: inert

    entry = _candidate(ticker="ZZZZ", recommended_as="anchor")
    entry["pending_edges"] = _pending()
    engine.candidates.set("ZZZZ", entry)
    engine.accept_candidate("ZZZZ", "anchor", source="auto")
    assert engine._can_produce_evidence("ZZZZ") is True   # connected: worth polling


def test_promotion_skips_a_filer_no_longer_in_the_universe(engine):
    entry = _candidate(ticker="ZZZZ", recommended_as="anchor")
    entry["pending_edges"] = _pending(from_symbol="GONE")
    engine.candidates.set("ZZZZ", entry)

    engine.accept_candidate("ZZZZ", "anchor", source="auto")

    assert engine.graph.relationships == []


def test_promotion_is_idempotent(engine):
    entry = _candidate(ticker="ZZZZ", recommended_as="anchor")
    entry["pending_edges"] = _pending()
    engine.candidates.set("ZZZZ", entry)

    engine.accept_candidate("ZZZZ", "anchor", source="auto")
    engine._promote_pending_edges("ZZZZ")

    assert len(engine.graph.relationships) == 1


def test_a_candidate_with_no_stashed_relationship_still_accepts(engine):
    """Entries written before pending_edges existed must not break accept."""
    engine.candidates.set("ZZZZ", _candidate(ticker="ZZZZ", recommended_as="anchor"))
    spec = engine.accept_candidate("ZZZZ", "anchor", source="auto")
    assert spec.symbol == "ZZZZ"
    assert engine.graph.relationships == []


# --- Not polling what cannot produce anything ---

def test_a_tradeable_is_always_worth_polling(engine):
    assert engine._can_produce_evidence("DCO") is True


def test_an_anchor_linked_only_to_another_anchor_stays_inert(engine):
    """An anchor is never its own target, so an anchor-to-anchor edge still
    resolves to zero analysis targets."""
    from smartboi.graph import Relationship

    engine.accept_candidate("ZZZZ", "anchor", source="auto")
    engine.graph.add(Relationship("RTX", "ZZZZ", "supplier", "d", "s", 0.9, "2026-07-29"))
    assert engine._can_produce_evidence("ZZZZ") is False


# --- Retroactive relevance filtering. The lender and biography filters run
# at EXTRACTION time, so entries recorded before they shipped stayed fully
# eligible for auto-accept off a persistent candidate list. Confirmed live:
# Danaher, ManpowerGroup, IDEX and SPX were all auto-accepted as anchors off
# one EPAC executive's CV. ---

async def test_a_biography_candidate_is_blocked_retroactively(engine):
    entry = _candidate(name="Danaher Corporation", ticker="DHR", recommended_as="anchor")
    entry["description"] = ("CEO Paul Sternlieb held management roles at Danaher (2011-2014), "
                            "a major diversified conglomerate.")
    entry["rel_types"] = ["competitor"]
    engine.candidates.set("DHR", entry)

    await engine._auto_accept_candidates()

    assert "DHR" not in engine.spec_by_symbol
    assert "biography" in engine.candidates.get("DHR")["auto_accept_blocked"]


async def test_a_lender_candidate_is_blocked_retroactively(engine):
    entry = _candidate(name="Piper Sandler & Co.", ticker="PIPR", recommended_as="anchor")
    entry["description"] = "Piper Sandler is the counterparty to KLXE's Equity Distribution Agreement."
    entry["rel_types"] = ["supplier"]
    engine.candidates.set("PIPR", entry)
    # "underwriter" is in the lender phrase list; use a phrase that is.
    entry["description"] = "Serves as underwriter and agent for the at-the-market offering program."
    engine.candidates.set("PIPR", entry)

    await engine._auto_accept_candidates()

    assert "PIPR" not in engine.spec_by_symbol
    assert "credit provider" in engine.candidates.get("PIPR")["auto_accept_blocked"]


async def test_a_genuine_commercial_candidate_is_untouched(engine):
    entry = _candidate(name="General Motors", ticker="GMX", recommended_as="anchor")
    entry["description"] = "General Motors accounted for approximately 25% of total revenues in 2025."
    entry["pending_edges"] = _pending()  # a real disclosed edge -> lands connected
    engine.candidates.set("GMX", entry)

    await engine._auto_accept_candidates()

    assert "GMX" in engine.spec_by_symbol


def test_an_already_accepted_candidate_is_not_retro_blocked(engine):
    """Blocking something already in the universe would say nothing about
    its membership -- that is reset_accepted_candidates' job."""
    entry = _candidate(name="Danaher Corporation", ticker="DHR", recommended_as="anchor")
    entry["description"] = "CEO Paul Sternlieb held management roles at Danaher."
    engine.candidates.set("DHR", entry)
    engine.accept_candidate("DHR", "anchor", source="auto")

    assert engine._block_junk_candidates() == 0
    assert engine.candidates.get("DHR").get("auto_accept_blocked") is None


# --- Ecosystem survives a restart.
#
# accept_candidate has always classified an acceptance into the ecosystem of
# whoever disclosed it, but only persisted {"as", "source"} -- so the
# classification lived in memory and died at every restart, and
# _apply_accepted_candidates rebuilt each symbol into the literal "accepted"
# bucket. That bucket is in _UNCLASSIFIED_ECOSYSTEMS, so _ecosystem_targets
# returns [] and _can_produce_evidence returns False: every restart quietly
# re-converted the accepted anchors into symbols whose news is never fetched.
# Live, that was 64 anchors, with auto-accept adding up to 20 more a day. ---

async def test_accepted_ecosystem_survives_a_restart(engine):
    engine.candidates.set("ZZZZ", _candidate())
    await engine._auto_accept_candidates()
    assert engine.spec_by_symbol["ZZZZ"].ecosystem == "custom"

    restarted = Engine(engine.settings)  # re-reads accepted_candidates.json

    assert restarted.spec_by_symbol["ZZZZ"].ecosystem == "custom"


async def test_a_legacy_entry_is_reclassified_from_its_discovery_record(engine):
    """Persisting the field only helps acceptances made from here on. The
    live deployment has a 69-symbol backlog written before it, and a
    candidate is only ever accepted once -- so nothing would revisit them."""
    engine.candidates.set("ZZZZ", _candidate())
    # The pre-fix on-disk shape: no ecosystem at all.
    engine.accepted_candidates.set("ZZZZ", {"as": "anchor", "source": "auto"})
    engine._apply_accepted_candidates()
    engine.spec_by_symbol = spec_by_symbol(engine.universe)
    assert engine.spec_by_symbol["ZZZZ"].ecosystem == "accepted"  # inert

    assert engine._reclassify_accepted_ecosystems() == 1

    assert engine.spec_by_symbol["ZZZZ"].ecosystem == "custom"
    assert engine.accepted_candidates.get("ZZZZ")["ecosystem"] == "custom"
    # ...and the type/source it was accepted under are not clobbered.
    assert engine.accepted_candidates.get("ZZZZ")["as"] == "anchor"
    assert engine.accepted_candidates.get("ZZZZ")["source"] == "auto"


async def test_reclassification_leaves_an_unresolvable_entry_alone(engine):
    engine.accepted_candidates.set("QQQQ", {"as": "anchor", "source": "auto"})
    engine._apply_accepted_candidates()
    engine.spec_by_symbol = spec_by_symbol(engine.universe)

    assert engine._reclassify_accepted_ecosystems() == 0
    assert engine.accepted_candidates.get("QQQQ") == {"as": "anchor", "source": "auto"}


def test_accepting_an_unvetted_candidate_as_tradeable_is_refused(engine):
    """Default-DENY. The guard used to reject only an explicit "anchor"
    recommendation, so recommended_as=None passed -- and None is the normal
    state for a freshly discovered candidate, or for any candidate when the
    market-cap lookup hasn't run. That is precisely the "added with zero
    vetting" incident the guard exists to prevent, reachable by clicking
    "+ Tradeable" on anything new."""
    engine.candidates.set("NEWCO", {"ticker": "NEWCO", "related_to": ["FORM"], "seen_count": 1})

    with pytest.raises(ValueError, match="no market-cap/analyst screen has run"):
        engine.accept_candidate("NEWCO", "tradeable")

    # As an ANCHOR it is fine -- anchors never trade, so there is nothing to vet.
    engine.accept_candidate("NEWCO", "anchor")
    assert "NEWCO" in engine.accepted_candidates.data


def test_a_screened_tradeable_recommendation_is_still_accepted(engine):
    """The guard must not block the path it exists to serve."""
    engine.candidates.set("SMALLCO", {
        "ticker": "SMALLCO", "related_to": ["FORM"], "seen_count": 1,
        "recommended_as": "tradeable", "recommendation_reason": "market cap $180M, 3 analysts",
    })

    engine.accept_candidate("SMALLCO", "tradeable")
    assert "SMALLCO" in engine.accepted_candidates.data
