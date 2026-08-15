import math
from datetime import datetime, timedelta, timezone

import pytest

from smartboi.config import Settings
from smartboi.dossier import (
    ECOSYSTEM_ASSOCIATION_CONFIDENCE,
    Dossier,
    DossierStore,
    EvidenceRecord,
    evidence_is_stale,
    evidence_weight,
    has_evidence,
    merge_evidence,
    recompute_decay,
)

# Fixed reference instant, decoupled from wall-clock time -- every test
# below passes NOW explicitly to merge_evidence/recompute_decay so results
# stay deterministic no matter when the suite actually runs.
NOW = datetime(2026, 7, 22, 12, 0, tzinfo=timezone.utc)


def _evidence(direction="LONG", confidence=0.7, magnitude=0.6, horizon_days=20, source_name="reuters.com",
              evidence_id="e1", published_at=None, relationship_confidence=None):
    return EvidenceRecord(
        evidence_id=evidence_id,
        source_type="news",
        source_name=source_name,
        url="https://example.com/a",
        headline="headline",
        published_at=published_at or NOW.isoformat(),
        origin_symbol="UCTT",
        is_propagated=False,
        relationship_note="",
        direction=direction,
        magnitude=magnitude,
        confidence=confidence,
        horizon_days=horizon_days,
        reasoning="because reasons",
        skeptic_note="",
        relationship_confidence=relationship_confidence,
    )


def test_a_skeptic_gutted_item_keeps_its_mass_but_loses_its_source_slot():
    """An item the skeptic scaled toward zero (confidence * decay weight <
    MIN_SOURCE_CONTRIBUTION) still adds decay mass, but no longer counts as an
    independent SOURCE. A slot lifts confidence AND multiplies magnitude, so a
    worthless item must not buy the corroboration that fires a trade."""
    dossier = Dossier(symbol="UCTT")
    merge_evidence(dossier, _evidence(evidence_id="e1", source_name="reuters.com", confidence=0.8), now=NOW)
    merge_evidence(dossier, _evidence(evidence_id="e2", source_name="bloomberg.com", confidence=0.8), now=NOW)
    assert dossier.independent_source_count == 2
    mass_before = dossier.mass_agree

    # A third, same-direction item the skeptic gutted to ~0 confidence.
    merge_evidence(dossier, _evidence(evidence_id="e3", source_name="cnbc.com", confidence=0.05), now=NOW)

    assert dossier.independent_source_count == 2  # 0.05 * ~1.0 < 0.15 -> no slot earned
    assert dossier.mass_agree > mass_before       # ...but its mass still counts


def test_first_evidence_sets_dossier_state():
    dossier = Dossier(symbol="UCTT")
    record = _evidence()
    merge_evidence(dossier, record, now=NOW)

    assert dossier.direction == "LONG"
    assert dossier.confidence == 0.7
    assert dossier.magnitude == 0.6
    assert dossier.horizon_days == 20
    assert dossier.independent_source_count == 1
    assert len(dossier.evidence) == 1


def test_corroborating_evidence_boosts_confidence():
    dossier = Dossier(symbol="UCTT")
    merge_evidence(dossier, _evidence(confidence=0.6, source_name="reuters.com", evidence_id="e1"), now=NOW)
    base_confidence = dossier.confidence

    merge_evidence(dossier, _evidence(confidence=0.6, source_name="bloomberg.com", evidence_id="e2"), now=NOW)

    assert dossier.independent_source_count == 2
    assert dossier.confidence > base_confidence
    assert dossier.confidence <= 1.0


def test_weak_disagreeing_evidence_does_not_flip_direction():
    dossier = Dossier(symbol="UCTT")
    merge_evidence(dossier, _evidence(direction="LONG", confidence=0.8, evidence_id="e1"), now=NOW)
    merge_evidence(dossier, _evidence(direction="SHORT", confidence=0.3, evidence_id="e2"), now=NOW)

    assert dossier.direction == "LONG"


def test_stronger_disagreeing_evidence_flips_direction():
    dossier = Dossier(symbol="UCTT")
    merge_evidence(dossier, _evidence(direction="LONG", confidence=0.4, evidence_id="e1"), now=NOW)
    merge_evidence(dossier, _evidence(direction="SHORT", confidence=0.9, evidence_id="e2"), now=NOW)

    assert dossier.direction == "SHORT"


def test_confidence_never_exceeds_one():
    dossier = Dossier(symbol="UCTT")
    for i in range(10):
        merge_evidence(
            dossier,
            _evidence(confidence=0.95, source_name=f"source{i}.com", evidence_id=f"e{i}"),
            now=NOW,
        )
    assert dossier.confidence <= 1.0


def test_dossier_store_round_trip(tmp_path):
    store = DossierStore(tmp_path / "dossiers")
    dossier = Dossier(symbol="UCTT")
    merge_evidence(dossier, _evidence(), now=NOW)
    store.save(dossier)

    reloaded = store.load("UCTT")
    assert reloaded.direction == "LONG"
    assert len(reloaded.evidence) == 1
    assert reloaded.evidence[0].source_name == "reuters.com"


def test_dossier_store_missing_symbol_returns_blank():
    store = DossierStore.__new__(DossierStore)  # avoid mkdir side effect for this check
    import tempfile
    from pathlib import Path

    with tempfile.TemporaryDirectory() as d:
        store = DossierStore(Path(d) / "dossiers")
        dossier = store.load("NOPE")
        assert dossier.symbol == "NOPE"
        assert dossier.direction == "NONE"
        assert dossier.evidence == []


def test_disagreeing_evidence_does_not_corrupt_source_count():
    # The count the signal gate reads must reflect sources agreeing with the
    # dossier's RESOLVED direction, not whatever direction the newest record had.
    dossier = Dossier(symbol="UCTT")
    merge_evidence(dossier, _evidence(direction="LONG", confidence=0.8, source_name="reuters.com", evidence_id="e1"), now=NOW)
    merge_evidence(dossier, _evidence(direction="LONG", confidence=0.8, source_name="bloomberg.com", evidence_id="e2"), now=NOW)
    merge_evidence(dossier, _evidence(direction="SHORT", confidence=0.3, source_name="ft.com", evidence_id="e3"), now=NOW)

    assert dossier.direction == "LONG"
    assert dossier.independent_source_count == 2  # the two LONG sources, not the SHORT one


