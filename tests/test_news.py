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
