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
from smartboi.webapp import _CSRF_HEADER, _parse_tickers, create_app

from tests.fakes import FakeFinnhub


def _client(engine) -> TestClient:
    """A dashboard client that carries the CSRF header on every request, like
    the real page's JS does on every POST (see _INDEX_HTML). The rejection of
    a POST that omits it is covered on its own below."""
    return TestClient(TestServer(create_app(engine)), headers={_CSRF_HEADER: "1"})


@pytest.fixture
def engine(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    settings = Settings(
        _env_file=None, symbols="DCO", anchor_symbols="RTX",
        enable_dashboard=False, enable_universe_autoscreen=False,
        # Pin the illustrative 8/16 institutional grid the cost-drag
        # diagnostics tests below document (59% <$300M break-even, +1.19R/
        # -1.72R at 600bp). The shipped default is now the wider
        # hold-to-horizon grid, so these must set the example explicitly
        # rather than inherit it.
        stop_loss_pct=8.0, take_profit_pct=16.0, transaction_cost_profile="institutional",
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
    async with _client(engine) as client:
        response = await client.post("/api/tools/screen", json={"tickers": "intt asys"})
        assert response.status == 200
        report = (await response.json())["report"]
        assert "INTT" in report and "ASYS" in report


async def test_screen_endpoint_without_finnhub_explains_instead_of_failing(engine):
    engine.finnhub = None
    async with _client(engine) as client:
        response = await client.post("/api/tools/screen", json={"tickers": "INTT"})
        assert response.status == 200
        assert "FINNHUB_API_KEY" in (await response.json())["report"]


async def test_malformed_body_is_a_400_not_a_500(engine):
    async with _client(engine) as client:
        response = await client.post("/api/tools/screen", data="{not json")
        assert response.status == 400


# --- CSRF: the whole write surface requires a custom header. The dashboard
# binds 0.0.0.0 with no auth, and the destructive endpoints don't even read
# their body, so a plain cross-origin form POST would otherwise reach them. ---

async def test_state_changing_post_without_the_csrf_header_is_rejected(engine):
    """reset-accepted is the sharpest case: it takes no body, so nothing but
    the required header stands between a cross-origin page and wiping every
    runtime-added symbol. Missing header -> 403, before the handler runs."""
    # A bare client (NOT _client) sends no CSRF header, exactly like a
    # cross-origin attacker's fetch/form would.
    async with TestClient(TestServer(create_app(engine))) as client:
        for path in ("universe/reset-accepted", "universe/rebuild-graph",
                     "tools/screen", "candidates/accept"):
            response = await client.post(f"/api/{path}", json={})
            assert response.status == 403, f"/api/{path} answered {response.status}, not 403"


async def test_reads_do_not_require_the_csrf_header(engine):
    """GET is a pure read here, and the page's own 10s auto-refresh sends no
    such header -- gating it would break the dashboard on load."""
    async with TestClient(TestServer(create_app(engine))) as client:
        assert (await client.get("/api/status")).status == 200
        assert (await client.get("/")).status == 200


async def test_the_csrf_header_lets_the_same_request_through(engine):
    """The guard rejects on the ABSENCE of the header, not its value -- a
    same-origin client that sets it (as the dashboard JS does) is unaffected.
    Pairs with the rejection test so a guard that blocked everything couldn't
    pass both."""
    async with _client(engine) as client:
        assert (await client.post("/api/universe/rebuild-graph", json={})).status == 200


async def test_forward_returns_endpoint_reports_no_data_gracefully(engine):
    """Nothing captured yet is the normal state of a fresh deployment --
    it must read as an explanation, not an error."""
    async with _client(engine) as client:
        response = await client.post("/api/tools/forward-returns", json={})
        assert response.status == 200
        assert "No dossier snapshots" in (await response.json())["report"]


async def test_exit_analysis_endpoint_reports_no_data_gracefully(engine):
    """Same as the other read-only reports: an empty ledger on a fresh
    deployment must read as an explanation, not an error."""
    async with _client(engine) as client:
        response = await client.post("/api/tools/exit-analysis", json={})
        assert response.status == 200
        assert "No paper trades" in (await response.json())["report"]


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

    async with _client(engine) as client:
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
    async with _client(engine) as client:
        response = await client.post("/api/tools/screen", json={"tickers": "INTT"})
        assert response.status == 500
        assert "error" in await response.json()


async def test_diagnostics_endpoint_returns_a_bundle(engine):
    async with _client(engine) as client:
        response = await client.post("/api/tools/diagnostics", json={})
        assert response.status == 200
        assert "=== SmartBoi diagnostics ===" in (await response.json())["report"]


async def test_diagnostics_state_the_after_cost_break_even_win_rate(engine):
    """The 8%/16% grid reads as 2:1 and isn't -- the sub-$300M bucket needs
    59% to net zero. That number decides whether the whole record can ever
    be profitable, so it has to appear without anyone asking for it."""
    async with _client(engine) as client:
        report = (await (await client.post("/api/tools/diagnostics", json={})).json())["report"]

    assert "Cost drag on the 8%/16% grid" in report
    assert "break-even win rate 59%" in report  # the <$300M institutional bucket
    assert "break-even win rate 38%" in report  # and the >$1B one, for contrast
    # The last bucket is a floor, not a catch-all; labelling it as one read
    # as if 600bp applied to every trade.
    assert "<$300M" in report
    assert "any cap" not in report


async def test_diagnostics_price_each_open_trade_at_its_own_cost_bucket(engine):
    engine.journal.open(
        "ESOA", "LONG", 15.05, 8.0, 16.0, 20, "t", 0.9, 3, [],
        cost_bps_round_trip=600.0, market_cap_musd=180.0,
    )
    async with _client(engine) as client:
        report = (await (await client.post("/api/tools/diagnostics", json={})).json())["report"]

    # An open trade showing worse than -1.00R while still ABOVE its stop is
    # not a marking bug -- it is the round-trip cost, and the per-trade line
    # is what makes that legible.
    assert "600bp round trip" in report
    assert "win +1.19R / loss -1.72R" in report


# --- Rebuild relationship graph: the recovery path for edges lost because
# their counterparty was accepted into the universe only AFTER the
# discovering filing had been extracted. ---

async def test_rebuild_graph_queues_every_tradeable(engine):
    engine.backfill_state.set("DCO", {"backfilled_at": "2026-07-01T00:00:00+00:00", "accession": "x"})
    async with _client(engine) as client:
        body = await (await client.post("/api/universe/rebuild-graph", json={})).json()

    assert body["ok"] is True
    assert "DCO" in body["symbols"]
    # The once-ever marker is cleared, so _run_relationship_backfill picks it
    # up on the next tick.
    assert engine.backfill_state.get("DCO") is None


async def test_rebuild_graph_never_removes_an_edge(engine):
    from smartboi.graph import Relationship

    engine.graph.add(Relationship("DCO", "RTX", "customer", "d", "s", 0.9, "2026-07-29"))
    async with _client(engine) as client:
        await client.post("/api/universe/rebuild-graph", json={})

    assert len(engine.graph.relationships) == 1


async def test_rebuild_graph_leaves_anchors_alone(engine):
    """Backfill extracts from the SMALL companies' filings -- a giant's 10-K
    never names its tier-2 suppliers, which is the whole reason the graph is
    discovered bottom-up."""
    async with _client(engine) as client:
        body = await (await client.post("/api/universe/rebuild-graph", json={})).json()

    assert "RTX" not in body["symbols"]


# --- GET /api/status: the 10-second poll every open tab runs forever ---

async def test_status_revalidates_instead_of_resending_an_unchanged_payload(engine):
    """The page polls this every 10s for as long as it is open, and between
    engine ticks the answer is usually byte-identical. Without a validator that
    is tens of megabytes an hour, per tab, of a graph that changes when a filing
    is read."""
    async with _client(engine) as client:
        first = await client.get("/api/status")
        etag = first.headers["ETag"]
        assert first.status == 200
        assert first.headers["Cache-Control"] == "no-cache"

        second = await client.get("/api/status", headers={"If-None-Match": etag})
        assert second.status == 304
        assert await second.read() == b""


async def test_status_etag_changes_when_the_state_does(engine):
    """The half that matters: a validator that never changes serves a stale
    dashboard forever."""
    from smartboi.graph import Relationship

    async with _client(engine) as client:
        etag = (await client.get("/api/status")).headers["ETag"]
        engine.graph.add(Relationship("DCO", "RTX", "customer", "d", "s", 0.9, "2026-07-29"))
        after = await client.get("/api/status", headers={"If-None-Match": etag})

    assert after.status == 200
    assert after.headers["ETag"] != etag


async def test_status_ships_the_graph_the_page_actually_reads(engine):
    """`by_symbol` fed the original relationship tables, which the redesign
    replaced with the canvas. It was 37% of the payload and no reader was left.
    nodes/edges -- what the Live Wire draws -- must still be there."""
    async with _client(engine) as client:
        body = await (await client.get("/api/status")).json()

    assert "by_symbol" not in body["graph"]
    assert "nodes" in body["graph"] and "edges" in body["graph"]
    assert "edge_count" in body["graph"]


async def test_status_reads_each_dossier_from_disk_once(engine):
    """Four gatherers want dossiers and each used to load them independently --
    a file read plus a JSON parse each, on the engine's own event loop, every
    10 seconds, per open tab."""
    from smartboi.dossier import Dossier

    for symbol in ("DCO", "RTX"):
        engine.dossiers.save(Dossier(symbol=symbol, direction="LONG", confidence=0.8, magnitude=0.7))

    loads: list[str] = []
    original = type(engine.dossiers).load

    def counting_load(self, symbol):
        loads.append(symbol)
        return original(self, symbol)

    type(engine.dossiers).load = counting_load
    try:
        async with _client(engine) as client:
            assert (await client.get("/api/status")).status == 200
    finally:
        type(engine.dossiers).load = original

    assert loads, "the payload should have read some dossiers"
    assert len(loads) == len(set(loads)), f"a dossier was re-read from disk: {loads}"


# --- GET /api/dossier/{symbol} -- what a clicked ladder row opens ---------


async def test_dossier_endpoint_returns_the_evidence_behind_a_score(engine):
    """The status payload carries only evidence_count, so the panel a click
    opens has to get the items from somewhere."""
    from smartboi.dossier import Dossier, EvidenceRecord

    engine.dossiers.save(Dossier(
        symbol="DCO", direction="LONG", confidence=0.8, magnitude=0.7,
        thesis_summary="Backlog build",
        evidence=[EvidenceRecord(
            evidence_id="e1", source_type="8-K", source_name="SEC EDGAR (8-K)",
            url="https://sec.gov/x", headline="Award announced", published_at="2026-08-01T00:00:00+00:00",
            origin_symbol="RTX", is_propagated=True, relationship_note="RTX is a customer of DCO",
            direction="LONG", magnitude=0.6, confidence=0.7, horizon_days=30,
            reasoning="Sole-source award", skeptic_note="Size undisclosed",
        )],
    ))

    async with _client(engine) as client:
        resp = await client.get("/api/dossier/DCO")
        assert resp.status == 200
        body = await resp.json()

    assert body["symbol"] == "DCO" and body["thesis_summary"] == "Backlog build"
    assert body["evidence_count"] == 1 and body["evidence_shown"] == 1
    item = body["evidence"][0]
    assert item["headline"] == "Award announced"
    # The propagation fields are the whole reason a graph-driven thesis is
    # explicable at all: without them an item about RTX on DCO's dossier looks
    # like a bug rather than the mechanism working.
    assert item["is_propagated"] is True and item["relationship_note"] == "RTX is a customer of DCO"


async def test_dossier_endpoint_is_case_insensitive_about_the_symbol(engine):
    from smartboi.dossier import Dossier

    engine.dossiers.save(Dossier(symbol="DCO", direction="LONG", confidence=0.5, magnitude=0.5))
    async with _client(engine) as client:
        assert (await client.get("/api/dossier/dco")).status == 200


async def test_dossier_endpoint_404s_for_a_symbol_with_no_dossier(engine):
    """A real ticker that simply has no file must not read as a server fault
    -- and must not hand back an empty Dossier() as though it were one."""
    async with _client(engine) as client:
        resp = await client.get("/api/dossier/ZZZZ")
        assert resp.status == 404
        assert "error" in await resp.json()


async def test_dossier_endpoint_refuses_a_path_traversal(engine):
    """DossierStore turns the symbol straight into `<dir>/<symbol>.json`, so an
    unvalidated segment here reads arbitrary files off the add-on host. Both
    the pattern check and the all_symbols membership check exist for this."""
    from smartboi.dossier import Dossier

    engine.dossiers.save(Dossier(symbol="DCO", direction="LONG", confidence=0.5, magnitude=0.5))
    secret = engine.dossiers.dir_path.parent / "secret.json"
    secret.write_text('{"symbol": "SECRET"}')

    async with _client(engine) as client:
        for attempt in ("../secret", "..%2Fsecret", "%2e%2e%2fsecret", "DCO/../../secret"):
            resp = await client.get(f"/api/dossier/{attempt}")
            assert resp.status in (400, 404), f"{attempt} returned {resp.status}"
            if resp.status == 400:
                assert (await resp.json())["error"] == "not a ticker"


async def test_dossier_endpoint_is_a_read_and_needs_no_csrf_header(engine):
    """It is a GET like /api/status, and the page's fetch sends no header on
    reads -- so a guard that demanded one would just break the panel."""
    from smartboi.dossier import Dossier

    engine.dossiers.save(Dossier(symbol="DCO", direction="LONG", confidence=0.5, magnitude=0.5))
    async with TestClient(TestServer(create_app(engine))) as client:
        assert (await client.get("/api/dossier/DCO")).status == 200


# --- The full diagnostics download ---------------------------------------


async def test_the_full_diagnostics_download_returns_a_real_zip(engine):
    import io
    import zipfile

    async with _client(engine) as client:
        response = await client.post("/api/tools/full-diagnostics", json={})

        assert response.status == 200
        assert response.headers["Content-Type"] == "application/zip"
        assert "attachment; filename=" in response.headers["Content-Disposition"]
        archive = zipfile.ZipFile(io.BytesIO(await response.read()))
        assert archive.testzip() is None
        assert "MANIFEST.txt" in archive.namelist()
        assert "diagnostics.txt" in archive.namelist()


async def test_the_full_diagnostics_download_requires_the_csrf_header():
    """POST, unlike its read-only siblings, and for the payload rather than a
    side effect. The CSRF guard exempts GET on the grounds that every GET is a
    pure read -- true of this one, but that rule was written when the richest
    GET was a status payload, and the dashboard binds 0.0.0.0 with no auth of
    its own. This endpoint hands back every dossier, the whole graph, the
    trade record and the logs."""
    settings = Settings(_env_file=None, symbols="DCO", anchor_symbols="RTX",
                        enable_dashboard=False, enable_universe_autoscreen=False)
    bare = Engine(settings)
    async with TestClient(TestServer(create_app(bare))) as client:  # no header
        response = await client.post("/api/tools/full-diagnostics", json={})
        assert response.status == 403