def test_has_evidence_is_idempotence_guard():
    dossier = Dossier(symbol="UCTT")
    merge_evidence(dossier, _evidence(evidence_id="news:https://example.com/a:2026-07-21"), now=NOW)
    assert has_evidence(dossier, "news:https://example.com/a:2026-07-21")
    assert not has_evidence(dossier, "news:https://example.com/b:2026-07-21")


# --- Evidence time-decay ---

def test_evidence_weight_full_within_own_horizon():
    record = _evidence(horizon_days=20)
    assert evidence_weight(record, NOW) == 1.0
    assert evidence_weight(record, NOW + timedelta(days=20)) == 1.0


def test_evidence_weight_decays_past_horizon():
    record = _evidence(horizon_days=20)  # stale cutoff = 40 days (2x horizon)
    w30 = evidence_weight(record, NOW + timedelta(days=30))
    w39 = evidence_weight(record, NOW + timedelta(days=39))
    assert 0.15 < w39 < w30 < 1.0


def test_evidence_is_stale_past_cutoff():
    record = _evidence(horizon_days=20)  # cutoff = max(40, 14) = 40 days
    assert not evidence_is_stale(record, NOW + timedelta(days=39))
    assert evidence_is_stale(record, NOW + timedelta(days=41))


def test_evidence_is_stale_respects_minimum_floor():
    # A 1-day horizon would give a 2-day cutoff without the 14-day floor --
    # that's too aggressive for evidence that just landed.
    record = _evidence(horizon_days=1)
    assert not evidence_is_stale(record, NOW + timedelta(days=13))
    assert evidence_is_stale(record, NOW + timedelta(days=15))


def test_stale_evidence_excluded_from_aggregate():
    dossier = Dossier(symbol="UCTT")
    merge_evidence(dossier, _evidence(confidence=0.9, magnitude=0.9, horizon_days=10, evidence_id="e1"), now=NOW)
    assert dossier.confidence == 0.9

    # 10-day horizon -> stale past a 20-day cutoff; recompute long after that.
    later = NOW + timedelta(days=60)
    recompute_decay(dossier, later)

    assert dossier.confidence == 0.0
    assert dossier.magnitude == 0.0
    assert dossier.independent_source_count == 0
    # Mass-based resolution (see _side_mass): once the only evidence on a
    # side goes stale, that side's mass drops to zero: with nothing on
    # either side, direction resolves to NONE rather than staying "sticky"
    # at LONG with 0 confidence, which would be a contradictory label.
    assert dossier.direction == "NONE"


def test_recompute_decay_fades_confidence_without_new_evidence():
    dossier = Dossier(symbol="UCTT")
    merge_evidence(dossier, _evidence(confidence=0.9, magnitude=0.9, horizon_days=20), now=NOW)
    fresh_confidence = dossier.confidence

    recompute_decay(dossier, NOW + timedelta(days=35))  # within [horizon, cutoff]

    assert dossier.confidence < fresh_confidence
    assert dossier.confidence > 0.0


def test_recompute_decay_is_a_noop_within_horizon():
    dossier = Dossier(symbol="UCTT")
    merge_evidence(dossier, _evidence(confidence=0.8, magnitude=0.7, horizon_days=20), now=NOW)
    fresh_confidence, fresh_magnitude = dossier.confidence, dossier.magnitude

    recompute_decay(dossier, NOW + timedelta(days=5))  # still within its own horizon

    assert dossier.confidence == fresh_confidence
    assert dossier.magnitude == fresh_magnitude


# --- Contestedness: opposing evidence discounts confidence (dossier.py's
# _side_mass / mass-based direction resolution) ---

def test_uncontested_evidence_is_unaffected():
    dossier = Dossier(symbol="UCTT")
    merge_evidence(dossier, _evidence(direction="LONG", confidence=0.8, source_name="a.com", evidence_id="e1"), now=NOW)
    merge_evidence(dossier, _evidence(direction="LONG", confidence=0.8, source_name="b.com", evidence_id="e2"), now=NOW)
    # No opposing evidence at all -- contest factor is 1, confidence behaves
    # exactly as the pre-contestedness formula (mean + corroboration bonus).
    assert dossier.direction == "LONG"
    assert dossier.confidence == pytest.approx(min(1.0, 0.8 + 0.1))


def test_contested_evidence_partially_discounts_confidence():
    dossier = Dossier(symbol="UCTT")
    merge_evidence(dossier, _evidence(direction="LONG", confidence=0.8, source_name="a.com", evidence_id="e1"), now=NOW)
    merge_evidence(dossier, _evidence(direction="LONG", confidence=0.8, source_name="b.com", evidence_id="e2"), now=NOW)
    uncontested_confidence = dossier.confidence

    merge_evidence(dossier, _evidence(direction="SHORT", confidence=0.3, evidence_id="e3"), now=NOW)

    assert dossier.direction == "LONG"  # opposing mass (0.3) well below agreeing mass (1.6)
    assert 0.0 < dossier.confidence < uncontested_confidence


def test_evenly_contested_evidence_zeroes_confidence():
    dossier = Dossier(symbol="UCTT")
    merge_evidence(dossier, _evidence(direction="LONG", confidence=0.7, evidence_id="e1"), now=NOW)
    merge_evidence(dossier, _evidence(direction="SHORT", confidence=0.7, evidence_id="e2"), now=NOW)
    # Exact tie in mass -- direction stays at whatever it already was
    # (LONG, set by the first merge), but a 50/50 split is not a thesis:
    # confidence is fully discounted to zero.
    assert dossier.direction == "LONG"
    assert dossier.confidence == 0.0


def test_direction_flips_only_when_opposing_mass_exceeds_accumulated():
    dossier = Dossier(symbol="UCTT")
    merge_evidence(dossier, _evidence(direction="LONG", confidence=0.8, evidence_id="e1"), now=NOW)
    merge_evidence(dossier, _evidence(direction="LONG", confidence=0.8, evidence_id="e2"), now=NOW)
    # Accumulated LONG mass = 1.6; a single SHORT item at 0.9 can't outvote
    # an accumulated majority the way a single-item comparison could.
    merge_evidence(dossier, _evidence(direction="SHORT", confidence=0.9, evidence_id="e3"), now=NOW)
    assert dossier.direction == "LONG"


