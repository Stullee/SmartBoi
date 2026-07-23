"""Starter universe: small/mid-cap companies grouped into ecosystems where a
big, heavily-covered player's news plausibly spills over onto a thinly-
covered second-order name with a lag -- see README point 1 ("pick a
universe where synthesis wins") and point 2 ("trade second-order effects").

Each ecosystem also lists "anchor" companies: large, efficiently-priced
names (signal_source_only=True) that are never trade targets themselves but
whose news is exactly the kind of headline that should propagate across the
relationship graph (graph.py) onto the ecosystem's tradeable names.

This is a starting-point candidate list, not a fixed universe -- see
universe_screen.py for the monthly market-cap/analyst-coverage recheck that
prunes names that no longer fit (acquired, graduated to broad coverage,
delisted). Override entirely via the SYMBOLS env var (see config.py); doing
so drops the ecosystem/anchor metadata below for any symbol not in this list."""
from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class CompanySpec:
    symbol: str
    name: str
    ecosystem: str
    signal_source_only: bool = False
    notes: str = ""


DEFAULT_UNIVERSE: list[CompanySpec] = [
    # --- Semiconductor equipment & materials: second-order to TSMC/Intel/
    # Samsung capex news, fab construction, export-control news ---
    CompanySpec("UCTT", "Ultra Clean Holdings", "semi_equipment",
                notes="Subsystems supplier to chip equipment makers; revenue tightly coupled to Applied Materials/Lam orders"),
    CompanySpec("ICHR", "Ichor Holdings", "semi_equipment",
                notes="Fluid delivery subsystems, same dynamic as UCTT"),
    CompanySpec("PLAB", "Photronics", "semi_equipment", notes="Photomasks; sensitive to fab utilization news"),
    CompanySpec("FORM", "FormFactor", "semi_equipment", notes="Test probe cards; advanced packaging exposure"),
    CompanySpec("COHU", "Cohu", "semi_equipment", notes="Test handling equipment"),
    CompanySpec("AEHR", "Aehr Test Systems", "semi_equipment", notes="Burn-in test for SiC/EV chips; small, news-sensitive"),
    CompanySpec("PDFS", "PDF Solutions", "semi_equipment", notes="Yield analytics software for fabs"),
    CompanySpec("KLIC", "Kulicke & Soffa", "semi_equipment", notes="Packaging equipment"),
    CompanySpec("CAMT", "Camtek", "semi_equipment", notes="Inspection equipment"),
    CompanySpec("AOSL", "Alpha and Omega Semiconductor", "semi_equipment", notes="Power semiconductors"),
    CompanySpec("AMAT", "Applied Materials", "semi_equipment", signal_source_only=True,
                notes="Anchor: semiconductor equipment capex bellwether, not a trade target"),
    CompanySpec("LRCX", "Lam Research", "semi_equipment", signal_source_only=True,
                notes="Anchor: semiconductor equipment capex bellwether, not a trade target"),
    CompanySpec("INTC", "Intel", "semi_equipment", signal_source_only=True,
                notes="Anchor: fab capex/build-out news source, not a trade target"),
    CompanySpec("TSM", "Taiwan Semiconductor (ADR)", "semi_equipment", signal_source_only=True,
                notes="Anchor: fab capex/build-out news source, not a trade target"),

    # --- Defense & aerospace tier-2: second-order to Pentagon budget lines,
    # European rearmament, prime-contractor award announcements ---
    CompanySpec("DCO", "Ducommun", "defense_tier2",
                notes="Structures/electronics for missile programs; an order to Raytheon flows down with a lag"),
    CompanySpec("ATRO", "Astronics", "defense_tier2", notes="Aircraft power/lighting subsystems"),
    CompanySpec("VSEC", "VSE Corp", "defense_tier2", notes="Aviation aftermarket parts"),
    CompanySpec("KRMN", "Karman Holdings", "defense_tier2", notes="Missile/hypersonics components; 2025 IPO, coverage still thin"),
    CompanySpec("LOAR", "Loar Holdings", "defense_tier2", notes="Niche aerospace components"),
    CompanySpec("RCAT", "Red Cat Holdings", "defense_tier2", notes="Speculative micro-cap drone maker; tradeable but very noisy"),
    CompanySpec("ONDS", "Ondas Holdings", "defense_tier2", notes="Speculative micro-cap drone/networks; tradeable but very noisy"),
    CompanySpec("KTOS", "Kratos Defense", "defense_tier2", signal_source_only=True,
                notes="Graduated to heavy coverage -- treat as a signal source for the tier-2 names, not a trade target"),
    CompanySpec("AVAV", "AeroVironment", "defense_tier2", signal_source_only=True,
                notes="Graduated to heavy coverage -- treat as a signal source for the tier-2 names, not a trade target"),
    CompanySpec("RTX", "RTX (Raytheon)", "defense_tier2", signal_source_only=True,
                notes="Anchor: prime contractor award announcements, not a trade target"),
    CompanySpec("LMT", "Lockheed Martin", "defense_tier2", signal_source_only=True,
                notes="Anchor: prime contractor award announcements, not a trade target"),
    CompanySpec("NOC", "Northrop Grumman", "defense_tier2", signal_source_only=True,
                notes="Anchor: prime contractor award announcements, not a trade target"),

    # --- Grid, electrification & data-center buildout: second-order to
    # hyperscaler data-center announcements, grid-upgrade programs -- the
    # headline is always about Microsoft/Google, never who builds the
    # substation ---
    CompanySpec("AGX", "Argan", "grid_datacenter", notes="Power plant EPC contractor"),
    CompanySpec("IESC", "IES Holdings", "grid_datacenter", notes="Electrical infrastructure services"),
    CompanySpec("LMB", "Limbach Holdings", "grid_datacenter", notes="Mechanical/electrical building systems"),
    CompanySpec("MTRX", "Matrix Service", "grid_datacenter", notes="Energy infrastructure engineering"),
    # THR (Thermon Group) removed: delisted from the NYSE effective June 1,
    # 2026 (trading suspended, Form 25 filed) -- SEC's live ticker map
    # correctly dropped the mapping, which is why it always failed CIK
    # lookup. The registrant (CIK 1489096) still exists/files, there's just
    # no tradeable ticker anymore. Exactly what universe_screen.py's
    # prune-only auto-screen exists to catch; pruned by hand here since it
    # never auto-removes.
    CompanySpec("PLPC", "Preformed Line Products", "grid_datacenter", notes="Grid hardware"),
    CompanySpec("POWL", "Powell Industries", "grid_datacenter",
                notes="Electrical switchgear; partly graduated to broader coverage already"),
    CompanySpec("MSFT", "Microsoft", "grid_datacenter", signal_source_only=True,
                notes="Anchor: hyperscaler data-center capex announcements, not a trade target"),
    CompanySpec("GOOGL", "Alphabet", "grid_datacenter", signal_source_only=True,
                notes="Anchor: hyperscaler data-center capex announcements, not a trade target"),

    # --- Battery & energy storage chain: second-order to EV maker news,
    # IRA/tariff policy, grid-storage procurement ---
    CompanySpec("ENVX", "Enovix", "battery_storage", notes="Next-gen battery cells"),
    CompanySpec("AMPX", "Amprius Technologies", "battery_storage", notes="Next-gen battery cells"),
    CompanySpec("EOSE", "Eos Energy", "battery_storage", notes="Zinc grid storage; moves on utility procurement news"),
    CompanySpec("AMSC", "American Superconductor", "battery_storage", notes="Grid tech with defense crossover"),
    CompanySpec("NVX", "Novonix", "battery_storage", notes="Battery materials; heavy sector M&A churn, verify status before relying on it"),
    CompanySpec("SLI", "Standard Lithium", "battery_storage", notes="Lithium materials; heavy sector M&A churn, verify status before relying on it"),
    CompanySpec("TSLA", "Tesla", "battery_storage", signal_source_only=True,
                notes="Anchor: EV/battery demand signal source, not a trade target"),
]

