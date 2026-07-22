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