def test_stale_opposition_does_not_discount_confidence():
    dossier = Dossier(symbol="UCTT")
    old_short = _evidence(
        direction="SHORT", confidence=0.9, horizon_days=5, evidence_id="e_old_short",
        published_at=(NOW - timedelta(days=100)).isoformat(),
    )
    merge_evidence(dossier, old_short, now=NOW)
    merge_evidence(dossier, _evidence(direction="LONG", confidence=0.8, evidence_id="e_fresh_long"), now=NOW)
    # old_short's horizon (5d) gives a 14-day stale cutoff (the floor); at
    # 100 days old it's already excluded from mass entirely, so it must not
    # discount the fresh LONG evidence's confidence at all.
    assert dossier.direction == "LONG"
    assert dossier.confidence == 0.8


# --- Regression: agreeing evidence must never LOWER confidence. The old
# aggregation averaged agreeing items, so a dossier at 0.9 that gained a
# weak 0.2-confidence CORROBORATING item fell to ~0.65 -- supporting
# evidence moved the dossier AWAY from the signal threshold, inverting the
# accumulated-evidence premise. ---

def test_weak_agreeing_evidence_never_lowers_confidence():
    dossier = Dossier(symbol="UCTT")
    merge_evidence(dossier, _evidence(confidence=0.9, source_name="reuters.com", evidence_id="e1"), now=NOW)
    strong_alone = dossier.confidence

    merge_evidence(dossier, _evidence(confidence=0.2, source_name="bloomberg.com", evidence_id="e2"), now=NOW)

    assert dossier.confidence >= strong_alone


def test_aged_agreeing_evidence_does_not_drag_down_a_fresh_item():
    dossier = Dossier(symbol="UCTT")
    aged = _evidence(
        confidence=0.8, horizon_days=10, source_name="old.com", evidence_id="e_old",
        published_at=(NOW - timedelta(days=19)).isoformat(),  # near the decay floor, not yet stale
    )
    merge_evidence(dossier, aged, now=NOW)
    merge_evidence(dossier, _evidence(confidence=0.8, source_name="fresh.com", evidence_id="e_new"), now=NOW)

    # Fresh 0.8 item + a second independent (aged) source: at least the
    # fresh item's own strength plus the corroboration bonus.
    assert dossier.confidence >= 0.8


def test_lone_item_still_fades_with_age():
    dossier = Dossier(symbol="UCTT")
    merge_evidence(dossier, _evidence(confidence=0.9, magnitude=0.9, horizon_days=20, evidence_id="e1"), now=NOW)
    fresh = dossier.confidence

    recompute_decay(dossier, NOW + timedelta(days=35))  # between horizon and stale cutoff

    assert 0.0 < dossier.confidence < fresh


# --- Regression: a record with an empty/unparseable published_at must age
# from its MERGE time, not live at full weight forever (immortal evidence
# that decay could never fade or expire). ---

def test_unparseable_published_at_decays_from_merge_time():
    dossier = Dossier(symbol="UCTT")
    record = _evidence(confidence=0.9, magnitude=0.9, horizon_days=10, published_at="not-a-date", evidence_id="e1")
    merge_evidence(dossier, record, now=NOW)
    assert record.merged_at  # stamped at merge
    assert dossier.confidence == 0.9  # fresh at merge time

    recompute_decay(dossier, NOW + timedelta(days=60))  # far past the 20-day stale cutoff

    assert dossier.confidence == 0.0
    assert dossier.direction == "NONE"


def test_empty_published_at_decays_from_merge_time():
    dossier = Dossier(symbol="UCTT")
    merge_evidence(dossier, _evidence(confidence=0.9, horizon_days=10, published_at=" ", evidence_id="e1"), now=NOW)
    recompute_decay(dossier, NOW + timedelta(days=60))
    assert dossier.confidence == 0.0


def test_aggregate_tracks_filing_evidence_flag():
    # News-only agreeing evidence -> False; any filing source -> True; a
    # filing on the LOSING side must not count (it doesn't corroborate the
    # thesis being signaled).
    dossier = Dossier(symbol="UCTT")
    merge_evidence(dossier, _evidence(evidence_id="n1", source_name="reuters.com"), now=NOW)
    assert dossier.has_filing_evidence is False

    filing = _evidence(evidence_id="f1", source_name="SEC EDGAR (8-K)")
    filing.source_type = "8-K"
    merge_evidence(dossier, filing, now=NOW)
    assert dossier.has_filing_evidence is True


def test_losing_side_filing_does_not_set_the_flag():
    dossier = Dossier(symbol="UCTT")
    merge_evidence(dossier, _evidence(evidence_id="n1", confidence=0.9, source_name="reuters.com"), now=NOW)
    merge_evidence(dossier, _evidence(evidence_id="n2", confidence=0.9, source_name="bloomberg.com"), now=NOW)
    contrary_filing = _evidence(evidence_id="f1", direction="SHORT", confidence=0.2,
                                source_name="SEC EDGAR (Form 4)")
    contrary_filing.source_type = "4"
    merge_evidence(dossier, contrary_filing, now=NOW)
    assert dossier.direction == "LONG"
    assert dossier.has_filing_evidence is False


def test_distinct_edgar_filings_on_different_days_corroborate():
    """Two separate filing EVENTS (different dates) are distinct primary
    disclosures and count as two independent sources. They used to collapse to
    one slot per form ('SEC EDGAR (8-K)'), capping a filings-only thesis at
    roughly one source per form type."""
    dossier = Dossier(symbol="DCO")
    merge_evidence(dossier, _evidence(evidence_id="f1", source_name="SEC EDGAR (8-K)",
                                      published_at="2026-07-18T00:00:00+00:00"), now=NOW)
    merge_evidence(dossier, _evidence(evidence_id="f2", source_name="SEC EDGAR (8-K)",
                                      published_at="2026-07-20T00:00:00+00:00"), now=NOW)
    assert dossier.independent_source_count == 2


def test_edgar_filings_on_the_same_day_stay_one_source():
    """Multiple parts of ONE filing event (same date) are not independent
    corroboration -- the anti-double-count is preserved at day granularity."""
    dossier = Dossier(symbol="DCO")
    merge_evidence(dossier, _evidence(evidence_id="f1", source_name="SEC EDGAR (8-K)",
                                      published_at="2026-07-20T00:00:00+00:00"), now=NOW)
    merge_evidence(dossier, _evidence(evidence_id="f2", source_name="SEC EDGAR (8-K)",
                                      published_at="2026-07-20T09:30:00+00:00"), now=NOW)
    assert dossier.independent_source_count == 1


