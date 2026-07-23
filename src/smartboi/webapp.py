"""Dashboard: dossiers, the relationship graph, open/closed paper trades,
recent signals, and discovered universe candidates -- runs alongside the
ingestion engine in the same process. Reachable directly
(http://<host>:dashboard_port/) and, for the Home Assistant add-on, as an
Ingress tab -- see ha-addons/smartboi/config.yaml. Uses only relative URLs
so it works unmodified behind Ingress's subpath proxying.

Mostly read-only (every GET handler just reads persisted state, see
status.py) with one deliberate exception: POST /api/candidates/accept lets
a human add a discovered candidate into the live universe with one click
(see engine.accept_candidate) -- its write surface is bounded to symbols
the extraction pipeline itself already surfaced, never an arbitrary
ticker, and it can only widen what's watched, never place an order or
directly create a trade (see handle_accept_candidate's docstring)."""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import asdict
from pathlib import Path

from aiohttp import web

from smartboi.status import (
    gather_dossiers,
    gather_graph_stats,
    gather_paper_trade_stats,
    gather_recent_signals,
    gather_universe_candidates,
    gather_usage,
)

log = logging.getLogger(__name__)

_STATUS_TIMEOUT_SEC = 8.0

_INDEX_HTML = """<!doctype html>
<html>
<head>
<meta charset="utf-8">
<title>SmartBoi Status</title>
<style>
  :root { color-scheme: light dark; }
  body { font-family: -apple-system, system-ui, sans-serif; margin: 0; padding: 1.5rem;
         background: #0f1115; color: #e6e6e6; }
  @media (prefers-color-scheme: light) { body { background: #f7f7f8; color: #1a1a1a; } }
  h1 { font-size: 1.3rem; margin: 0 0 0.25rem; }
  .subtitle { opacity: 0.6; font-size: 0.85rem; margin-bottom: 1rem; }
  h2 { font-size: 1rem; margin: 1.5rem 0 0.5rem; opacity: 0.85; }
  .cards { display: flex; flex-wrap: wrap; gap: 0.75rem; }
  .card { background: rgba(128,128,128,0.12); border-radius: 8px; padding: 0.9rem 1.2rem; min-width: 140px; }
  .card .label { font-size: 0.75rem; opacity: 0.65; text-transform: uppercase; letter-spacing: 0.03em; }
  .card .value { font-size: 1.4rem; font-weight: 600; margin-top: 0.2rem; }
  .pos { color: #3ecf6e; } .neg { color: #ef5350; } .warn { color: #e6b800; }
  table { border-collapse: collapse; width: 100%; margin-top: 0.4rem; font-size: 0.9rem; }
  th, td { text-align: left; padding: 0.35rem 0.6rem; border-bottom: 1px solid rgba(128,128,128,0.2); }
  th { opacity: 0.65; font-weight: 500; font-size: 0.8rem; }
  .empty { opacity: 0.5; padding: 0.4rem 0.6rem; font-size: 0.9rem; }
  .updated { opacity: 0.5; font-size: 0.75rem; margin-top: 1.5rem; }
  .err { color: #ef5350; }
  .badge { display: inline-block; padding: 0.1rem 0.5rem; border-radius: 4px; font-size: 0.78rem; }
  .badge.on { background: rgba(62,207,110,0.18); color: #3ecf6e; }
  .badge.off { background: rgba(128,128,128,0.18); opacity: 0.7; }
</style>
</head>
<body>
<h1>SmartBoi Status</h1>
<div class="subtitle">Paper-only cross-company evidence synthesis -- no order-placement code exists in this system.</div>
<div id="app">Loading&hellip;</div>
<div class="updated" id="updated"></div>
<script>
function fmt(n, d) { d = d === undefined ? 2 : d; return (n === null || n === undefined) ? "-" : n.toFixed(d); }
function cls(n) { return n > 0 ? "pos" : (n < 0 ? "neg" : ""); }
function esc(s) { return (s === null || s === undefined) ? "" : String(s).replace(/[&<>]/g, function(c){ return {"&":"&amp;","<":"&lt;",">":"&gt;"}[c]; }); }

function badge(dir) {
  if (dir === "LONG") return '<span class="pos">&#9650; LONG</span>';
  if (dir === "SHORT") return '<span class="neg">&#9660; SHORT</span>';
  return '<span style="opacity:0.5">NONE</span>';
}

function capBadge(on) {
  return '<span class="badge ' + (on ? 'on">ENABLED' : 'off">DISABLED') + '</span>';
}

function renderCapabilities(c) {
  return '<div class="cards">' +
    '<div class="card"><div class="label">EDGAR ingestion</div><div class="value">' + capBadge(c.edgar) + '</div></div>' +
    '<div class="card"><div class="label">News ingestion</div><div class="value">' + capBadge(c.news) + '</div></div>' +
    '<div class="card"><div class="label">Dossier engine (Claude)</div><div class="value">' + capBadge(c.anthropic) + '</div></div>' +
    '<div class="card"><div class="label">IB price feed</div><div class="value">' + capBadge(c.ib) + '</div></div>' +
    '</div>';
}

function renderDossiers(rows) {
  if (!rows.length) return '<div class="empty">No dossiers yet.</div>';
  var body = rows.map(function(d) {
    var contested = d.mass_opposing > 0
      ? '<span class="warn" title="Opposing evidence mass -- discounts confidence">' + fmt(d.mass_agree, 2) + ' vs ' + fmt(d.mass_opposing, 2) + '</span>'
      : (d.mass_agree > 0 ? fmt(d.mass_agree, 2) : "-");
    return "<tr><td><b>" + d.symbol + "</b></td><td>" + badge(d.direction) + "</td><td>" +
      fmt(d.confidence) + "</td><td>" + fmt(d.magnitude) + "</td><td>" + d.horizon_days + "d</td><td>" +
      d.independent_source_count + "</td><td>" + contested + "</td><td>" + d.evidence_count + "</td><td>" + esc(d.status) + "</td><td>" +
      (d.signaled_price != null ? "$" + fmt(d.signaled_price) + " @ " + (d.signaled_at || "").slice(0, 10) : "-") +
      "</td><td style='max-width:32rem'>" + esc(d.thesis_summary) + "</td></tr>";
  }).join("");
  return "<table><tr><th>Symbol</th><th>Dir</th><th>Confidence</th><th>Magnitude</th><th>Horizon</th>" +
    "<th>Sources</th><th>Mass (agree vs oppose)</th><th>Evidence</th><th>Status</th><th>Signaled @</th><th>Thesis</th></tr>" + body + "</table>";
}

function renderPaperTrades(rows, openTrades) {
  var html = "";
  if (openTrades.length) {
    html += "<h2>Open Paper Trades</h2>";
    html += "<table><tr><th>Symbol</th><th>Dir</th><th>Entry</th><th>Current</th><th>Unrealized</th>" +
      "<th>Stop</th><th>Target</th><th>Horizon</th><th>Opened</th></tr>" +
      openTrades.map(function(t) {
        var r = t.unrealized_r;
        return "<tr><td>" + t.symbol + "</td><td>" + badge(t.direction) + "</td><td>" + fmt(t.entry_price) +
          "</td><td>" + (t.last_price !== null && t.last_price !== undefined ? fmt(t.last_price) : "-") + "</td><td class=\\"" +
          cls(r) + "\\">" + (r === null || r === undefined ? "-" : (r >= 0 ? "+" : "") + fmt(r) + "R") + "</td><td>" +
          fmt(t.stop_price) + "</td><td>" + fmt(t.target_price) + "</td><td>" + t.horizon_days + "d</td><td>" +
          (t.opened_at || "").slice(0, 10) + "</td></tr>";
      }).join("") + "</table>";
  } else {
    html += "<h2>Open Paper Trades</h2><div class='empty'>None open.</div>";
  }
  html += "<h2>Closed Paper Trades (most recent)</h2>";
  if (!rows.length) {
    html += '<div class="empty">No paper trades closed yet.</div>';
  } else {
    html += "<table><tr><th>Closed</th><th>Symbol</th><th>Dir</th><th>Status</th><th>Entry</th><th>Exit</th><th>R</th></tr>" +
      rows.slice().reverse().map(function(t) {
        var r = t.r_multiple;
        return "<tr><td>" + (t.closed_at || "").slice(0, 10) + "</td><td>" + t.symbol + "</td><td>" + badge(t.direction) +
          "</td><td>" + esc(t.status) + "</td><td>" + fmt(t.entry_price) + "</td><td>" + fmt(t.exit_price) + "</td><td class=\\"" +
          cls(r || 0) + "\\">" + (r !== null && r !== undefined ? fmt(r) + "R" : "-") + "</td></tr>";
      }).join("") + "</table>";
  }
  return html;
}

function renderSignals(rows) {
  if (!rows.length) return '<div class="empty">No signals yet.</div>';
  return "<table><tr><th>When</th><th>Symbol</th><th>Dir</th><th>Confidence</th><th>Magnitude</th><th>Sources</th><th>Thesis</th></tr>" +
    rows.slice().reverse().map(function(s) {
      return "<tr><td>" + (s.generated_at || "").slice(0, 16).replace("T", " ") + "</td><td>" + s.symbol + "</td><td>" +
        badge(s.direction) + "</td><td>" + fmt(s.confidence) + "</td><td>" + fmt(s.magnitude) + "</td><td>" +
        s.independent_source_count + "</td><td style='max-width:28rem'>" + esc(s.thesis_summary) + "</td></tr>";
    }).join("") + "</table>";
}

function candidateAction(c) {
  if (!c.ticker) return '<span style="opacity:0.5">no ticker</span>';
  if (c.accepted_as) return '<span class="badge on">added: ' + esc(c.accepted_as) + '</span>';
  return '<button class="accept-btn" data-symbol="' + esc(c.ticker) + '" data-as="tradeable">+ Tradeable</button> ' +
    '<button class="accept-btn" data-symbol="' + esc(c.ticker) + '" data-as="anchor">+ Anchor</button>';
}

function renderCandidates(rows) {
  if (!rows.length) return '<div class="empty">None discovered yet -- candidates appear as filings disclose relationships to companies outside the universe.</div>';
  return "<table><tr><th>Name</th><th>Ticker</th><th>Related to</th><th>Type</th><th>Mentions</th><th>Description</th><th>Action</th></tr>" +
    rows.map(function(c) {
      return "<tr><td>" + esc(c.name) + "</td><td>" + esc(c.ticker || "?") + "</td><td>" +
        esc((c.related_to || []).join(", ")) + "</td><td>" + esc((c.rel_types || []).join(", ")) + "</td><td>" +
        (c.seen_count || 0) + "</td><td style='max-width:28rem'>" + esc(c.description) + "</td><td>" +
        candidateAction(c) + "</td></tr>";
    }).join("") + "</table>" +
    '<div class="empty">"+ Tradeable" adds it as a trade target; "+ Anchor" adds it purely as a news source (never traded). ' +
    'Takes effect on the next poll, no restart needed. Candidates with no ticker resolved (private companies, ' +
    'regulators, generic customer classes) can only be added manually via <b>symbols</b>/<b>anchor_symbols</b> if you have a ticker for them.</div>';
}

function renderGraph(g) {
  if (!g.edges.length) return '<div class="empty">No relationships extracted yet.</div>';
  return "<table><tr><th>From</th><th>Type</th><th>To</th><th>Confidence</th><th>Description</th></tr>" +
    g.edges.map(function(e) {
      return "<tr><td>" + e.from + "</td><td>" + esc(e.type) + "</td><td>" + e.to + "</td><td>" + fmt(e.confidence) +
        "</td><td style='max-width:32rem'>" + esc(e.description) + "</td></tr>";
    }).join("") + "</table>";
}

function render(data) {
  var html = "";
  html += renderCapabilities(data.capabilities);
  html += '<div class="cards" style="margin-top:0.75rem">';
  html += '<div class="card"><div class="label">Universe</div><div class="value">' + data.universe_size + '</div></div>';
  html += '<div class="card"><div class="label">Graph edges</div><div class="value">' + data.graph.edge_count + '</div></div>';
  html += '<div class="card"><div class="label">Active dossiers</div><div class="value">' + data.dossiers.length + '</div></div>';
  html += '<div class="card"><div class="label">Paper trades open</div><div class="value">' + data.open_paper_trades.length + '</div></div>';
  html += '<div class="card"><div class="label">Paper win rate</div><div class="value">' +
    (data.paper_stats.closed ? Math.round(data.paper_stats.win_rate * 100) + "%" : "-") + '</div></div>';
  html += '<div class="card"><div class="label">Paper avg R</div><div class="value ' + cls(data.paper_stats.avg_r) + '">' +
    (data.paper_stats.closed ? fmt(data.paper_stats.avg_r) : "-") + '</div></div>';
  html += '<div class="card"><div class="label">LLM calls today</div><div class="value">' +
    data.usage.calls + ' / ' + data.usage.daily_call_budget + '</div></div>';
  html += '<div class="card"><div class="label">LLM tokens today (in/out)</div><div class="value" style="font-size:1rem">' +
    (data.usage.input_tokens / 1000).toFixed(1) + 'k / ' + (data.usage.output_tokens / 1000).toFixed(1) + 'k</div></div>';
  html += "</div>";

  html += "<h2>Dossiers</h2>" + renderDossiers(data.dossiers);
  html += renderPaperTrades(data.closed_paper_trades, data.open_paper_trades);
  html += "<h2>Recent Signals</h2>" + renderSignals(data.recent_signals);
  html += "<h2>Relationship Graph</h2>" + renderGraph(data.graph);
  html += "<h2>Universe Candidates (discovered, awaiting your review)</h2>" + renderCandidates(data.universe_candidates || []);

  document.getElementById("app").innerHTML = html;
  document.getElementById("updated").textContent = "Updated " + new Date().toLocaleTimeString();
}

// See TradingBot's webapp.py for why this is built from location.pathname
// rather than a bare relative fetch() -- HA Ingress can deliver this page
// at a per-install subpath with no trailing slash, which would otherwise
// silently break the API URL.
var API_BASE = location.pathname.replace(/\\/?$/, "/") + "api/";
var API_STATUS_URL = API_BASE + "status";
var REFRESH_TIMEOUT_MS = 12000;

document.addEventListener("click", function(ev) {
  var btn = ev.target.closest(".accept-btn");
  if (!btn) return;
  btn.disabled = true;
  fetch(API_BASE + "candidates/accept", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ symbol: btn.dataset.symbol, as: btn.dataset.as }),
  }).then(function(r) { return r.json(); }).then(function(result) {
    if (result.error) {
      btn.disabled = false;
      alert("Could not accept: " + result.error);
      return;
    }
    refresh();
  }).catch(function(err) {
    btn.disabled = false;
    alert("Could not accept: " + err);
  });
});

function refresh() {
  var controller = new AbortController();
  var timedOut = false;
  var timer = setTimeout(function() { timedOut = true; controller.abort(); }, REFRESH_TIMEOUT_MS);
  fetch(API_STATUS_URL, { signal: controller.signal }).then(function(r) {
    clearTimeout(timer);
    return r.json();
  }).then(function(data) {
    if (data.error) {
      document.getElementById("app").innerHTML = '<div class="err">' + data.error + "</div>";
      return;
    }
    try {
      render(data);
    } catch (err) {
      document.getElementById("app").innerHTML = '<div class="err">Render error: ' + err + "</div>";
    }
  }).catch(function(err) {
    clearTimeout(timer);
    var msg = timedOut
      ? "No response after " + (REFRESH_TIMEOUT_MS / 1000) + "s."
      : "Failed to load status: " + err;
    document.getElementById("app").innerHTML = '<div class="err">' + msg + "</div>";
  });
}

refresh();
setInterval(refresh, 10000);
</script>
</body>
</html>
"""


