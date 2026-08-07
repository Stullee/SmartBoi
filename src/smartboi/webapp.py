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
import re
import time
from dataclasses import asdict
from pathlib import Path

from aiohttp import web

from smartboi.news import redact_token
from smartboi.screen import (
    DEFAULT_MAX_ANALYSTS as SCREEN_MAX_ANALYSTS,
    DEFAULT_MAX_CAP_MUSD as SCREEN_MAX_CAP_MUSD,
    DEFAULT_MIN_CAP_MUSD as SCREEN_MIN_CAP_MUSD,
)
from smartboi.tools import (
    run_diagnostics,
    run_event_study,
    run_exit_analysis,
    run_forward_returns,
    run_screen,
    run_supplier_research,
)
from smartboi.status import (
    gather_coverage,
    gather_dossiers,
    gather_graph_stats,
    gather_paper_trade_stats,
    gather_strategy_generations,
    gather_recent_signals,
    gather_universe_candidates,
    gather_usage,
)

log = logging.getLogger(__name__)

# Every state-changing request must carry this header, and the dashboard's
# own JS sets it on every POST (see _INDEX_HTML). It is the standard custom-
# header CSRF defense, and here it is the ONLY thing guarding the write
# surface: the dashboard binds 0.0.0.0 with no auth, and reset-accepted /
# rebuild-graph don't even read their body -- so without it a plain
# cross-origin <form> POST from any page the operator happened to open could
# reset the universe on their host. A browser will not attach a non-safelisted
# header to a cross-origin request without a CORS preflight, which this server
# answers for no origin, so only same-origin dashboard JS gets through. GET is
# left open on purpose: every GET here is a pure read with no side effect, and
# the page's own 10s auto-refresh sends no such header. (Keep this string in
# sync with the POST_HEADERS constant in the dashboard JS.)
_CSRF_HEADER = "X-SmartBoi-Request"


@web.middleware
async def _require_csrf_header(request: web.Request, handler):
    if request.method == "POST" and _CSRF_HEADER not in request.headers:
        return web.json_response(
            {"error": f"missing required {_CSRF_HEADER} header"}, status=403
        )
    return await handler(request)


_STATUS_TIMEOUT_SEC = 8.0
# A ticker as the screener will accept it: letters, digits, and the dot/dash
# real symbols use (BRK.B, RDS-A). Anything else in the free-text box is
# dropped rather than forwarded to Finnhub.
_TICKER_RE = re.compile(r"^[A-Z0-9][A-Z0-9.\-]{0,9}$")


def _parse_tickers(raw: object) -> list[str]:
    """The dashboard's free-text ticker box -> a clean symbol list. Accepts
    whitespace, comma or semicolon separation (a free-text box should not
    silently swallow a ticker over punctuation choice), uppercases,
    de-duplicates while keeping the typed order, and drops anything that
    isn't ticker-shaped -- this is operator input going into outbound API
    calls, so it's validated against a pattern rather than passed through."""
    if not isinstance(raw, str):
        return []
    out: list[str] = []
    for token in re.split(r"[\s,;]+", raw.strip().upper()):
        if token and _TICKER_RE.match(token) and token not in out:
            out.append(token)
    return out