# --- Disclosed-link backing: whether the CAUSAL LINK behind a piece of
# propagated evidence came from a primary filing disclosure. Drives the
# corroboration bar in signals.evaluate. ---

def test_a_strongly_disclosed_link_marks_the_dossier_as_backed():
    dossier = Dossier(symbol="ULH")
    # Live shape: "General Motors is ULH's top customer, ~25% of revenues",
    # extracted from a 10-K at confidence 0.95.
    merge_evidence(dossier, _evidence(relationship_confidence=0.95), now=NOW)
    assert dossier.has_disclosed_link_evidence is True


def test_a_weakly_inferred_link_leaves_the_dossier_unbacked():
    dossier = Dossier(symbol="FDX")
    # Live shape: "Google Drive is integrated with FedEx Office" at 0.60 --
    # a real mention, not a revenue-material disclosed relationship.
    merge_evidence(dossier, _evidence(relationship_confidence=0.60), now=NOW)
    assert dossier.has_disclosed_link_evidence is False


def test_direct_unpropagated_evidence_leaves_the_dossier_unbacked():
    """Evidence about the company itself carries no relationship at all --
    there is no disclosed link to lean on, so the bar stays where it was."""
    dossier = Dossier(symbol="UCTT")
    merge_evidence(dossier, _evidence(relationship_confidence=None), now=NOW)
    assert dossier.has_disclosed_link_evidence is False


def test_backing_only_counts_on_the_agreeing_side():
    """A disclosed link behind evidence for the LOSING direction says
    nothing about the thesis the dossier actually resolved to."""
    dossier = Dossier(symbol="ULH")
    merge_evidence(dossier, _evidence(direction="SHORT", relationship_confidence=0.95,
                                      evidence_id="e1", confidence=0.3, magnitude=0.3), now=NOW)
    merge_evidence(dossier, _evidence(direction="LONG", relationship_confidence=None,
                                      evidence_id="e2", confidence=0.9, magnitude=0.9), now=NOW)
    assert dossier.direction == "LONG"
    assert dossier.has_disclosed_link_evidence is False


def test_backing_decays_away_with_the_evidence_that_carried_it():
    """Once the only disclosed-link evidence goes stale it stops counting,
    the same as every other aggregate -- a dossier cannot keep trading on a
    relaxed bar forever off one long-dead article."""
    dossier = Dossier(symbol="ULH")
    merge_evidence(dossier, _evidence(relationship_confidence=0.95, horizon_days=20), now=NOW)
    assert dossier.has_disclosed_link_evidence is True

    recompute_decay(dossier, NOW + timedelta(days=90))
    assert dossier.has_disclosed_link_evidence is False


# --- Magnitude accumulates with corroboration, exactly as confidence does.
# It used to be a bare max() -- the one aggregator invariant to N -- which
# made a strategy defined as accumulating small second-order effects unable
# to accumulate the very quantity that sizes the trade. ---

def test_magnitude_grows_with_independent_sources():
    dossier = Dossier(symbol="THRM")
    for i, src in enumerate(("reuters.com", "bloomberg.com", "wsj.com")):
        merge_evidence(dossier, _evidence(magnitude=0.22, source_name=src, evidence_id=f"e{i}"), now=NOW)

    assert dossier.independent_source_count == 3
    # Per DOUBLING, not per source: 0.22 * (1 + 0.25*log2(3)) = 0.307. The
    # linear form this replaced gave 0.33 here but ran away entirely once
    # the independence key stopped collapsing distinct counterparties onto
    # one publisher name -- a DCO-shaped dossier came out at magnitude 1.00.
    # See MAGNITUDE_CORROBORATION_STEP.
    assert dossier.magnitude == pytest.approx(0.22 * (1 + 0.25 * math.log2(3)))
    assert dossier.magnitude > 0.22  # ...but corroboration still grows it


def test_a_single_source_gets_no_magnitude_bonus():
    """A corroboration bonus without corroboration would just be a blanket
    threshold cut wearing a disguise."""
    dossier = Dossier(symbol="KULR")
    merge_evidence(dossier, _evidence(magnitude=0.25), now=NOW)

    assert dossier.independent_source_count == 1
    assert dossier.magnitude == pytest.approx(0.25)


def test_republishes_from_one_publisher_do_not_inflate_magnitude():
    """Keyed to DISTINCT source names, not item count -- otherwise N
    restatements of one wire story would size the trade."""
    dossier = Dossier(symbol="SIF")
    for i in range(4):
        merge_evidence(dossier, _evidence(magnitude=0.28, source_name="reuters.com",
                                          evidence_id=f"e{i}"), now=NOW)

    assert dossier.independent_source_count == 1
    assert dossier.magnitude == pytest.approx(0.28)


def test_magnitude_is_capped_at_one():
    dossier = Dossier(symbol="BIG")
    for i, src in enumerate(("a.com", "b.com", "c.com", "d.com", "e.com")):
        merge_evidence(dossier, _evidence(magnitude=0.9, source_name=src, evidence_id=f"e{i}"), now=NOW)

    assert dossier.magnitude == 1.0


def test_corroboration_only_counts_the_agreeing_side():
    """Opposing evidence must not size the trade it argues against."""
    dossier = Dossier(symbol="X")
    merge_evidence(dossier, _evidence(direction="LONG", magnitude=0.3, confidence=0.9,
                                      source_name="a.com", evidence_id="e1"), now=NOW)
    merge_evidence(dossier, _evidence(direction="SHORT", magnitude=0.3, confidence=0.2,
                                      source_name="b.com", evidence_id="e2"), now=NOW)

    assert dossier.direction == "LONG"
    assert dossier.independent_source_count == 1
    assert dossier.magnitude == pytest.approx(0.3)


# --- What counts as INDEPENDENT corroboration.
#
# The unit used to be the publisher name alone. That quietly capped the whole
# strategy: the live feed yields six publisher names for the entire universe
# with one aggregator ("Yahoo") accounting for ~69% of items, so a thesis
# built the way this system is designed to build one -- accumulating
# second-order evidence across several disclosed counterparties -- could
# hardly ever exceed two "independent sources". DCO carried 17 agreeing items
# across RTX, LMT and NOC and counted 2; items 3 through 17 contributed
# nothing to confidence and nothing to magnitude. ---

