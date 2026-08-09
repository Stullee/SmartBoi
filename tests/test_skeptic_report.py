import json

from smartboi.skeptic_report import analyze_skeptic_effect, format_skeptic_report


def _acc(is_propagated=False, model="haiku", pc=0.6, pm=0.5, c=0.6, m=0.5):
    return {"is_propagated": is_propagated, "model": model,
            "proposed_confidence": pc, "proposed_magnitude": pm, "confidence": c, "magnitude": m}


def test_refutation_rate_is_over_all_judged_items():
    accepted = [_acc(), _acc()]  # 2 passed the skeptic
    refutations = [{"is_propagated": False, "model": "haiku"},
                   {"is_propagated": True, "model": "haiku"}]  # 2 refuted
    a = analyze_skeptic_effect(accepted, refutations)
    assert a["overall"]["n_accepted"] == 2
    assert a["overall"]["n_refuted"] == 2
    assert a["overall"]["refutation_rate"] == 0.5


def test_scaling_distribution_and_deltas():
    accepted = [
        _acc(pc=0.8, pm=0.8, c=0.4, m=0.5),   # 0.64 -> 0.20  scaled DOWN
        _acc(pc=0.3, pm=0.3, c=0.5, m=0.5),   # 0.09 -> 0.25  scaled UP
        _acc(pc=0.5, pm=0.5, c=0.5, m=0.5),   # 0.25 -> 0.25  unchanged
    ]
    o = analyze_skeptic_effect(accepted, [])["overall"]
    assert (o["n_scaled_down"], o["n_scaled_up"], o["n_unchanged"]) == (1, 1, 1)
    assert round(o["mean_confidence_delta"], 4) == round((-0.4 + 0.2 + 0.0) / 3, 4)


def test_splits_by_type_and_model():
    accepted = [_acc(is_propagated=True, model="opus"), _acc(is_propagated=False, model="haiku")]
    refutations = [{"is_propagated": True, "model": "opus"}]
    a = analyze_skeptic_effect(accepted, refutations)
    assert a["type:propagated"]["n_accepted"] == 1 and a["type:propagated"]["n_refuted"] == 1
    assert a["type:direct"]["n_refuted"] == 0
    assert a["model:opus"]["n_refuted"] == 1
    assert a["model:haiku"]["n_accepted"] == 1


def test_legacy_records_without_pre_numbers_count_but_do_not_skew_deltas():
    a = analyze_skeptic_effect([_acc(pc=None, pm=None)], [])
    assert a["overall"]["n_accepted"] == 1
    assert a["overall"]["mean_confidence_delta"] is None  # no proposed_* -> no delta
    assert a["overall"]["n_scaled_down"] == 0


def test_format_handles_no_activity():
    assert "No skeptic activity" in format_skeptic_report(analyze_skeptic_effect([], []))


def test_format_includes_the_split_sections():
    a = analyze_skeptic_effect([_acc(is_propagated=True)], [{"is_propagated": False, "model": "haiku"}])
    report = format_skeptic_report(a)
    assert "Skeptic effect" in report
    assert "by evidence type" in report
    assert "by skeptic model" in report


def test_run_skeptic_report_reads_dossiers_and_refutations(tmp_path):
    from smartboi.dossier import Dossier, DossierStore, EvidenceRecord, merge_evidence
    from smartboi.tools import run_skeptic_report

    store = DossierStore(tmp_path / "dossiers")
    d = Dossier(symbol="FORM")
    merge_evidence(d, EvidenceRecord(
        evidence_id="e1", source_type="news", source_name="reuters.com", url="u", headline="h",
        published_at="2026-07-23", origin_symbol="FORM", is_propagated=False, relationship_note="",
        direction="LONG", magnitude=0.5, confidence=0.5, horizon_days=20, reasoning="r",
        skeptic_note="", reviewed_by_model="haiku", proposed_confidence=0.8, proposed_magnitude=0.8))
    store.save(d)
    (tmp_path / "logs").mkdir()
    (tmp_path / "logs" / "skeptic_refutations.jsonl").write_text(
        json.dumps({"is_propagated": True, "model": "haiku"}) + "\n")

    report = run_skeptic_report(tmp_path / "logs", store)
    assert "Skeptic effect" in report and "haiku" in report
