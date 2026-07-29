from smartboi.dossier import Dossier
from smartboi.graph import Relationship, RelationshipGraph
from smartboi.status import gather_graph_stats, gather_universe_candidates, snapshot_dossier


def test_snapshot_dossier_includes_computed_score():
    dossier = Dossier(symbol="UCTT", direction="LONG", confidence=0.8, magnitude=0.5,
                       independent_source_count=2, status="SIGNALED")
    row = snapshot_dossier(dossier, "2026-07-23T00:00:00+00:00")
    assert row["symbol"] == "UCTT"
    assert row["direction"] == "LONG"
    assert row["confidence"] == 0.8
    assert row["magnitude"] == 0.5
    assert row["score"] == 0.4  # confidence * magnitude
    assert row["independent_source_count"] == 2
    assert row["status"] == "SIGNALED"
    assert row["snapshotted_at"] == "2026-07-23T00:00:00+00:00"


def test_snapshot_dossier_covers_empty_dossiers_too():
    # A dossier with no evidence still gets a real (score=0) data point --
    # the daily snapshot is unconditional, not gated on anything changing.
    dossier = Dossier(symbol="COHU")
    row = snapshot_dossier(dossier, "2026-07-23T00:00:00+00:00")
    assert row["direction"] == "NONE"
    assert row["score"] == 0.0


def test_gather_universe_candidates_puts_addable_ones_first():
    # "no ticker" has the highest seen_count but nothing to click; the
    # unresolved-name entry shouldn't outrank a resolved-but-lower-count one.
    candidates = {
        "no ticker": {"name": "No Ticker Co", "ticker": "", "seen_count": 50},
        "AAA": {"name": "Low Count Co", "ticker": "AAA", "seen_count": 1},
        "BBB": {"name": "High Count Co", "ticker": "BBB", "seen_count": 10},
    }
    rows = gather_universe_candidates(candidates, accepted={})
    assert [r["ticker"] for r in rows] == ["BBB", "AAA", ""]


def test_gather_universe_candidates_demotes_already_accepted():
    # Already-accepted candidates have nothing left to click either --
    # they should sort with the unresolved tail, not the addable head.
    candidates = {
        "AAA": {"name": "Already Added", "ticker": "AAA", "seen_count": 100},
        "BBB": {"name": "Still Pending", "ticker": "BBB", "seen_count": 1},
    }
    rows = gather_universe_candidates(candidates, accepted={"AAA": "AAA"})
    assert [r["ticker"] for r in rows] == ["BBB", "AAA"]


def test_gather_graph_stats_groups_by_filer_symbol(tmp_path):
    graph = RelationshipGraph(tmp_path / "graph.json")
    graph.add(Relationship("FORM", "AMAT", "customer", "desc 1", "manual", 0.6, "2026-07-01"))
    graph.add(Relationship("FORM", "INTC", "supplier", "desc 2", "manual", 0.9, "2026-07-02"))
    graph.add(Relationship("UCTT", "AMAT", "customer", "desc 3", "manual", 0.7, "2026-07-03"))

    stats = gather_graph_stats(graph)
    assert stats["edge_count"] == 3
    assert [g["symbol"] for g in stats["by_symbol"]] == ["FORM", "UCTT"]  # sorted, one group per filer

    form_group = stats["by_symbol"][0]
    assert len(form_group["relationships"]) == 2
    # Strongest confidence first within a group.
    assert [r["counterparty"] for r in form_group["relationships"]] == ["INTC", "AMAT"]


def test_gather_graph_stats_empty_graph(tmp_path):
    graph = RelationshipGraph(tmp_path / "graph.json")
    stats = gather_graph_stats(graph)
    assert stats == {"edge_count": 0, "by_symbol": []}


# --- Coverage: how much of the TRADEABLE universe is actually live. A
# dossier count far below the tradeable count means most of the universe is
# dark, not that the market is quiet -- and the connectivity rows say why. ---

def _spec(symbol, anchor=False):
    from smartboi.universe import CompanySpec

    return CompanySpec(symbol, symbol, "test", signal_source_only=anchor)


def _rel(a, b):
    from smartboi.graph import Relationship

    return Relationship(a, b, "customer", "d", "s", 0.9, "2026-07-29")


