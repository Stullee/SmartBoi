from types import SimpleNamespace

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


def test_add_upgrades_an_edge_when_a_stronger_disclosure_arrives(tmp_path):
    """A weak passing-mention edge extracted first must NOT permanently block a
    later quantified-concentration disclosure from raising it above the
    disclosed-link bar (2.4)."""
    graph = RelationshipGraph(tmp_path / "graph.json")
    graph.add(Relationship("UCTT", "AMAT", "customer", "passing mention", "10-Q", 0.55, "2026-07-01T00:00:00"))
    upgraded = graph.add(Relationship("UCTT", "AMAT", "customer", "25% of net sales", "10-K", 0.95, "2026-07-21T00:00:00"))

    assert upgraded is True
    assert len(graph.relationships) == 1
    edge = graph.relationships[0]
    assert edge.confidence == 0.95
    assert edge.description == "25% of net sales"
    assert edge.extracted_at == "2026-07-21T00:00:00"
    # And it persists (not just in memory).
    assert RelationshipGraph(tmp_path / "graph.json").relationships[0].confidence == 0.95


def test_add_does_not_downgrade_a_strong_edge_but_refreshes_its_age(tmp_path):
    graph = RelationshipGraph(tmp_path / "graph.json")
    graph.add(Relationship("UCTT", "AMAT", "customer", "25% of net sales", "10-K", 0.95, "2026-06-01T00:00:00"))
    result = graph.add(Relationship("UCTT", "AMAT", "customer", "vague", "news", 0.55, "2026-07-01T00:00:00"))

    assert result is False
    edge = graph.relationships[0]
    assert edge.confidence == 0.95 and edge.description == "25% of net sales"  # substance unchanged
    assert edge.extracted_at == "2026-07-01T00:00:00"  # but re-confirmation moved the aging anchor


def test_add_refreshes_extracted_at_on_equal_confidence_reconfirmation(tmp_path):
    graph = RelationshipGraph(tmp_path / "graph.json")
    graph.add(Relationship("UCTT", "AMAT", "customer", "d", "s", 0.85, "2026-06-01T00:00:00"))
    graph.add(Relationship("UCTT", "AMAT", "customer", "d", "s", 0.85, "2026-07-01T00:00:00"))
    assert graph.relationships[0].extracted_at == "2026-07-01T00:00:00"


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


# --- Extraction response shape -------------------------------------------


class _ToolUseBlock:
    type = "tool_use"

    def __init__(self, payload):
        self.input = payload


class _Response:
    def __init__(self, payload):
        self.content = [_ToolUseBlock(payload)]
        self.usage = SimpleNamespace(input_tokens=10, output_tokens=5)


class _Messages:
    def __init__(self, payload):
        self._payload = payload

    async def create(self, **_kwargs):
        return _Response(self._payload)


def _extractor(tmp_path, payload):
    from smartboi.graph import RelationshipExtractor
    from smartboi.usage import UsageTracker

    extractor = RelationshipExtractor(
        api_key="k", model="claude-haiku-4-5",
        usage=UsageTracker(tmp_path / "usage.json", daily_call_budget=10),
    )
    extractor._client = SimpleNamespace(messages=_Messages(payload))
    return extractor


async def test_a_string_payload_is_discarded_once_not_walked_per_character(tmp_path, caplog):
    """The tool schema says 'relationships' is an array. When the model hands
    back a STRING instead, the caller's per-element loop walks it one
    CHARACTER at a time and every character logs its own 'non-object entry'
    warning -- 7,618 of them from three filings, live, which is both a log
    storm and a completely illegible way to say the call produced nothing.
    One bad element in a good list is a different failure; this is the
    container being wrong, and it belongs where the contract is declared."""
    extractor = _extractor(tmp_path, {"relationships": "customer|AAA|BBB"})

    with caplog.at_level("WARNING"):
        out = await extractor.extract("AAA", "8-K", "filing text", ["AAA", "BBB"])

    assert out == []
    assert len([r for r in caplog.records if "not a list" in r.getMessage()]) == 1


async def test_a_well_formed_list_is_returned_untouched(tmp_path):
    rels = [{"to_symbol": "BBB", "rel_type": "customer", "confidence": 0.9}]
    extractor = _extractor(tmp_path, {"relationships": rels})

    assert await extractor.extract("AAA", "8-K", "filing text", ["AAA", "BBB"]) == rels


async def test_a_missing_relationships_key_is_an_empty_list_not_a_retry(tmp_path):
    """[] means 'genuinely nothing found' and must not be confused with None,
    which the engine treats as 'retry this filing later'."""
    extractor = _extractor(tmp_path, {})

    assert await extractor.extract("AAA", "8-K", "filing text", ["AAA"]) == []
