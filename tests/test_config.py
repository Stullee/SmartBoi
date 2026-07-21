import os

import pytest

from smartboi.config import Settings
from smartboi.universe import DEFAULT_UNIVERSE


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    # Settings reads .env / process env -- isolate tests from whatever is
    # actually set in this environment.
    for key in list(os.environ):
        if key.upper().startswith(("SYMBOLS", "EDGAR_", "ENABLE_", "FINNHUB_", "ANTHROPIC_", "IB_")):
            monkeypatch.delenv(key, raising=False)


def test_default_symbol_list_uses_default_universe():
    settings = Settings(_env_file=None)
    assert settings.symbol_list == [c.symbol for c in DEFAULT_UNIVERSE]


def test_symbols_override(monkeypatch):
    monkeypatch.setenv("SYMBOLS", "aapl, msft ,NVDA")
    settings = Settings(_env_file=None)
    assert settings.symbol_list == ["AAPL", "MSFT", "NVDA"]


def test_edgar_forms_set_parses_csv():
    settings = Settings(_env_file=None, edgar_forms="8-K, 10-K,4")
    assert settings.edgar_forms_set == {"8-K", "10-K", "4"}


def test_optional_integrations_default_off_without_keys():
    settings = Settings(_env_file=None)
    assert settings.edgar_user_agent == ""
    assert settings.finnhub_api_key == ""
    assert settings.anthropic_api_key == ""
    assert settings.enable_ib_price_feed is False
    # These should NOT raise even with no keys configured -- see config.py's
    # docstring: missing keys are a disabled feature, not a startup error.