# Well-documented ecosystem relationships worth seeding directly rather than
# waiting for the extraction pipeline to (re)discover them from filings --
# see graph.py's RelationshipGraph.add. Deliberately limited to relationships
# that are industry-common-knowledge and specific (a named supplier of a
# named buyer), not the more speculative "sector exposure" links (e.g. grid
# contractors to hyperscaler capex) the user's own framing flagged as
# thematic rather than a disclosed direct contract -- those are left for
# the LLM extraction pipeline to find (or not) in actual filings rather than
# asserted here as fact.
SEED_RELATIONSHIPS: list[tuple[str, str, str, str, float]] = [
    # (from_symbol, to_symbol, rel_type, description, confidence)
    ("UCTT", "AMAT", "customer",
     "Applied Materials is a major customer of Ultra Clean Holdings' semiconductor equipment subsystems.", 0.85),
    ("UCTT", "LRCX", "customer",
     "Lam Research is a major customer of Ultra Clean Holdings' semiconductor equipment subsystems.", 0.85),
    ("ICHR", "AMAT", "customer",
     "Applied Materials is a major customer of Ichor Holdings' fluid delivery subsystems.", 0.85),
    ("ICHR", "LRCX", "customer",
     "Lam Research is a major customer of Ichor Holdings' fluid delivery subsystems.", 0.85),
    ("DCO", "RTX", "customer",
     "RTX (Raytheon) missile programs are a major customer relationship for Ducommun's structures/electronics.", 0.85),
]