def _propagated(origin, source_name, evidence_id, magnitude=0.3, confidence=0.6):
    record = _evidence(magnitude=magnitude, confidence=confidence,
                       source_name=source_name, evidence_id=evidence_id)
    record.origin_symbol = origin
    record.is_propagated = True
    record.relationship_note = f"{origin} is a customer"
    record.relationship_confidence = 0.9
    return record


def test_a_corrupt_dossier_file_is_quarantined_not_silently_wiped(tmp_path):
    """A dossier is the permanent record of accumulated evidence, so a corrupt
    one must be renamed aside (recoverable) rather than silently overwritten by
    the next save."""
    store = DossierStore(tmp_path)
    (tmp_path / "UCTT.json").write_text("{ not valid json")

    dossier = store.load("UCTT")  # must not raise

    assert dossier.symbol == "UCTT"
    assert dossier.evidence == []
    quarantined = list(tmp_path.glob("UCTT.json.corrupt-*"))
    assert len(quarantined) == 1
    assert quarantined[0].read_text() == "{ not valid json"


def _ecosystem(origin, source_name, evidence_id, magnitude=0.3, confidence=0.6):
    """A propagated item over an ECOSYSTEM-association link (sector
    co-membership), the weakest propagation type -- relationship_confidence at
    ECOSYSTEM_ASSOCIATION_CONFIDENCE, not a disclosed counterparty edge."""
    record = _propagated(origin, source_name, evidence_id, magnitude, confidence)
    record.relationship_confidence = ECOSYSTEM_ASSOCIATION_CONFIDENCE
    record.relationship_note = f"{origin} and the target are both in the same ecosystem"
    return record


def test_ecosystem_association_fanout_is_constant_in_volume():
    """A correlated sector story fanned in from several anchors is ONE macro
    fact. The class collapses to exactly one collective slot, so the count is
    invariant to how many items, origins or publishers arrive -- which is what
    keeps pure fan-out from lifting the corroboration bonuses and saturating
    the score (AUDIT-2026-08-FOLLOWUP HIGH-3)."""
    dossier = Dossier(symbol="DCO")
    merge_evidence(dossier, _propagated("LMT", "Yahoo", "e0"), now=NOW)  # disclosed edge (0.9)
    merge_evidence(dossier, _ecosystem("ECO0", "Yahoo", "x0"), now=NOW)
    one_eco_sources = dossier.independent_source_count
    one_eco_conf, one_eco_mag = dossier.confidence, dossier.magnitude
    base_mass = dossier.mass_agree

    # Five more ecosystem items, all distinct origins AND distinct publishers.
    for i in range(1, 6):
        merge_evidence(dossier, _ecosystem(f"ECO{i}", f"Pub{i}", f"x{i}"), now=NOW)

    assert one_eco_sources == 2                            # 1 disclosed + 1 collective eco slot
    assert dossier.independent_source_count == 2           # UNCHANGED by 5x the volume
    assert dossier.confidence == pytest.approx(one_eco_conf)   # so no corroboration lift
    assert dossier.magnitude == pytest.approx(one_eco_mag)
    assert dossier.mass_agree > base_mass                  # but they DO still contribute mass
    assert dossier.ecosystem_slot_counted is True


def test_pure_ecosystem_fanout_cannot_single_handedly_qualify():
    """Eight ecosystem items from distinct anchors resolve a direction and earn
    the class its ONE slot -- which is below min_independent_sources, so a
    dossier built purely from sector fan-out still cannot clear the signal
    bar on its own. 'Can raise a thesis, can never single-handedly qualify
    one' is the stated design intent; one slot implements it, zero did not."""
    dossier = Dossier(symbol="DCO")
    for i in range(8):
        merge_evidence(dossier, _ecosystem(f"ECO{i}", "Yahoo", f"x{i}"), now=NOW)

    assert dossier.direction == "LONG"
    assert dossier.independent_source_count == 1
    assert dossier.independent_source_count < Settings().min_independent_sources


def test_different_counterparties_are_independent_even_from_one_publisher():
    """A Lockheed story and a Raytheon story are two different facts about
    Ducommun. Which outlet published each is irrelevant to whether they
    corroborate one another."""
    dossier = Dossier(symbol="DCO")
    merge_evidence(dossier, _propagated("LMT", "Yahoo", "e1"), now=NOW)
    merge_evidence(dossier, _propagated("RTX", "Yahoo", "e2"), now=NOW)
    merge_evidence(dossier, _propagated("NOC", "Yahoo", "e3"), now=NOW)

    assert dossier.independent_source_count == 3


def test_one_counterparty_via_one_publisher_stays_a_single_source():
    """The anti-syndication defence is untouched: the publisher is still part
    of the key, so repeated coverage of ONE company by ONE outlet cannot
    manufacture corroboration."""
    dossier = Dossier(symbol="DCO")
    merge_evidence(dossier, _propagated("LMT", "Yahoo", "e1"), now=NOW)
    merge_evidence(dossier, _propagated("LMT", "Yahoo", "e2"), now=NOW)

    assert dossier.independent_source_count == 1


def test_one_counterparty_across_two_publishers_still_counts_two():
    dossier = Dossier(symbol="DCO")
    merge_evidence(dossier, _propagated("LMT", "Yahoo", "e1"), now=NOW)
    merge_evidence(dossier, _propagated("LMT", "Benzinga", "e2"), now=NOW)

    assert dossier.independent_source_count == 2


def test_direct_evidence_is_still_keyed_on_the_publisher_alone():
    """Direct evidence has no counterparty to distinguish it -- the origin IS
    the dossier's own symbol, so folding it into the key would make every
    direct item from one publisher collapse exactly as before, and every
    item from two publishers count two. Unchanged behaviour, asserted."""
    dossier = Dossier(symbol="UCTT")
    merge_evidence(dossier, _evidence(source_name="reuters.com", evidence_id="e1"), now=NOW)
    merge_evidence(dossier, _evidence(source_name="reuters.com", evidence_id="e2"), now=NOW)
    assert dossier.independent_source_count == 1

    merge_evidence(dossier, _evidence(source_name="bloomberg.com", evidence_id="e3"), now=NOW)
    assert dossier.independent_source_count == 2


# --- Corroboration bonuses are per DOUBLING, not per source. Linear was
# defensible only while the count could not realistically exceed 2-3; with
# the independence key fixed, a well-corroborated dossier reaches 7+ and the
# linear form returned confidence 1.00 and magnitude 1.00 -- certainty and a
# maximum re-rating, from individually modest second-order items. ---

