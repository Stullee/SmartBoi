from smartboi.graph import Relationship, RelationshipGraph


def _rel(from_sym="UCTT", to_sym="AMAT", rel_type="customer"):
    return Relationship(from_sym, to_sym, rel_type, "desc", "manual seed", 0.85, "2026-07-21T00:00:00")


def test_add_returns_true_for_new_edge(tmp_path):
    graph = RelationshipGraph(tmp_path / "graph.json")
    assert graph.add(_rel()) is True
    assert len(graph.relationships) == 1


def test_add_dedupes_on_from_to_type(tmp_path):
    graph = RelationshipGraph(tmp_path / "graph.json")
    graph.add(_rel())
    assert graph.add(_rel()) is False
    assert len(graph.relationships) == 1


def test_add_allows_different_rel_type_same_pair(tmp_path):
    graph = RelationshipGraph(tmp_path / "graph.json")
    graph.add(_rel(rel_type="customer"))
    assert graph.add(_rel(rel_type="competitor")) is True
    assert len(graph.relationships) == 2


def test_load_prunes_invalid_rel_type(tmp_path):
    import json

    path = tmp_path / "graph.json"
    path.write_text(json.dumps([
        {"from_symbol": "UCTT", "to_symbol": "AMAT", "rel_type": "customer", "description": "d",
         "source": "s", "confidence": 0.8, "extracted_at": ""},
        {"from_symbol": "PDFS", "to_symbol": "ATRO", "rel_type": "partner", "description": "d2",
         "source": "s", "confidence": 0.7, "extracted_at": ""},
    ]))
    graph = RelationshipGraph(path)
    assert len(graph.relationships) == 1
    assert graph.relationships[0].rel_type == "customer"
    # The pruned file is rewritten so the bad edge doesn't reappear on the next load.
    reloaded = RelationshipGraph(path)
    assert len(reloaded.relationships) == 1


def test_load_with_no_invalid_edges_does_not_rewrite(tmp_path):
    graph = RelationshipGraph(tmp_path / "graph.json")
    graph.add(_rel())
    mtime_before = graph.path.stat().st_mtime_ns
    RelationshipGraph(graph.path)
    assert graph.path.stat().st_mtime_ns == mtime_before


def test_linked_symbols_both_directions(tmp_path):
    graph = RelationshipGraph(tmp_path / "graph.json")
    graph.add(_rel("UCTT", "AMAT", "customer"))
    universe = {"UCTT", "AMAT", "ICHR"}

    from_uctt = graph.linked_symbols("UCTT", universe)
    assert [s for s, _ in from_uctt] == ["AMAT"]

    from_amat = graph.linked_symbols("AMAT", universe)
    assert [s for s, _ in from_amat] == ["UCTT"]


def test_linked_symbols_filters_outside_universe(tmp_path):
    graph = RelationshipGraph(tmp_path / "graph.json")
    graph.add(_rel("UCTT", "AMAT", "customer"))
    result = graph.linked_symbols("UCTT", {"UCTT"})  # AMAT not in universe
    assert result == []


def test_graph_persists_across_instances(tmp_path):
    path = tmp_path / "graph.json"
    graph = RelationshipGraph(path)
    graph.add(_rel())

    reloaded = RelationshipGraph(path)
    assert len(reloaded.relationships) == 1
    assert reloaded.relationships[0].from_symbol == "UCTT"


def test_a_corrupt_graph_file_is_quarantined_not_silently_wiped(tmp_path):
    """The graph is expensive to rebuild (a full-universe re-extraction), so a
    corrupt file is renamed aside rather than overwritten by the next _save()."""
    path = tmp_path / "graph.json"
    path.write_text("{ not valid json")

    graph = RelationshipGraph(path)  # must not raise

    assert graph.relationships == []
    quarantined = list(tmp_path.glob("graph.json.corrupt-*"))
    assert len(quarantined) == 1
    assert quarantined[0].read_text() == "{ not valid json"
    # A subsequent add writes a clean file rather than clobbering the original.
    graph.add(_rel())
    assert len(RelationshipGraph(path).relationships) == 1
