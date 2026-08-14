"""Web supplier research (research.py). The safety-critical property here is
NEGATIVE: research must produce universe candidates and never a graph edge.
A web-sourced relationship is not a disclosure, and an edge at or above
DISCLOSED_LINK_CONFIDENCE satisfies the corroboration bar that fires trades
-- so letting research mint one would let a blog post, a stale article or a
hallucination clear a bar a 10-K disclosure was supposed to."""
from types import SimpleNamespace
from smartboi.research import (
    ResearchedSupplier,
    _to_suppliers,
    merge_into_candidates,
    researched_anchors,
)
from smartboi.state import JsonState


def _supplier(anchor="TSLA", name="Some Supplier Inc", ticker="ZZZZ", rel_type="supplier",
              confidence=0.8):
    return ResearchedSupplier(anchor=anchor, name=name, ticker=ticker, rel_type=rel_type,
                              description="supplies battery modules", evidence_url="https://x/1",
                              confidence=confidence)


def test_research_never_writes_a_pending_edge(tmp_path):
    """pending_edges is what makes an acceptance create a graph edge (see
    engine._promote_pending_edges). Research must never set it."""
    candidates = JsonState(tmp_path / "c.json")
    merge_into_candidates(candidates, [_supplier()])

    entry = candidates.get("ZZZZ")
    assert "pending_edges" not in entry
    assert entry["researched_only"] is True


def test_a_repeat_research_sighting_does_not_inflate_seen_count(tmp_path):
    """seen_count gates auto-accept as TRADEABLE and is meant to count
    independent FILING disclosures. Letting research inflate it would auto-add
    a trade target on nothing but web sourcing."""
    candidates = JsonState(tmp_path / "c.json")
    merge_into_candidates(candidates, [_supplier(anchor="TSLA")])
    merge_into_candidates(candidates, [_supplier(anchor="GM")])

    entry = candidates.get("ZZZZ")
    assert entry["seen_count"] == 1
    assert entry["related_to"] == ["TSLA", "GM"]  # ...but both anchors are recorded


def test_a_filing_sourced_candidate_is_not_downgraded_by_research(tmp_path):
    """A name a filing already disclosed keeps its edge and its provenance."""
    candidates = JsonState(tmp_path / "c.json")
    candidates.set("ZZZZ", {
        "name": "Some Supplier Inc", "ticker": "ZZZZ", "seen_count": 2,
        "related_to": ["DCO"], "rel_types": ["supplier"],
        "pending_edges": [{"from_symbol": "DCO", "rel_type": "supplier", "confidence": 0.9}],
    })

    merge_into_candidates(candidates, [_supplier(anchor="TSLA")])

    entry = candidates.get("ZZZZ")
    assert entry["pending_edges"]           # the disclosed edge survives
    assert entry["researched_only"] is False
    assert entry["seen_count"] == 2


def test_merge_reports_new_versus_updated(tmp_path):
    candidates = JsonState(tmp_path / "c.json")
    assert merge_into_candidates(candidates, [_supplier()]) == (1, 0)
    assert merge_into_candidates(candidates, [_supplier()]) == (0, 1)


def test_a_tickerless_supplier_is_keyed_by_name(tmp_path):
    """An empty ticker is the correct answer for a private or non-SEC company,
    and is resolved downstream against SEC's filer list rather than guessed."""
    candidates = JsonState(tmp_path / "c.json")
    merge_into_candidates(candidates, [_supplier(ticker="")])

    assert candidates.get("Some Supplier Inc") is not None


def test_researched_anchors_lets_a_rerun_continue(tmp_path):
    candidates = JsonState(tmp_path / "c.json")
    merge_into_candidates(candidates, [_supplier(anchor="TSLA"), _supplier(anchor="GM", ticker="YYYY")])

    assert researched_anchors(candidates) == {"TSLA", "GM"}


def test_model_output_is_sanitised():
    payload = {"suppliers": [
        {"name": "  Real Co  ", "ticker": " zzzz ", "rel_type": "supplier",
         "description": "d", "evidence_url": "u", "confidence": 5.0},
        {"name": "", "ticker": "AAAA", "rel_type": "supplier",
         "description": "d", "evidence_url": "u", "confidence": 0.5},
        {"name": "Bad Conf", "ticker": "BBBB", "rel_type": "supplier",
         "description": "d", "evidence_url": "u", "confidence": "not a number"},
    ]}
    out = _to_suppliers("TSLA", payload)

    assert [s.name for s in out] == ["Real Co", "Bad Conf"]  # nameless row dropped
    assert out[0].ticker == "ZZZZ"
    assert out[0].confidence == 1.0    # clamped, not trusted
    assert out[1].confidence == 0.0    # unparseable -> zero, not crash