_INDEX_HTML = """<!doctype html>
<html>
<head>
<meta charset="utf-8">
<title>SmartBoi Status</title>
<style>
  :root { color-scheme: light dark;
    --gc-cust:#3a86d4; --gc-supp:#2fa35a; --gc-comp:#ef5350; --gc-reg:#9a7fe0; --gc-eco:#8493a6;
    --gn-long:#3ecf6e; --gn-short:#ef5350; --gn-none:#6b7885;
    --gn-anchor:#8a97a8; --gn-stroke:#0f1115; --gn-txt:#e6e6e6; }
  body { font-family: -apple-system, system-ui, sans-serif; margin: 0; padding: 1.5rem;
         background: #0f1115; color: #e6e6e6; }
  @media (prefers-color-scheme: light) {
    body { background: #f7f7f8; color: #1a1a1a; }
    :root { --gn-anchor:#606c7b; --gn-stroke:#ffffff; --gn-txt:#1a1a1a; }
  }
  h1 { font-size: 1.3rem; margin: 0 0 0.25rem; }
  .subtitle { opacity: 0.6; font-size: 0.85rem; margin-bottom: 1rem; }
  /* Section headers in the console idiom: a quiet uppercase eyebrow with a
     hairline rule, so "Dossiers" / "Recent Signals" read as the same system
     as the overview's ov-k micro-labels rather than as old bold headings. */
  h2 { font-size: 0.78rem; text-transform: uppercase; letter-spacing: 0.06em; font-weight: 600;
       opacity: 0.55; margin: 1.7rem 0 0.55rem; padding-bottom: 0.4rem;
       border-bottom: 1px solid rgba(128,128,128,0.16); }
  .pos { color: #3ecf6e; } .neg { color: #ef5350; } .warn { color: #e6b800; }
  /* Capability rail -- a compact live/off status strip replacing the old big
     value-cards, so the top of the page matches the overview panels below. */
  .rail { display: flex; flex-wrap: wrap; gap: 0.5rem; }
  .rail-item { display: flex; align-items: center; gap: 0.5rem; background: rgba(128,128,128,0.08);
               border: 1px solid rgba(128,128,128,0.18); border-radius: 9px; padding: 0.5rem 0.75rem; }
  .rail-dot { width: 8px; height: 8px; border-radius: 50%; background: #3ecf6e; flex: none;
              box-shadow: 0 0 0 3px rgba(62,207,110,0.16); }
  .rail-dot.off { background: #8a8a8a; box-shadow: none; }
  .rail-lab { font-size: 0.8rem; opacity: 0.8; }
  .rail-state { font-size: 0.62rem; text-transform: uppercase; letter-spacing: 0.05em; opacity: 0.5; }
  /* legacy card -- kept for renderCoverage; restyled to the panel look. */
  .card { background: rgba(128,128,128,0.10); border: 1px solid rgba(128,128,128,0.20);
          border-radius: 10px; padding: 0.85rem 1rem; }
  .card .label { font-size: 0.7rem; opacity: 0.6; text-transform: uppercase; letter-spacing: 0.05em; }
  .card .value { font-size: 1.4rem; font-weight: 600; margin-top: 0.2rem; }
  /* Detail tables live in a bordered, horizontally-scrollable panel so an
     11-column table can't shove the whole page sideways on a phone, and so
     each section reads as a panel like the overview above it. */
  .tbl { overflow-x: auto; border: 1px solid rgba(128,128,128,0.18); border-radius: 10px;
         background: rgba(128,128,128,0.05); margin-top: 0.2rem; }
  table { border-collapse: collapse; width: 100%; font-size: 0.85rem; }
  th, td { text-align: left; padding: 0.5rem 0.7rem; white-space: nowrap; }
  th { font-size: 0.64rem; text-transform: uppercase; letter-spacing: 0.05em; font-weight: 600;
       opacity: 0.5; border-bottom: 1px solid rgba(128,128,128,0.22); }
  td { border-bottom: 1px solid rgba(128,128,128,0.10); font-variant-numeric: tabular-nums; }
  tr:last-child td { border-bottom: none; }
  tr:hover td { background: rgba(128,128,128,0.05); }
  td[style*="max-width"] { white-space: normal; }
  .empty { opacity: 0.6; padding: 0.7rem 0.9rem; font-size: 0.85rem; background: rgba(128,128,128,0.05);
           border: 1px dashed rgba(128,128,128,0.22); border-radius: 10px; margin-top: 0.2rem; }
  .updated { opacity: 0.5; font-size: 0.75rem; margin-top: 1.5rem; }
  .err { color: #ef5350; }
  .badge { display: inline-block; padding: 0.12rem 0.55rem; border-radius: 999px; font-size: 0.7rem;
           font-weight: 600; letter-spacing: 0.02em; }
  .badge.on { background: rgba(62,207,110,0.16); color: #3ecf6e; }
  .badge.off { background: rgba(128,128,128,0.16); opacity: 0.7; }
  button { font: inherit; padding: 0.4rem 0.85rem; border-radius: 8px; cursor: pointer;
           border: 1px solid rgba(128,128,128,0.35); background: rgba(128,128,128,0.12); color: inherit; }
  button:hover:not(:disabled) { background: rgba(128,128,128,0.24); }
  button:disabled { opacity: 0.5; cursor: default; }
  input[type=text] { font: inherit; padding: 0.4rem 0.6rem; border-radius: 8px; min-width: 22rem;
                     border: 1px solid rgba(128,128,128,0.35); background: rgba(128,128,128,0.08); color: inherit; }
  .toolbar { display: flex; flex-wrap: wrap; gap: 0.5rem; align-items: center; }
  .toolhint { opacity: 0.55; font-size: 0.8rem; margin: 0.4rem 0 0; line-height: 1.5; }
  #tool-output { background: rgba(128,128,128,0.12); border-radius: 8px; padding: 0.8rem 1rem;
                 margin-top: 0.7rem; overflow-x: auto; font-size: 0.82rem; line-height: 1.45;
                 white-space: pre; display: none; }
  /* --- Overview (operations-console) components. Scoped with an ov- prefix so
       they never collide with the legacy card/table styles below them. --- */
  .mono { font-variant-numeric: tabular-nums;
          font-family: ui-monospace,"SF Mono","JetBrains Mono",Menlo,Consolas,monospace; }
  .ov { display: grid; gap: 0.8rem; margin: 0.4rem 0 0.4rem;
        grid-template-columns: 1.5fr 1fr 1fr 1fr; }
  @media (max-width: 900px) { .ov { grid-template-columns: 1fr 1fr; } }
  @media (max-width: 560px) { .ov { grid-template-columns: 1fr; } }
  .ov-p { background: rgba(128,128,128,0.10); border: 1px solid rgba(128,128,128,0.20);
          border-radius: 10px; padding: 0.85rem 1rem; }
  .ov-k { font-size: 0.7rem; text-transform: uppercase; letter-spacing: 0.05em; opacity: 0.6; }
  .ov-big { font-size: 1.7rem; font-weight: 650; margin-top: 0.15rem; line-height: 1.05; letter-spacing: -0.01em; }
  .ov-sub { font-size: 0.72rem; opacity: 0.6; margin-top: 0.3rem; }
  .ov-spark { width: 100%; height: 46px; display: block; margin-top: 0.4rem; }
  .ov-foot { display: flex; gap: 0.9rem; flex-wrap: wrap; font-size: 0.72rem; opacity: 0.7; margin-top: 0.5rem; }
  .ov-flag { font-size: 0.7rem; color: #e6b800; background: rgba(230,184,0,0.12);
             border-radius: 6px; padding: 0.35rem 0.5rem; margin-top: 0.55rem; }
  .ci-wrap { position: relative; height: 22px; margin-top: 0.55rem; }
  .ci-axis { position: absolute; left: 0; right: 0; top: 10px; height: 2px; background: rgba(128,128,128,0.25); border-radius: 2px; }
  .ci-band { position: absolute; top: 6px; height: 10px; background: rgba(62,207,110,0.18);
             border: 1px solid #3ecf6e; border-radius: 6px; }
  .ci-pt { position: absolute; top: 3px; width: 2px; height: 16px; background: #3ecf6e; border-radius: 2px; }
  .ci-lab { display: flex; justify-content: space-between; font-size: 0.62rem; opacity: 0.5; margin-top: 0.1rem; }
  .funnel { display: flex; flex-direction: column; gap: 0.45rem; }
  .fn-row { display: grid; grid-template-columns: 130px 1fr auto; align-items: center; gap: 0.6rem; }
  .fn-lab { font-size: 0.78rem; opacity: 0.85; }
  .fn-lab small { display: block; font-size: 0.62rem; opacity: 0.55; }
  .fn-bar { height: 20px; border-radius: 5px; background: rgba(128,128,128,0.16); overflow: hidden; position: relative; }
  .fn-bar > span { position: absolute; inset: 0 auto 0 0; border-radius: 5px;
                   background: linear-gradient(90deg,#3ea6cf,rgba(62,166,207,0.45)); }
  .fn-val { font-size: 0.8rem; min-width: 44px; text-align: right; }
  .fn-val small { opacity: 0.5; }
  .lad { display: flex; flex-direction: column; gap: 0.05rem; }
  .lad-scale { position: relative; height: 12px; margin: 0 0 0.1rem 3.4rem; }
  .lad-line { position: absolute; top: 6px; left: 0; right: 0; border-top: 1px dashed #e6b800; }
  .lad-line-lab { position: absolute; top: -2px; font-size: 0.6rem; color: #e6b800; transform: translateX(-50%); white-space: nowrap; }
  .lad-row { display: grid; grid-template-columns: 3.2rem 1fr; align-items: center; gap: 0.4rem; height: 1.25rem; }
  .lad-sym { font-size: 0.72rem; text-align: right; }
  .lad-trk { position: relative; height: 0.7rem; }
  .lad-fill { position: absolute; top: 0.18rem; left: 0; height: 0.34rem; border-radius: 3px; }
  .lad-dot { position: absolute; top: 0.05rem; width: 0.6rem; height: 0.6rem; border-radius: 50%; transform: translateX(-50%); }
  .lad-con { position: absolute; top: -0.05rem; width: 0.8rem; height: 0.8rem; border-radius: 50%;
             transform: translateX(-50%); border: 1.5px dashed #e6b800; }
  .fresh { font-size: 0.7rem; opacity: 0.55; }
  .fresh.stale { color: #e6b800; opacity: 0.9; }
  /* Strategy record: win-loss split by generation. */
  .gen-head, .gen-row { display: grid; grid-template-columns: 1fr 4.4rem 3rem 4rem; gap: 0.6rem; align-items: center; }
  .gen-head { font-size: 0.62rem; text-transform: uppercase; letter-spacing: 0.05em; opacity: 0.45;
              padding: 0.4rem 0 0.3rem; border-bottom: 1px solid rgba(128,128,128,0.16); }
  .gen-head span:not(:first-child) { text-align: right; }
  .gen-row { padding: 0.45rem 0; border-bottom: 1px solid rgba(128,128,128,0.08); font-size: 0.86rem; }
  .gen-row:last-child { border-bottom: none; }
  .gen-row > div:not(.gen-name) { text-align: right; }
  .gen-name { display: flex; align-items: center; gap: 0.45rem; min-width: 0; }
  .gen-name b { white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
  .gen-dot { width: 8px; height: 8px; border-radius: 50%; background: #8a8a8a; flex: none; }
  .gen-dot.on { background: #3ecf6e; box-shadow: 0 0 0 3px rgba(62,207,110,0.16); }
  .gen-dot.off { background: #8a8a8a; opacity: 0.5; }
  .gen-ver { font-size: 0.68rem; opacity: 0.5; }
  .gen-tag { font-size: 0.58rem; text-transform: uppercase; letter-spacing: 0.04em; color: #3ecf6e;
             border: 1px solid rgba(62,207,110,0.4); border-radius: 999px; padding: 0.03rem 0.36rem; }
  /* LLM spend-by-category: a segmented bar + a compact legend, so the panel
     shows WHERE the day's budget went, not just the total. */
  .catbar { display: flex; height: 7px; border-radius: 4px; overflow: hidden; margin-top: 0.55rem;
            background: rgba(128,128,128,0.16); }
  .catbar > div { height: 100%; }
  .catleg { display: grid; grid-template-columns: 1fr 1fr; gap: 0.15rem 0.7rem; margin-top: 0.4rem; font-size: 0.66rem; opacity: 0.82; }
  .lc { display: flex; align-items: center; gap: 0.32rem; }
  .lc-dot { width: 8px; height: 8px; border-radius: 2px; flex: none; }
  .lc b { font-weight: 600; }
  /* Interactive relationship graph -- the mockup, ported: the whole web on
     one canvas, nodes coloured by live thesis, edges by relationship type. */
  .gviz { border: 1px solid rgba(128,128,128,0.18); border-radius: 10px; background: rgba(128,128,128,0.04);
          margin-top: 0.2rem; overflow: hidden; }
  .gviz svg { display: block; width: 100%; height: 480px; }
  .gnode { cursor: pointer; }
  .gn-lbl { fill: var(--gn-txt); font-size: 11px; font-weight: 600; pointer-events: none; }
  .gn-sc { fill: var(--gn-stroke); font-size: 8px; font-weight: 700; pointer-events: none; }
  .gviz-note { font-size: 0.68rem; opacity: 0.5; margin-top: 0.35rem; }
  .gviz-leg { display: flex; flex-wrap: wrap; gap: 0.45rem 1rem; margin-top: 0.5rem; font-size: 0.72rem; opacity: 0.82; }
  .gl { display: inline-flex; align-items: center; gap: 0.35rem; }
  .gl-l { width: 16px; height: 3px; border-radius: 2px; }
  .gl-d { width: 10px; height: 10px; border-radius: 50%; }
  .gtables { margin-top: 0.7rem; }
  .gtables > summary { cursor: pointer; font-size: 0.78rem; opacity: 0.6; }
  .gtip { position: fixed; pointer-events: none; z-index: 20; background: #111821; color: #e6e6e6;
          border: 1px solid rgba(128,128,128,0.3); border-radius: 8px; padding: 0.45rem 0.6rem;
          font-size: 0.74rem; max-width: 250px; opacity: 0; transition: opacity 0.1s;
          box-shadow: 0 8px 26px -8px rgba(0,0,0,0.6); }
  .gtip .gt-h { font-weight: 700; margin-bottom: 0.12rem; }
  .gtip .gt-m { opacity: 0.85; line-height: 1.4; }
</style>
</head>
<body>
<h1>SmartBoi Status</h1>
<div class="subtitle">Paper-only cross-company evidence synthesis -- no order-placement code exists in this system.</div>

<!-- Deliberately OUTSIDE #app: render() replaces that element's entire
     innerHTML on every 10s auto-refresh, which would wipe a half-typed
     ticker list and any report the operator is still reading. -->
<h2 style="margin-top:0">Tools</h2>
<div class="toolbar">
  <input type="text" id="screen-tickers" placeholder="INTT ASYS SIF  (blank = screen discovered candidates)">
  <label>cap $M <input type="text" id="screen-min-cap" value="75" style="min-width:4rem"></label>
  <label>to <input type="text" id="screen-max-cap" value="3000" style="min-width:5rem"></label>
  <label>max analysts <input type="text" id="screen-max-analysts" value="10" style="min-width:3rem"></label>
  <button id="btn-screen">Screen candidates</button>
  <button id="btn-analyze">Forward-return report</button>
  <button id="btn-event-study">Signal event study</button>
  <button id="btn-exit-analysis">Exit analysis</button>
  <button id="btn-research">Research anchor suppliers (web)</button>
  <button id="btn-diagnostics">Diagnostics bundle</button>
  <button id="btn-rebuild-graph">Rebuild relationship graph</button>
  <button id="btn-reset" style="border-color:rgba(239,83,80,0.5)">Reset added symbols</button>
</div>
<div class="toolhint">The first four are read-only: screening does market-cap/analyst lookups, the other three
  read already-persisted state (forward returns, the signal event study, and the exit analysis of the closed trade ledger). <b>Research anchor suppliers</b> runs web searches to find small-cap counterparties
  of your anchors &mdash; the direction SEC filings structurally never disclose, since a giant's 10-K names its big
  customers, not its small suppliers. It writes universe <i>candidates</i> only and never a relationship edge: a
  web-sourced link is a lead, not a disclosure, and edges at disclosed confidence are what satisfy the corroboration
  bar that fires trades. Accept a candidate and its own 10-K is backfilled, which is where a real edge comes from.
  It costs LLM budget (web search plus one call per anchor), covers ten anchors a run &mdash; most-inert first
  &mdash; and skips ones already researched, so re-run to continue. None changes a dossier, the universe, or any trade, and the diagnostics bundle is
  safe to paste &mdash; credentials and personal data are omitted and log lines are scrubbed.
  <b>Reset added symbols</b> is the one that changes things: it removes every symbol added at runtime, returning the
  universe to the curated list, and archives (never deletes) the dossiers that orphans. It asks first.</div>
<div id="tool-output-actions" style="display:none; justify-content:flex-end; margin-top:0.7rem">
  <button id="btn-copy-output" title="Copy the whole report to the clipboard"
          style="font-size:0.8rem; padding:0.25rem 0.7rem">Copy report</button>
</div>
<pre id="tool-output"></pre>

<div class="gtip" id="gtip"></div>
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
  function item(label, on) {
    return '<div class="rail-item"><span class="rail-dot ' + (on ? "" : "off") + '"></span>' +
      '<span class="rail-lab">' + label + '</span>' +
      '<span class="rail-state">' + (on ? "live" : "off") + '</span></div>';
  }
  return '<div class="rail">' + item("EDGAR ingestion", c.edgar) + item("News ingestion", c.news) +
    item("Dossier engine (Claude)", c.anthropic) + item("IB price feed", c.ib) + '</div>';
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
  return "<div class='tbl'><table><tr><th>Symbol</th><th>Dir</th><th>Confidence</th><th>Magnitude</th><th>Horizon</th>" +
    "<th>Sources</th><th>Mass (agree vs oppose)</th><th>Evidence</th><th>Status</th><th>Signaled @</th><th>Thesis</th></tr>" + body + "</table></div>";
}

function renderPaperTrades(rows, openTrades) {
  var html = "";
  if (openTrades.length) {
    html += "<h2>Open Paper Trades</h2>";
    html += "<div class='tbl'><table><tr><th>Symbol</th><th>Dir</th><th>Entry</th><th>Current</th><th>Unreal R</th><th>Unreal &euro;</th>" +
      "<th>Stop</th><th>Target</th><th>Horizon</th><th>Opened</th><th>Marked</th></tr>" +
      openTrades.map(function(t) {
        var r = t.unrealized_r, uc = t.unrealized_currency, fr = timeAgo(t.last_marked_at);
        return "<tr><td>" + t.symbol + "</td><td>" + badge(t.direction) + "</td><td>" + fmt(t.entry_price) +
          "</td><td>" + (t.last_price !== null && t.last_price !== undefined ? fmt(t.last_price) : "-") + "</td><td class=\\"" +
          cls(r) + "\\">" + (r === null || r === undefined ? "-" : (r >= 0 ? "+" : "") + fmt(r) + "R") + "</td><td class=\\"" +
          cls(uc) + "\\">" + (uc === null || uc === undefined ? "-" : (uc >= 0 ? "+" : "") + fmt(uc, 0)) + "</td><td>" +
          fmt(t.stop_price) + "</td><td>" + fmt(t.target_price) + "</td><td>" + t.horizon_days + "d</td><td>" +
          (t.opened_at || "").slice(0, 10) + "</td><td class=\\"fresh" + (fr.stale ? " stale" : "") + "\\">" + fr.t + "</td></tr>";
      }).join("") + "</table></div>";
  } else {
    html += "<h2>Open Paper Trades</h2><div class='empty'>None open.</div>";
  }
  html += "<h2>Closed Paper Trades (most recent)</h2>";
  if (!rows.length) {
    html += '<div class="empty">No paper trades closed yet.</div>';
  } else {
    html += "<div class='tbl'><table><tr><th>Closed</th><th>Symbol</th><th>Dir</th><th>Status</th><th>Entry</th><th>Exit</th><th>R</th></tr>" +
      rows.slice().reverse().map(function(t) {
        var r = t.r_multiple;
        return "<tr><td>" + (t.closed_at || "").slice(0, 10) + "</td><td>" + t.symbol + "</td><td>" + badge(t.direction) +
          "</td><td>" + esc(t.status) + "</td><td>" + fmt(t.entry_price) + "</td><td>" + fmt(t.exit_price) + "</td><td class=\\"" +
          cls(r || 0) + "\\">" + (r !== null && r !== undefined ? fmt(r) + "R" : "-") + "</td></tr>";
      }).join("") + "</table></div>";
  }
  return html;
}

function renderSignals(rows) {
  if (!rows.length) return '<div class="empty">No signals yet.</div>';
  return "<div class='tbl'><table><tr><th>When</th><th>Symbol</th><th>Dir</th><th>Confidence</th><th>Magnitude</th><th>Sources</th><th>Thesis</th></tr>" +
    rows.slice().reverse().map(function(s) {
      return "<tr><td>" + (s.generated_at || "").slice(0, 16).replace("T", " ") + "</td><td>" + s.symbol + "</td><td>" +
        badge(s.direction) + "</td><td>" + fmt(s.confidence) + "</td><td>" + fmt(s.magnitude) + "</td><td>" +
        s.independent_source_count + "</td><td style='max-width:28rem'>" + esc(s.thesis_summary) + "</td></tr>";
    }).join("") + "</table></div>";
}

function candidateAction(c) {
  if (!c.ticker) return '<span style="opacity:0.5">no ticker</span>';
  if (c.accepted_as) return '<span class="badge on">added: ' + esc(c.accepted_as) +
    (c.accepted_source === "auto" ? " (auto)" : "") + '</span>';
  var reasonAttr = c.recommendation_reason ? ' title="' + esc(c.recommendation_reason) + '"' : '';
  var tradeableBtn = '<button class="accept-btn" data-symbol="' + esc(c.ticker) + '" data-as="tradeable"' +
    (c.recommended_as === "tradeable" ? ' style="font-weight:700"' + reasonAttr : '') + '>+ Tradeable</button>';
  var anchorBtn = '<button class="accept-btn" data-symbol="' + esc(c.ticker) + '" data-as="anchor"' +
    (c.recommended_as === "anchor" ? ' style="font-weight:700"' + reasonAttr : '') + '>+ Anchor</button>';
  return tradeableBtn + ' ' + anchorBtn;
}

function candidateTable(rows) {
  return "<div class='tbl'><table><tr><th>Name</th><th>Ticker</th><th>Related to</th><th>Type</th><th>Mentions</th><th>Description</th><th>Action</th></tr>" +
    rows.map(function(c) {
      return "<tr><td>" + esc(c.name) + "</td><td>" + esc(c.ticker || "?") + "</td><td>" +
        esc((c.related_to || []).join(", ")) + "</td><td>" + esc((c.rel_types || []).join(", ")) + "</td><td>" +
        (c.seen_count || 0) + "</td><td style='max-width:28rem'>" + esc(c.description) + "</td><td>" +
        candidateAction(c) + "</td></tr>";
    }).join("") + "</table></div>";
}

function renderCandidates(rows) {
  if (!rows.length) return '<div class="empty">None discovered yet -- candidates appear as filings disclose relationships to companies outside the universe.</div>';
  var actionable = rows.filter(function(c) { return c.ticker; });
  var noTicker = rows.filter(function(c) { return !c.ticker; });
  var html = actionable.length ? candidateTable(actionable) : '<div class="empty">No addable candidates yet -- see the list below.</div>';
  if (noTicker.length) {
    html += '<details style="margin-top:0.6rem"><summary style="cursor:pointer;opacity:0.7">' +
      noTicker.length + ' candidate(s) with no resolved ticker (private companies, regulators, or names ' +
      'not yet matched -- rechecked automatically once a day)</summary>' + candidateTable(noTicker) + '</details>';
  }
  return html +
    '<div class="empty">"+ Tradeable" adds it as a trade target; "+ Anchor" adds it purely as a news source (never traded). ' +
    'Takes effect on the next poll, no restart needed. A bold button is the suggested choice from market cap/analyst ' +
    'coverage -- hover it for why. Candidates with no ticker resolved can only be added manually via ' +
    '<b>symbols</b>/<b>anchor_symbols</b> if you have a ticker for them.</div>';
}

function renderGraph(g) {
  if (!g.edge_count) return '<div class="empty">No relationships extracted yet.</div>';
  return g.by_symbol.map(function(group) {
    var rows = group.relationships.map(function(r) {
      return "<tr><td>" + esc(r.type) + "</td><td><b>" + r.counterparty + "</b></td><td>" + fmt(r.confidence) +
        "</td><td style='max-width:32rem'>" + esc(r.description) + "</td></tr>";
    }).join("");
    return "<div style='margin-top:0.6rem'><b>" + group.symbol + "</b> <span style='opacity:0.6'>(" +
      group.relationships.length + ")</span></div>" +
      "<div class='tbl'><table><tr><th>Type</th><th>Counterparty</th><th>Confidence</th><th>Description</th></tr>" + rows + "</table></div>";
  }).join("");
}

// --- Interactive relationship graph (the approved mockup, ported) ----------
// The whole web on one SVG: anchors (news sources) and the tradeables that
// hang off them, nodes coloured by live dossier thesis, edges by relationship
// type and thickened by disclosed confidence. Layout is a deterministic
// force settle (seeded, no animation) so the 10s dashboard refresh redraws
// the exact same picture -- no jitter, and hover survives via delegation.
var GVIZ = {};

function gvColor(type) {
  if (type === "customer") return "--gc-cust";
  if (type === "supplier") return "--gc-supp";
  if (type === "competitor") return "--gc-comp";
  if (type === "regulator") return "--gc-reg";
  return "--gc-eco";
}
function gvNodeR(n) {
  if (n.kind === "anchor") return 13;
  if (n.score != null) return 6 + n.score * 10;
  return 6;
}
function gvNodeFill(n) {
  if (n.kind === "anchor") return "--gn-anchor";
  if (n.dir === "LONG") return "--gn-long";
  if (n.dir === "SHORT") return "--gn-short";
  return "--gn-none";
}
function gvLegend() {
  return '<div class="gviz-leg">' +
    '<span class="gl"><i class="gl-l" style="background:var(--gc-cust)"></i>customer</span>' +
    '<span class="gl"><i class="gl-l" style="background:var(--gc-comp)"></i>competitor</span>' +
    '<span class="gl"><i class="gl-l" style="background:var(--gc-eco)"></i>ecosystem</span>' +
    '<span class="gl"><i class="gl-d" style="background:var(--gn-anchor)"></i>anchor</span>' +
    '<span class="gl"><i class="gl-d" style="background:var(--gn-long)"></i>long thesis</span>' +
    '<span class="gl"><i class="gl-d" style="background:var(--gn-short)"></i>short thesis</span>' +
    '<span class="gl"><i class="gl-d" style="background:var(--gn-none)"></i>no thesis</span></div>';
}

function renderGraphViz(g) {
  var nodes = (g.nodes || []).slice(), edges = (g.edges || []).slice(), i, j;
  if (!edges.length) return '<div class="empty">No relationships extracted yet.</div>';
  var deg = {};
  edges.forEach(function(e) { deg[e[0]] = (deg[e[0]] || 0) + 1; deg[e[1]] = (deg[e[1]] || 0) + 1; });
  var note = "";
  if (nodes.length > 70) {
    nodes = nodes.slice().sort(function(a, b) { return (deg[b.id] || 0) - (deg[a.id] || 0); }).slice(0, 70);
    var keep = {}; nodes.forEach(function(n) { keep[n.id] = 1; });
    edges = edges.filter(function(e) { return keep[e[0]] && keep[e[1]]; });
    note = '<div class="gviz-note">Showing the 70 most-connected of ' + (g.nodes || []).length + ' symbols.</div>';
  }
  var VW = 1000, VH = 560, pos = {};
  var seed = 1234567;
  function rnd() { seed = (seed * 1664525 + 1013904223) % 4294967296; return seed / 4294967296; }
  nodes.forEach(function(n, k) {
    var a = k * 2 * Math.PI / nodes.length;
    pos[n.id] = { x: VW / 2 + (170 + rnd() * 50) * Math.cos(a), y: VH / 2 + (140 + rnd() * 50) * Math.sin(a), vx: 0, vy: 0 };
  });
  for (var it = 0; it < 250; it++) {
    for (i = 0; i < nodes.length; i++) {
      for (j = i + 1; j < nodes.length; j++) {
        var pa = pos[nodes[i].id], pb = pos[nodes[j].id];
        var dx = pa.x - pb.x, dy = pa.y - pb.y, d2 = dx * dx + dy * dy + 0.01, d = Math.sqrt(d2);
        var rep = 5200 / d2, ux = dx / d, uy = dy / d;
        pa.vx += ux * rep; pa.vy += uy * rep; pb.vx -= ux * rep; pb.vy -= uy * rep;
      }
    }
    edges.forEach(function(e) {
      var qa = pos[e[0]], qb = pos[e[1]]; if (!qa || !qb) return;
      var ex = qb.x - qa.x, ey = qb.y - qa.y, ed = Math.sqrt(ex * ex + ey * ey) + 0.01;
      var f = (ed - 92) * 0.02 * (0.5 + e[3]), nx = ex / ed, ny = ey / ed;
      qa.vx += nx * f; qa.vy += ny * f; qb.vx -= nx * f; qb.vy -= ny * f;
    });
    nodes.forEach(function(n) {
      var p = pos[n.id];
      p.vx += (VW / 2 - p.x) * 0.006; p.vy += (VH / 2 - p.y) * 0.006;
      p.vx *= 0.85; p.vy *= 0.85; p.x += p.vx; p.y += p.vy;
      p.x = Math.max(38, Math.min(VW - 38, p.x)); p.y = Math.max(32, Math.min(VH - 32, p.y));
    });
  }
  for (var pass = 0; pass < 9; pass++) {
    for (i = 0; i < nodes.length; i++) {
      for (j = i + 1; j < nodes.length; j++) {
        var ra = pos[nodes[i].id], rb = pos[nodes[j].id];
        var sx = rb.x - ra.x, sy = rb.y - ra.y, sd = Math.sqrt(sx * sx + sy * sy) + 0.01;
        var need = gvNodeR(nodes[i]) + gvNodeR(nodes[j]) + 13;
        if (sd < need) { var push = (need - sd) / 2, mx = sx / sd, my = sy / sd; ra.x -= mx * push; ra.y -= my * push; rb.x += mx * push; rb.y += my * push; }
      }
    }
  }
  GVIZ = {};
  nodes.forEach(function(n) { GVIZ[n.id] = { kind: n.kind, dir: n.dir, score: n.score, edges: [] }; });
  edges.forEach(function(e) {
    if (GVIZ[e[0]]) GVIZ[e[0]].edges.push([e[1], e[2], e[3]]);
    if (GVIZ[e[1]]) GVIZ[e[1]].edges.push([e[0], e[2], e[3]]);
  });
  var svgE = edges.map(function(e) {
    var pa = pos[e[0]], pb = pos[e[1]]; if (!pa || !pb) return "";
    var dash = (e[2] === "ecosystem" || e[2] === "eco") ? ' stroke-dasharray="3 4"' : "";
    return '<line x1="' + pa.x.toFixed(1) + '" y1="' + pa.y.toFixed(1) + '" x2="' + pb.x.toFixed(1) + '" y2="' + pb.y.toFixed(1) +
      '" stroke="var(' + gvColor(e[2]) + ')" stroke-width="' + (0.6 + e[3] * 2.4).toFixed(1) +
      '" stroke-opacity="' + (e[3] >= 0.6 ? 0.7 : 0.42) + '"' + dash + " />";
  }).join("");
  var svgN = nodes.map(function(n) {
    var p = pos[n.id], r = gvNodeR(n);
    var ly = n.kind === "anchor" ? (p.y + 3.5) : (p.y - r - 4);
    var lbl = '<text x="' + p.x.toFixed(1) + '" y="' + ly.toFixed(1) + '" text-anchor="middle" class="gn-lbl">' + esc(n.id) + '</text>';
    var sc = (n.kind !== "anchor" && n.score != null && r > 9)
      ? '<text x="' + p.x.toFixed(1) + '" y="' + (p.y + 3).toFixed(1) + '" text-anchor="middle" class="gn-sc">' + n.score.toFixed(2) + '</text>' : "";
    return '<g class="gnode" data-sym="' + esc(n.id) + '"><circle cx="' + p.x.toFixed(1) + '" cy="' + p.y.toFixed(1) +
      '" r="' + r.toFixed(1) + '" fill="var(' + gvNodeFill(n) + ')" stroke="var(--gn-stroke)" stroke-width="1.5"/>' + lbl + sc + "</g>";
  }).join("");
  return '<div class="gviz"><svg viewBox="0 0 ' + VW + " " + VH + '" preserveAspectRatio="xMidYMid meet">' +
    svgE + svgN + "</svg></div>" + note + gvLegend();
}

// Coverage: how much of the TRADEABLE universe is actually live, which is a
// different question from how many symbols are configured. A dossier count
// far below the tradeable count means most of the universe is dark, not that
// the market is quiet -- and the two connectivity rows say why.
function renderCoverage(c) {
  if (!c) return "";
  function pct(n, d) { return d ? Math.round(n / d * 100) + "%" : "-"; }
  function bar(n, d, label, hint) {
    var p = d ? Math.min(100, n / d * 100) : 0;
    // Amber below half, green at four fifths -- these are progress numbers,
    // not alarms, so the scale is deliberately forgiving.
    var colour = p >= 80 ? "#66bb6a" : (p >= 50 ? "#ffa726" : "#ef5350");
    return '<div style="margin:0.55rem 0">' +
      '<div style="display:flex;justify-content:space-between;font-size:0.85rem">' +
        '<span>' + label + '</span>' +
        '<span style="opacity:0.75">' + n + ' / ' + d + '  (' + pct(n, d) + ')</span>' +
      '</div>' +
      '<div style="height:7px;border-radius:4px;background:rgba(128,128,128,0.22);margin-top:0.25rem">' +
        '<div style="height:7px;border-radius:4px;width:' + p + '%;background:' + colour + '"></div>' +
      '</div>' +
      (hint ? '<div style="font-size:0.75rem;opacity:0.55;margin-top:0.2rem">' + hint + '</div>' : "") +
      '</div>';
  }
  var html = '<div class="card" style="grid-column:1/-1">';
  html += '<div class="label">Coverage &mdash; ' + c.tradeables + ' tradeable, ' + c.anchors + ' anchors</div>';
  html += bar(c.tradeables_with_dossier, c.tradeables, "Tradeables with a dossier",
              "A thesis has started accumulating. This is the number to watch.");
  html += bar(c.tradeables_connected, c.tradeables, "Tradeables connected to the graph",
              "An unconnected tradeable can never receive an anchor's news &mdash; it can only build " +
              "a dossier from its own coverage, and these names are chosen for having almost none.");
  html += bar(c.anchors_live, c.anchors, "Anchors linked to a tradeable",
              "An anchor is never its own analysis target, so one with no link to a tradeable is inert: " +
              "its news reaches nothing and is discarded unread.");
  if (c.tradeables_unconnected.length) {
    html += '<div style="font-size:0.75rem;opacity:0.6;margin-top:0.5rem">Unconnected tradeables: ' +
      c.tradeables_unconnected.join(" ") + '</div>';
  }
  html += "</div>";
  return html;
}

// ---- Operations-console overview: the bottom line before the detail. ----
function timeAgo(iso) {
  if (!iso) return { t: "never marked", stale: true };
  var ms = Date.now() - Date.parse(iso);
  if (isNaN(ms)) return { t: "", stale: false };
  var m = Math.floor(ms / 60000);
  var t = m < 1 ? "just now" : (m < 60 ? m + "m ago" : Math.floor(m / 60) + "h " + (m % 60) + "m ago");
  return { t: t, stale: m >= 90 };
}

function cumR(closed) {
  var s = 0, out = [0];
  for (var i = 0; i < closed.length; i++) { s += (closed[i].r_multiple || 0); out.push(s); }
  return out;
}

function sparkline(vals) {
  if (!vals || vals.length < 2) return '<div class="ov-sub">no closed trades yet</div>';
  var W = 280, H = 46, pad = 4, i;
  var hi = Math.max.apply(null, vals.concat([0])), lo = Math.min.apply(null, vals.concat([0])), rng = (hi - lo) || 1;
  function x(i) { return pad + i * (W - 2 * pad) / (vals.length - 1); }
  function y(v) { return pad + (hi - v) * (H - 2 * pad) / rng; }
  var zy = y(0), line = "";
  for (i = 0; i < vals.length; i++) { line += (i ? "L" : "M") + x(i).toFixed(1) + " " + y(vals[i]).toFixed(1) + " "; }
  var last = vals[vals.length - 1], col = last >= 0 ? "#3ecf6e" : "#ef5350";
  var fillc = last >= 0 ? "rgba(62,207,110,0.15)" : "rgba(239,83,80,0.15)";
  var area = line + "L" + x(vals.length - 1).toFixed(1) + " " + zy.toFixed(1) + " L" + x(0).toFixed(1) + " " + zy.toFixed(1) + " Z";
  return '<svg class="ov-spark" viewBox="0 0 ' + W + ' ' + H + '" preserveAspectRatio="none">' +
    '<path d="' + area + '" fill="' + fillc + '"/>' +
    '<line x1="0" y1="' + zy.toFixed(1) + '" x2="' + W + '" y2="' + zy.toFixed(1) + '" stroke="rgba(128,128,128,0.3)" stroke-dasharray="3 3"/>' +
    '<path d="' + line + '" fill="none" stroke="' + col + '" stroke-width="2" vector-effect="non-scaling-stroke"/>' +
    '<circle cx="' + x(vals.length - 1).toFixed(1) + '" cy="' + y(last).toFixed(1) + '" r="3" fill="' + col + '"/></svg>';
}

function renderAccount(data) {
  var ps = data.paper_stats, cur = ps.currency || "", closed = data.closed_paper_trades || [];
  var cr = cumR(closed), lastR = cr[cr.length - 1];
  var openUR = 0, hasUR = false;
  (data.open_paper_trades || []).forEach(function(t) {
    if (t.unrealized_currency !== null && t.unrealized_currency !== undefined) { openUR += t.unrealized_currency; hasUR = true; }
  });
  var realized = ps.realized_pnl || 0, equity = ps.equity || ps.initial_capital || 0;
  return '<div class="ov-p"><div class="ov-k">Account &middot; ' + cur + " " + fmt(ps.initial_capital, 0) + ' start</div>' +
    '<div class="ov-big ' + cls(realized) + '">' + cur + " " + fmt(equity, 0) + '</div>' +
    '<div class="ov-sub">realized <b class="' + cls(realized) + '">' + (realized >= 0 ? "+" : "") + fmt(realized, 0) + '</b> ' + cur +
      (hasUR ? ' &middot; open <b class="' + cls(openUR) + '">' + (openUR >= 0 ? "+" : "") + fmt(openUR, 0) + '</b> unreal.' : "") + '</div>' +
    sparkline(cr) +
    '<div class="ov-foot"><span>cum <b class="' + cls(lastR) + '">' + (lastR >= 0 ? "+" : "") + fmt(lastR) + 'R</b></span>' +
      '<span>' + ps.closed + ' closed</span><span class="pos">' + ps.wins + 'W</span><span class="neg">' + ps.losses + 'L</span></div></div>';
}

// The generation matching the live config -- the numbers that actually
// describe what the bot is doing now, as opposed to the pooled all-time
// record contaminated by trades taken under an old, abandoned strategy.
function currentGen(data) {
  var gs = (data.strategy_generations || []).filter(function(g) { return g.is_current; });
  return gs.length ? gs[0] : null;
}

function renderWinRate(data) {
  var cg = currentGen(data), ps = data.paper_stats;
  var name = cg ? cg.label : "";
  var allTime = '<div class="ov-sub" style="margin-top:0.35rem">all-time ' + ps.wins + "W&ndash;" + ps.losses + "L" +
    (ps.closed ? " &middot; " + Math.round(ps.win_rate * 100) + "%" : "") + "</div>";
  if (!cg || !cg.closed) {
    return '<div class="ov-p"><div class="ov-k">Win rate &middot; current strategy</div>' +
      '<div class="ov-big">&ndash;</div>' +
      '<div class="ov-sub">no closed trades yet' + (name ? ' under <b>' + esc(name) + '</b>' : '') + '</div>' +
      allTime + '</div>';
  }
  var lo = cg.win_rate_ci_low * 100, hi = cg.win_rate_ci_high * 100, pt = cg.win_rate * 100;
  return '<div class="ov-p"><div class="ov-k">Win rate &middot; ' + esc(name) + '</div>' +
    '<div class="ov-big">' + Math.round(pt) + '<span style="font-size:1rem;opacity:0.5">%</span></div>' +
    '<div class="ov-sub">95% CI ' + Math.round(lo) + "&ndash;" + Math.round(hi) + "% &middot; n=" + cg.closed + '</div>' +
    '<div class="ci-wrap"><div class="ci-axis"></div>' +
      '<div class="ci-band" style="left:' + lo + "%;width:" + (hi - lo) + '%"></div>' +
      '<div class="ci-pt" style="left:' + pt + '%"></div></div>' +
    '<div class="ci-lab"><span>0%</span><span>100%</span></div>' + allTime + '</div>';
}

function budgetMeter(u) {
  var pct = u.daily_usd_budget ? Math.min(100, u.usd_spent / u.daily_usd_budget * 100) : 0;
  var now = new Date(), frac = (now.getUTCHours() * 3600 + now.getUTCMinutes() * 60) / 86400 * 100;
  return '<div style="height:6px;border-radius:4px;background:rgba(128,128,128,0.2);margin-top:0.5rem;position:relative;overflow:hidden">' +
    '<div style="position:absolute;top:0;bottom:0;left:0;width:' + pct + "%;border-radius:4px;background:" + (pct > 85 ? "#e6b800" : "#3ea6cf") + '"></div>' +
    '<div style="position:absolute;top:0;bottom:0;left:' + frac + '%;width:2px;background:currentColor;opacity:0.55"></div></div>' +
    '<div class="ci-lab" style="margin-top:0.2rem"><span>' + u.calls + " / " + u.daily_call_budget + ' calls</span><span>| ' + Math.round(frac) + '% into UTC day</span></div>';
}

// Where the day's LLM spend actually went -- a segmented bar (scaled to the
// daily budget so headroom shows) plus a compact per-category legend. The
// split is what reveals one category starving another (see usage.py), which
// the single total can't.
function categoryBreakdown(u) {
  var cats = [["extraction", "#3ea6cf"], ["dossier", "#7e6bd0"], ["synthesis", "#e6a03e"], ["research", "#4bb886"]];
  var bc = u.by_category || {};
  var scale = (u.daily_usd_budget || 0) > 0 ? u.daily_usd_budget : (u.usd_spent || 1);
  var seg = cats.map(function(c) {
    var v = (bc[c[0]] || {}).usd || 0;
    return v > 0 ? '<div style="width:' + Math.min(100, v / scale * 100) + "%;background:" + c[1] + '"></div>' : "";
  }).join("");
  var leg = cats.map(function(c) {
    var v = (bc[c[0]] || {}).usd || 0;
    return '<span class="lc"><span class="lc-dot" style="background:' + c[1] + '"></span>' +
      c[0] + ' <b class="mono">$' + v.toFixed(2) + '</b></span>';
  }).join("");
  return '<div class="catbar">' + seg + '</div><div class="catleg">' + leg + '</div>';
}

function renderOverview(data) {
  var cg = currentGen(data), exp = (cg && cg.closed) ? cg : null;
  return '<div class="ov">' +
    renderAccount(data) +
    renderWinRate(data) +
    '<div class="ov-p"><div class="ov-k">Expectancy / trade &middot; current</div>' +
      '<div class="ov-big ' + (exp ? cls(exp.avg_r) : "") + '">' + (exp ? (exp.avg_r >= 0 ? "+" : "") + fmt(exp.avg_r) + "R" : "&ndash;") + '</div>' +
      '<div class="ov-sub mono">' + (exp ? "gross " + (exp.avg_r_gross >= 0 ? "+" : "") + fmt(exp.avg_r_gross) + "R" : "awaiting first close") + '</div>' +
      '<div class="ov-sub">open exposure <b>' + data.open_paper_trades.length + '</b> pos</div></div>' +
    '<div class="ov-p"><div class="ov-k">LLM budget today</div>' +
      '<div class="ov-big mono">$' + (data.usage.usd_spent || 0).toFixed(2) +
      '<span style="font-size:0.9rem;opacity:0.5"> / ' + (data.usage.daily_usd_budget || 0).toFixed(0) + '</span></div>' +
      budgetMeter(data.usage) + categoryBreakdown(data.usage) + '</div>' +
    '</div>';
}

function renderFunnel(data) {
  var c = data.coverage;
  if (!c) return "";
  var signaled = (data.dossiers || []).filter(function(d) { return d.status === "SIGNALED"; }).length;
  var base = c.tradeables || 1;
  var stages = [
    ["Tradeables", "in the universe", c.tradeables],
    ["Connected", "can receive anchor news", c.tradeables_connected],
    ["Has a dossier", "thesis accumulating", c.tradeables_with_dossier],
    ["Signaled now", "over the conviction bar", signaled]
  ];
  return '<div class="funnel">' + stages.map(function(s) {
    var p = Math.min(100, s[2] / base * 100);
    return '<div class="fn-row"><div class="fn-lab">' + s[0] + "<small>" + s[1] + '</small></div>' +
      '<div class="fn-bar"><span style="width:' + p + '%"></span></div>' +
      '<div class="fn-val mono">' + s[2] + ' <small>/' + c.tradeables + "</small></div></div>";
  }).join("") + '</div><div class="ov-sub" style="margin-top:0.5rem">Only <b>' +
    Math.round(c.tradeables_with_dossier / base * 100) + "%</b> of tradeables have a thesis &mdash; that gap, not market quiet, caps signal output.</div>";
}

function renderLadder(data) {
  var ds = (data.dossiers || []).slice().sort(function(a, b) {
    return (b.confidence * b.magnitude) - (a.confidence * a.magnitude);
  }).slice(0, 14);
  if (!ds.length) return '<div class="ov-sub">No dossiers yet.</div>';
  var rows = ds.map(function(d) {
    var sc = d.confidence * d.magnitude, pct = Math.min(100, sc * 100), isS = d.direction === "SHORT";
    var col = isS ? "#ef5350" : "#3ecf6e", soft = isS ? "rgba(239,83,80,0.2)" : "rgba(62,207,110,0.2)";
    var con = d.mass_opposing > 0.3 ? '<span class="lad-con" style="left:' + pct + '%"></span>' : "";
    return '<div class="lad-row"><div class="lad-sym mono">' + esc(d.symbol) + '</div>' +
      '<div class="lad-trk"><div class="lad-fill" style="width:' + pct + "%;background:" + soft + '"></div>' +
      con + '<div class="lad-dot" style="left:' + pct + "%;background:" + col + '"></div></div></div>';
  }).join("");
  return '<div class="lad"><div class="lad-scale"><div class="lad-line"></div>' +
    '<div class="lad-line-lab" style="left:50%">bar 0.50</div></div>' + rows + '</div>' +
    '<div class="ov-sub" style="margin-top:0.4rem">&#9679; long &#9679; short &middot; <span style="color:#e6b800">&#9676;</span> contested. Right of 0.50 fires.</div>';
}

// The win-loss record split by strategy generation -- the current strategy
// measured on its own trades, the old config's record kept separate and
// labelled, so a new strategy's performance is never pooled with an
// abandoned one (see status.gather_strategy_generations).
function renderStrategyRecord(gens) {
  if (!gens || !gens.length) return "";
  var rows = gens.map(function(g) {
    var wl = g.closed ? (g.wins + "W&ndash;" + g.losses + "L") : "&mdash;";
    var wr = g.closed ? Math.round(g.win_rate * 100) + "%" : "&mdash;";
    var avgr = g.closed ? ((g.avg_r >= 0 ? "+" : "") + fmt(g.avg_r) + "R") : "&mdash;";
    var ver = "";
    if (!g.legacy && g.version_from) {
      ver = "v" + g.version_from + (g.version_to && g.version_to !== g.version_from ? "&ndash;v" + g.version_to : "");
    }
    var dotcls = g.is_current ? "gen-dot on" : (g.legacy ? "gen-dot off" : "gen-dot");
    return '<div class="gen-row"><div class="gen-name"><span class="' + dotcls + '"></span><b>' + esc(g.label) + '</b>' +
      (ver ? ' <span class="gen-ver">' + ver + '</span>' : "") +
      (g.is_current ? ' <span class="gen-tag">live</span>' : "") + '</div>' +
      '<div class="mono">' + wl + '</div><div class="mono">' + wr + '</div>' +
      '<div class="mono ' + (g.closed ? cls(g.avg_r) : "") + '">' + avgr + '</div></div>';
  }).join("");
  return '<div class="ov-p" style="margin-top:0.8rem"><div class="ov-k">Strategy record &middot; win&ndash;loss by generation</div>' +
    '<div class="gen-head"><span>strategy</span><span>W&ndash;L</span><span>win</span><span>avg R</span></div>' + rows +
    '<div class="ov-sub" style="margin-top:0.5rem">A strategy change starts a fresh record &mdash; the current strategy is measured only on its own trades, never pooled with the old config. <b>Legacy</b> = trades taken before generation tracking began.</div></div>';
}

function render(data) {
  var html = "";
  html += renderCapabilities(data.capabilities);
  html += renderOverview(data);
  html += '<div class="ov" style="grid-template-columns:1.4fr 1fr">' +
    '<div class="ov-p"><div class="ov-k">Universe activation</div>' + renderFunnel(data) + '</div>' +
    '<div class="ov-p"><div class="ov-k">Dossiers by conviction</div>' + renderLadder(data) + '</div></div>';
  html += renderStrategyRecord(data.strategy_generations);
  html += "<h2>Dossiers</h2>" + renderDossiers(data.dossiers);
  html += renderPaperTrades(data.closed_paper_trades, data.open_paper_trades);
  html += "<h2>Recent Signals</h2>" + renderSignals(data.recent_signals);
  html += "<h2>Relationship Graph</h2>" + renderGraphViz(data.graph) +
    '<details class="gtables"><summary>Relationships as a table</summary>' + renderGraph(data.graph) + "</details>";
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
// Sent on every state-changing POST. The custom header is what the server's
// CSRF guard requires (see _CSRF_HEADER in webapp.py) -- keep the name in
// sync with it. GET (the auto-refresh) deliberately sends nothing.
var POST_HEADERS = { "Content-Type": "application/json", "X-SmartBoi-Request": "1" };

document.addEventListener("click", function(ev) {
  var btn = ev.target.closest(".accept-btn");
  if (!btn) return;
  btn.disabled = true;
  fetch(API_BASE + "candidates/accept", {
    method: "POST",
    headers: POST_HEADERS,
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

// --- Tools (see /api/tools/* in webapp.py) ---------------------------------
// A screening run is ~2.2s per ticker (two rate-limited Finnhub calls), so a
// full run can legitimately take minutes -- far past REFRESH_TIMEOUT_MS,
// hence its own much longer budget.
var TOOL_TIMEOUT_MS = 300000;

// Reveal the shared tool-output panel and its Copy button together. Several
// tools write into #tool-output, and all of them want the Copy button shown.
function showToolOutput() {
  document.getElementById("tool-output").style.display = "block";
  document.getElementById("tool-output-actions").style.display = "flex";
}

// One-click copy of whatever report is currently shown -- the diagnostics
// bundle is long and drag-selecting it is miserable. navigator.clipboard
// needs a secure context, which a plain-HTTP LAN Home Assistant install is
// not, so fall back to a hidden-textarea execCommand (works over HTTP) and,
// if even that is blocked, select the block so Ctrl-C still copies it.
function copyToolOutput(btn) {
  var text = document.getElementById("tool-output").textContent || "";
  if (!text) return;
  function flash(msg) { btn.textContent = msg; setTimeout(function() { btn.textContent = "Copy report"; }, 1500); }
  if (navigator.clipboard && navigator.clipboard.writeText) {
    navigator.clipboard.writeText(text).then(function() { flash("Copied!"); },
                                          function() { fallbackCopyToolOutput(text, flash); });
  } else {
    fallbackCopyToolOutput(text, flash);
  }
}

function fallbackCopyToolOutput(text, flash) {
  var ta = document.createElement("textarea");
  ta.value = text;
  ta.style.position = "fixed";
  ta.style.top = "-1000px";
  document.body.appendChild(ta);
  ta.focus();
  ta.select();
  var ok = false;
  try { ok = document.execCommand("copy"); } catch (e) { ok = false; }
  document.body.removeChild(ta);
  if (ok) { flash("Copied!"); return; }
  // Even execCommand refused -- leave the report selected so the keyboard
  // shortcut still works.
  var range = document.createRange();
  range.selectNodeContents(document.getElementById("tool-output"));
  var sel = window.getSelection();
  sel.removeAllRanges();
  sel.addRange(range);
  flash("Press Ctrl-C");
}

function runTool(path, body, button) {
  var out = document.getElementById("tool-output");
  var buttons = [document.getElementById("btn-screen"), document.getElementById("btn-analyze"),
                 document.getElementById("btn-event-study"), document.getElementById("btn-exit-analysis"),
                 document.getElementById("btn-diagnostics"), document.getElementById("btn-research")];
  buttons.forEach(function(b) { b.disabled = true; });
  showToolOutput();
  out.textContent = "Running… (this can take a few minutes for a long ticker list)";

  var controller = new AbortController();
  var timedOut = false;
  var timer = setTimeout(function() { timedOut = true; controller.abort(); }, TOOL_TIMEOUT_MS);
  fetch(API_BASE + path, {
    method: "POST",
    headers: POST_HEADERS,
    body: JSON.stringify(body || {}),
    signal: controller.signal,
  }).then(function(r) {
    clearTimeout(timer);
    return r.json();
  }).then(function(result) {
    out.textContent = result.error ? ("Error: " + result.error) : result.report;
  }).catch(function(err) {
    clearTimeout(timer);
    out.textContent = timedOut
      ? "No response after " + (TOOL_TIMEOUT_MS / 1000) + "s -- the run may still be going; try again shortly."
      : "Failed: " + err;
  }).finally(function() {
    buttons.forEach(function(b) { b.disabled = false; });
  });
}

document.getElementById("btn-screen").addEventListener("click", function() {
  // Bounds are editable so a screen can be re-run at a different bar without
  // a terminal -- the whole point of this panel.
  runTool("tools/screen", {
    tickers: document.getElementById("screen-tickers").value,
    min_cap: parseFloat(document.getElementById("screen-min-cap").value),
    max_cap: parseFloat(document.getElementById("screen-max-cap").value),
    max_analysts: parseInt(document.getElementById("screen-max-analysts").value, 10),
  }, this);
});
document.getElementById("btn-analyze").addEventListener("click", function() {
  runTool("tools/forward-returns", {}, this);
});
document.getElementById("btn-event-study").addEventListener("click", function() {
  runTool("tools/event-study", {}, this);
});
document.getElementById("btn-exit-analysis").addEventListener("click", function() {
  runTool("tools/exit-analysis", {}, this);
});
document.getElementById("btn-diagnostics").addEventListener("click", function() {
  runTool("tools/diagnostics", {}, this);
});
document.getElementById("btn-research").addEventListener("click", function() {
  if (!confirm("Research the 10 most inert anchors for small-cap suppliers?\\n\\n" +
      "This spends LLM budget (web search + one call per anchor) and can take a few minutes. " +
      "It adds universe CANDIDATES for your review; it never adds a symbol or a relationship edge.")) {
    return;
  }
  runTool("tools/supplier-research", {}, this);
});
document.getElementById("btn-rebuild-graph").addEventListener("click", function() {
  if (!confirm("Re-extract relationships from every tradeable's latest 10-K?\\n\\n" +
               "Additive only -- edges are deduped, so this can never remove an edge or touch a " +
               "dossier, trade or log. Costs about one LLM call per tradeable symbol. Runs in the " +
               "background over the next few polling ticks.")) return;
  var out = document.getElementById("tool-output");
  showToolOutput();
  out.textContent = "Queueing…";
  fetch(API_BASE + "universe/rebuild-graph", {
    method: "POST", headers: POST_HEADERS, body: "{}",
  }).then(function(r) { return r.json(); }).then(function(res) {
    out.textContent = res.error ? ("Error: " + res.error)
      : ("Queued " + res.queued + " symbol(s) for relationship re-extraction (graph currently has " +
         res.edges_before + " edges).\\nWatch the Relationships panel and the log -- new edges appear " +
         "over the next few ticks as each 10-K is re-read.");
    refresh();
  }).catch(function(err) { out.textContent = "Failed: " + err; });
});
document.getElementById("btn-reset").addEventListener("click", function() {
  if (!confirm("Remove every symbol added at runtime and return to the curated universe?\\n\\n" +
               "Dossiers for the removed symbols are ARCHIVED, not deleted. Candidates, trades " +
               "and captured logs are untouched.")) return;
  var out = document.getElementById("tool-output");
  showToolOutput();
  out.textContent = "Resetting…";
  fetch(API_BASE + "universe/reset-accepted", {
    method: "POST", headers: POST_HEADERS, body: "{}",
  }).then(function(r) { return r.json(); }).then(function(res) {
    out.textContent = res.error ? ("Error: " + res.error)
      : ("Removed " + res.removed.length + " added symbol(s): " + (res.removed.join(", ") || "none") +
         "\\nUniverse is now " + res.universe_size + " symbols.");
    refresh();
  }).catch(function(err) { out.textContent = "Failed: " + err; });
});
document.getElementById("btn-copy-output").addEventListener("click", function() {
  copyToolOutput(this);
});

// Graph tooltip: delegated on document so it keeps working after the 10s
// innerHTML refresh rebuilds the SVG. Reads GVIZ, which renderGraphViz
// repopulates each render.
(function() {
  var gtip = document.getElementById("gtip");
  document.addEventListener("mouseover", function(ev) {
    var node = ev.target.closest ? ev.target.closest(".gnode") : null;
    if (!node) return;
    var sym = node.getAttribute("data-sym"), info = GVIZ[sym];
    if (!info) return;
    var kind = info.kind === "anchor" ? "anchor &middot; news source"
      : (info.kind === "tradeable" ? "tradeable" : "external");
    var thesis = info.kind === "anchor" ? ""
      : (info.score != null
          ? "<br>" + (info.dir === "LONG" ? "LONG" : info.dir === "SHORT" ? "SHORT" : "no") + " thesis, score " + info.score.toFixed(2)
          : "<br>no thesis yet");
    var links = info.edges.length
      ? "<br>" + info.edges.length + " link(s): " + info.edges.slice(0, 6).map(function(x) { return x[0] + " (" + x[1] + ")"; }).join(", ")
      : "";
    gtip.innerHTML = '<div class="gt-h">' + sym + '</div><div class="gt-m">' + kind + thesis + links + "</div>";
    gtip.style.opacity = "1";
  });
  document.addEventListener("mousemove", function(ev) {
    if (gtip.style.opacity !== "1") return;
    gtip.style.left = (ev.clientX + 14) + "px";
    gtip.style.top = (ev.clientY + 12) + "px";
  });
  document.addEventListener("mouseout", function(ev) {
    if (ev.target.closest && ev.target.closest(".gnode")) gtip.style.opacity = "0";
  });
})();

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
    paper_stats, closed_trades = gather_paper_trade_stats(
        log_dir / "paper_trades.jsonl",
        settings.initial_trading_capital, settings.trading_currency,
    )
    current_strategy = settings.strategy_signature()
    strategy_generations = gather_strategy_generations(
        log_dir / "paper_trades.jsonl", current_strategy
    )
    open_trades = []
    for t in engine.journal.open_trades.values():
        row = asdict(t)
        row["unrealized_r"] = t.unrealized_r_multiple()
        row["unrealized_currency"] = t.unrealized_currency()
        open_trades.append(row)

    return {
        "capabilities": {
            "edgar": engine.edgar_client is not None,
            "news": engine.finnhub is not None,
            "anthropic": engine.updater is not None,
            "ib": engine.price_feed is not None,
        },
        "universe_size": len(engine.symbol_list),
        "coverage": gather_coverage(engine.universe, engine.graph, engine.dossiers),
        "dossiers": gather_dossiers(engine.dossiers),
        "graph": gather_graph_stats(engine.graph, engine.universe, engine.dossiers),
        "open_paper_trades": open_trades,
        "closed_paper_trades": closed_trades,
        "paper_stats": paper_stats.__dict__,
        "strategy_generations": [asdict(g) for g in strategy_generations],
        "current_strategy": current_strategy,
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
        try:
            spec = engine.accept_candidate(symbol, as_type)
        except ValueError as exc:
            # accept_candidate refuses to make a large, heavily-covered name a
            # trade target regardless of which button was clicked -- surface
            # its reason rather than a generic failure.
            return web.json_response({"error": str(exc)}, status=400)
        return web.json_response({"ok": True, "symbol": spec.symbol, "as": as_type})

    # Serializes tool runs. A screening pass shares the engine's own
    # FinnhubClient (see tools.run_screen for why), and that client's
    # request spacing is a single instance-wide timer -- two concurrent
    # runs would interleave and blow through the free tier's 60/min.
    tool_lock = asyncio.Lock()

    async def _run_tool(handler) -> web.Response:
        """Shared wrapper for /api/tools/*: one at a time, never 500s the
        page, and never lets a tool failure touch the engine."""
        if tool_lock.locked():
            return web.json_response(
                {"error": "Another tool run is already in progress -- wait for it to finish."}, status=409
            )
        async with tool_lock:
            try:
                return web.json_response({"report": await handler()})
            except Exception as exc:  # noqa: BLE001 - a tool failure is a message, not a dead dashboard
                log.exception("Dashboard tool run failed")
                return web.json_response({"error": redact_token(exc)}, status=500)

    async def handle_tool_screen(request: web.Request) -> web.Response:
        """Runs the candidate screener (smartboi.tools.run_screen) and
        returns its report as text. Read-only: it performs market-cap and
        analyst-count lookups and formats a table. It cannot add a symbol
        to the universe -- that stays a separate, deliberate click on a
        discovered candidate (see handle_accept_candidate)."""
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001 - a malformed body is a 400, not a 500
            return web.json_response({"error": "invalid JSON body"}, status=400)
        tickers = _parse_tickers(body.get("tickers"))

        async def run() -> str:
            return await run_screen(
                engine.finnhub, engine.universe, tickers,
                float(body.get("min_cap") or SCREEN_MIN_CAP_MUSD),
                float(body.get("max_cap") or SCREEN_MAX_CAP_MUSD),
                int(body.get("max_analysts") or SCREEN_MAX_ANALYSTS),
            )

        return await _run_tool(run)

    async def handle_tool_supplier_research(request: web.Request) -> web.Response:
        """Researches anchors for the small-cap counterparties the filing
        path structurally cannot find (smartboi.tools.run_supplier_research).

        Writes universe CANDIDATES only -- never a graph edge. A web-sourced
        relationship is not a disclosure, and an edge at or above
        DISCLOSED_LINK_CONFIDENCE satisfies the corroboration bar that fires
        trades; a candidate accepted from here gets its own 10-K backfilled,
        and the edge is created only if a filing actually discloses it."""
        async def run() -> str:
            return await run_supplier_research(engine)

        return await _run_tool(run)

    async def handle_tool_forward_returns(request: web.Request) -> web.Response:
        """Runs the forward-return analysis (smartboi.tools.run_forward_returns)
        over the captured snapshot/price logs. Pure file reads -- no network,
        no LLM, nothing mutated."""
        async def run() -> str:
            # Offloaded to a thread: this reads two append-only logs that
            # grow every day, and parsing them synchronously on the event
            # loop would stall the engine's polling for the duration.
            return await asyncio.to_thread(
                run_forward_returns, engine.settings.log_dir, engine.universe
            )

        return await _run_tool(run)

    async def handle_tool_event_study(request: web.Request) -> web.Response:
        """Runs the signal-episode event study (smartboi.tools.run_event_study)
        over signals.jsonl + decisions.jsonl + price_marks.jsonl -- forward
        returns after each signal episode, split by what the engine did with
        it. Pure file reads -- no network, no LLM, nothing mutated."""
        async def run() -> str:
            # Threaded for the same reason as the forward-return report:
            # these logs grow daily and parsing them on the event loop
            # would stall the engine's polling.
            return await asyncio.to_thread(run_event_study, engine.settings.log_dir)

        return await _run_tool(run)

    async def handle_tool_exit_analysis(request: web.Request) -> web.Response:
        """Runs the exit-quality analysis (smartboi.tools.run_exit_analysis)
        over paper_trades.jsonl + price_marks.jsonl -- holding period vs
        horizon, realized reward:risk, stop-gap integrity, cost drag, and the
        hold-to-horizon counterfactual. Pure file reads -- no network, no LLM,
        nothing mutated."""
        async def run() -> str:
            # Threaded like the other report tools: the ledger and price
            # marks grow daily and parsing them on the event loop would
            # stall the engine's polling.
            return await asyncio.to_thread(run_exit_analysis, engine.settings.log_dir)

        return await _run_tool(run)

    async def handle_tool_diagnostics(request: web.Request) -> web.Response:
        """One pasteable runtime-state bundle (smartboi.tools.run_diagnostics)
        -- integrations, universe, graph, dossiers, where evidence is coming
        from, spend, signals, trades, candidates, capture coverage, recent
        problems. Pure reads; credentials and personal data are omitted by an
        allow-list, and log lines are scrubbed, because this is meant to be
        pasted somewhere."""
        async def run() -> str:
            # Threaded: reads several append-only logs that grow daily, which
            # would otherwise stall the engine's polling for the duration.
            return await asyncio.to_thread(run_diagnostics, engine)

        return await _run_tool(run)

    async def handle_reset_accepted(request: web.Request) -> web.Response:
        """Drops every runtime-accepted symbol, returning the universe to the
        curated list, and archives the dossiers that orphans (see
        engine.reset_accepted_candidates). The only DESTRUCTIVE endpoint here,
        and deliberately narrow: it removes additions, never the curated
        universe, never a candidate, never a trade or a captured log. The UI
        confirms before calling it."""
        # Runs directly on the event loop, NOT in a worker thread: it
        # mutates live engine state (universe, spec_by_symbol, the
        # accepted-candidates dict) that the engine's own coroutines read
        # and write between awaits -- a thread doing the same concurrently
        # raced them ("dictionary changed size during iteration" aborting
        # the reset half-done, or a spec swap landing mid-_process_evidence).
        # It's a handful of small file writes and renames; the brief event-
        # loop stall is the price of it being atomic w.r.t. the engine.
        result = engine.reset_accepted_candidates()
        return web.json_response({"ok": True, **result})

    async def handle_rebuild_graph(request: web.Request) -> web.Response:
        """Re-extracts relationships from every tradeable's latest 10-K (see
        engine.rebuild_relationship_graph). Additive only -- graph.add
        dedupes, so this cannot remove an edge or touch a dossier, a trade,
        or a captured log. Runs on the event loop for the same reason the
        reset does: it mutates engine state the polling coroutines read."""
        result = engine.rebuild_relationship_graph()
        return web.json_response({"ok": True, **result})

    app = web.Application(middlewares=[_require_csrf_header])
    app.router.add_get("/", handle_index)
    app.router.add_get("/api/status", handle_status)
    app.router.add_post("/api/candidates/accept", handle_accept_candidate)
    app.router.add_post("/api/tools/screen", handle_tool_screen)
    app.router.add_post("/api/tools/supplier-research", handle_tool_supplier_research)
    app.router.add_post("/api/tools/forward-returns", handle_tool_forward_returns)
    app.router.add_post("/api/tools/event-study", handle_tool_event_study)
    app.router.add_post("/api/tools/exit-analysis", handle_tool_exit_analysis)
    app.router.add_post("/api/tools/diagnostics", handle_tool_diagnostics)
    app.router.add_post("/api/universe/reset-accepted", handle_reset_accepted)
    app.router.add_post("/api/universe/rebuild-graph", handle_rebuild_graph)
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
