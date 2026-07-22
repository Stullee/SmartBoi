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
