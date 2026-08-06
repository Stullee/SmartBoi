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


# --- Per-category budget shares. One shared pool is first-come-first-served,
# and the reset at UTC midnight (= 20:00 ET) hands extraction thirteen and a
# half hours of night to spend the whole day before the market opens. ---

from smartboi.usage import CAT_DOSSIER, CAT_EXTRACTION, CAT_RESEARCH, CAT_SYNTHESIS


def _tracker(tmp_path, **kw):
    return UsageTracker(
        tmp_path / "u.json", daily_call_budget=1000, daily_usd_budget=10.0,
        category_shares={CAT_EXTRACTION: 0.35, CAT_SYNTHESIS: 0.25, CAT_RESEARCH: 0.10},
        **kw,
    )


def test_extraction_cannot_spend_the_whole_day(tmp_path):
    """The live failure: budget exhausted before the US market opened, by the
    one pass whose output is not time-sensitive."""
    u = _tracker(tmp_path)
    # $3.50 = 35% of $10. Haiku at $1/$5 per MTok: 3.5M input tokens.
    u.record(3_500_000, 0, model="claude-haiku-4-5", category=CAT_EXTRACTION)

    assert not u.budget_remaining(CAT_EXTRACTION)
    # ...and the pass that turns news into a position is untouched.
    assert u.budget_remaining(CAT_DOSSIER)


def test_the_dossier_bucket_is_uncapped_and_can_use_the_whole_day(tmp_path):
    """Uncapped is the point: on a quiet day for filings it should be able to
    spend everything, which a fixed partition would forbid."""
    u = _tracker(tmp_path)
    u.record(6_000_000, 0, model="claude-haiku-4-5", category=CAT_DOSSIER)  # $6, past every share
    assert u.budget_remaining(CAT_DOSSIER)

    u.record(4_000_000, 0, model="claude-haiku-4-5", category=CAT_DOSSIER)  # $10 total
    assert not u.budget_remaining(CAT_DOSSIER)  # the TOTAL cap still binds


def test_one_category_exhausting_itself_does_not_block_the_others(tmp_path):
    u = _tracker(tmp_path)
    u.record(1_000_000, 0, model="claude-haiku-4-5", category=CAT_RESEARCH)  # $1 = 10%
    assert not u.budget_remaining(CAT_RESEARCH)
    for cat in (CAT_DOSSIER, CAT_SYNTHESIS, CAT_EXTRACTION):
        assert u.budget_remaining(cat), cat


def test_the_total_cap_still_overrides_an_unspent_share(tmp_path):
    """A category with room left must still be refused once the day is gone
    -- shares are caps, not reservations against the total."""
    u = _tracker(tmp_path)
    u.record(10_000_000, 0, model="claude-haiku-4-5", category=CAT_DOSSIER)
    assert not u.budget_remaining(CAT_EXTRACTION)  # its own 35% is untouched
    assert not u.budget_remaining(CAT_SYNTHESIS)


def test_a_zero_share_switches_a_category_off(tmp_path):
    """Useful for anything you don't want running unsupervised."""
    u = UsageTracker(tmp_path / "u.json", 1000, 10.0, category_shares={CAT_RESEARCH: 0.0})
    assert not u.budget_remaining(CAT_RESEARCH)
    assert u.budget_remaining(CAT_DOSSIER)


def test_a_share_of_one_is_uncapped(tmp_path):
    u = UsageTracker(tmp_path / "u.json", 1000, 10.0, category_shares={CAT_EXTRACTION: 1.0})
    u.record(9_000_000, 0, model="claude-haiku-4-5", category=CAT_EXTRACTION)
    assert u.budget_remaining(CAT_EXTRACTION)


def test_call_shares_bind_even_when_the_model_is_unpriced(tmp_path):
    """A mispriced/unknown model must not let a category consume the day with
    its dollar counter frozen at zero -- the call share is the backstop."""
    u = UsageTracker(tmp_path / "u.json", daily_call_budget=100, daily_usd_budget=0.0,
                     category_shares={CAT_EXTRACTION: 0.10})
    for _ in range(10):  # 10% of 100 calls
        u.record(1000, 100, category=CAT_EXTRACTION)  # no model -> no cost recorded

    assert not u.budget_remaining(CAT_EXTRACTION)
    assert u.budget_remaining(CAT_DOSSIER)


def test_categories_roll_over_at_utc_midnight_with_the_totals(tmp_path):
    u = _tracker(tmp_path)
    u.record(3_500_000, 0, today="2026-07-31", model="claude-haiku-4-5", category=CAT_EXTRACTION)
    assert not u.budget_remaining(CAT_EXTRACTION, today="2026-07-31")
    # Across a month boundary, which the 10-day window actually spans.
    assert u.budget_remaining(CAT_EXTRACTION, today="2026-08-01")
    assert u.snapshot(today="2026-08-01").by_category == {}


