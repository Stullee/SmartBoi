from smartboi.ratelimit import SlidingWindowLimiter


def test_allows_up_to_max_events_within_window():
    limiter = SlidingWindowLimiter(max_events=3, window_sec=3600)
    assert limiter.allow("a", 0.0)
    assert limiter.allow("a", 10.0)
    assert limiter.allow("a", 20.0)
    assert not limiter.allow("a", 30.0)


def test_events_expire_out_of_the_window():
    limiter = SlidingWindowLimiter(max_events=2, window_sec=100)
    assert limiter.allow("a", 0.0)
    assert limiter.allow("a", 10.0)
    assert not limiter.allow("a", 20.0)
    # The first two events are now more than 100s in the past.
    assert limiter.allow("a", 150.0)


def test_keys_are_independent():
    limiter = SlidingWindowLimiter(max_events=1, window_sec=3600)
    assert limiter.allow("a", 0.0)
    assert not limiter.allow("a", 1.0)
    assert limiter.allow("b", 1.0)  # a different key has its own budget


def test_would_allow_does_not_record():
    limiter = SlidingWindowLimiter(max_events=1, window_sec=3600)
    assert limiter.would_allow("a", 0.0)
    assert limiter.would_allow("a", 1.0)  # peeking again still True -- nothing was recorded
    assert limiter.would_allow("a", 2.0)


def test_record_consumes_budget_checked_by_would_allow():
    limiter = SlidingWindowLimiter(max_events=1, window_sec=3600)
    assert limiter.would_allow("a", 0.0)
    limiter.record("a", 0.0)
    assert not limiter.would_allow("a", 1.0)


def test_deferred_attempt_does_not_double_charge():
    # Simulates: pre-filter says yes (would_allow), the actual work fails
    # and is retried later without ever calling record -- the retry's own
    # would_allow check must still see the slot as free.
    limiter = SlidingWindowLimiter(max_events=1, window_sec=3600)
    assert limiter.would_allow("a", 0.0)  # first attempt, pre-filter passes
    # ... work fails, nothing recorded ...
    assert limiter.would_allow("a", 5.0)  # retry's pre-filter still passes
    limiter.record("a", 5.0)  # retry succeeds this time
    assert not limiter.would_allow("a", 6.0)
