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
so drops the ecosystem/anchor metadata below for any symbol not in this list.

## 2026-07 refresh: why most of the original tradeable list became anchors

The original watchlist was assembled when its names were genuinely small and
thinly covered. The 2024-2026 AI/defense/electrification rally pulled almost
all of them up into well-covered mid/large-cap territory, and a live
universe_screen run confirmed it: 27 of 29 tradeable names FAILED the
thin-coverage criteria (only PLPC and NVX still fit). Some had ballooned past
the market-cap ceiling (IESC $12.9B, POWL $8.9B, FORM $8.8B, AGX $8.5B, CAMT
$7.3B); the rest had simply acquired broad analyst coverage (ENVX 17
analysts, AMPX 16, EOSE 16, ONDS 14, ICHR 13, COHU 13).

That matters because the edge this system hunts -- gradual information
diffusion onto names nobody is watching -- only exists where coverage is
thin. Six days of forward data (dossier_snapshots.jsonl vs price_marks.jsonl)
showed the predictable result: dossier direction on those names carried no
demonstrated edge, and the book behaved like one correlated bet on AI capex.

So the graduated names were **demoted to anchors rather than deleted**. They
are still perfectly good NEWS SOURCES for the smaller names now taking their
place -- and an anchor with no graph edges costs zero LLM calls (engine.py's
_process_evidence finds no propagation targets and stops), so keeping them
is close to free and fully reversible: promote one back by flipping
signal_source_only if it ever screens thin again.

Two structural notes behind the anchor expansion:

- **Anchors are for RECOGNITION, not discovery.** SEC disclosure obligations
  run upward: a small supplier must disclose customer concentration (a
  material risk), while a giant never discloses its small suppliers. Confirmed
  live -- extracting KLA's own 10-K produced only KLAC->TSM, KLAC->AMAT,
  KLAC->ONTO, i.e. other large caps and not one small supplier. So a wide
  anchor list does NOT surface new trade targets; what it does is let the
  extractor turn a tradeable company's mention of ASML/GEV/ETN into a real
  graph EDGE instead of a dead-end universe candidate. Trade targets have to
  be chosen by screening (scripts/screen_candidates.py), not discovered.
- **A fifth, deliberately uncorrelated ecosystem (medtech_supply).** The
  original four are largely one macro bet -- semis, grid/data-center and
  battery are all the AI/electrification capex cycle, which is why they all
  sold off together. Medical-device suppliers have the identical structure (a
  small component maker disclosing a concentrated big-medtech customer) with
  no exposure to that cycle.

## Why the tradeable bound is <=10 analysts, not <=6

The original <=6-analyst bound was picked a priori, and a live screen of 15
fresh candidates showed it is very nearly unreachable: 14 failed, and the
failures cluster tightly just above it (7, 8, 8, 9, 9, 9, 9, 9, 10) rather
than being spread out. The only names that clear <=6 are micro-caps below the
$100M floor (CVU $63M, FLUX $13M, ULBI $90M). In other words the window
between "big enough to clear the cap floor" and "obscure enough to have <=6
analysts" is close to empty in US small caps.

So the bound moved to <=10, with a $75M floor -- which also makes this file
self-consistent, since PLAB (9), AOSL (10), DCO (9) and LMB (9) were already
retained under an effective bound of ~10 while the screener still enforced 6.

This is a different judgement from the SIGNAL threshold, which deliberately
was NOT loosened: that one governs when to TRADE, and forward data showed
sub-threshold signals losing money. This one governs what to WATCH, where the
evidence says the original number was simply unattainable and nothing
suggests a 9-analyst $400M company lacks the diffusion lag this strategy
trades. Note the surviving names are still dramatically thinner than what
they replaced -- 7-10 analysts at $100M-$3B, versus 9-19 analysts at $1-13B.

