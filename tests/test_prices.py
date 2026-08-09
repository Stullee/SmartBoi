"""IB circuit-breaker tests. The breaker STATE machine is pure (no IB
connection), and last_bars' skip/short-circuit behaviour is exercised with a
monkeypatched last_bar so no Gateway is needed."""
import smartboi.prices as prices
from smartboi.prices import ReadOnlyPriceFeed, _IB_BREAKER_THRESHOLD


def _feed():
    # Constructs an IB() but never connects -- only the breaker helpers are used.
    return ReadOnlyPriceFeed("localhost", 4002, 1)


def test_breaker_opens_after_consecutive_failures():
    feed = _feed()
    for _ in range(_IB_BREAKER_THRESHOLD - 1):
        feed._record_ib_failure()
    assert not feed._breaker_open()  # not yet
    feed._record_ib_failure()        # crosses the threshold
    assert feed._breaker_open()


def test_a_success_resets_the_failure_streak():
    feed = _feed()
    for _ in range(_IB_BREAKER_THRESHOLD - 1):
        feed._record_ib_failure()
    feed._record_ib_success()  # IB answered -- streak cleared
    for _ in range(_IB_BREAKER_THRESHOLD - 1):
        feed._record_ib_failure()
    assert not feed._breaker_open()  # the reset means we never reached the threshold


async def test_last_bars_skips_ib_entirely_while_the_breaker_is_open(monkeypatch):
    feed = _feed()
    for _ in range(_IB_BREAKER_THRESHOLD):
        feed._record_ib_failure()
    assert feed._breaker_open()

    called = []

    async def fake_last_bar(symbol):
        called.append(symbol)
        return None

    monkeypatch.setattr(feed, "last_bar", fake_last_bar)
    result = await feed.last_bars(["A", "B", "C"])

    assert result == {}
    assert called == []  # did not touch IB at all -- straight to Finnhub


async def test_last_bars_stops_hammering_ib_once_the_breaker_trips_mid_pass(monkeypatch):
    """The fix for the ~73-min whole-universe stall: once a run of timeouts
    trips the breaker partway through, the rest of the universe is skipped
    rather than each paying the full timeout."""
    monkeypatch.setattr(prices, "_REQUEST_GAP_SEC", 0.0)  # no real sleeps in the test
    feed = _feed()
    calls = []

    async def failing_last_bar(symbol):
        calls.append(symbol)
        feed._record_ib_failure()  # every call "times out"
        return None

    monkeypatch.setattr(feed, "last_bar", failing_last_bar)
    await feed.last_bars([f"S{i}" for i in range(50)])

    # Stopped at the threshold instead of calling all 50.
    assert len(calls) == _IB_BREAKER_THRESHOLD