def test_corroboration_bonuses_are_unchanged_at_one_and_two_sources():
    """Where every live dossier actually sits. The recalibration must not be
    a stealth loosening at today's operating point."""
    one = Dossier(symbol="A")
    merge_evidence(one, _evidence(confidence=0.6, magnitude=0.4), now=NOW)
    assert one.confidence == pytest.approx(0.6)
    assert one.magnitude == pytest.approx(0.4)

    two = Dossier(symbol="B")
    merge_evidence(two, _evidence(confidence=0.6, magnitude=0.4, source_name="a.com", evidence_id="e1"), now=NOW)
    merge_evidence(two, _evidence(confidence=0.6, magnitude=0.4, source_name="b.com", evidence_id="e2"), now=NOW)
    assert two.confidence == pytest.approx(0.7)   # 0.6 + 0.10
    assert two.magnitude == pytest.approx(0.5)    # 0.4 * 1.25


def test_corroboration_growth_is_sublinear_and_bounded():
    dossier = Dossier(symbol="DCO")
    for i in range(16):
        merge_evidence(dossier, _propagated(f"ANCHOR{i}", "Yahoo", f"e{i}",
                                            magnitude=0.30, confidence=0.60), now=NOW)

    assert dossier.independent_source_count == 16
    # 0.60 + min(0.25, 0.10*log2(16)) = 0.60 + 0.25 -- capped, so accumulated
    # weak items can never manufacture near-certainty.
    assert dossier.confidence == pytest.approx(0.85)
    # Magnitude is now capped at the SAME doublings as confidence
    # (MAX_CORROBORATION_DOUBLINGS = 2.5): 0.30 * (1 + 0.25*2.5) = 0.30 * 1.625.
    # Previously it was 0.30 * (1 + 0.25*log2(16)) = 0.60, unbounded in S --
    # which is what let fan-out mass alone saturate the score.
    assert dossier.magnitude == pytest.approx(0.4875)


def test_magnitude_corroboration_is_bounded_past_the_doublings_cap():
    """Beyond MAX_CORROBORATION_DOUBLINGS more agreeing sources cannot lift the
    magnitude multiplier at all -- the guard against pure fan-out saturation.
    Eight sources already exceed the 2.5-doubling cap, so sixteen must give an
    identical (capped) magnitude, not a larger one."""
    def _mag(n_sources: int) -> float:
        d = Dossier(symbol=f"S{n_sources}")
        for i in range(n_sources):
            merge_evidence(d, _propagated(f"ANCHOR{i}", "Yahoo", f"e{i}",
                                          magnitude=0.30, confidence=0.60), now=NOW)
        return d.magnitude

    assert _mag(8) == pytest.approx(0.4875)
    assert _mag(16) == pytest.approx(_mag(8))  # bounded: no growth past the cap


# --- A competitor edge corroborates that two firms COMPETE, not that news
# transmits from one to the other. On the live graph it is the largest edge
# class (448 of 1066) with 84% at or above DISCLOSED_LINK_CONFIDENCE -- a
# higher clearance rate than customer (75%) or supplier (76%) -- so the most
# numerous and most sign-ambiguous class was relaxing the corroboration bar
# more often than the channels that actually carry causation. ---

def _typed(origin, rel_type, source_name, evidence_id, confidence=0.95):
    record = _propagated(origin, source_name, evidence_id)
    record.relationship_confidence = confidence
    record.relationship_type = rel_type
    return record


def test_a_competitor_edge_does_not_buy_the_corroboration_discount():
    dossier = Dossier(symbol="KLAC")
    merge_evidence(dossier, _typed("AMAT", "competitor", "Yahoo", "c1"), now=NOW)

    assert dossier.independent_source_count == 1, "it still claims a source slot"
    assert dossier.has_disclosed_link_evidence is False, "but not the discount"


def test_customer_and_supplier_edges_still_buy_it():
    for rel_type in ("customer", "supplier"):
        dossier = Dossier(symbol="DCO")
        merge_evidence(dossier, _typed("LMT", rel_type, "Yahoo", "d1"), now=NOW)
        assert dossier.has_disclosed_link_evidence is True, rel_type


def test_a_record_predating_the_field_keeps_the_old_behaviour():
    """Historical evidence carries no relationship_type. Treating it as
    'unknown, so excluded' would silently re-score the existing record --
    exactly what SCORING_VERSION exists to prevent."""
    dossier = Dossier(symbol="DCO")
    record = _propagated("LMT", "Yahoo", "old")   # relationship_type defaults to ""
    assert record.relationship_type == ""
    merge_evidence(dossier, record, now=NOW)

    assert dossier.has_disclosed_link_evidence is True


def test_a_competitor_edge_alongside_a_customer_edge_still_qualifies():
    """The flag is 'any agreeing item over a transmitting link', so one
    competitor item cannot revoke what a customer item established."""
    dossier = Dossier(symbol="DCO")
    merge_evidence(dossier, _typed("AMAT", "competitor", "Yahoo", "c1"), now=NOW)
    merge_evidence(dossier, _typed("LMT", "customer", "Reuters", "d1"), now=NOW)

    assert dossier.has_disclosed_link_evidence is True


# --- New primary source types (regulatory documents, DoD contract awards).
# has_filing_evidence is "not news", and its purpose is to say the evidence
# cannot be two outlets rewording one wire story. A government document
# cannot be. But the LINK still has to carry it. ---

def test_a_direct_contract_award_is_primary_evidence():
    """A DoD award to Ducommun IS news about Ducommun -- direct evidence, no
    edge involved -- and a government announcement is not a reworded article."""
    dossier = Dossier(symbol="DCO")
    record = _evidence(source_name="DoD Contract Announcements", evidence_id="a1")
    record.source_type = "contract_award"
    merge_evidence(dossier, record, now=NOW)

    assert dossier.has_filing_evidence is True


def test_a_regulatory_document_over_a_weak_edge_is_not_primary_backing():
    """The document is primary, but it reached this dossier over a seeded
    regulator edge at 0.60-0.80 -- below DISCLOSED_LINK_CONFIDENCE. A
    sector-wide rule can raise a thesis; it must not also relax the
    corroboration bar, which is a claim about the LINK, not the source."""
    dossier = Dossier(symbol="AOSL")
    record = _typed("BIS", "regulator", "Federal Register (BIS/entity-list)", "r1",
                    confidence=0.80)
    record.source_type = "regulatory"
    merge_evidence(dossier, record, now=NOW)

    assert dossier.has_filing_evidence is False
    assert dossier.has_disclosed_link_evidence is False


