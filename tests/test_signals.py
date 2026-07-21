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


def test_no_signal_when_already_signaled():
    dossier = _dossier(status="SIGNALED", confidence=0.9, magnitude=0.9, independent_source_count=3)
    signal = evaluate(dossier, confidence_threshold=0.5, min_independent_sources=2)
    assert signal is None
