import os

import pytest

from smartboi.config import Settings
from smartboi.universe import DEFAULT_UNIVERSE


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    # Settings reads .env / process env -- isolate tests from whatever is
    # actually set in this environment.
    for key in list(os.environ):
        if key.upper().startswith(("SYMBOLS", "ANCHOR_", "EDGAR_", "ENABLE_", "FINNHUB_", "ANTHROPIC_", "IB_")):
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


def test_anchor_symbols_build_custom_universe(monkeypatch):
    monkeypatch.setenv("SYMBOLS", "uctt, ichr")
    monkeypatch.setenv("ANCHOR_SYMBOLS", "aapl, MSFT")
    settings = Settings(_env_file=None)
    universe = settings.universe

    by_symbol = {c.symbol: c for c in universe}
    assert set(by_symbol) == {"UCTT", "ICHR", "AAPL", "MSFT"}
    assert by_symbol["AAPL"].signal_source_only
    assert by_symbol["MSFT"].signal_source_only
    assert not by_symbol["UCTT"].signal_source_only
    assert set(settings.symbol_list) == {"UCTT", "ICHR", "AAPL", "MSFT"}


def test_anchor_only_universe(monkeypatch):
    # Anchors without tradeables is a valid (if not very useful) universe --
    # it must not silently fall back to the starter watchlist.
    monkeypatch.setenv("ANCHOR_SYMBOLS", "AAPL")
    settings = Settings(_env_file=None)
    assert [c.symbol for c in settings.universe] == ["AAPL"]


# --- The add-on's options/schema must track Settings.
#
# _addon_options.py turns every key in /data/options.json into an env var,
# and those OVERRIDE the code defaults. So a Settings field missing from the
# add-on schema cannot be tuned from the HA UI at all, and an options key
# that is not a Settings field is silently exported as an env var nothing
# reads -- a typo that looks configured and does nothing. Both are easy to
# introduce (they live in a different file, in a different language) and
# invisible until someone wonders why a setting has no effect. ---

def _addon_config() -> dict:
    import yaml
    from pathlib import Path

    return yaml.safe_load(
        (Path(__file__).resolve().parents[1] / "ha-addons/smartboi/config.yaml").read_text()
    )


def test_every_addon_option_is_a_real_setting():
    stray = set(_addon_config()["options"]) - set(Settings.model_fields)
    assert not stray, f"add-on options that no Settings field reads: {sorted(stray)}"


def test_every_addon_option_has_a_schema_entry():
    config = _addon_config()
    missing = set(config["options"]) - set(config["schema"])
    assert not missing, f"options with no schema entry (HA will reject them): {sorted(missing)}"


def test_every_schema_entry_has_a_default():
    config = _addon_config()
    missing = set(config["schema"]) - set(config["options"])
    assert not missing, f"schema entries with no default: {sorted(missing)}"


def test_the_addon_default_matches_the_code_default():
    """Where the add-on ships a default it must be the same one the code
    would have used, or a fresh install behaves differently from the docs."""
    options = _addon_config()["options"]
    defaults = Settings(_env_file=None)
    for key in ("edgar_poll_interval_sec", "edgar_forms", "max_daily_usd",
                "budget_share_extraction", "budget_share_synthesis", "budget_share_research",
                "extraction_model", "dossier_model", "skeptic_model",
                "synthesis_model", "backfill_anchors", "transaction_cost_profile"):
        assert options[key] == getattr(defaults, key), (
            f"{key}: add-on ships {options[key]!r}, code default is {getattr(defaults, key)!r}"
        )


def test_the_dockerfile_version_mirrors_the_addon_version():
    """The Dockerfile carries its own copy of the version (it becomes
    SMARTBOI_VERSION in the container, which is what diagnostics reports).
    Bumping config.yaml alone leaves the running add-on reporting the
    previous release -- which is exactly how a deployment gets diagnosed as
    'two versions behind' when it isn't."""
    import re
    from pathlib import Path

    dockerfile = (
        Path(__file__).resolve().parents[1] / "ha-addons/smartboi/Dockerfile"
    ).read_text()
    match = re.search(r"^ARG SMARTBOI_VERSION=(.+)$", dockerfile, re.MULTILINE)
    assert match, "Dockerfile has no ARG SMARTBOI_VERSION"
    assert match.group(1).strip() == str(_addon_config()["version"]), (
        f"Dockerfile ships {match.group(1).strip()!r}, config.yaml says "
        f"{_addon_config()['version']!r}"
    )


def test_every_budget_and_threshold_setting_is_tunable_from_the_addon():
    """The existing guards check options -> Settings (no stray keys) but never
    Settings -> options, so a new setting could ship unreachable from the HA
    UI with every test green. That happened: the three budget shares were
    added to Settings and silently omitted here.

    Scoped to the settings an operator actually has to reach without a
    rebuild -- budgets, thresholds and the switches that cost money."""
    options = set(_addon_config()["options"])
    must_be_tunable = {
        name for name in Settings.model_fields
        if name.startswith(("budget_share_", "max_daily_", "signal_", "min_independent_"))
    }
    missing = must_be_tunable - options
    assert not missing, f"Settings unreachable from the add-on UI: {sorted(missing)}"