def test_two_proceedings_by_one_agency_stay_independent():
    """The search key is in the source name, so one agency running two
    unrelated rulemakings is two facts. Collapsing them would cap a regulatory
    thesis at one source per agency forever."""
    dossier = Dossier(symbol="AOSL")
    for key in ("entity-list", "semi-export-controls"):
        record = _typed("BIS", "regulator", f"Federal Register (BIS/{key})", key, confidence=0.80)
        record.source_type = "regulatory"
        merge_evidence(dossier, record, now=NOW)

    assert dossier.independent_source_count == 2


# --- Corroboration paid on facts, not channels (SCORING_VERSION 7) --------


def _fanned_out_dossier(sources=8):
    """One macro story arriving over `sources` distinct channels -- the exact
    shape that produced a 0.94 arithmetic score on a body of evidence
    synthesis rated at 0.12."""
    dossier = Dossier(symbol="BWEN")
    for n in range(sources):
        merge_evidence(
            dossier,
            _evidence(source_name=f"outlet{n}.com", evidence_id=f"e{n}"),
            now=NOW,
        )
    return dossier


def test_corroboration_is_capped_by_the_distinct_fact_count():
    """Twelve channels reporting one fact is one fact. The bonus is paid on
    what synthesis counted, not on how many places it was republished."""
    inflated = _fanned_out_dossier()
    recompute_decay(inflated, NOW)

    honest = _fanned_out_dossier()
    honest.synthesis_at = NOW.isoformat()
    honest.distinct_fact_count = 1
    recompute_decay(honest, NOW)

    assert honest.confidence < inflated.confidence
    assert honest.magnitude < inflated.magnitude
    # One fact earns no corroboration bonus at all: log2(1) == 0.
    assert honest.confidence == pytest.approx(honest.evidence[0].confidence)


def test_a_dossier_that_never_synthesised_keeps_its_full_corroboration():
    """distinct_fact_count is 0 until a dossier reaches the synthesis floor.
    Reading that as 'zero facts' would zero the corroboration of every
    un-synthesised dossier in the system -- the opposite of the intent."""
    never = _fanned_out_dossier()
    assert never.distinct_fact_count == 0
    recompute_decay(never, NOW)

    baseline = _fanned_out_dossier()
    recompute_decay(baseline, NOW)
    assert never.confidence == pytest.approx(baseline.confidence)


def test_a_stale_verdict_stops_capping_corroboration():
    """Same lapse rule the merge-path cap has: once the daily pass stops
    refreshing a dossier the ceiling lifts, rather than suppressing it
    forever on a judgement nobody has revisited."""
    stale = _fanned_out_dossier()
    stale.synthesis_at = (NOW - timedelta(hours=48)).isoformat()
    stale.distinct_fact_count = 1
    recompute_decay(stale, NOW)

    uncapped = _fanned_out_dossier()
    recompute_decay(uncapped, NOW)
    assert stale.confidence == pytest.approx(uncapped.confidence)


def test_the_fact_count_can_only_cap_never_lift():
    """Synthesis is allowed to veto and to trim, never to raise a score into
    a trade. A fact count ABOVE the channel count must change nothing."""
    generous = _fanned_out_dossier(sources=2)
    generous.synthesis_at = NOW.isoformat()
    generous.distinct_fact_count = 99
    recompute_decay(generous, NOW)

    baseline = _fanned_out_dossier(sources=2)
    recompute_decay(baseline, NOW)
    assert generous.confidence == pytest.approx(baseline.confidence)


def test_the_source_count_gate_is_left_alone():
    """Only the BONUS is paid on facts. independent_source_count still counts
    disclosure channels, because the signal bar's corroboration gates were
    written and tested against that meaning."""
    capped = _fanned_out_dossier()
    capped.synthesis_at = NOW.isoformat()
    capped.distinct_fact_count = 1
    recompute_decay(capped, NOW)

    assert capped.independent_source_count == 8


def test_the_fact_cap_never_hides_a_dossier_from_the_pass_that_capped_it():
    """The decay pass only synthesises a dossier whose score clears
    signal_confidence_threshold * synthesis_score_floor_pct. Gating that on
    the CAPPED score is circular -- a verdict suppresses the re-judgement that
    would renew it -- and the circle fails open: the verdict goes stale at
    36h, the cap lapses, and the score springs back to the arithmetic the
    verdict rejected, above the signal bar, free to fire.

    Reproduced at base 0.60/0.45 over eight channels carrying one distinct
    fact: 0.622 uncapped, 0.270 capped, against a 0.30 floor and a 0.50 bar.
    arithmetic_score is what keeps the pass scheduled on the arithmetic."""
    dossier = Dossier(symbol="BWEN")
    for n in range(8):
        merge_evidence(dossier, _evidence(source_name=f"outlet{n}.com", evidence_id=f"e{n}",
                                          confidence=0.60, magnitude=0.45), now=NOW)
    dossier.synthesis_at = NOW.isoformat()
    dossier.distinct_fact_count = 1
    recompute_decay(dossier, NOW)

    floor, bar = 0.5 * 0.6, 0.5
    capped = dossier.confidence * dossier.magnitude
    assert capped < floor, "precondition: the cap must push this under the synthesis floor"
    # The uncapped arithmetic is recorded, is above the floor, and is what the
    # pass is scheduled against -- so the verdict keeps being refreshed and
    # the cap holds continuously instead of lapsing every 36h.
    assert dossier.arithmetic_score > floor
    assert dossier.arithmetic_score > bar, "and it would clear the signal bar if it ever lapsed"


def test_the_uncapped_arithmetic_is_recorded_even_with_no_verdict():
    """It is the arithmetic, not a synthesis artefact: a dossier that has
    never been judged must still carry it, or the floor gate falls back to a
    capped score on the one pass that would have set the verdict."""
    dossier = _fanned_out_dossier()
    recompute_decay(dossier, NOW)

    assert dossier.arithmetic_score == pytest.approx(dossier.confidence * dossier.magnitude)


# --- Independence counted per fact, not per channel (SCORING_VERSION 8) ---


