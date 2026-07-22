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