def test_coverage_counts_tradeables_anchors_and_dossiers(tmp_path):
    from smartboi.dossier import Dossier, DossierStore
    from smartboi.graph import RelationshipGraph
    from smartboi.status import gather_coverage

    store = DossierStore(tmp_path / "d")
    store.save(Dossier(symbol="ULH"))
    graph = RelationshipGraph(tmp_path / "g.json")
    universe = [_spec("ULH"), _spec("THRM"), _spec("GM", anchor=True)]

    c = gather_coverage(universe, graph, store)

    assert c["tradeables"] == 2
    assert c["anchors"] == 1
    assert c["tradeables_with_dossier"] == 1


def test_coverage_ignores_a_dossier_for_a_non_tradeable(tmp_path):
    """Dossier FILES outlive universe membership (a demoted or dropped
    symbol keeps its file), so counting files would overstate coverage."""
    from smartboi.dossier import Dossier, DossierStore
    from smartboi.graph import RelationshipGraph
    from smartboi.status import gather_coverage

    store = DossierStore(tmp_path / "d")
    store.save(Dossier(symbol="GM"))       # an anchor
    store.save(Dossier(symbol="GONE"))     # no longer in the universe at all
    graph = RelationshipGraph(tmp_path / "g.json")

    c = gather_coverage([_spec("ULH"), _spec("GM", anchor=True)], graph, store)

    assert c["tradeables_with_dossier"] == 0


def test_coverage_reports_connectivity_in_both_directions(tmp_path):
    """Edges are stored filer -> counterparty, but propagation traverses
    both ways (graph.linked_symbols), so connectivity must too."""
    from smartboi.dossier import DossierStore
    from smartboi.graph import RelationshipGraph
    from smartboi.status import gather_coverage

    graph = RelationshipGraph(tmp_path / "g.json")
    graph.add(_rel("ULH", "GM"))
    universe = [_spec("ULH"), _spec("THRM"), _spec("GM", anchor=True)]

    c = gather_coverage(universe, graph, DossierStore(tmp_path / "d"))

    assert c["tradeables_connected"] == 1
    assert c["tradeables_unconnected"] == ["THRM"]
    assert c["anchors_live"] == 1
    assert c["anchors_inert"] == []


def test_an_anchor_linked_only_to_another_anchor_counts_as_inert(tmp_path):
    """An anchor is never its own analysis target, so an anchor-to-anchor
    edge still resolves to zero targets -- 'live' means linked to something
    tradeable, not merely linked."""
    from smartboi.dossier import DossierStore
    from smartboi.graph import RelationshipGraph
    from smartboi.status import gather_coverage

    graph = RelationshipGraph(tmp_path / "g.json")
    graph.add(_rel("GM", "F"))
    universe = [_spec("ULH"), _spec("GM", anchor=True), _spec("F", anchor=True)]

    c = gather_coverage(universe, graph, DossierStore(tmp_path / "d"))

    assert c["anchors_live"] == 0
    assert c["anchors_inert"] == ["F", "GM"]


def test_coverage_on_an_empty_universe_does_not_divide_by_zero(tmp_path):
    from smartboi.dossier import DossierStore
    from smartboi.graph import RelationshipGraph
    from smartboi.status import gather_coverage

    c = gather_coverage([], RelationshipGraph(tmp_path / "g.json"), DossierStore(tmp_path / "d"))

    assert c["tradeables"] == 0 and c["anchors"] == 0
    assert c["tradeables_with_dossier"] == 0


def test_snapshot_records_which_scoring_logic_produced_it():
    """A change to how magnitude or confidence is aggregated makes old and
    new rows incomparable. Stamping the version lets the forward-return
    analysis split at the boundary instead of silently mixing them --
    forward data can't be backfilled and old rows must never be re-scored."""
    from smartboi.dossier import SCORING_VERSION, Dossier
    from smartboi.status import snapshot_dossier

    row = snapshot_dossier(Dossier(symbol="DCO", direction="LONG", confidence=0.7,
                                   magnitude=0.4, independent_source_count=2),
                           "2026-07-29T00:00:00+00:00")

    assert row["scoring_version"] == SCORING_VERSION
