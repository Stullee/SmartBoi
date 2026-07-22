from smartboi.news import redact_token


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