async def _status_payload(engine) -> dict:
    settings = engine.settings
    log_dir = Path(settings.log_dir)
    paper_stats, closed_trades = gather_paper_trade_stats(log_dir / "paper_trades.jsonl")
    open_trades = []
    for t in engine.journal.open_trades.values():
        row = asdict(t)
        row["unrealized_r"] = t.unrealized_r_multiple()
        open_trades.append(row)

    return {
        "capabilities": {
            "edgar": engine.edgar_client is not None,
            "news": engine.finnhub is not None,
            "anthropic": engine.updater is not None,
            "ib": engine.price_feed is not None,
        },
        "universe_size": len(engine.symbol_list),
        "dossiers": gather_dossiers(engine.dossiers),
        "graph": gather_graph_stats(engine.graph),
        "open_paper_trades": open_trades,
        "closed_paper_trades": closed_trades,
        "paper_stats": paper_stats.__dict__,
        "recent_signals": gather_recent_signals(log_dir / "signals.jsonl"),
        "universe_candidates": gather_universe_candidates(engine.candidates.data, engine.accepted_candidates.data),
        "usage": gather_usage(engine.usage.snapshot()),
    }


def create_app(engine) -> web.Application:
    async def handle_index(request: web.Request) -> web.Response:
        return web.Response(text=_INDEX_HTML, content_type="text/html")

    async def handle_status(request: web.Request) -> web.Response:
        start = time.monotonic()
        try:
            data = await asyncio.wait_for(_status_payload(engine), timeout=_STATUS_TIMEOUT_SEC)
        except asyncio.TimeoutError:
            return web.json_response(
                {"error": f"Status query timed out after {_STATUS_TIMEOUT_SEC:.0f}s"}, status=504
            )
        except Exception as exc:  # noqa: BLE001 - never let a bad query 500-loop the page
            log.exception("Dashboard status query failed")
            return web.json_response({"error": str(exc)}, status=500)
        log.debug("Dashboard: /api/status responded in %.2fs", time.monotonic() - start)
        return web.json_response(data)

    async def handle_accept_candidate(request: web.Request) -> web.Response:
        """The dashboard's one-click Accept: adds a discovered universe
        candidate into the live universe (see engine.accept_candidate).
        The only write endpoint this otherwise-read-only dashboard exposes
        -- its write surface is bounded to symbols the system ITSELF
        already discovered and surfaced as a candidate (validated against
        engine.candidates below), not an arbitrary ticker, so it can widen
        what's watched but can't be used to inject something the
        extraction pipeline never found. Widening the watch universe alone
        can't create a trade either -- a dossier/signal still has to form
        independently for the newly-added symbol."""
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001 - a malformed body is a 400, not a 500
            return web.json_response({"error": "invalid JSON body"}, status=400)
        symbol = (body.get("symbol") or "").strip().upper()
        as_type = body.get("as") or "tradeable"
        if as_type not in ("tradeable", "anchor"):
            return web.json_response({"error": "'as' must be 'tradeable' or 'anchor'"}, status=400)
        if not symbol:
            return web.json_response({"error": "'symbol' is required"}, status=400)
        is_known_candidate = any(
            key == symbol or (c.get("ticker") or "").upper() == symbol
            for key, c in engine.candidates.data.items()
        )
        if not is_known_candidate:
            return web.json_response(
                {"error": f"{symbol} is not a discovered candidate -- use SYMBOLS/ANCHOR_SYMBOLS to add it manually."},
                status=404,
            )
        spec = engine.accept_candidate(symbol, as_type)
        return web.json_response({"ok": True, "symbol": spec.symbol, "as": as_type})

    app = web.Application()
    app.router.add_get("/", handle_index)
    app.router.add_get("/api/status", handle_status)
    app.router.add_post("/api/candidates/accept", handle_accept_candidate)
    return app


async def run_dashboard(engine) -> None:
    """Runs until cancelled. The caller wraps this in a background task with
    broad exception handling -- a dashboard failure must never take down
    ingestion (see engine.py)."""
    app = create_app(engine)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", engine.settings.dashboard_port)
    await site.start()
    log.info("Dashboard listening on 0.0.0.0:%d", engine.settings.dashboard_port)
    try:
        await asyncio.Event().wait()
    finally:
        await runner.cleanup()