`notes` records each tradeable name's live screen result. Names that failed
are not kept as anchors the way the graduated names were: CEVA (15 analysts),
TATT (13) and KIDS (15) are well-covered but too small for their news to
propagate usefully, and CVU ($63M) and FLUX ($13M) are too small to be either
a credible news source or a liquid target.
"""
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
    # ================================================================
    # Semiconductor equipment & materials
    # Second-order to fab capex, export controls, AI accelerator demand.
    # ================================================================
    # --- Tradeable ---
    CompanySpec("PLAB", "Photronics", "semi_equipment",
                notes="screen-verified 2026-07: $1.8B, 9 analysts. Photomasks; sensitive to fab utilization"),
    CompanySpec("AOSL", "Alpha and Omega Semiconductor", "semi_equipment",
                notes="screen-verified 2026-07: $1.0B, 10 analysts. Power semiconductors"),
    CompanySpec("INTT", "inTEST Corp", "semi_equipment",
                notes="screen-verified 2026-07: 9 analysts, within the $100M-$3B band. Semi/industrial test"),
    CompanySpec("ASYS", "Amtech Systems", "semi_equipment",
                notes="screen-verified 2026-07: 7 analysts, within the $100M-$3B band. Semi equipment (diffusion/polishing)"),
    # --- Anchors: the capex bellwethers ---
    CompanySpec("AMAT", "Applied Materials", "semi_equipment", signal_source_only=True,
                notes="Anchor: semiconductor equipment capex bellwether"),
    CompanySpec("LRCX", "Lam Research", "semi_equipment", signal_source_only=True,
                notes="Anchor: semiconductor equipment capex bellwether"),
    CompanySpec("ASML", "ASML Holding", "semi_equipment", signal_source_only=True,
                notes="Anchor: lithography monopoly -- the single most important equipment order signal"),
    CompanySpec("KLAC", "KLA Corp", "semi_equipment", signal_source_only=True,
                notes="Anchor: process control/inspection. Was briefly accepted as tradeable -- ~$28B, far too large"),
    CompanySpec("NVDA", "Nvidia", "semi_equipment", signal_source_only=True,
                notes="Anchor: demand engine for the entire AI fab buildout"),
    CompanySpec("INTC", "Intel", "semi_equipment", signal_source_only=True,
                notes="Anchor: fab capex/build-out news source"),
    CompanySpec("TSM", "Taiwan Semiconductor (ADR)", "semi_equipment", signal_source_only=True,
                notes="Anchor: fab capex/build-out news source"),
    CompanySpec("MU", "Micron Technology", "semi_equipment", signal_source_only=True,
                notes="Anchor: memory capex cycle"),
    CompanySpec("AVGO", "Broadcom", "semi_equipment", signal_source_only=True,
                notes="Anchor: AI/networking silicon demand"),
    CompanySpec("AMD", "Advanced Micro Devices", "semi_equipment", signal_source_only=True,
                notes="Anchor: accelerator demand"),
    CompanySpec("TXN", "Texas Instruments", "semi_equipment", signal_source_only=True,
                notes="Anchor: broad analog/industrial capex read"),
    # --- Anchors: demoted 2026-07, graduated past the tradeable bounds ---
    CompanySpec("FORM", "FormFactor", "semi_equipment", signal_source_only=True,
                notes="Demoted 2026-07: $8.8B, 16 analysts -- past the cap ceiling"),
    CompanySpec("CAMT", "Camtek", "semi_equipment", signal_source_only=True,
                notes="Demoted 2026-07: $7.3B, 19 analysts -- past the cap ceiling"),
    CompanySpec("KLIC", "Kulicke & Soffa", "semi_equipment", signal_source_only=True,
                notes="Demoted 2026-07: $5.6B, 10 analysts -- past the cap ceiling"),
    CompanySpec("ONTO", "Onto Innovation", "semi_equipment", signal_source_only=True,
                notes="Demoted 2026-07: ~$14B. Was accepted as tradeable from a discovered candidate"),
    CompanySpec("UCTT", "Ultra Clean Holdings", "semi_equipment", signal_source_only=True,
                notes="Demoted 2026-07: $4.7B, 11 analysts -- coverage no longer thin"),
    CompanySpec("ICHR", "Ichor Holdings", "semi_equipment", signal_source_only=True,
                notes="Demoted 2026-07: $3.3B, 13 analysts -- coverage no longer thin"),
    CompanySpec("COHU", "Cohu", "semi_equipment", signal_source_only=True,
                notes="Demoted 2026-07: $2.6B, 13 analysts -- coverage no longer thin"),
    CompanySpec("AEHR", "Aehr Test Systems", "semi_equipment", signal_source_only=True,
                notes="Demoted 2026-07: $3.2B, 10 analysts -- coverage no longer thin"),
    CompanySpec("PDFS", "PDF Solutions", "semi_equipment", signal_source_only=True,
                notes="Demoted 2026-07: $2.3B, 10 analysts -- coverage no longer thin"),

    # ================================================================
    # Defense & aerospace tier-2
    # Second-order to Pentagon budget lines, European rearmament,
    # prime-contractor award announcements.
    # ================================================================
    # --- Tradeable ---
    CompanySpec("DCO", "Ducommun", "defense_tier2",
                notes="screen-verified 2026-07: $2.6B, 9 analysts. Best-functioning name in the graph "
                      "(disclosed RTX/LMT/NOC concentration at 0.95 confidence)"),
    CompanySpec("SIF", "SIFCO Industries", "defense_tier2",
                notes="screen-verified 2026-07: $164M, NO analyst coverage on record -- the thinnest name here. Forgings for aerospace/defense"),
    # --- Anchors: primes whose awards flow down ---
    CompanySpec("RTX", "RTX (Raytheon)", "defense_tier2", signal_source_only=True,
                notes="Anchor: prime contractor award announcements"),
    CompanySpec("LMT", "Lockheed Martin", "defense_tier2", signal_source_only=True,
                notes="Anchor: prime contractor award announcements"),
    CompanySpec("NOC", "Northrop Grumman", "defense_tier2", signal_source_only=True,
                notes="Anchor: prime contractor award announcements"),
    CompanySpec("GD", "General Dynamics", "defense_tier2", signal_source_only=True,
                notes="Anchor: prime contractor (land/marine/aero) award flow-down"),
    CompanySpec("BA", "Boeing", "defense_tier2", signal_source_only=True,
                notes="Anchor: commercial + defense aero; drives the aftermarket names"),
    CompanySpec("GE", "GE Aerospace", "defense_tier2", signal_source_only=True,
                notes="Anchor: jet engines -- aerospace component demand"),
    CompanySpec("LHX", "L3Harris", "defense_tier2", signal_source_only=True,
                notes="Anchor: defense electronics prime"),
    CompanySpec("HWM", "Howmet Aerospace", "defense_tier2", signal_source_only=True,
                notes="Anchor: aerospace structural materials"),
    CompanySpec("TDG", "TransDigm", "defense_tier2", signal_source_only=True,
                notes="Anchor: aerospace components bellwether"),
    CompanySpec("KTOS", "Kratos Defense", "defense_tier2", signal_source_only=True,
                notes="Anchor: heavy coverage -- signal source for the tier-2 names"),
    CompanySpec("AVAV", "AeroVironment", "defense_tier2", signal_source_only=True,
                notes="Anchor: heavy coverage -- signal source for the tier-2 names"),
    # --- Anchors: demoted 2026-07 ---
    CompanySpec("KRMN", "Karman Holdings", "defense_tier2", signal_source_only=True,
                notes="Demoted 2026-07: $6.1B, 17 analysts -- past the cap ceiling"),
    CompanySpec("LOAR", "Loar Holdings", "defense_tier2", signal_source_only=True,
                notes="Demoted 2026-07: $6.5B, 11 analysts -- past the cap ceiling"),
    CompanySpec("VSEC", "VSE Corp", "defense_tier2", signal_source_only=True,
                notes="Demoted 2026-07: $5.6B, 16 analysts -- past the cap ceiling"),
    CompanySpec("ATRO", "Astronics", "defense_tier2", signal_source_only=True,
                notes="Demoted 2026-07: $3.0B, 11 analysts -- coverage no longer thin"),
    CompanySpec("RCAT", "Red Cat Holdings", "defense_tier2", signal_source_only=True,
                notes="Demoted 2026-07: $1.3B, 12 analysts -- coverage no longer thin"),
    CompanySpec("ONDS", "Ondas Holdings", "defense_tier2", signal_source_only=True,
                notes="Demoted 2026-07: $4.3B, 14 analysts -- coverage no longer thin"),

    # ================================================================
    # Grid, electrification & data-center buildout
    # The headline is always about the hyperscaler, never about who
    # builds the substation.
    # ================================================================
    # --- Tradeable ---
    CompanySpec("PLPC", "Preformed Line Products", "grid_datacenter",
                notes="screen-verified 2026-07: $1.6B, 6 analysts -- PASSES the thin-coverage bounds. Grid hardware"),
    CompanySpec("MTRX", "Matrix Service", "grid_datacenter",
                notes="screen-verified 2026-07: $353M, 7 analysts. Energy infrastructure engineering"),
    CompanySpec("LMB", "Limbach Holdings", "grid_datacenter",
                notes="screen-verified 2026-07: $887M, 9 analysts. Mechanical/electrical building systems"),
    CompanySpec("BWEN", "Broadwind", "grid_datacenter",
                notes="screen-verified 2026-07: 9 analysts, within the $100M-$3B band. Fabricator (towers, industrial weldments)"),
    CompanySpec("ESOA", "Energy Services of America", "grid_datacenter",
                notes="screen-verified 2026-07: 8 analysts, within the $100M-$3B band. Utility/pipeline contractor"),
    CompanySpec("WLDN", "Willdan Group", "grid_datacenter",
                notes="screen-verified 2026-07: 9 analysts, within the $100M-$3B band. Energy efficiency/grid services to utilities"),
    # --- Anchors: hyperscaler capex + grid equipment ---
    CompanySpec("MSFT", "Microsoft", "grid_datacenter", signal_source_only=True,
                notes="Anchor: hyperscaler data-center capex announcements"),
    CompanySpec("GOOGL", "Alphabet", "grid_datacenter", signal_source_only=True,
                notes="Anchor: hyperscaler data-center capex announcements"),
    CompanySpec("AMZN", "Amazon (AWS)", "grid_datacenter", signal_source_only=True,
                notes="Anchor: largest hyperscaler capex driver"),
    CompanySpec("META", "Meta Platforms", "grid_datacenter", signal_source_only=True,
                notes="Anchor: massive AI data-center buildout capex"),
    CompanySpec("ORCL", "Oracle", "grid_datacenter", signal_source_only=True,
                notes="Anchor: OCI data-center capex"),
    CompanySpec("GEV", "GE Vernova", "grid_datacenter", signal_source_only=True,
                notes="Anchor: grid/power equipment giant -- drives the electrical contractors"),
    CompanySpec("ETN", "Eaton", "grid_datacenter", signal_source_only=True,
                notes="Anchor: electrical power management for data centers/grid"),
    CompanySpec("VRT", "Vertiv Holdings", "grid_datacenter", signal_source_only=True,
                notes="Anchor: data-center cooling/power -- direct read on buildout pace"),
    CompanySpec("PWR", "Quanta Services", "grid_datacenter", signal_source_only=True,
                notes="Anchor: grid/infrastructure construction"),
    # --- Anchors: demoted 2026-07 ---
    CompanySpec("IESC", "IES Holdings", "grid_datacenter", signal_source_only=True,
                notes="Demoted 2026-07: $12.9B -- far past the cap ceiling"),
    CompanySpec("POWL", "Powell Industries", "grid_datacenter", signal_source_only=True,
                notes="Demoted 2026-07: $8.9B, 12 analysts -- past the cap ceiling"),
    CompanySpec("AGX", "Argan", "grid_datacenter", signal_source_only=True,
                notes="Demoted 2026-07: $8.5B, 10 analysts -- past the cap ceiling"),

    # ================================================================
    # Battery & energy storage chain
    # Second-order to EV maker news, IRA/tariff policy, grid-storage
    # procurement.
    # ================================================================
    # --- Tradeable ---
    CompanySpec("NVX", "Novonix", "battery_storage",
                notes="screen-verified 2026-07: $149M, 5 analysts -- PASSES the thin-coverage bounds. Battery materials"),
    CompanySpec("ULBI", "Ultralife Corp", "battery_storage",
                notes="screen-verified 2026-07: $90M -- just under the $100M floor, kept deliberately (see docstring). Batteries/comms, defense crossover"),
    # --- Anchors ---
    CompanySpec("TSLA", "Tesla", "battery_storage", signal_source_only=True,
                notes="Anchor: EV/battery demand signal source"),
    CompanySpec("ALB", "Albemarle", "battery_storage", signal_source_only=True,
                notes="Anchor: lithium giant -- key materials price/demand driver"),
    CompanySpec("GM", "General Motors", "battery_storage", signal_source_only=True,
                notes="Anchor: EV demand"),
    CompanySpec("F", "Ford Motor", "battery_storage", signal_source_only=True,
                notes="Anchor: EV demand"),
    CompanySpec("FSLR", "First Solar", "battery_storage", signal_source_only=True,
                notes="Anchor: solar + storage demand"),
    CompanySpec("ENPH", "Enphase Energy", "battery_storage", signal_source_only=True,
                notes="Anchor: storage/inverter demand"),
    # --- Anchors: demoted 2026-07 ---
    CompanySpec("ENVX", "Enovix", "battery_storage", signal_source_only=True,
                notes="Demoted 2026-07: $1.1B, 17 analysts -- coverage no longer thin"),
    CompanySpec("AMPX", "Amprius Technologies", "battery_storage", signal_source_only=True,
                notes="Demoted 2026-07: $1.5B, 16 analysts -- coverage no longer thin"),
    CompanySpec("EOSE", "Eos Energy", "battery_storage", signal_source_only=True,
                notes="Demoted 2026-07: $1.5B, 16 analysts -- coverage no longer thin"),
    CompanySpec("AMSC", "American Superconductor", "battery_storage", signal_source_only=True,
                notes="Demoted 2026-07: $1.7B, 10 analysts -- coverage no longer thin"),
    CompanySpec("SLI", "Standard Lithium", "battery_storage", signal_source_only=True,
                notes="Demoted 2026-07: $778M, 12 analysts -- coverage no longer thin"),

    # ================================================================
    # Medical device supply chain (added 2026-07)
    # Deliberately UNCORRELATED with the AI/electrification capex cycle
    # that drives the other four ecosystems -- same structure (a small
    # component maker disclosing a concentrated big-medtech customer),
    # entirely different macro driver.
    # ================================================================
    # --- Tradeable (all unverified -- screen before relying on these) ---
    CompanySpec("UFPT", "UFP Technologies", "medtech_supply",
                notes="screen-verified 2026-07: 10 analysts, within the $100M-$3B band. Components/packaging for medical devices"),
    CompanySpec("IRMD", "IRadimed", "medtech_supply",
                notes="screen-verified 2026-07: 8 analysts, within the $100M-$3B band. MRI-compatible devices"),
    CompanySpec("MLAB", "Mesa Laboratories", "medtech_supply",
                notes="screen-verified 2026-07: 9 analysts, within the $100M-$3B band. Sterilization/QC instruments for medtech and pharma"),
    # --- Anchors: the big medtechs whose news should propagate down ---
    CompanySpec("MDT", "Medtronic", "medtech_supply", signal_source_only=True,
                notes="Anchor: largest device maker -- procurement/product news propagates to suppliers"),
    CompanySpec("SYK", "Stryker", "medtech_supply", signal_source_only=True,
                notes="Anchor: ortho/surgical device demand"),
    CompanySpec("BSX", "Boston Scientific", "medtech_supply", signal_source_only=True,
                notes="Anchor: interventional device demand"),
    CompanySpec("ABT", "Abbott Laboratories", "medtech_supply", signal_source_only=True,
                notes="Anchor: diagnostics/device demand"),
    CompanySpec("BDX", "Becton Dickinson", "medtech_supply", signal_source_only=True,
                notes="Anchor: medical supplies/consumables demand"),
    CompanySpec("ZBH", "Zimmer Biomet", "medtech_supply", signal_source_only=True,
                notes="Anchor: orthopedic implant demand"),
]

# Well-documented ecosystem relationships worth seeding directly rather than
# waiting for the extraction pipeline to (re)discover them from filings --
# see graph.py's RelationshipGraph.add. Deliberately limited to relationships
# that are industry-common-knowledge and specific (a named supplier of a
# named buyer), not the more speculative "sector exposure" links -- those are
# left for the LLM extraction pipeline to find (or not) in actual filings
# rather than asserted here as fact.
#
# Only DCO's edges are currently LIVE: UCTT and ICHR were demoted to anchors
# in the 2026-07 refresh, so their seeds now describe an anchor->anchor pair
# and propagate to nothing (engine.py skips signal_source_only targets).
# They are kept rather than deleted because they remain true, cost nothing
# (graph.add dedupes), and become live again the moment either name is
# promoted back to tradeable. Nothing is seeded for the newly added
# tradeables: their relationships should come from their own filings, which
# is exactly what the extraction pipeline is for.
SEED_RELATIONSHIPS: list[tuple[str, str, str, str, float]] = [
    # (from_symbol, to_symbol, rel_type, description, confidence)
    ("DCO", "RTX", "customer",
     "RTX (Raytheon) missile programs are a major customer relationship for Ducommun's structures/electronics.", 0.85),
    ("UCTT", "AMAT", "customer",
     "Applied Materials is a major customer of Ultra Clean Holdings' semiconductor equipment subsystems.", 0.85),
    ("UCTT", "LRCX", "customer",
     "Lam Research is a major customer of Ultra Clean Holdings' semiconductor equipment subsystems.", 0.85),
    ("ICHR", "AMAT", "customer",
     "Applied Materials is a major customer of Ichor Holdings' fluid delivery subsystems.", 0.85),
    ("ICHR", "LRCX", "customer",
     "Lam Research is a major customer of Ichor Holdings' fluid delivery subsystems.", 0.85),
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