def build_universe(symbols: list[str], anchor_symbols: list[str]) -> list[CompanySpec]:
    """Custom universe from plain ticker lists (the SYMBOLS / ANCHOR_SYMBOLS
    settings): `symbols` become tradeable targets, `anchor_symbols` become
    signal-source-only anchors -- large names whose news feeds the
    relationship graph but which are never trade targets themselves. A
    ticker appearing in both lists is treated as an anchor (the safe
    reading: never accidentally trade something you meant as a news
    source). Relationships between them are NOT configured here -- they're
    discovered automatically by the extraction pipeline from the tradeable
    companies' 10-K filings (a small supplier must disclose its dominant
    customers; the giant's own filings never name its small suppliers),
    including a one-time backfill of each tradeable company's most recent
    10-K on first run (see engine.py)."""
    anchors: list[str] = []
    for raw in anchor_symbols:
        symbol = raw.strip().upper()
        if symbol and symbol not in anchors:
            anchors.append(symbol)
    specs = [
        CompanySpec(symbol, symbol, "custom", signal_source_only=True,
                    notes="Anchor from ANCHOR_SYMBOLS: news source, not a trade target")
        for symbol in anchors
    ]
    seen = set(anchors)
    for raw in symbols:
        symbol = raw.strip().upper()
        if not symbol or symbol in seen:
            continue
        seen.add(symbol)
        specs.append(CompanySpec(symbol, symbol, "custom", notes="Tradeable from SYMBOLS"))
    return specs


def tradeable_symbols(universe: list[CompanySpec] | None = None) -> list[str]:
    """Symbols this system may open a hypothetical trade on -- excludes
    signal-source-only anchors, which exist purely to feed evidence into
    the graph, not to be traded themselves (see class docstring)."""
    universe = universe if universe is not None else DEFAULT_UNIVERSE
    return [c.symbol for c in universe if not c.signal_source_only]


def all_symbols(universe: list[CompanySpec] | None = None) -> list[str]:
    universe = universe if universe is not None else DEFAULT_UNIVERSE
    return [c.symbol for c in universe]


def spec_by_symbol(universe: list[CompanySpec] | None = None) -> dict[str, CompanySpec]:
    universe = universe if universe is not None else DEFAULT_UNIVERSE
    return {c.symbol: c for c in universe}
