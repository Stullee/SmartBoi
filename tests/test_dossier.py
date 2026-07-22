from datetime import datetime, timedelta, timezone

from smartboi.dossier import (
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
              evidence_id="e1", published_at=None):
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
    )


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
    assert dossier.direction == "LONG"  # direction itself doesn't revert on decay


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
