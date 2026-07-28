from smartboi.news import _best_search_match, redact_token


def test_redact_token_scrubs_api_key_from_exception_text():
    msg = (
        "Client error '429 Too Many Requests' for url "
        "'https://finnhub.io/api/v1/company-news?symbol=LOAR&from=2026-07-19&token=supersecretkey123'"
    )
    redacted = redact_token(msg)
    assert "supersecretkey123" not in redacted
    assert "token=REDACTED" in redacted


def test_redact_token_handles_token_mid_query():
    msg = "https://finnhub.io/api/v1/stock/profile2?token=abc123&symbol=UCTT"
    redacted = redact_token(msg)
    assert "abc123" not in redacted
    assert "symbol=UCTT" in redacted


def test_redact_token_leaves_clean_text_alone():
    assert redact_token("connection timed out") == "connection timed out"


# --- _best_search_match (Finnhub /search fallback filtering) ---

def test_best_search_match_finds_exact_normalized_match():
    results = [{"symbol": "AAPL", "description": "APPLE INC", "type": "Common Stock"}]
    assert _best_search_match(results, "apple") == "AAPL"


def test_best_search_match_allows_either_direction_prefix():
    # Query is the full legal name, result description is the bare brand name.
    results = [{"symbol": "IBM", "description": "IBM", "type": "Common Stock"}]
    assert _best_search_match(results, "international business machines") is None
    # Query is the bare brand name, result description is the fuller title.
    results = [{"symbol": "BA", "description": "BOEING CO", "type": "Common Stock"}]
    assert _best_search_match(results, "boeing") == "BA"


def test_best_search_match_skips_foreign_listings():
    results = [
        {"symbol": "005930.KS", "description": "SAMSUNG ELECTRONICS", "type": "Common Stock"},
        {"symbol": "SSNLF", "description": "SAMSUNG ELECTRONICS", "type": "Common Stock"},
    ]
    assert _best_search_match(results, "samsung electronics") == "SSNLF"


def test_best_search_match_skips_non_common_stock():
    results = [
        {"symbol": "SPY", "description": "SPDR S&P 500 ETF", "type": "ETP"},
        {"symbol": "COHU", "description": "COHU INC", "type": "Common Stock"},
    ]
    assert _best_search_match(results, "cohu") == "COHU"


def test_best_search_match_returns_none_when_nothing_plausible():
    results = [{"symbol": "RANDM", "description": "SOME UNRELATED COMPANY", "type": "Common Stock"}]
    assert _best_search_match(results, "acme corp") is None


def test_best_search_match_handles_empty_results():
    assert _best_search_match([], "acme corp") is None


# --- Finnhub's /search isn't on every plan, and an excluded plan rejects
# every query with 422 rather than saying so once. Confirmed live: every
# search failed that way while news/profile calls on the same key worked,
# costing one guaranteed-to-fail rate-limited request per unresolved
# candidate per day.

class _Rejecting422:
    """Minimal stand-in for an httpx error carrying a 422 response."""
    def __init__(self, status_code):
        self.response = type("R", (), {"status_code": status_code})()


def test_plan_rejection_recognises_auth_and_unprocessable_statuses():
    from smartboi.news import _is_plan_rejection
    for status in (401, 403, 422):
        assert _is_plan_rejection(_Rejecting422(status)) is True
    # A genuine transient failure must NOT trip the breaker.
    for status in (429, 500, 503):
        assert _is_plan_rejection(_Rejecting422(status)) is False
    assert _is_plan_rejection(RuntimeError("no response attribute")) is False


async def test_repeated_plan_rejections_disable_the_search(monkeypatch):
    import httpx
    from smartboi.news import FinnhubClient, _SEARCH_UNAVAILABLE_AFTER

    client = FinnhubClient("key")
    attempts = []

    async def rejecting_get(url, params):
        attempts.append(params.get("q"))
        raise httpx.HTTPStatusError(
            "422", request=None,
            response=httpx.Response(422, request=httpx.Request("GET", "https://finnhub.io/x")),
        )

    monkeypatch.setattr(client, "_throttled_get", rejecting_get)

    for i in range(_SEARCH_UNAVAILABLE_AFTER + 5):
        assert await client.search_ticker_by_name(f"Company {i}") is None

    # Stopped calling once the plan rejection was unambiguous, instead of
    # retrying every candidate every day forever.
    assert len(attempts) == _SEARCH_UNAVAILABLE_AFTER
    assert client._search_unavailable is True
    await client.aclose()