def test_the_snapshot_attributes_spend_per_category(tmp_path):
    u = _tracker(tmp_path)
    u.record(1_000_000, 0, model="claude-haiku-4-5", category=CAT_EXTRACTION)
    u.record(200_000, 0, model="claude-opus-5", category=CAT_SYNTHESIS)

    by_cat = u.snapshot().by_category
    assert by_cat[CAT_EXTRACTION] == (1.0, 1)
    assert by_cat[CAT_SYNTHESIS] == (1.0, 1)  # 0.2M input at $5/MTok
    assert u.snapshot().usd_spent == pytest.approx(2.0)


def test_an_uncategorised_call_still_counts_against_the_totals(tmp_path):
    """Back-compat: a call site that hasn't been given a category must not
    become invisible to the budget."""
    u = _tracker(tmp_path)
    u.record(10_000_000, 0, model="claude-haiku-4-5")
    assert not u.budget_remaining(CAT_DOSSIER)


# --- Reservations. A ceiling protected nothing: the total-budget check runs
# first, so once dossier had spent the day every other category was refused
# however much of its own share was untouched. Measured live after a week:
# dossier $6.47, extraction $3.54, synthesis $0.00 of a $2.50 ceiling -- and
# synthesis had therefore never run once in the system's entire life. ---


def _reserved(tmp_path):
    return UsageTracker(
        tmp_path / "u.json", daily_call_budget=5000, daily_usd_budget=10.0,
        category_shares={CAT_EXTRACTION: 0.35, CAT_SYNTHESIS: 0.25, CAT_RESEARCH: 0.0},
        category_reserved={CAT_SYNTHESIS: 0.15},
    )


def test_the_live_starvation_no_longer_happens(tmp_path):
    """The observed week, replayed through the GATE rather than forced past
    it -- record() only meters, budget_remaining() is what refuses."""
    u = _reserved(tmp_path)
    u.record(6_470_000, 0, model="claude-haiku-4-5", category=CAT_DOSSIER)   # $6.47

    # Extraction's own 35% ceiling ($3.50) is what stops it here, and even
    # without that the pool would run out at $8.50, not $10.
    assert u.budget_remaining(CAT_EXTRACTION)
    u.record(2_100_000, 0, model="claude-haiku-4-5", category=CAT_EXTRACTION)  # $2.10 -> $8.57

    assert not u.budget_remaining(CAT_DOSSIER)     # $8.50 pool is gone
    assert not u.budget_remaining(CAT_EXTRACTION)
    assert u.budget_remaining(CAT_SYNTHESIS)       # the reserved $1.50 survives


def test_an_uncapped_category_cannot_spend_a_reservation(tmp_path):
    u = _reserved(tmp_path)
    # $8.50 is the whole day minus synthesis's reserved $1.50.
    u.record(8_500_000, 0, model="claude-haiku-4-5", category=CAT_DOSSIER)

    assert not u.budget_remaining(CAT_DOSSIER)
    assert not u.budget_remaining(CAT_EXTRACTION)
    assert u.budget_remaining(CAT_SYNTHESIS)  # its own money, untouched


def test_a_spent_reservation_stops_shrinking_everyone_elses_pool(tmp_path):
    """The reservation must not waste budget: once synthesis has actually
    used it, the money is no longer set aside and dossier can have the rest."""
    u = _reserved(tmp_path)
    u.record(300_000, 0, model="claude-opus-5", category=CAT_SYNTHESIS)  # $1.50, its full reserve
    u.record(8_000_000, 0, model="claude-haiku-4-5", category=CAT_DOSSIER)  # $8.00

    # Nothing is set aside any more, so the last $0.50 is dossier's to take
    # -- the protection costs the other categories nothing once it is used.
    assert u.budget_remaining(CAT_DOSSIER)
    assert u.snapshot().usd_spent == pytest.approx(9.5)

    u.record(500_000, 0, model="claude-haiku-4-5", category=CAT_DOSSIER)  # $10.00
    assert not u.budget_remaining(CAT_DOSSIER)
    assert not u.budget_remaining(CAT_SYNTHESIS)        # reserve used, no special claim


def test_a_reservation_does_not_exempt_a_category_from_its_own_ceiling(tmp_path):
    u = _reserved(tmp_path)
    u.record(500_000, 0, model="claude-opus-5", category=CAT_SYNTHESIS)  # $2.50 = its 25% ceiling
    assert not u.budget_remaining(CAT_SYNTHESIS)


def test_no_reservation_configured_reproduces_the_old_behaviour(tmp_path):
    u = UsageTracker(tmp_path / "u.json", 5000, 10.0,
                     category_shares={CAT_SYNTHESIS: 0.25})
    u.record(10_000_000, 0, model="claude-haiku-4-5", category=CAT_DOSSIER)
    assert not u.budget_remaining(CAT_SYNTHESIS)
