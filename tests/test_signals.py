from smartboi.dossier import Dossier
from smartboi.signals import evaluate


def _dossier(**overrides):
    base = dict(
        symbol="UCTT", direction="LONG", magnitude=0.8, confidence=0.8,
        horizon_days=20, thesis_summary="thesis", independent_source_count=2, status="ACTIVE",
    )
    base.update(overrides)
    return Dossier(**base)


def test_signal_fires_when_crossing_threshold():
    dossier = _dossier(confidence=0.8, magnitude=0.8, independent_source_count=2)
    signal = evaluate(dossier, confidence_threshold=0.5, min_independent_sources=2)
    assert signal is not None
    assert signal.symbol == "UCTT"
    assert signal.direction == "LONG"


def test_no_signal_below_threshold():
    dossier = _dossier(confidence=0.3, magnitude=0.3, independent_source_count=2)
    signal = evaluate(dossier, confidence_threshold=0.5, min_independent_sources=2)
    assert signal is None


def test_no_signal_with_too_few_independent_sources():
    dossier = _dossier(confidence=0.9, magnitude=0.9, independent_source_count=1)
    signal = evaluate(dossier, confidence_threshold=0.5, min_independent_sources=2)
    assert signal is None


def test_no_signal_when_direction_none():
    dossier = _dossier(direction="NONE", confidence=0.9, magnitude=0.9, independent_source_count=3)
    signal = evaluate(dossier, confidence_threshold=0.5, min_independent_sources=2)
    assert signal is None


def test_signaled_dossier_still_relogs_signals():
    # Without a price feed nothing ever resets SIGNALED back to ACTIVE, so
    # evaluation must be status-blind or each symbol could only ever log one
    # signal; the SIGNALED status only gates opening a paper trade (engine.py).
    dossier = _dossier(status="SIGNALED", confidence=0.9, magnitude=0.9, independent_source_count=3)
    signal = evaluate(dossier, confidence_threshold=0.5, min_independent_sources=2)
    assert signal is not None


def test_news_only_dossier_needs_the_higher_source_bar():
    # Two news publishers can be one reworded wire story that slipped past
    # dedup -- not enough for a news-only dossier when the stricter bar says 3.
    dossier = _dossier(independent_source_count=2, has_filing_evidence=False)
    assert evaluate(dossier, confidence_threshold=0.5, min_independent_sources=2,
                    min_independent_sources_news_only=3) is None
    dossier = _dossier(independent_source_count=3, has_filing_evidence=False)
    assert evaluate(dossier, confidence_threshold=0.5, min_independent_sources=2,
                    min_independent_sources_news_only=3) is not None


def test_filing_corroborated_dossier_keeps_the_normal_bar():
    # A filing is a primary disclosure -- immune to the reworded-wire-story
    # failure mode -- so filing-corroborated dossiers keep the normal bar.
    dossier = _dossier(independent_source_count=2, has_filing_evidence=True)
    assert evaluate(dossier, confidence_threshold=0.5, min_independent_sources=2,
                    min_independent_sources_news_only=3) is not None


def test_disclosed_link_evidence_keeps_the_normal_bar():
    """The elevated bar guards against two outlets rewording one wire story.
    Evidence propagated over a link a 10-K states outright (usually with a
    quantified share of revenue) is not in that failure mode: the part
    actually at risk of being wrong -- is the causal link real -- was
    answered by a primary source. Confirmed live: DCO sat at 17 agreeing
    items, mass 8.88, zero opposing, over 0.85-0.95 disclosed links to
    RTX/LMT/NOC, and could not act for want of a third publisher."""
    dossier = _dossier(independent_source_count=2, has_filing_evidence=False,
                       has_disclosed_link_evidence=True)
    assert evaluate(dossier, confidence_threshold=0.5, min_independent_sources=2,
                    min_independent_sources_news_only=3) is not None


def test_disclosed_link_backing_never_goes_below_the_base_bar():
    """Backing relaxes the ELEVATED bar back to the normal one -- it must
    never let a single uncorroborated source through."""
    dossier = _dossier(independent_source_count=1, has_filing_evidence=False,
                       has_disclosed_link_evidence=True)
    assert evaluate(dossier, confidence_threshold=0.5, min_independent_sources=2,
                    min_independent_sources_news_only=3) is None


def test_a_weakly_inferred_link_does_not_relax_the_bar():
    """Only STRONGLY disclosed edges count. A passing-mention or speculative
    link (live examples: FDX->GOOGL at 0.60, GTX->HON at 0.65, IESC->TSLA at
    0.30) is exactly the kind of causal claim the extra corroboration is
    for, so it must leave the elevated bar in place."""
    dossier = _dossier(independent_source_count=2, has_filing_evidence=False,
                       has_disclosed_link_evidence=False)
    assert evaluate(dossier, confidence_threshold=0.5, min_independent_sources=2,
                    min_independent_sources_news_only=3) is None


def test_news_only_bar_disabled_when_none():
    dossier = _dossier(independent_source_count=2, has_filing_evidence=False)
    assert evaluate(dossier, confidence_threshold=0.5, min_independent_sources=2,
                    min_independent_sources_news_only=None) is not None


# --- Entry timing: favorable_drift_pct / signal_expired ---

from datetime import datetime, timedelta, timezone

from smartboi.signals import favorable_drift_pct, signal_expired

NOW = datetime(2026, 7, 22, 12, 0, tzinfo=timezone.utc)


def test_favorable_drift_positive_for_long_moving_up():
    assert favorable_drift_pct("LONG", signaled_price=100.0, current_price=110.0) == 10.0


def test_favorable_drift_positive_for_short_moving_down():
    assert favorable_drift_pct("SHORT", signaled_price=100.0, current_price=90.0) == 10.0


def test_favorable_drift_negative_when_price_moved_against_the_thesis():
    assert favorable_drift_pct("LONG", signaled_price=100.0, current_price=95.0) == -5.0


def test_favorable_drift_zero_price_guard():
    assert favorable_drift_pct("LONG", signaled_price=0.0, current_price=10.0) == 0.0


def test_signal_not_expired_when_blank():
    assert not signal_expired("", deadline_days=5, now=NOW)


def test_signal_expired_past_deadline():
    signaled_at = (NOW - timedelta(days=6)).isoformat()
    assert signal_expired(signaled_at, deadline_days=5, now=NOW)


def test_signal_not_expired_before_deadline():
    signaled_at = (NOW - timedelta(days=2)).isoformat()
    assert not signal_expired(signaled_at, deadline_days=5, now=NOW)
