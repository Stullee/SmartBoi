from smartboi.universe import DEFAULT_UNIVERSE, SEED_RELATIONSHIPS, all_symbols, tradeable_symbols


def test_universe_symbols_are_unique():
    symbols = all_symbols()
    assert len(symbols) == len(set(symbols))


def test_tradeable_symbols_excludes_signal_sources():
    tradeable = set(tradeable_symbols())
    anchors = {c.symbol for c in DEFAULT_UNIVERSE if c.signal_source_only}
    assert anchors.isdisjoint(tradeable)
    assert "AMAT" in anchors
    assert "PLPC" in tradeable  # screen-verified thin-coverage name


def test_mega_caps_are_never_tradeable():
    """The 2026-07 refresh demoted every name that had graduated past the
    thin-coverage bounds (see universe.py's docstring) -- a large, heavily
    covered company is a NEWS SOURCE, never a trade target, because the
    information-diffusion lag this strategy trades only exists where
    coverage is thin. Guards against one being re-promoted by accident."""
    tradeable = set(tradeable_symbols())
    never_tradeable = {
        # Mega caps.
        "NVDA", "MSFT", "GOOGL", "AMZN", "META", "TSLA", "AVGO", "ASML", "TSM",
        "INTC", "AMD", "MU", "TXN", "ORCL", "RTX", "LMT", "NOC", "GD", "BA", "GE",
        "MDT", "SYK", "BSX", "ABT", "BDX", "ZBH",
        # Demoted 2026-07 after the live screen showed them graduated.
        "KLAC", "ONTO", "FORM", "CAMT", "KLIC", "UCTT", "ICHR", "COHU", "AEHR",
        "PDFS", "IESC", "POWL", "AGX", "KRMN", "LOAR", "VSEC", "ATRO", "RCAT",
        "ONDS", "ENVX", "AMPX", "EOSE", "AMSC", "SLI",
    }
    assert never_tradeable.isdisjoint(tradeable)


def test_every_ecosystem_has_both_tradeables_and_anchors():
    """An ecosystem with no anchor has nothing to propagate INTO it; one
    with no tradeable has nothing to propagate ONTO. Either way that half
    of the universe is inert, which is silent and easy to miss."""
    ecosystems = {c.ecosystem for c in DEFAULT_UNIVERSE}
    for ecosystem in ecosystems:
        members = [c for c in DEFAULT_UNIVERSE if c.ecosystem == ecosystem]
        assert any(c.signal_source_only for c in members), f"{ecosystem} has no anchor"
        assert any(not c.signal_source_only for c in members), f"{ecosystem} has no tradeable"


def test_seed_relationships_reference_known_symbols():
    known = set(all_symbols())
    for from_sym, to_sym, rel_type, description, confidence in SEED_RELATIONSHIPS:
        assert from_sym in known
        assert to_sym in known
        assert rel_type in ("customer", "supplier", "competitor", "regulator")
        assert 0.0 <= confidence <= 1.0
        assert description


def test_build_universe_anchor_wins_on_overlap():
    from smartboi.universe import build_universe

    universe = build_universe(["UCTT", "AAPL"], ["AAPL"])
    by_symbol = {c.symbol: c for c in universe}
    assert len(universe) == 2
    assert by_symbol["AAPL"].signal_source_only  # never accidentally tradeable
    assert not by_symbol["UCTT"].signal_source_only


def test_build_universe_normalizes_and_dedupes():
    from smartboi.universe import build_universe

    universe = build_universe([" uctt ", "UCTT", "ichr"], ["msft "])
    assert [c.symbol for c in universe] == ["MSFT", "UCTT", "ICHR"]
