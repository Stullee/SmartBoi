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
