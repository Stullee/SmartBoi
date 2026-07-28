"""The dashboard's /api/tools/* endpoints -- the whole point of which is
that an operator never needs shell access on the Home Assistant host to run
the screener or the forward-return analysis. These cover the input
validation (operator free text going into outbound API calls), the
one-at-a-time guard (a screening run shares the engine's rate-limited
Finnhub client), and that a tool failure degrades to a message instead of
taking the dashboard down."""
from __future__ import annotations

import asyncio

import pytest
from aiohttp.test_utils import TestClient, TestServer

from smartboi.config import Settings
from smartboi.engine import Engine
from smartboi.webapp import _parse_tickers, create_app

from tests.fakes import FakeFinnhub


@pytest.fixture
def engine(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    settings = Settings(
        _env_file=None, symbols="DCO", anchor_symbols="RTX",
        enable_dashboard=False, enable_universe_autoscreen=False,
    )
    e = Engine(settings)
    finnhub = FakeFinnhub()
    finnhub.market_cap_musd = lambda symbol: asyncio.sleep(0, result=850.0)
    finnhub.analyst_count = lambda symbol: asyncio.sleep(0, result=4)
    e.finnhub = finnhub
    return e


# --- Operator free text -> outbound API calls: validated, not passed through ---

def test_parse_tickers_normalizes_dedupes_and_keeps_order():
    # Mixed separators, mixed case, and a repeat -- the repeat is dropped
    # rather than screened (and billed) twice.
    assert _parse_tickers(" intt , asys;cvu  intt ") == ["INTT", "ASYS", "CVU"]


def test_parse_tickers_allows_real_symbol_punctuation():
    assert _parse_tickers("BRK.B RDS-A") == ["BRK.B", "RDS-A"]


def test_parse_tickers_drops_non_ticker_shaped_input():
    assert _parse_tickers("VALID <script> ../etc/passwd WAYTOOLONGTICKER") == ["VALID"]


def test_parse_tickers_ignores_non_strings():
    assert _parse_tickers(None) == []
    assert _parse_tickers(["INTT"]) == []


# --- Endpoints ---

async def test_screen_endpoint_returns_a_report(engine):
    async with TestClient(TestServer(create_app(engine))) as client:
        response = await client.post("/api/tools/screen", json={"tickers": "intt asys"})
        assert response.status == 200
        report = (await response.json())["report"]
        assert "INTT" in report and "ASYS" in report


async def test_screen_endpoint_without_finnhub_explains_instead_of_failing(engine):
    engine.finnhub = None
    async with TestClient(TestServer(create_app(engine))) as client:
        response = await client.post("/api/tools/screen", json={"tickers": "INTT"})
        assert response.status == 200
        assert "FINNHUB_API_KEY" in (await response.json())["report"]


async def test_malformed_body_is_a_400_not_a_500(engine):
    async with TestClient(TestServer(create_app(engine))) as client:
        response = await client.post("/api/tools/screen", data="{not json")
        assert response.status == 400


async def test_forward_returns_endpoint_reports_no_data_gracefully(engine):
    """Nothing captured yet is the normal state of a fresh deployment --
    it must read as an explanation, not an error."""
    async with TestClient(TestServer(create_app(engine))) as client:
        response = await client.post("/api/tools/forward-returns", json={})
        assert response.status == 200
        assert "No dossier snapshots" in (await response.json())["report"]


async def test_concurrent_tool_runs_are_rejected(engine):
    """Two screening runs at once would interleave against the engine's
    single rate-limited Finnhub client and blow the free tier's 60/min."""
    started = asyncio.Event()
    release = asyncio.Event()

    async def slow_market_cap(symbol):
        started.set()
        await release.wait()
        return 850.0

    engine.finnhub.market_cap_musd = slow_market_cap

    async with TestClient(TestServer(create_app(engine))) as client:
        first = asyncio.create_task(client.post("/api/tools/screen", json={"tickers": "INTT"}))
        await asyncio.wait_for(started.wait(), timeout=5)

        second = await client.post("/api/tools/screen", json={"tickers": "ASYS"})
        assert second.status == 409

        release.set()
        assert (await first).status == 200


async def test_tool_failure_returns_an_error_not_a_dead_dashboard(engine):
    async def boom(symbol):
        raise RuntimeError("finnhub exploded")

    engine.finnhub.market_cap_musd = boom
    async with TestClient(TestServer(create_app(engine))) as client:
        response = await client.post("/api/tools/screen", json={"tickers": "INTT"})
        assert response.status == 500
        assert "error" in await response.json()


async def test_diagnostics_endpoint_returns_a_bundle(engine):
    async with TestClient(TestServer(create_app(engine))) as client:
        response = await client.post("/api/tools/diagnostics", json={})
        assert response.status == 200
        assert "=== SmartBoi diagnostics ===" in (await response.json())["report"]