# --- A suppressed call is not a finding ----------------------------------


class _NeverCalled:
    async def create(self, **_kwargs):
        raise AssertionError("no request should go out when the budget gate refuses")


async def test_a_suppressed_call_returns_none_not_an_empty_list(tmp_path):
    """The caller records an anchor as researched and never revisits it, so
    "no request went out" and "the call ran and found nothing" must not look
    alike. They did -- and routing the circuit breaker through the same gate
    made one open breaker able to retire the entire anchor list in a single
    run, irreversibly, since nothing expires anchor_research.json."""
    from smartboi.research import SupplierResearcher
    from smartboi.usage import CAT_RESEARCH, UsageTracker

    usage = UsageTracker(tmp_path / "u.json", daily_call_budget=10,
                         category_shares={CAT_RESEARCH: 0.0})  # switched off
    researcher = SupplierResearcher(api_key="k", model="claude-haiku-4-5", usage=usage)
    researcher._client = SimpleNamespace(messages=_NeverCalled())

    assert await researcher.research("RTX", "RTX Corp", "defense", 75.0, 3000.0) is None


async def test_an_open_breaker_leaves_every_anchor_unmarked(tmp_path, monkeypatch):
    """The whole point: the run stops and the anchors stay eligible, rather
    than being marked researched having cost nothing and learned nothing."""
    import smartboi.tools as tools_module
    from smartboi.config import Settings
    from smartboi.engine import Engine

    monkeypatch.chdir(tmp_path)
    engine = Engine(Settings(_env_file=None, enable_dashboard=False,
                             enable_universe_autoscreen=False, anthropic_api_key="k"))
    engine.usage.note_failure(Exception("your credit balance is too low"))

    async def _suppressed(*_a, **_k):
        return None
    monkeypatch.setattr(tools_module.SupplierResearcher, "research", _suppressed)

    await tools_module.run_supplier_research(engine)

    assert engine.research_state.data == {}, "a suppressed run must mark nothing"


# --- The skip list must record who was actually researched ----------------
#
# researched_anchors() builds the SKIP LIST that run_supplier_research filters
# on, so anything wrongly in it is an anchor that never gets a paid call
# again. Two mechanisms put anchors there that were never researched: the
# EDGAR full-text pass shared the web-research marker, and the candidate scan
# credited every symbol in `related_to` -- a shared accumulator, not the
# anchor the run was for. Live: 35 of 160 anchors locked out, ratio 172/160.

def test_the_edgar_pass_does_not_mark_anchors_as_web_researched(tmp_path):
    from smartboi.state import JsonState

    cands = JsonState(tmp_path / "c.json")
    merge_into_candidates(cands, [ResearchedSupplier(
        anchor="NOC", name="Some Supplier", ticker="SUP", rel_type="supplier",
        description="d", confidence=0.8, evidence_url="u")],
        marker_key="last_edgar_searched_at")

    entry = cands.get("SUP")
    assert entry["last_edgar_searched_at"], "the EDGAR pass wrote no marker of its own"
    assert "last_researched_at" not in entry, "EDGAR stamped the web-research marker"
    assert researched_anchors(cands, None) == set(), \
        "an EDGAR find marked its anchor as web-researched -- it is now skipped forever"


def test_web_research_credits_only_the_anchor_it_ran_for(tmp_path):
    """`related_to` accumulates every symbol that ever named a candidate, so
    crediting it counted co-occurring anchors as researched too."""
    from smartboi.state import JsonState

    cands = JsonState(tmp_path / "c.json")
    cands.set("SKYW", {"name": "SkyWest", "related_to": ["UAL", "DAL", "AAL"], "seen_count": 1})
    merge_into_candidates(cands, [ResearchedSupplier(
        anchor="AAL", name="SkyWest", ticker="SKYW", rel_type="supplier",
        description="d", confidence=0.8, evidence_url="u")])

    assert researched_anchors(cands, None) == {"AAL"}, \
        "UAL and DAL were credited for a run that only covered AAL"


def test_an_attempt_that_found_nothing_still_counts(tmp_path):
    """The reason research_state exists: 'searched, found nothing' writes no
    candidate, and without the attempt being recorded that anchor is
    reselected and re-billed on every future run."""
    from smartboi.state import JsonState

    cands = JsonState(tmp_path / "c.json")
    state = JsonState(tmp_path / "r.json")
    state.set("GEV", {"researched_at": "2026-08-12T00:00:00+00:00", "found": 0})

    assert researched_anchors(cands, state) == {"GEV"}
