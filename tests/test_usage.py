import pytest

from smartboi.usage import UsageTracker


def test_budget_remaining_true_when_under_cap(tmp_path):
    tracker = UsageTracker(tmp_path / "usage.json", daily_call_budget=2)
    assert tracker.budget_remaining(today="2026-07-22")


def test_budget_exhausted_after_reaching_cap(tmp_path):
    tracker = UsageTracker(tmp_path / "usage.json", daily_call_budget=2)
    tracker.record(100, 50, today="2026-07-22")
    tracker.record(100, 50, today="2026-07-22")
    assert not tracker.budget_remaining(today="2026-07-22")


def test_record_accumulates_tokens(tmp_path):
    tracker = UsageTracker(tmp_path / "usage.json", daily_call_budget=10)
    tracker.record(100, 50, today="2026-07-22")
    tracker.record(200, 75, today="2026-07-22")
    snap = tracker.snapshot(today="2026-07-22")
    assert snap.calls == 2
    assert snap.input_tokens == 300
    assert snap.output_tokens == 125


def test_budget_resets_on_a_new_day(tmp_path):
    tracker = UsageTracker(tmp_path / "usage.json", daily_call_budget=1)
    tracker.record(100, 50, today="2026-07-22")
    assert not tracker.budget_remaining(today="2026-07-22")
    assert tracker.budget_remaining(today="2026-07-23")


def test_usage_persists_across_instances_within_the_same_day(tmp_path):
    path = tmp_path / "usage.json"
    tracker = UsageTracker(path, daily_call_budget=10)
    tracker.record(100, 50, today="2026-07-22")

    reloaded = UsageTracker(path, daily_call_budget=10)
    snap = reloaded.snapshot(today="2026-07-22")
    assert snap.calls == 1
    assert snap.input_tokens == 100


# --- USD metering.
#
# The call cap alone stopped being a usable spend proxy the moment per-call
# cost could span an order of magnitude across configured models and adaptive
# thinking made output tokens unbounded by max_tokens. A 3000-call day costs
# a few dollars on Haiku and several hundred on Opus. ---

def test_spend_is_metered_per_model(tmp_path):
    tracker = UsageTracker(tmp_path / "u.json", daily_call_budget=100, daily_usd_budget=10.0)
    tracker.record(1_000_000, 0, model="claude-haiku-4-5")   # $1.00 in
    tracker.record(0, 1_000_000, model="claude-haiku-4-5")   # $5.00 out

    assert tracker.snapshot().usd_spent == pytest.approx(6.0)


def test_an_exhausted_dollar_budget_stops_calls_even_with_calls_left(tmp_path):
    tracker = UsageTracker(tmp_path / "u.json", daily_call_budget=1000, daily_usd_budget=1.0)
    assert tracker.budget_remaining() is True

    tracker.record(1_000_000, 0, model="claude-opus-5")  # $5.00, over the $1 cap

    assert tracker.budget_remaining() is False
    assert tracker.snapshot().calls == 1  # nowhere near the 1000-call cap


def test_a_zero_dollar_budget_means_call_cap_only(tmp_path):
    """The pre-existing semantics, kept reachable."""
    tracker = UsageTracker(tmp_path / "u.json", daily_call_budget=2, daily_usd_budget=0.0)
    tracker.record(1_000_000, 1_000_000, model="claude-opus-5")  # $30
    assert tracker.budget_remaining() is True


def test_an_unknown_model_is_priced_at_the_most_expensive_entry(tmp_path):
    """A model-string typo must never look free -- the dollar budget is the
    only thing between it and an unbounded bill."""
    tracker = UsageTracker(tmp_path / "u.json", daily_call_budget=100, daily_usd_budget=100.0)
    tracker.record(1_000_000, 0, model="claude-totally-made-up")

    assert tracker.snapshot().usd_spent == pytest.approx(10.0)  # Fable-tier input price


def test_spend_resets_with_the_rest_of_the_daily_counters(tmp_path):
    tracker = UsageTracker(tmp_path / "u.json", daily_call_budget=100, daily_usd_budget=10.0)
    tracker.record(1_000_000, 0, today="2026-07-28", model="claude-opus-5")
    assert tracker.snapshot(today="2026-07-28").usd_spent == pytest.approx(5.0)

    assert tracker.snapshot(today="2026-07-29").usd_spent == 0.0


def test_recording_without_a_model_still_counts_the_call(tmp_path):
    """A metering gap is recoverable; a crashed ingestion pass is not."""
    tracker = UsageTracker(tmp_path / "u.json", daily_call_budget=100, daily_usd_budget=10.0)
    tracker.record(1000, 100)

    snapshot = tracker.snapshot()
    assert snapshot.calls == 1
    assert snapshot.usd_spent == 0.0
