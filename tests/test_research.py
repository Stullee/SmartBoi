"""Web supplier research (research.py). The safety-critical property here is
NEGATIVE: research must produce universe candidates and never a graph edge.
A web-sourced relationship is not a disclosure, and an edge at or above
DISCLOSED_LINK_CONFIDENCE satisfies the corroboration bar that fires trades
-- so letting research mint one would let a blog post, a stale article or a
hallucination clear a bar a 10-K disclosure was supposed to."""
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