def _from_anchor(origin, source_name, fact_key="", evidence_id=None):
    r = _evidence(source_name=source_name, evidence_id=evidence_id or f"{origin}-{source_name}")
    return EvidenceRecord(**{**r.__dict__, "origin_symbol": origin, "is_propagated": True,
                             "relationship_confidence": 0.95, "fact_key": fact_key})


def _scored(records):
    d = Dossier(symbol="X")
    for r in records:
        merge_evidence(d, r, now=NOW)
    recompute_decay(d, NOW)
    return d


def test_three_anchors_reporting_one_event_count_as_one_source():
    """BWEN, live: MSFT, META and EQIX items over disclosed edges in one
    window, which the whole-body pass read as essentially ONE fact -- the same
    quarter's AI capex, reported three times. The origin-symbol key counted
    three independent corroborations."""
    d = _scored([
        _from_anchor("MSFT", "Yahoo", "ai datacenter capex q2 2026"),
        _from_anchor("META", "Benzinga", "ai datacenter capex q2 2026"),
        _from_anchor("EQIX", "SeekingAlpha", "ai datacenter capex q2 2026"),
    ])
    assert d.independent_source_count == 1


def test_three_anchors_reporting_three_events_still_count_as_three():
    """DCO, live: RTX, LMT and NOC over disclosed edges in one window, which
    the same pass read as SEVEN OR EIGHT distinct facts. Structurally
    identical to the case above in every field a key can see -- so the fix
    must not collapse it. This is the regression v5 was written to prevent."""
    d = _scored([
        _from_anchor("RTX", "Yahoo", "rtx fy guidance raise"),
        _from_anchor("LMT", "Yahoo", "lmt sentinel contract award"),
        _from_anchor("NOC", "Yahoo", "noc missile production surge"),
    ])
    assert d.independent_source_count == 3


def test_two_publishers_on_one_company_event_are_one_fact():
    """The publisher key counted these as two. A company's earnings reported
    by Yahoo and by Benzinga is one event."""
    d = _scored([
        _evidence(source_name="Yahoo", evidence_id="a"),
        _evidence(source_name="Benzinga", evidence_id="b"),
    ])
    assert d.independent_source_count == 2  # no fact key: unchanged behaviour

    labelled = _scored([
        EvidenceRecord(**{**_evidence(source_name="Yahoo", evidence_id="a").__dict__,
                          "fact_key": "x q2 earnings beat"}),
        EvidenceRecord(**{**_evidence(source_name="Benzinga", evidence_id="b").__dict__,
                          "fact_key": "x q2 earnings beat"}),
    ])
    assert labelled.independent_source_count == 1


def test_a_label_can_never_split_what_the_channel_already_merged():
    """THRM/STRT/ULH, live: one Ford announcement labelled "ford phases out
    china lincoln imports" and "ford increases lincoln us production 2030" --
    two labels, one event. Both arrive via F|Yahoo, so the channel key minted
    ONE slot and the fact key minted two.

    Measured across the whole first labelled cohort (108 items, everything
    merged after the 2026-08-13 cutover), the fact key produced 92 slots
    against the channel key's 64: a +43.8% split, 13 dossiers splitting, 18
    holding, none collapsing. The corroboration bonus is convex in the slot
    count, so every split inflates a score. The mechanism was introduced to
    MERGE; it must never be allowed to shatter."""
    d = _scored([
        _from_anchor("F", "Yahoo", "ford phases out china lincoln imports", evidence_id="a"),
        _from_anchor("F", "Yahoo", "ford increases lincoln us production 2030", evidence_id="b"),
    ])
    assert d.independent_source_count == 1


def test_the_guard_still_lets_a_label_merge_across_channels():
    """The guard takes the SMALLER partition, so the merge the fact key was
    built for is untouched: three origins, three channels, one label -> one
    slot. Only splitting is unreachable."""
    d = _scored([
        _from_anchor("MSFT", "Yahoo", "ai datacenter capex q2 2026"),
        _from_anchor("META", "Benzinga", "ai datacenter capex q2 2026"),
        _from_anchor("EQIX", "SeekingAlpha", "ai datacenter capex q2 2026"),
    ])
    assert d.independent_source_count == 1


def test_a_paraphrased_label_still_collapses():
    """The updater is shown the existing labels and told to reuse them, which
    is the real mechanism -- but normalisation is the backstop for when it
    varies case, punctuation or a trailing clause."""
    d = _scored([
        _from_anchor("MSFT", "Yahoo", "AI Datacenter Capex, Q2 2026"),
        _from_anchor("META", "Benzinga", "ai datacenter capex  q2 2026"),
        _from_anchor("EQIX", "CNBC", "ai datacenter capex q2 2026!"),
    ])
    assert d.independent_source_count == 1


def test_evidence_without_a_fact_key_scores_exactly_as_before():
    """Existing dossiers were merged before this field existed, and a model
    can omit it. Both must score under the previous rules rather than
    collapsing to a single slot."""
    d = _scored([
        _from_anchor("RTX", "Yahoo"),
        _from_anchor("LMT", "Yahoo"),
        _from_anchor("NOC", "Benzinga"),
    ])
    assert d.independent_source_count == 3


def test_the_updater_is_shown_the_labels_already_on_the_dossier():
    """A closed vocabulary the model can see beats fuzzy-matching free text
    afterwards, so the existing labels have to reach the prompt."""
    from smartboi.dossier import fact_keys_on

    d = _scored([
        _from_anchor("MSFT", "Yahoo", "AI Datacenter Capex Q2 2026"),
        _from_anchor("LMT", "Yahoo", "lmt sentinel contract award"),
    ])
    keys = fact_keys_on(d, NOW)
    assert "ai datacenter capex q2 2026" in keys
    assert "lmt sentinel contract award" in keys


def test_stale_evidence_does_not_keep_offering_its_label():
    """A label whose evidence has aged out is not a fact the dossier still
    holds, and offering it would invite the model to re-attach new evidence
    to a dead event."""
    from datetime import timedelta

    from smartboi.dossier import fact_keys_on

    old = _from_anchor("MSFT", "Yahoo", "ai datacenter capex q1 2026")
    old = EvidenceRecord(**{**old.__dict__,
                            "published_at": (NOW - timedelta(days=400)).isoformat()})
    d = _scored([old])
    assert fact_keys_on(d, NOW) == []
