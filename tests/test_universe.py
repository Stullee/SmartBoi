from smartboi.universe import DEFAULT_UNIVERSE, SEED_RELATIONSHIPS, all_symbols, tradeable_symbols


def test_universe_symbols_are_unique():
    symbols = all_symbols()
    assert len(symbols) == len(set(symbols))


def test_tradeable_symbols_excludes_signal_sources():
    tradeable = set(tradeable_symbols())
    anchors = {c.symbol for c in DEFAULT_UNIVERSE if c.signal_source_only}
    assert anchors.isdisjoint(tradeable)
    assert "AMAT" in anchors
    assert "UCTT" in tradeable


def test_seed_relationships_reference_known_symbols():
    known = set(all_symbols())
    for from_sym, to_sym, rel_type, description, confidence in SEED_RELATIONSHIPS:
        assert from_sym in known
        assert to_sym in known
        assert rel_type in ("customer", "supplier", "competitor", "regulator")
        assert 0.0 <= confidence <= 1.0
        assert description
