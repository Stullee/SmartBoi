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
import hashlib
import json
import logging
import os
import re
import time
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

from aiohttp import web

from smartboi.news import redact_token
from smartboi.research import researched_anchors
from smartboi.screen import (
    DEFAULT_MAX_ANALYSTS as SCREEN_MAX_ANALYSTS,
    DEFAULT_MAX_CAP_MUSD as SCREEN_MAX_CAP_MUSD,
    DEFAULT_MIN_CAP_MUSD as SCREEN_MIN_CAP_MUSD,
)
from smartboi.tools import (
    collect_full_diagnostics,
    run_diagnostics,
    run_event_study,
    run_exit_analysis,
    run_forward_returns,
    run_screen,
    run_edgar_supplier_search,
    run_graph_maintenance,
    run_supplier_research,
)
from smartboi.status import (
    gather_coverage,
    gather_dossier_detail,
    gather_dossiers,
    gather_graph_stats,
    gather_graph_health,
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
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>SmartBoi Status</title>
<style>
  /* ===== Theme tokens: bare :root = light; dark via media + explicit stamp ===== */
  :root {
    --ground:#eef1f5; --panel:#ffffff; --panel-hi:#f4f7fb; --sunk:#e9edf3;
    --line:#d8e0ea; --line-soft:#e6ebf2;
    --ink:#111821; --ink-2:#3a4554; --muted:#66788b; --faint:#93a1b2;
    --accent:#0784b5; --accent-soft:rgba(7,132,181,0.12);
    --pos:#12813c; --pos-soft:rgba(18,129,60,0.13);
    --neg:#c4362b; --neg-soft:rgba(196,54,43,0.12);
    --warn:#9a6a00; --warn-soft:rgba(154,106,0,0.14);
    --gc-cust:#3a86d4; --gc-supp:#2fa35a; --gc-comp:#c4362b; --gc-reg:#7a6bd0; --gc-eco:#8493a6;
    --gn-anchor:#5a6675; --gn-txt:#111821; --gn-stroke:#ffffff;
    --cat-ext:#0784b5; --cat-dos:#7a6bd0; --cat-syn:#c07a1e; --cat-res:#2fa35a;
    --shadow:0 1px 2px rgba(16,26,40,0.06),0 8px 24px -12px rgba(16,26,40,0.18);
  }
  @media (prefers-color-scheme: dark) {
    :root:not([data-theme="light"]) {
      --ground:#0a0e13; --panel:#121821; --panel-hi:#171f2a; --sunk:#0d131a;
      --line:#232f3d; --line-soft:#1b2531;
      --ink:#e9eff6; --ink-2:#c2ccd8; --muted:#8494a6; --faint:#5d6c7e;
      --accent:#38bdf0; --accent-soft:rgba(56,189,240,0.14);
      --pos:#42c265; --pos-soft:rgba(66,194,101,0.15);
      --neg:#f26a5e; --neg-soft:rgba(242,106,94,0.15);
      --warn:#e3b341; --warn-soft:rgba(227,179,65,0.15);
      --gc-cust:#5aa9f0; --gc-supp:#42c265; --gc-comp:#f26a5e; --gc-reg:#a79bf5; --gc-eco:#8494a6;
      --gn-anchor:#8a97a8; --gn-txt:#e9eff6; --gn-stroke:#121821;
      --cat-ext:#38bdf0; --cat-dos:#a79bf5; --cat-syn:#e3b341; --cat-res:#42c265;
      --shadow:0 1px 2px rgba(0,0,0,0.3),0 10px 30px -14px rgba(0,0,0,0.6);
    }
  }
  :root[data-theme="dark"] {
    --ground:#0a0e13; --panel:#121821; --panel-hi:#171f2a; --sunk:#0d131a;
    --line:#232f3d; --line-soft:#1b2531;
    --ink:#e9eff6; --ink-2:#c2ccd8; --muted:#8494a6; --faint:#5d6c7e;
    --accent:#38bdf0; --accent-soft:rgba(56,189,240,0.14);
    --pos:#42c265; --pos-soft:rgba(66,194,101,0.15);
    --neg:#f26a5e; --neg-soft:rgba(242,106,94,0.15);
    --warn:#e3b341; --warn-soft:rgba(227,179,65,0.15);
    --gc-cust:#5aa9f0; --gc-supp:#42c265; --gc-comp:#f26a5e; --gc-reg:#a79bf5; --gc-eco:#8494a6;
    --gn-anchor:#8a97a8; --gn-txt:#e9eff6; --gn-stroke:#121821;
    --cat-ext:#38bdf0; --cat-dos:#a79bf5; --cat-syn:#e3b341; --cat-res:#42c265;
    --shadow:0 1px 2px rgba(0,0,0,0.3),0 10px 30px -14px rgba(0,0,0,0.6);
  }

  * { box-sizing:border-box; }
  body { margin:0; background:var(--ground); color:var(--ink); font-size:14px; line-height:1.45;
         font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,"Helvetica Neue",Arial,sans-serif;
         -webkit-font-smoothing:antialiased; }
  .mono { font-family:ui-monospace,"SF Mono","JetBrains Mono",Menlo,Consolas,monospace; font-variant-numeric:tabular-nums; }
  .wrap { max-width:1240px; margin:0 auto; padding:0 20px 64px; }
  .pos{ color:var(--pos);} .neg{ color:var(--neg);} .warn{ color:var(--warn);} .acc{ color:var(--accent);}

  /* rail */
  .rail { position:sticky; top:0; z-index:30; background:color-mix(in srgb,var(--ground) 86%,transparent);
          backdrop-filter:blur(10px); border-bottom:1px solid var(--line); }
  .rail-in { max-width:1240px; margin:0 auto; padding:10px 20px; display:flex; align-items:center; gap:16px; flex-wrap:wrap; }
  .brand { display:flex; align-items:baseline; gap:9px; }
  .brand b { font-size:15px; letter-spacing:-0.01em; }
  .brand .v { font-size:11px; color:var(--muted); }
  .brand .paper { font-size:10px; text-transform:uppercase; letter-spacing:0.08em; color:var(--accent);
                  border:1px solid var(--accent); border-radius:999px; padding:1px 7px; }
  .caps { display:flex; gap:6px; flex-wrap:wrap; }
  .cap { display:inline-flex; align-items:center; gap:5px; font-size:11.5px; color:var(--ink-2);
         background:var(--panel); border:1px solid var(--line); border-radius:7px; padding:3px 8px; }
  .cap .dot { width:6px; height:6px; border-radius:50%; background:var(--faint); }
  .cap.on .dot { background:var(--pos); box-shadow:0 0 0 3px var(--pos-soft); }
  .cap.off { color:var(--faint); }
  .rail-right { margin-left:auto; display:flex; align-items:center; gap:14px; }
  .updated { font-size:11.5px; color:var(--muted); display:flex; align-items:center; gap:6px; }
  .live { width:7px; height:7px; border-radius:50%; background:var(--pos); animation:pulse 2.4s infinite; }
  @keyframes pulse { 0%{box-shadow:0 0 0 0 var(--pos-soft);} 70%{box-shadow:0 0 0 6px transparent;} 100%{box-shadow:0 0 0 0 transparent;} }
  @media (prefers-reduced-motion:reduce){ .live,.dotlive{animation:none;} }
  .toggle { font:inherit; font-size:12px; color:var(--ink-2); background:var(--panel); border:1px solid var(--line);
            border-radius:7px; padding:4px 9px; cursor:pointer; }
  .toggle:hover { background:var(--panel-hi); }

  h1 { font-size:0; margin:0; height:0; }
  .eyebrow { font-size:11px; text-transform:uppercase; letter-spacing:0.11em; color:var(--muted);
             margin:26px 0 11px; display:flex; align-items:center; gap:9px; }
  .eyebrow::after { content:""; flex:1; height:1px; background:var(--line-soft); }

  .grid { display:grid; gap:14px; }
  /* A grid item defaults to min-width:auto, i.e. it refuses to shrink below
     its own min-content -- which is what pushed the whole PAGE into a
     horizontal scroll on a phone even though every grid here collapses to
     one column. The panels that hold wide content already wrap it in
     .scroll, so they can shrink safely. */
  .grid > * { min-width:0; }
  .phead { flex-wrap:wrap; gap:4px 10px; }
  .panel { background:var(--panel); border:1px solid var(--line); border-radius:12px; box-shadow:var(--shadow); }
  .pad { padding:15px 17px; }

  .hero { grid-template-columns:1.55fr 1fr 1fr 1fr; }
  @media (max-width:900px){ .hero{ grid-template-columns:1fr 1fr; } }
  @media (max-width:560px){ .hero{ grid-template-columns:1fr; } }
  .k { font-size:11px; text-transform:uppercase; letter-spacing:0.06em; color:var(--muted); }
  .big { font-size:30px; font-weight:650; letter-spacing:-0.02em; margin-top:3px; line-height:1.05; }
  .u { font-size:16px; color:var(--muted); font-weight:500; }
  .sub { font-size:12px; color:var(--muted); margin-top:5px; }
  .pnl-head { display:flex; justify-content:space-between; align-items:flex-start; }
  .spark { width:100%; height:74px; margin-top:8px; display:block; }
  .foot { display:flex; gap:16px; margin-top:9px; font-size:12px; color:var(--muted); flex-wrap:wrap; }
  .foot b { color:var(--ink-2); font-weight:600; }
  .note { font-size:11px; color:var(--faint); margin-top:9px; }
  .regime { margin-top:11px; font-size:11.5px; color:var(--warn); background:var(--warn-soft);
            border-radius:7px; padding:6px 9px; display:flex; gap:7px; align-items:flex-start; }

  .meter { height:6px; border-radius:4px; background:var(--sunk); overflow:hidden; margin-top:9px; position:relative; }
  .meter > span { position:absolute; inset:0 auto 0 0; border-radius:4px; }
  .ci-track { position:relative; height:26px; margin-top:10px; }
  .ci-axis { position:absolute; left:0; right:0; top:11px; height:2px; background:var(--sunk); border-radius:2px; }
  .ci-band { position:absolute; top:7px; height:10px; background:var(--accent-soft); border:1px solid var(--accent); border-radius:6px; }
  .ci-point { position:absolute; top:4px; width:2px; height:16px; background:var(--accent); border-radius:2px; }
  .ci-be { position:absolute; top:2px; width:0; height:22px; border-left:2px dashed var(--neg); }
  .ci-labels { display:flex; justify-content:space-between; font-size:10.5px; color:var(--faint); margin-top:1px; }

  /* strategy record */
  .gens { display:flex; flex-direction:column; }
  .gen { display:grid; grid-template-columns:1fr auto auto auto; gap:14px; align-items:center; padding:9px 0;
         border-bottom:1px solid var(--line-soft); }
  .gen:last-child { border-bottom:none; }
  .gen-h { font-size:10.5px; text-transform:uppercase; letter-spacing:0.05em; color:var(--faint); padding-bottom:6px; border-bottom:1px solid var(--line-soft); }
  .gen-h > span:not(:first-child), .gen > div:not(.gen-name){ text-align:right; }
  .gen-name { display:flex; align-items:center; gap:8px; min-width:0; }
  .gen-name b { white-space:nowrap; overflow:hidden; text-overflow:ellipsis; }
  .gen-dot { width:8px; height:8px; border-radius:50%; background:var(--faint); flex:none; }
  .gen-dot.cur { background:var(--pos); box-shadow:0 0 0 3px var(--pos-soft); }
  .gen-ver { font-size:11px; color:var(--faint); }
  .gen-live { font-size:9.5px; text-transform:uppercase; letter-spacing:0.04em; color:var(--pos);
              border:1px solid var(--pos); border-radius:999px; padding:1px 6px; }

  /* funnel */
  .funnel { display:flex; flex-direction:column; gap:9px; }
  .stage { display:grid; grid-template-columns:150px 1fr auto; align-items:center; gap:12px; }
  .stage .lab { font-size:12.5px; color:var(--ink-2); }
  .stage .lab small { display:block; color:var(--faint); font-size:10.5px; }
  .bar { height:24px; border-radius:6px; background:var(--sunk); overflow:hidden; position:relative; }
  .bar > span { position:absolute; inset:0 auto 0 0; border-radius:6px;
                background:linear-gradient(90deg,var(--accent),color-mix(in srgb,var(--accent) 55%,var(--panel))); }
  .stage .val { font-size:14px; min-width:38px; text-align:right; }
  .stage .val small { color:var(--faint); font-size:11px; }

  .budget-cats { display:flex; flex-direction:column; gap:7px; margin-top:11px; }
  .bcat { display:grid; grid-template-columns:80px 1fr auto; align-items:center; gap:9px; font-size:11.5px; }
  .bcat .bl { color:var(--ink-2); }
  .bcat .bmeter { height:5px; border-radius:3px; background:var(--sunk); overflow:hidden; position:relative; }
  .bcat .bmeter > span { position:absolute; inset:0 auto 0 0; border-radius:3px; }

  /* live wire */
  .wire { grid-template-columns:1fr 320px; align-items:stretch; }
  @media (max-width:860px){ .wire{ grid-template-columns:1fr; } }
  /* Below this the canvas is ~348x195: 4.5px nodes and 3.8px labels, under a
     legend that renders at full CSS size -- the caption is legible and the
     thing it captions is not. The feed carries the panel instead; it holds
     the same signals, in the same order, with the thesis text readable. */
  @media (max-width:620px){ .wire .stagewrap{ display:none; } }
  .stagewrap { position:relative; overflow:hidden; }
  canvas#wireCanvas { display:block; width:100%; height:460px; }
  .tip { position:fixed; pointer-events:none; background:var(--ink); color:var(--ground); font-size:0.74rem;
         border-radius:8px; padding:0.45rem 0.6rem; opacity:0; transition:opacity 0.1s; max-width:250px; z-index:40;
         box-shadow:0 8px 26px -8px rgba(0,0,0,0.55); line-height:1.4; }
  .tip .h { font-weight:700; margin-bottom:0.1rem; }
  .tip .d { opacity:0.9; }
  .feed { display:flex; flex-direction:column; overflow:hidden; }
  .feed-h { padding:12px 15px 10px; border-bottom:1px solid var(--line-soft); display:flex; align-items:center; gap:8px; }
  .feed-h .t { font-size:12px; font-weight:600; }
  .feed-h .dotlive { width:7px; height:7px; border-radius:50%; background:var(--pos); animation:pulse 2s infinite; }
  .feed-h .c { margin-left:auto; font-size:10.5px; color:var(--faint); }
  /* flex-basis:0 so this contributes NOTHING to the panel's natural height --
     the canvas panel alone sets the row height and the list then fills whatever
     the stretch gives it. A fixed 404px left ~74px of dead space under the last
     row; sizing to content instead just moved that space to the other panel.
     Once the grid has stacked there is no canvas to match, so it sizes to its
     content, capped against the viewport. */
  .feed-list { overflow-y:auto; padding:6px; flex:1 1 0; min-height:0; }
  @media (max-width:860px){ .feed-list{ flex:0 1 auto; max-height:70vh; } }
  .ev { display:block; width:100%; text-align:left; font:inherit; cursor:pointer; border:1px solid transparent;
        background:transparent; color:var(--ink); border-radius:9px; padding:9px 10px; }
  .ev:hover { background:var(--panel-hi); }
  .ev.on { background:var(--panel-hi); border-color:var(--line); }
  .ev-empty { padding:16px 12px; font-size:12px; color:var(--faint); }
  .feed-h .dotlive.idle { background:var(--faint); animation:none; }
  .ev-top { display:flex; align-items:center; gap:7px; margin-bottom:3px; }
  .ev-sym { font-size:12px; font-weight:700; }
  .ev-time { font-size:10px; color:var(--faint); margin-left:auto; }
  /* A real clamp: max-height + overflow:hidden cut the thesis mid-word with no
     ellipsis, so a truncated summary read as a complete sentence that stopped. */
  .ev-body { font-size:11.5px; color:var(--muted); line-height:1.35; overflow:hidden;
             display:-webkit-box; -webkit-line-clamp:2; -webkit-box-orient:vertical; }

  .phead { display:flex; align-items:center; justify-content:space-between; padding:12px 16px; border-bottom:1px solid var(--line-soft); }
  .phead h2 { font-size:13px; margin:0; font-weight:600; letter-spacing:-0.005em; }
  .phead .hint { font-size:11px; color:var(--muted); }

  /* These two were inline styles, which no media query can override -- so the
     page scrolled sideways at phone width (body 572px against a 390px
     viewport) because a two-column grid could not shrink below the funnel's
     min-content. */
  .act { grid-template-columns:1.4fr 1fr; align-items:start; }
  .ghgrid { grid-template-columns:1fr 1fr; align-items:start; }
  @media (max-width:900px){ .act, .ghgrid { grid-template-columns:1fr; } }
  .work { grid-template-columns:1.3fr 1fr; align-items:start; }
  @media (max-width:900px){ .work{ grid-template-columns:1fr; } }

  /* Column widths live here as custom properties because the 0.50 rule below
     is positioned from the same arithmetic -- hard-coding both is how the
     label and the thing it labels drift apart. */
  .ladder { padding:14px 16px 16px;
            --lad-sym:56px; --lad-dir:16px; --lad-gap:6px; --lad-val:42px;
            --lad-l:calc(var(--lad-sym) + var(--lad-dir) + var(--lad-gap)*2);
            --lad-r:calc(var(--lad-val) + var(--lad-gap)); }
  /* The signal bar is a VERTICAL rule through every row at exactly the x the
     bars are measured against. It used to be a horizontal dashed line ACROSS
     THE TOP of the panel with the "0.50" caption floating over its midpoint,
     which read as a heading underline -- and left the one question this panel
     exists to answer ("which of these fire?") with nothing on any row that
     could answer it. Nothing marked 0.50; you could not tell a 0.34 from a
     0.62 by looking. */
  .lad-plot { position:relative; padding-top:15px; }
  .lad-bar { position:absolute; top:15px; bottom:2px; z-index:2; pointer-events:none;
             left:calc(var(--lad-l) + (100% - var(--lad-l) - var(--lad-r))/2);
             border-left:1px dashed var(--warn); }
  .lad-bar b { position:absolute; top:-15px; left:0; transform:translateX(-50%); font-weight:500;
               font-size:9.5px; color:var(--warn); white-space:nowrap; }
  .lad-row { display:grid; width:100%; height:24px; align-items:center; gap:var(--lad-gap);
             grid-template-columns:var(--lad-sym) var(--lad-dir) 1fr var(--lad-val);
             font:inherit; color:var(--ink); background:transparent; border:0; padding:0;
             border-radius:5px; cursor:pointer; text-align:left; }
  .lad-row:hover { background:var(--panel-hi); }
  .lad-row:focus-visible { outline:2px solid var(--accent); outline-offset:1px; }
  .lad-sym { font-size:12px; text-align:right; }
  /* Direction was encoded in the bar colour and nowhere else, which is no
     encoding at all for the ~8% of men who cannot separate this red from this
     green -- and the legend's own two dots rendered grey, because bare bullets
     in a .note inherit .note's colour. */
  .lad-dir { font-size:9px; font-weight:700; text-align:center; border-radius:3px; line-height:14px; }
  .lad-dir.L { color:var(--pos); background:var(--pos-soft); }
  .lad-dir.S { color:var(--neg); background:var(--neg-soft); }
  .lad-track { position:relative; height:15px; }
  .lad-fill { position:absolute; top:3px; left:0; height:9px; border-radius:5px; }
  .lad-dot { position:absolute; top:1px; width:13px; height:13px; border-radius:50%; transform:translateX(-50%); border:2px solid var(--panel); }
  .lad-con { position:absolute; top:-2px; width:19px; height:19px; border-radius:50%; transform:translateX(-50%); border:1.5px dashed var(--warn); }
  /* The score in figures. Fourteen bars carried no number anywhere, and the
     one table that does is folded away behind a <details>. */
  .lad-val { font-size:11px; text-align:right; color:var(--muted); }
  .lad-row.fires .lad-val { color:var(--ink); font-weight:600; }
  .lad-empty { font-size:12px; color:var(--faint); padding:10px 0 2px; }
  .lad-key { display:flex; flex-wrap:wrap; align-items:center; gap:4px 12px; margin-top:10px; font-size:11px; color:var(--faint); }
  .lad-key i { display:inline-block; width:9px; height:9px; border-radius:50%; vertical-align:-1px; margin-right:5px; }
  .lad-key i.con { width:11px; height:11px; background:transparent; border:1.5px dashed var(--warn); }

  /* ===== Dossier sheet =====
     What a ladder row (or an all-dossiers row) is a summary OF. Fixed and
     outside every panel on purpose: the page re-renders #ladder from a fresh
     payload every 10 seconds, and a detail view nested inside it would be
     wiped mid-read. */
  .dsh { position:fixed; inset:0; z-index:60; display:flex; align-items:flex-start; justify-content:center; padding:5vh 16px; }
  .dsh[hidden] { display:none; }
  .dsh-back { position:absolute; inset:0; background:rgba(4,8,13,0.55); }
  .dsh-card { position:relative; display:flex; flex-direction:column; width:min(760px,100%); max-height:90vh;
              background:var(--panel); border:1px solid var(--line); border-radius:14px; box-shadow:var(--shadow); overflow:hidden; }
  .dsh-head { display:flex; align-items:center; gap:9px; padding:13px 16px; border-bottom:1px solid var(--line-soft); }
  .dsh-head h3 { margin:0; font-size:15px; font-weight:650; letter-spacing:-0.01em; }
  .dsh-x { margin-left:auto; width:26px; height:26px; flex:none; font-size:13px; line-height:1; cursor:pointer;
           background:transparent; border:1px solid var(--line); color:var(--muted); border-radius:7px; }
  .dsh-x:hover { background:var(--panel-hi); color:var(--ink); }
  .dsh-body { overflow-y:auto; padding:14px 16px 18px; }
  .dsh-score { display:flex; align-items:baseline; flex-wrap:wrap; gap:5px 10px; }
  .dsh-score .n { font-size:30px; font-weight:650; letter-spacing:-0.02em; line-height:1; }
  .dsh-score .c { font-size:11.5px; color:var(--muted); }
  .dsh-grid { display:grid; grid-template-columns:repeat(auto-fit,minmax(104px,1fr)); gap:8px; margin-top:13px; }
  .dsh-s { background:var(--sunk); border-radius:8px; padding:7px 9px; }
  .dsh-s .l { font-size:9.5px; text-transform:uppercase; letter-spacing:0.06em; color:var(--faint); }
  .dsh-s .v { font-size:14px; font-weight:600; margin-top:1px; }
  .dsh-h { font-size:11px; text-transform:uppercase; letter-spacing:0.06em; color:var(--muted); margin:17px 0 7px; }
  .dsh-p { font-size:12.5px; color:var(--ink-2); line-height:1.5; }
  .dsh-ev { border:1px solid var(--line-soft); border-radius:9px; padding:9px 11px; margin-bottom:7px; }
  .dsh-ev-h { font-size:12.5px; font-weight:600; line-height:1.35; }
  .dsh-ev-h a { color:inherit; }
  .dsh-ev-m { display:flex; flex-wrap:wrap; gap:2px 9px; margin-top:5px; font-size:10.5px; color:var(--faint); }
  .dsh-ev-n { margin-top:6px; font-size:11.5px; color:var(--muted); line-height:1.4; }
  .dsh-tags { display:flex; flex-wrap:wrap; align-items:center; gap:5px; margin-bottom:4px; }
  .dsh-via { font-size:10px; color:var(--accent); background:var(--accent-soft); border-radius:5px; padding:1px 6px; }
  .dsh-skep { border-left:2px solid var(--warn); padding-left:8px; }
  #dossTable tbody tr[data-sym] { cursor:pointer; }

  table { border-collapse:collapse; width:100%; font-size:12.5px; }
  .scroll { overflow-x:auto; }
  th { text-align:left; font-weight:500; font-size:10.5px; text-transform:uppercase; letter-spacing:0.05em;
       color:var(--muted); padding:9px 14px 7px; border-bottom:1px solid var(--line-soft); white-space:nowrap; }
  td { padding:8px 14px; border-bottom:1px solid var(--line-soft); white-space:nowrap; }
  tr:last-child td { border-bottom:none; }
  tbody tr:hover { background:var(--panel-hi); }
  .num { text-align:right; }
  .sym { font-weight:600; }
  .dir { font-size:10px; font-weight:700; letter-spacing:0.03em; padding:1px 6px; border-radius:5px; }
  .dir.L { color:var(--pos); background:var(--pos-soft); }
  .dir.S { color:var(--neg); background:var(--neg-soft); }
  .pill { font-size:10px; padding:2px 7px; border-radius:999px; border:1px solid var(--line); color:var(--muted); }
  .pill.sig { color:var(--accent); border-color:var(--accent); background:var(--accent-soft); }
  .pill.win { color:var(--pos); border-color:var(--pos); background:var(--pos-soft); }
  .pill.loss { color:var(--neg); border-color:var(--neg); background:var(--neg-soft); }
  .rcell { font-weight:600; }
  .thesis { color:var(--muted); font-size:11.5px; max-width:340px; white-space:normal; }
  .stale { color:var(--warn); }
  .fresh { color:var(--faint); }

  /* Emitted by renderGraphHealth (edge-type counts, audit kinds). Undefined
     until now, so the swatches had no size and the rows did not lay out. */
  .catleg { display:grid; gap:6px 12px; margin-top:9px; font-size:11.5px; color:var(--muted); }
  .lc { display:inline-flex; align-items:center; gap:6px; }
  .lc-dot { width:8px; height:8px; border-radius:50%; flex:none; }
  .gviz-leg { display:flex; flex-wrap:wrap; gap:8px 14px; padding:10px 15px; border-top:1px solid var(--line-soft); font-size:11px; color:var(--muted); }
  .gl { display:inline-flex; align-items:center; gap:5px; }
  .gl-l { width:15px; height:3px; border-radius:2px; }
  .gl-d { width:9px; height:9px; border-radius:50%; }
  .gl-d.none { width:11px; height:11px; background:transparent; border:1.5px dashed var(--warn); }
  .gl-d.small { width:7px; height:7px; }
  .gl-d.hollow { background:transparent; border:1.5px solid var(--faint); }

  details.more { margin-top:14px; }
  details.more > summary { cursor:pointer; list-style:none; font-size:12px; color:var(--accent);
     padding:9px 14px; background:var(--panel); border:1px solid var(--line); border-radius:9px;
     display:inline-flex; align-items:center; gap:7px; box-shadow:var(--shadow); }
  details.more > summary::-webkit-details-marker { display:none; }
  details.more[open] > summary { margin-bottom:12px; }
  .split { display:grid; grid-template-columns:1fr 1fr; gap:14px; }
  @media (max-width:760px){ .split{ grid-template-columns:1fr; } }

  /* admin & tools */
  .toolbar { display:flex; flex-wrap:wrap; gap:8px; align-items:center; }
  .tin { font:inherit; font-size:12.5px; padding:7px 10px; border-radius:8px; border:1px solid var(--line); background:var(--panel); color:var(--ink); }
  .tin:focus { outline:none; border-color:var(--accent); }
  .tlabel { font-size:11.5px; color:var(--muted); display:inline-flex; align-items:center; gap:5px; }
  .tbtn { font:inherit; font-size:12.5px; color:var(--ink-2); background:var(--panel); border:1px solid var(--line);
          border-radius:8px; padding:7px 12px; cursor:pointer; }
  .tbtn:hover { background:var(--panel-hi); border-color:var(--faint); }
  .tbtn.danger { color:var(--neg); border-color:color-mix(in srgb,var(--neg) 42%,transparent); }
  .tbtn.danger:hover { background:var(--neg-soft); }
  .thint { font-size:11.5px; color:var(--muted); line-height:1.55; margin-top:11px; }
  .thint b { color:var(--ink-2); }
  .tout-actions { display:flex; justify-content:flex-end; margin-top:12px; }
  .tout { background:var(--sunk); border:1px solid var(--line-soft); border-radius:10px; padding:12px 14px; margin-top:11px;
          font-family:ui-monospace,"SF Mono",Menlo,Consolas,monospace; font-size:11.5px; line-height:1.5; overflow-x:auto; white-space:pre; color:var(--ink-2); }
</style>
</head>
<body>

<div class="rail">
  <div class="rail-in">
    <div class="brand"><b>SmartBoi</b><span class="v mono" id="ver">dev</span><span class="paper">paper-only</span></div>
    <div class="caps" id="caps"></div>
    <div class="rail-right">
      <span class="updated"><span class="live"></span>updated <span id="upd" class="mono">just now</span></span>
      <button class="toggle" id="tgl">◐ Theme</button>
    </div>
  </div>
</div>

<div class="wrap">
  <h1>SmartBoi status</h1>
  <div id="err" style="display:none; margin:14px 0 0; padding:10px 14px; border-radius:10px; background:var(--neg-soft); border:1px solid var(--neg); color:var(--neg); font-size:12.5px;"></div>

  <div class="eyebrow">Bottom line &mdash; current strategy</div>
  <div class="grid hero">
    <div class="panel pad" id="pnl"></div>
    <div class="panel pad" id="winrate"></div>
    <div class="panel pad" id="exposure"></div>
    <div class="panel pad" id="expectancy"></div>
  </div>

  <div class="eyebrow">Strategy record &mdash; win&ndash;loss by generation</div>
  <div class="grid"><div class="panel pad" id="genrec"></div></div>

  <div class="eyebrow">Universe activation &amp; budget</div>
  <div class="grid act" id="act">
    <div class="panel pad"><div class="funnel" id="funnel"></div></div>
    <div class="panel pad" id="budget"></div>
  </div>

  <div class="eyebrow">News &rarr; supply chain &mdash; the live wire</div>
  <div class="grid wire">
    <div class="panel stagewrap"><canvas id="wireCanvas"></canvas><div class="gviz-leg" id="wireLeg"></div></div>
    <div class="panel feed">
      <div class="feed-h"><span class="dotlive" id="feedDot"></span><span class="t">Live wire</span><span class="c" id="feedCount"></span></div>
      <div class="feed-list" id="feed"></div>
    </div>
  </div>

  <div class="eyebrow">Graph health &amp; maintenance</div>
  <div class="grid ghgrid" id="ghGrid">
    <div class="panel pad" id="ghStats"></div>
    <div class="panel pad" id="ghMaint"></div>
  </div>

  <div class="eyebrow">Live now</div>
  <div class="grid work">
    <div class="panel">
      <div class="phead"><h2>Dossiers by conviction</h2><span class="hint">score = conf &times; mag &middot; click a row for its dossier</span></div>
      <div class="ladder" id="ladder"></div>
    </div>
    <div class="panel">
      <div class="phead"><h2>Open paper trades</h2><span class="hint" id="openCount"></span></div>
      <div class="scroll"><table id="openTable"></table></div>
    </div>
  </div>

  <div class="grid" style="margin-top:14px;">
    <div class="panel">
      <div class="phead"><h2>Recent signals</h2><span class="hint">crossings of the conviction bar, newest first</span></div>
      <div class="scroll"><table id="sigTable"></table></div>
    </div>
  </div>

  <details class="more">
    <summary>▸ Full detail &mdash; closed trades, all dossiers, universe candidates</summary>
    <div class="split">
      <div class="panel">
        <div class="phead"><h2>Closed trades</h2><span class="hint">most recent</span></div>
        <div class="scroll"><table id="closedTable"></table></div>
      </div>
      <div class="panel">
        <div class="phead"><h2>All dossiers</h2><span class="hint" id="dossCount"></span></div>
        <div class="scroll" style="max-height:380px;"><table id="dossTable"></table></div>
      </div>
    </div>
  </details>

  <div class="eyebrow">Admin &amp; tools</div>
  <div class="grid"><div class="panel pad">
    <div class="toolbar">
      <input class="tin" id="screen-tickers" placeholder="INTT ASYS SIF  (blank = screen discovered candidates)" style="min-width:21rem">
      <span class="tlabel">cap $M <input class="tin" id="screen-min-cap" value="75" style="width:4.5rem"></span>
      <span class="tlabel">to <input class="tin" id="screen-max-cap" value="3000" style="width:5.5rem"></span>
      <span class="tlabel">max analysts <input class="tin" id="screen-max-analysts" value="10" style="width:3.5rem"></span>
      <button class="tbtn" id="btn-screen">Screen candidates</button>
      <button class="tbtn" id="btn-analyze">Forward-return report</button>
      <button class="tbtn" id="btn-event-study">Signal event study</button>
      <button class="tbtn" id="btn-exit-analysis">Exit analysis</button>
      <button class="tbtn" id="btn-graph-maint">Graph maintenance (dry run)</button>
      <button class="tbtn danger" id="btn-graph-maint-apply">Apply graph maintenance</button>
      <button class="tbtn" id="btn-research">Research anchor suppliers (web)</button>
      <button class="tbtn" id="btn-edgar-search">Search EDGAR for anchor suppliers</button>
      <button class="tbtn" id="btn-diagnostics">Diagnostics bundle</button>
      <button class="tbtn" id="btn-full-diagnostics">Download FULL diagnostics (.zip)</button>
      <button class="tbtn" id="btn-rebuild-graph">Rebuild relationship graph</button>
      <button class="tbtn" id="btn-reconcile-preview">Anchor reconcile (dry run)</button>
      <button class="tbtn danger" id="btn-reconcile-apply">Apply anchor reconcile</button>
      <button class="tbtn danger" id="btn-reset">Reset added symbols</button>
      <button class="tbtn danger" id="btn-reset-runtime">Reset signals &amp; trades</button>
    </div>
    <div class="thint">The first five are read-only &mdash; screening does market-cap/analyst lookups; the others read
      already-persisted state (forward returns, the signal event study, the exit analysis of the closed ledger).
      <b>Research anchor suppliers</b> runs web searches to find small-cap counterparties of your anchors and writes
      universe candidates only. The diagnostics bundle is safe to paste &mdash; credentials and personal data are omitted
      and log lines scrubbed. <b>Download FULL diagnostics</b> is that same bundle plus the actual runtime files
      &mdash; logs including rotations, every capture log, the graph, the dossiers and the state files &mdash; as one
      zip, scrubbed the same way and containing no configuration file. Use it when a summary is not enough to see
      what is wrong. <b>Anchor reconcile</b> grows the universe with candidates that land connected to a
      tradeable and prunes runtime-accepted anchors that reach none; the dry run previews, <b>Apply</b> asks first and
      never removes curated seed anchors. <b>Reset added symbols</b> removes every runtime-added symbol and archives the
      orphaned dossiers; it asks first.</div>
    <div class="tout-actions" id="tool-output-actions" style="display:none"><button class="tbtn" id="btn-copy-output">Copy report</button></div>
    <pre class="tout" id="tool-output" style="display:none"></pre>
  </div></div>
</div>
<div class="tip" id="tip"></div>
<div class="dsh" id="dossierSheet" hidden>
  <div class="dsh-back" id="dossierBack"></div>
  <div class="dsh-card" role="dialog" aria-modal="true" aria-labelledby="dossierTitle">
    <div class="dsh-head">
      <h3 class="mono" id="dossierTitle"></h3>
      <span class="hint" id="dossierSub"></span>
      <button class="dsh-x" id="dossierClose" aria-label="Close dossier">&#10005;</button>
    </div>
    <div class="dsh-body" id="dossierBody"></div>
  </div>
</div>

<script>
var data = {};

// Build the /api/status URL from location.pathname (HA Ingress can serve this
// page at a per-install subpath) -- no regex escape needed.
var _p = location.pathname; if (_p.charAt(_p.length - 1) !== "/") _p += "/";
var API_BASE = _p + "api/";
var API_STATUS_URL = API_BASE + "status";
var REFRESH_TIMEOUT_MS = 12000;
var TOOL_TIMEOUT_MS = 300000;
// The custom header the server's CSRF guard requires (see _CSRF_HEADER). GET sends nothing.
var POST_HEADERS = { "Content-Type": "application/json", "X-SmartBoi-Request": "1" };

function showErr(msg){ var e = el("err"); if (e){ e.textContent = msg; e.style.display = "block"; } }
function hideErr(){ var e = el("err"); if (e){ e.style.display = "none"; } }
function showToolOutput(){ el("tool-output").style.display = "block"; el("tool-output-actions").style.display = "flex"; }

function copyToolOutput(btn){
  var text = el("tool-output").textContent || ""; if (!text) return;
  function flash(m){ btn.textContent = m; setTimeout(function(){ btn.textContent = "Copy report"; }, 1500); }
  if (navigator.clipboard && navigator.clipboard.writeText){
    navigator.clipboard.writeText(text).then(function(){ flash("Copied!"); }, function(){ fallbackCopyToolOutput(text, flash); });
  } else { fallbackCopyToolOutput(text, flash); }
}
function fallbackCopyToolOutput(text, flash){
  var ta = document.createElement("textarea"); ta.value = text; ta.style.position = "fixed"; ta.style.top = "-1000px";
  document.body.appendChild(ta); ta.focus(); ta.select();
  var ok = false; try { ok = document.execCommand("copy"); } catch (e) { ok = false; }
  document.body.removeChild(ta);
  if (ok){ flash("Copied!"); return; }
  var range = document.createRange(); range.selectNodeContents(el("tool-output"));
  var sel = window.getSelection(); sel.removeAllRanges(); sel.addRange(range); flash("Press Ctrl-C");
}

function runTool(path, body, button){
  var out = el("tool-output");
  var buttons = [el("btn-screen"), el("btn-analyze"), el("btn-event-study"), el("btn-exit-analysis"), el("btn-diagnostics"), el("btn-research")];
  buttons.forEach(function(b){ b.disabled = true; });
  showToolOutput(); out.textContent = "Running... (this can take a few minutes for a long ticker list)";
  var controller = new AbortController(), timedOut = false;
  var timer = setTimeout(function(){ timedOut = true; controller.abort(); }, TOOL_TIMEOUT_MS);
  fetch(API_BASE + path, { method:"POST", headers:POST_HEADERS, body:JSON.stringify(body || {}), signal:controller.signal })
    .then(function(r){ clearTimeout(timer); return r.json(); })
    .then(function(res){ out.textContent = res.error ? ("Error: " + res.error) : res.report; })
    .catch(function(err){ clearTimeout(timer); out.textContent = timedOut ? ("No response after " + (TOOL_TIMEOUT_MS/1000) + "s -- the run may still be going; try again shortly.") : ("Failed: " + err); })
    .finally(function(){ buttons.forEach(function(b){ b.disabled = false; }); });
}

el("btn-screen").addEventListener("click", function(){
  runTool("tools/screen", { tickers: el("screen-tickers").value, min_cap: parseFloat(el("screen-min-cap").value),
    max_cap: parseFloat(el("screen-max-cap").value), max_analysts: parseInt(el("screen-max-analysts").value, 10) }, this);
});
el("btn-analyze").addEventListener("click", function(){ runTool("tools/forward-returns", {}, this); });
el("btn-event-study").addEventListener("click", function(){ runTool("tools/event-study", {}, this); });
el("btn-exit-analysis").addEventListener("click", function(){ runTool("tools/exit-analysis", {}, this); });
el("btn-diagnostics").addEventListener("click", function(){ runTool("tools/diagnostics", {}, this); });
// Not runTool (the response is a zip, not text for the output pane) and not a
// plain navigation either: this is a POST so it carries the CSRF header, which
// a navigation cannot. Fetch it, then hand the blob to a synthetic link.
el("btn-full-diagnostics").addEventListener("click", function(){
  var btn = this, label = btn.textContent;
  btn.disabled = true; btn.textContent = "Collecting...";
  function done(msg){ btn.disabled = false; btn.textContent = msg || label; }
  fetch(API_BASE + "tools/full-diagnostics", { method:"POST", headers:POST_HEADERS, body:"{}" })
    .then(function(r){
      if (!r.ok) return r.json().then(function(j){ throw new Error(j.error || r.status); });
      // Plain string scanning, not a regex: a /filename="..."/ literal trips
      // the unterminated-string guard in test_webapp_html, which cannot parse
      // regex literals and should not be loosened to try.
      var name = "smartboi-diagnostics.zip";
      var cd = r.headers.get("Content-Disposition") || "";
      var at = cd.indexOf("filename=");
      if (at >= 0) {
        var rest = cd.slice(at + 9);
        if (rest.charAt(0) === '"') { rest = rest.slice(1, rest.indexOf('"', 1)); }
        if (rest) name = rest;
      }
      return r.blob().then(function(b){ return { blob:b, name:name }; });
    })
    .then(function(res){
      var url = URL.createObjectURL(res.blob);
      var a = document.createElement("a");
      a.href = url; a.download = res.name;
      document.body.appendChild(a); a.click(); document.body.removeChild(a);
      // Revoked on a delay: Safari cancels an in-flight download if the
      // object URL is released the instant after click().
      setTimeout(function(){ URL.revokeObjectURL(url); }, 30000);
      done();
    })
    .catch(function(e){ showErr("Full diagnostics failed: " + e.message); done(); });
});
el("btn-graph-maint").addEventListener("click", function(){
  runTool("tools/graph-maintenance", {apply:false}, this);
});
el("btn-graph-maint-apply").addEventListener("click", function(){
  if (!confirm("Apply graph maintenance? This QUARANTINES symbols the audit found structurally unfit to hold (delisted, not common equity, misresolved, or a financing relationship) and applies the anchor connectivity reconcile. Nothing is deleted -- quarantined symbols keep their row and their reason in data/quarantined_symbols.json and can be restored by hand. A symbol with an OPEN paper trade is never touched. Run the dry run first to see exactly what it would do.")) return;
  runTool("tools/graph-maintenance", {apply:true}, this);
});
el("btn-research").addEventListener("click", function(){
  if (!confirm("Research the 10 most inert anchors for small-cap suppliers? This spends LLM budget (web search plus one call per anchor) and can take a few minutes. It adds universe CANDIDATES for your review; it never adds a symbol or a relationship edge.")) return;
  runTool("tools/supplier-research", {}, this);
});
el("btn-edgar-search").addEventListener("click", function(){
  if (!confirm("Ask EDGAR full-text search which OTHER filers disclose revenue concentration on the 5 most inert anchors? No LLM spend -- SEC requests only -- but it fetches a handful of filings and can take a couple of minutes. It adds universe CANDIDATES for your review; it never adds a symbol or a relationship edge.")) return;
  runTool("tools/edgar-search", {}, this);
});
el("btn-rebuild-graph").addEventListener("click", function(){
  if (!confirm("Re-extract relationships from every tradeable's latest 10-K? Additive only -- edges are deduped, so this can never remove an edge or touch a dossier, trade or log. Costs about one LLM call per tradeable symbol. Runs in the background over the next few polling ticks.")) return;
  var out = el("tool-output"); showToolOutput(); out.textContent = "Queueing...";
  fetch(API_BASE + "universe/rebuild-graph", { method:"POST", headers:POST_HEADERS, body:"{}" })
    .then(function(r){ return r.json(); }).then(function(res){
      out.textContent = res.error ? ("Error: " + res.error) : ("Queued " + res.queued + " symbol(s) for relationship re-extraction (graph currently has " + res.edges_before + " edges). New edges appear over the next few ticks as each 10-K is re-read.");
      refresh();
    }).catch(function(err){ out.textContent = "Failed: " + err; });
});
el("btn-reset").addEventListener("click", function(){
  if (!confirm("Remove every symbol added at runtime and return to the curated universe? Dossiers for the removed symbols are ARCHIVED, not deleted. Candidates, trades and captured logs are untouched.")) return;
  var out = el("tool-output"); showToolOutput(); out.textContent = "Resetting...";
  fetch(API_BASE + "universe/reset-accepted", { method:"POST", headers:POST_HEADERS, body:"{}" })
    .then(function(r){ return r.json(); }).then(function(res){
      out.textContent = res.error ? ("Error: " + res.error) : ("Removed " + res.removed.length + " added symbol(s): " + (res.removed.join(", ") || "none") + ". Universe is now " + res.universe_size + " symbols.");
      refresh();
    }).catch(function(err){ out.textContent = "Failed: " + err; });
});
el("btn-reset-runtime").addEventListener("click", function(){
  if (!confirm("Start a clean measurement window? This ARCHIVES every open paper trade (they never reached an outcome) and resets every dossier's signal to ACTIVE so signals re-fire under the current scoring rules. Accumulated evidence, the graph, the universe and the captured forward logs are KEPT (old rows stay segregated by scoring version).")) return;
  var out = el("tool-output"); showToolOutput(); out.textContent = "Resetting runtime state...";
  fetch(API_BASE + "runtime/reset", { method:"POST", headers:POST_HEADERS, body:"{}" })
    .then(function(r){ return r.json(); }).then(function(res){
      out.textContent = res.error ? ("Error: " + res.error) : ("Archived " + res.archived_open_trades.length + " open trade(s): " + (res.archived_open_trades.join(", ") || "none") + ". Reset " + res.dossiers_reset + " dossier(s) to ACTIVE.");
      refresh();
    }).catch(function(err){ out.textContent = "Failed: " + err; });
});
function runReconcile(apply){
  var out = el("tool-output"); showToolOutput();
  out.textContent = apply ? "Applying anchor reconcile..." : "Computing anchor reconcile (dry run)...";
  fetch(API_BASE + "universe/reconcile-connectivity", { method:"POST", headers:POST_HEADERS, body: JSON.stringify({apply:apply}) })
    .then(function(r){ return r.json(); }).then(function(res){
      if (res.error){ out.textContent = "Error: " + res.error; return; }
      var addList = apply ? res.added : res.would_add.map(function(a){ return a.symbol; });
      var prList  = apply ? res.pruned : res.would_prune;
      var lines = [(apply ? "APPLIED" : "DRY RUN") + " -- universe now " + res.universe_size + " symbols.", ""];
      lines.push((apply ? "Added " : "Would add ") + addList.length + " connected anchor(s):");
      res.would_add.forEach(function(a){ lines.push("  + " + a.symbol + " [" + a.ecosystem + "]  <- " + a.links.join(", ") + "  (" + a.name + ")"); });
      lines.push("");
      lines.push((apply ? "Pruned " : "Would prune ") + prList.length + " inert accepted anchor(s): " + (prList.join(", ") || "none"));
      if (res.add_skipped && res.add_skipped.length){
        lines.push("", "Skipped " + res.add_skipped.length + " connected-but-unfit candidate(s):");
        res.add_skipped.forEach(function(s){ lines.push("  ~ " + s.symbol + ": " + s.reason); });
      }
      lines.push("", res.inert_seed_anchors.length + " curated DEFAULT_UNIVERSE anchor(s) inert (kept -- edit universe.py to drop): " + (res.inert_seed_anchors.join(", ") || "none"));
      out.textContent = lines.join(String.fromCharCode(10));
      refresh();
    }).catch(function(err){ out.textContent = "Failed: " + err; });
}
el("btn-reconcile-preview").addEventListener("click", function(){ runReconcile(false); });
el("btn-reconcile-apply").addEventListener("click", function(){
  if (!confirm("Apply the anchor connectivity reconcile? This ACCEPTS connected candidates as anchors (writing their disclosed tradeable edges into the graph) and REMOVES runtime-accepted anchors that reach no tradeable. Curated DEFAULT_UNIVERSE anchors are never removed. Run the dry run first to preview.")) return;
  runReconcile(true);
});
document.getElementById("btn-copy-output").addEventListener("click", function(){ copyToolOutput(this); });

function refresh(){
  var controller = new AbortController(), timedOut = false;
  var timer = setTimeout(function(){ timedOut = true; controller.abort(); }, REFRESH_TIMEOUT_MS);
  fetch(API_STATUS_URL, { signal:controller.signal }).then(function(r){ clearTimeout(timer); return r.json(); })
    .then(function(payload){
      if (payload.error){ showErr(payload.error); return; }
      data = payload;
      try { renderAll(data); hideErr(); } catch (e){ showErr("Render error: " + e); }
    }).catch(function(err){ clearTimeout(timer); showErr(timedOut ? ("No response after " + (REFRESH_TIMEOUT_MS/1000) + "s.") : ("Failed to load status: " + err)); });
}

// ---------- helpers ----------
function fx(n,d){ d=d===undefined?2:d; return (n===null||n===undefined)?"–":(+n).toFixed(d); }
function sgn(n,d){ return (n>=0?"+":"")+fx(n,d); }
function cls(n){ return n>0?"pos":(n<0?"neg":""); }
function esc(s){ return (s===null||s===undefined)?"":String(s).replace(/[&<>]/g,function(c){return {"&":"&amp;","<":"&lt;",">":"&gt;"}[c];}); }
function el(id){ return document.getElementById(id); }
function timeAgo(iso){ if(!iso) return {t:"never",stale:true}; var ms=Date.now()-Date.parse(iso); if(isNaN(ms)) return {t:"",stale:false};
  var m=Math.floor(ms/60000); return {t:m<1?"just now":(m<60?m+"m":Math.floor(m/60)+"h "+(m%60)+"m"),stale:m>=90}; }

var CO = { LONG:"L", SHORT:"S" };
var DESC = { META:"Social platforms; big AI data-center buildout", GOOGL:"Search, cloud & AI", NEE:"Largest US utility; renewables & grid",
  CAT:"Heavy machinery & backup gensets", ETN:"Electrical power-management gear", AMAT:"World's #1 chip-fab equipment", LRCX:"Chip etch & deposition tools",
  LMT:"Defense prime; F-35 & missiles", NOC:"Defense prime; B-21 & space", RTX:"Aerospace & defense; missiles", GM:"Automaker", F:"Automaker",
  GD:"Defense; subs & Gulfstream", BA:"Commercial & defense aircraft", XOM:"Integrated oil & gas major",
  ESOA:"Utility pipeline & electrical contractor", MTRX:"Energy/industrial infrastructure", LMB:"Building HVAC; data-center fit-out",
  BWEN:"Wind towers & heavy steel fab", DCO:"Aero/defense structures & electronics", VVX:"Defense logistics & mission support",
  RDW:"Space-infrastructure components", SIF:"Aerospace forgings & repair", UCTT:"Subsystems for chip-fab tools", ICHR:"Fluid-delivery subsystems",
  STRT:"Vehicle locks/keys/access", THRM:"Automotive thermal comfort", PUMP:"Oilfield frac services", OIS:"Oilfield equipment & services" };

// ---------- capabilities rail ----------
function renderCaps(d){
  var names={edgar:"EDGAR",news:"News",anthropic:"Dossier engine",ib:"IB price feed"};
  el("caps").innerHTML = Object.keys(names).map(function(k){
    var on=d.capabilities[k]; return '<span class="cap '+(on?"on":"off")+'"><span class="dot"></span>'+names[k]+"</span>";
  }).join("");
  if(d.version) el("ver").textContent = "v"+d.version;
  el("upd").textContent = d.updated_at || new Date().toLocaleTimeString();
}

// ---------- hero: current-strategy generation drives win-rate & expectancy ----------
function currentGen(d){ var g=(d.strategy_generations||[]).filter(function(x){return x.is_current;}); return g.length?g[0]:null; }

function renderPnl(d){
  var ps=d.paper_stats, cur=ps.currency||"", closed=(d.closed_paper_trades||[]).slice().reverse();
  var cum=[0], s=0; closed.forEach(function(t){ s+=(t.r_multiple||0); cum.push(s); });
  var W=300,H=74,pad=6, hi=Math.max.apply(null,cum.concat([0])), lo=Math.min.apply(null,cum.concat([0])), rng=(hi-lo)||1;
  function X(i){return pad+i*(W-2*pad)/(Math.max(1,cum.length-1));} function Y(v){return pad+(hi-v)*(H-2*pad)/rng;}
  var zY=Y(0), line=cum.map(function(v,i){return (i?"L":"M")+X(i).toFixed(1)+" "+Y(v).toFixed(1);}).join(" ");
  var last=cum[cum.length-1], col=last>=0?"var(--pos)":"var(--neg)", soft=last>=0?"var(--pos-soft)":"var(--neg-soft)";
  var area=line+" L"+X(cum.length-1).toFixed(1)+" "+zY.toFixed(1)+" L"+X(0).toFixed(1)+" "+zY.toFixed(1)+" Z";
  var openUR=0; (d.open_paper_trades||[]).forEach(function(t){ if(t.unrealized_r!=null) openUR+=t.unrealized_r; });
  var eqCls=cls(ps.realized_pnl||0);
  var svg='<svg class="spark" viewBox="0 0 '+W+" "+H+'" preserveAspectRatio="none" aria-hidden="true">'+
    '<path d="'+area+'" fill="'+soft+'"/>'+
    '<line x1="0" y1="'+zY.toFixed(1)+'" x2="'+W+'" y2="'+zY.toFixed(1)+'" stroke="var(--line)" stroke-dasharray="3 3"/>'+
    '<path d="'+line+'" fill="none" stroke="'+col+'" stroke-width="2" stroke-linejoin="round" vector-effect="non-scaling-stroke"/>'+
    '<circle cx="'+X(cum.length-1).toFixed(1)+'" cy="'+Y(last).toFixed(1)+'" r="3.4" fill="'+col+'"/></svg>';
  el("pnl").innerHTML =
    '<div class="pnl-head"><div><div class="k">Account &middot; '+cur+" "+fx(ps.initial_capital,0)+' start</div>'+
      '<div class="big '+eqCls+'">'+cur+" "+fx(ps.equity,0)+'</div></div>'+
      '<div style="text-align:right"><div class="k">Realized</div><div class="mono '+eqCls+'" style="font-size:15px">'+sgn(ps.realized_pnl,0)+'</div></div></div>'+
    ((ps.peak_concurrent>ps.max_concurrent_positions && ps.max_concurrent_positions>0)
      ? '<div class="k" style="color:var(--neg)">levered: peak '+ps.peak_concurrent+' positions open vs '+ps.max_concurrent_positions+' slots - equity is a return on more than the stated capital</div>'
      : '')+
    svg+
    '<div class="foot"><span>cum <b class="mono '+cls(last)+'">'+sgn(last)+"R</b></span>"+
      '<span><b class="mono pos">'+ps.wins+"</b>W / <b class=\\"mono neg\\">"+ps.losses+"</b>L / "+ps.timeouts+"T</span>"+
      '<span>open <b class="mono '+cls(openUR)+'">'+sgn(openUR)+'R</b></span></div>';
}

function renderWinRate(d){
  var cg=currentGen(d), ps=d.paper_stats, name=cg?cg.label:"";
  var dirSplit=(ps.closed_long||ps.closed_short)?(" &middot; long "+Math.round(ps.win_rate_long*100)+"% / short "+Math.round(ps.win_rate_short*100)+"%"):"";
  var allTime='<div class="sub">all-time '+ps.wins+"W&ndash;"+ps.losses+"L net of cost"+(ps.closed?" &middot; "+Math.round(ps.win_rate*100)+"%"+dirSplit:"")+"</div>";
  if(!cg||!cg.closed){
    el("winrate").innerHTML='<div class="k">Win rate &middot; current</div><div class="big">–</div>'+
      '<div class="sub">no closed trades yet'+(name?' under <b>'+esc(name)+"</b>":"")+"</div>"+allTime; return;
  }
  var lo=cg.win_rate_ci_low*100, hi=cg.win_rate_ci_high*100, pt=cg.win_rate*100;
  el("winrate").innerHTML='<div class="k">Win rate &middot; '+esc(name)+'</div>'+
    '<div class="big">'+Math.round(pt)+'<span class="u">%</span></div>'+
    '<div class="sub">95% CI '+Math.round(lo)+"&ndash;"+Math.round(hi)+"% &middot; n="+cg.closed+"</div>"+
    '<div class="ci-track"><div class="ci-axis"></div><div class="ci-band" style="left:'+lo+"%;width:"+(hi-lo)+'%"></div>'+
      '<div class="ci-point" style="left:'+pt+'%"></div></div><div class="ci-labels"><span>0%</span><span>100%</span></div>'+allTime;
}

function renderExposure(d){
  var open=d.open_paper_trades||[], ur=0; open.forEach(function(t){ if(t.unrealized_r!=null) ur+=t.unrealized_r; });
  el("exposure").innerHTML='<div class="k">Open exposure</div><div class="big">'+open.length+'<span class="u"> pos</span></div>'+
    '<div class="sub">across the tradeable book</div>'+
    '<div class="foot" style="margin-top:14px"><span>unrealized <b class="mono '+cls(ur)+'">'+sgn(ur)+'R</b></span></div>';
}

function renderExpectancy(d){
  var cg=currentGen(d), exp=(cg&&cg.closed)?cg:null;
  el("expectancy").innerHTML='<div class="k">Expectancy / trade &middot; current</div>'+
    '<div class="big '+(exp?cls(exp.avg_r):"")+'">'+(exp?sgn(exp.avg_r):"–")+(exp?'<span class="u">R</span>':"")+"</div>"+
    '<div class="sub mono">'+(exp?"gross "+sgn(exp.avg_r_gross)+"R &middot; cost "+fx(exp.avg_r_gross-exp.avg_r)+"R":"awaiting first close")+"</div>"+
    '<div class="note" style="margin-top:12px">Costs are the drag on a thin-edge strategy &mdash; you’re on the retail model.</div>';
}

function renderGenrec(d){
  var gens=d.strategy_generations||[];
  var rows=gens.map(function(g){
    var wl=g.closed?(g.wins+"W–"+g.losses+"L"):"—", wr=g.closed?Math.round(g.win_rate*100)+"%":"—",
        ar=g.closed?sgn(g.avg_r)+"R":"—";
    var ver=""; if(!g.legacy&&g.version_from){ ver="v"+g.version_from+(g.version_to&&g.version_to!==g.version_from?"–v"+g.version_to:""); }
    return '<div class="gen"><div class="gen-name"><span class="gen-dot'+(g.is_current?" cur":"")+'"></span><b>'+esc(g.label)+"</b>"+
      (ver?' <span class="gen-ver">'+ver+"</span>":"")+(g.is_current?' <span class="gen-live">live</span>':"")+"</div>"+
      '<div class="mono">'+wl+'</div><div class="mono">'+wr+'</div><div class="mono '+(g.closed?cls(g.avg_r):"")+'">'+ar+"</div></div>";
  }).join("");
  el("genrec").innerHTML='<div class="gen gen-h"><span>strategy</span><span>W–L</span><span>win</span><span>avg R</span></div>'+rows+
    '<div class="note" style="margin-top:9px">A strategy change starts a fresh record &mdash; the current one is measured on its own trades, never pooled with the old config.</div>';
}

function renderFunnel(d){
  var c=d.coverage, signaled=(d.dossiers||[]).filter(function(x){return x.status==="SIGNALED";}).length, base=c.tradeables||1;
  var stages=[["Tradeables","in the universe",c.tradeables],["Connected","can receive anchor news",c.tradeables_connected],
    ["Has a dossier","thesis accumulating",c.tradeables_with_dossier],["Signaled now","over the conviction bar",signaled]];
  el("funnel").innerHTML=stages.map(function(s){ var p=Math.min(100,s[2]/base*100);
    return '<div class="stage"><div class="lab">'+s[0]+"<small>"+s[1]+'</small></div><div class="bar"><span style="width:'+p+'%"></span></div>'+
      '<div class="val mono">'+s[2]+' <small>/'+c.tradeables+"</small></div></div>";
  }).join("")+'<div class="note" style="margin-top:2px">Only <b class="mono">'+Math.round(c.tradeables_with_dossier/base*100)+
    "%</b> of tradeables have a thesis &mdash; that gap, not market quiet, caps signal output.</div>";
}

function renderBudget(d){
  var u=d.usage, pct=u.daily_usd_budget?u.usd_spent/u.daily_usd_budget*100:0;
  var now=new Date(), timePct=(now.getUTCHours()*3600+now.getUTCMinutes()*60)/86400*100;
  var cats=[["extraction","--cat-ext"],["dossier","--cat-dos"],["synthesis","--cat-syn"],["research","--cat-res"]];
  var bc=u.by_category||{}, scale=(u.daily_usd_budget||0)>0?u.daily_usd_budget:(u.usd_spent||1);
  var rows=cats.map(function(c){ var v=(bc[c[0]]||{}).usd||0;
    return '<div class="bcat"><span class="bl">'+c[0]+'</span><span class="bmeter"><span style="width:'+Math.min(100,v/scale*100)+
      "%;background:var("+c[1]+')"></span></span><span class="mono" style="color:var(--muted)">$'+v.toFixed(2)+"</span></div>";
  }).join("");
  el("budget").innerHTML='<div class="k">LLM budget today</div>'+
    '<div class="big mono">$'+(u.usd_spent||0).toFixed(2)+'<span class="u"> / '+(u.daily_usd_budget||0).toFixed(0)+'</span></div>'+
    '<div class="meter"><span style="width:'+pct+"%;background:"+(pct>85?"var(--warn)":"var(--accent)")+'"></span>'+
      '<span style="left:'+timePct+'%;width:2px;background:var(--ink);opacity:0.5"></span></div>'+
    '<div class="ci-labels" style="margin-top:3px"><span>'+u.calls+" / "+u.daily_call_budget+' calls</span><span>| = now, '+Math.round(timePct)+'% into day</span></div>'+
    '<div class="budget-cats">'+rows+"</div>";
}

// Graph health: the mechanism the whole strategy runs on. An edge is the only
// path by which an anchor's news reaches a tradeable, so a missing edge is a
// trade that never happens -- these are the numbers that say whether the edge
// map is being kept alive.
function ratioRow(label, have, total, hint, warnBelow){
  var pct=total?Math.round(have/total*100):0;
  var col=(warnBelow!==undefined&&pct<warnBelow)?"var(--warn)":"var(--accent)";
  return '<div class="stage" style="grid-template-columns:132px 1fr auto"><div class="lab">'+label+
    (hint?"<small>"+hint+"</small>":"")+'</div><div class="bar"><span style="width:'+pct+"%;background:"+col+'"></span></div>'+
    '<div class="val mono">'+have+' <small>/'+total+"</small></div></div>";
}
function renderGraphHealth(d){
  var g=d.graph_health; if(!g){ el("ghStats").innerHTML=""; el("ghMaint").innerHTML=""; return; }
  var types=Object.keys(g.edges_by_type||{}).map(function(k){
    return '<span class="lc"><span class="lc-dot" style="background:var(--gc-'+
      ({customer:"cust",supplier:"supp",competitor:"comp",regulator:"reg"}[k]||"eco")+')"></span>'+esc(k)+
      ' <b class="mono">'+g.edges_by_type[k]+"</b></span>";
  }).join("");
  // Per symbol, not one blanket sentence. This used to promise that "the
  // rolling refresh re-reads filings against the current universe to close
  // exactly these holes", which was false for every name that carried the
  // flag: extraction had already run on all of them and found only own
  // subsidiaries, auditors, and foreign or private customers. A re-read
  // returns the same names forever, so the advice sent the operator to wait
  // for a fix that could not arrive.
  var reasons = g.disconnected_reasons || {};
  var why = Object.keys(reasons).map(function(sym){
    var r = reasons[sym], line;
    if (!r.found) line = "no counterparty extracted yet &mdash; a refresh may close this one";
    else if (r.resolvable) line = r.resolvable+" with a ticker waiting to be accepted: "+
      (r.resolvable_examples||[]).join(", ");
    else line = "extraction found "+r.found+", none resolvable: "+(r.examples||[]).join(", ");
    return '<div class="mono" style="margin-top:4px;opacity:0.85"><b>'+sym+"</b> &mdash; "+line+"</div>";
  }).join("");
  var stuck = Object.keys(reasons).some(function(s){
    return reasons[s].found && !reasons[s].resolvable; });
  var warn = g.disconnected_with_thesis
    ? '<div class="regime"><span>&#9873;</span><span><b>'+g.disconnected_with_thesis+
      '</b> tradeable(s) carry a thesis with <b>no graph edge at all</b> &mdash; their dossier came only from their own filings, so the cross-company mechanism never fired for them.'+
      (stuck ? " Unlisted, foreign or private counterparties cannot become edges, so those names are permanently direct-only: they still trade on their own filings, but the graph is not going to reach them." : "")+
      (why ? why : (g.disconnected_with_thesis_symbols&&g.disconnected_with_thesis_symbols.length
        ? '<div class="mono" style="margin-top:5px;opacity:0.85">'+g.disconnected_with_thesis_symbols.join(" ")+"</div>":""))+
      "</span></div>" : "";
  el("ghStats").innerHTML='<div class="k">Relationship graph</div>'+
    '<div class="big mono">'+g.edges+'<span class="u"> edges</span></div>'+
    '<div class="catleg" style="grid-template-columns:1fr 1fr 1fr">'+types+"</div>"+
    '<div class="funnel" style="margin-top:12px">'+
      ratioRow("Tradeables linked","tradeables_connected" in g?g.tradeables_connected:0,g.tradeables,"can receive anchor news",60)+
      ratioRow("Anchors live",g.anchors_live,g.anchors,"linked to a tradeable",40)+
    "</div>"+warn;

  var ra=g.last_refresh_days, sa=g.last_research_days;
  function ago(v){ return v===null||v===undefined?"never":(v<1?"today":v.toFixed(0)+"d ago"); }
  var rows=[
    ["Rolling re-extraction", g.refresh_per_day?(g.refresh_per_day+"/day &middot; full pass ~"+Math.round(g.cycle_days)+"d"):"<span class='warn'>disabled</span>", ago(ra)],
    ["Supplier research", g.researched_anchors+" / "+g.anchors+" anchors done", ago(sa)],
    ["Extraction age", (g.median_extraction_age_days===null?"&mdash;":"median "+Math.round(g.median_extraction_age_days)+"d")+
       (g.stalest_days!==null&&g.stalest_days!==undefined?" &middot; stalest "+Math.round(g.stalest_days)+"d":""),
       g.never_extracted?(g.never_extracted+" never"):"all read"]
  ].map(function(r){
    return '<div class="gen"><div class="gen-name"><b>'+r[0]+'</b></div><div class="mono" style="text-align:right;color:var(--muted)">'+r[1]+
      '</div><div class="mono" style="text-align:right">'+r[2]+"</div></div>";
  }).join("");
  // The correctness half of graph health. Everything above asks whether the
  // graph is BIG enough; the audit asks whether what is already in it is
  // RIGHT -- a delisted shell or a bond fund accepted as a trade target is
  // polled hourly and accrues spend against a thesis that cannot exist.
  var a=g.audit, aud;
  if(!a){
    aud='<div class="note" style="margin-top:9px">Universe audit has not run yet &mdash; it runs daily and on the <b>Graph maintenance</b> button.</div>';
  } else {
    var kinds=Object.keys(a.by_kind||{}).sort(function(x,y){return a.by_kind[y]-a.by_kind[x];}).map(function(k){
      return '<span class="lc">'+esc(k.replace(/_/g," "))+' <b class="mono">'+a.by_kind[k]+"</b></span>";
    }).join("");
    var act=a.actionable||0;
    aud='<div class="k" style="margin-top:14px">Universe audit'+
      '<span class="hint" style="float:right;font-weight:400">'+ago(g.audit_age_days)+"</span></div>"+
      '<div class="big mono" style="color:'+(act?"var(--warn)":"var(--muted)")+'">'+act+
        '<span class="u"> need a decision</span></div>'+
      (a.symbols_at_fault&&a.symbols_at_fault.length
        ? '<div class="mono" style="margin-top:4px;opacity:0.85">'+a.symbols_at_fault.map(esc).join(" ")+"</div>":"")+
      '<div class="catleg" style="grid-template-columns:1fr 1fr;margin-top:8px">'+kinds+"</div>"+
      (act?'<div class="note" style="margin-top:8px">Press <b>Graph maintenance (dry run)</b> for the evidence behind each one. Applying quarantines them &mdash; nothing is deleted, and a symbol with an open trade is never touched.</div>':"");
  }
  el("ghMaint").innerHTML='<div class="k">Maintenance</div>'+
    '<div class="gens" style="margin-top:6px">'+rows+"</div>"+
    '<div class="note" style="margin-top:9px">The graph only grows two ways: filings (annual/quarterly) and web research. The rolling pass re-reads the least-recently-extracted names <b>against the current universe</b> &mdash; extraction only writes an edge when the counterparty is already a member, so re-reading is what fills holes left when the universe was smaller.</div>'+aud;
}

function renderLadder(d){
  var ds=(d.dossiers||[]).slice().sort(function(a,b){return (b.confidence*b.magnitude)-(a.confidence*a.magnitude);}).slice(0,14);
  if(!ds.length){ el("ladder").innerHTML='<div class="lad-empty">No dossiers yet &mdash; nothing has accumulated a thesis.</div>'; return; }
  var rows=ds.map(function(x){
    var sc=x.confidence*x.magnitude, pct=Math.max(0,Math.min(100,sc*100)).toFixed(1), fires=sc>=0.5;
    // NONE is a real direction here (a dossier that has accumulated evidence
    // pointing both ways), and on a young install one can be inside the top
    // 14 -- so it must not render as a green L.
    var isS=x.direction==="SHORT", isL=x.direction==="LONG", con=x.mass_opposing>0.3;
    var dc=isS?"S":(isL?"L":"?");
    var col=isS?"var(--neg)":(isL?"var(--pos)":"var(--faint)");
    // Solid fill means it cleared 0.50, tinted means it did not. The rule
    // alone already answers that, but only if you sight along it; the fill
    // says the same thing from across the room.
    var soft=isS?"var(--neg-soft)":(isL?"var(--pos-soft)":"var(--line)");
    var tip=x.symbol+" "+x.direction+" \\u2014 score "+fx(sc)+" (conf "+fx(x.confidence)+" \\u00d7 mag "+fx(x.magnitude)+"), "+
      x.independent_source_count+" independent sources"+(con?", contested":"")+
      (fires?" \\u2014 above the bar" : " \\u2014 below the 0.50 bar")+". Click for the dossier.";
    return '<button type="button" class="lad-row'+(fires?" fires":"")+'" data-sym="'+escAttr(x.symbol)+'" title="'+escAttr(tip)+'">'+
      '<span class="lad-sym mono">'+esc(x.symbol)+'</span>'+
      '<span class="lad-dir '+dc+'">'+dc+"</span>"+
      '<span class="lad-track"><span class="lad-fill" style="width:'+pct+"%;background:"+(fires?col:soft)+'"></span>'+
      (con?'<span class="lad-con" style="left:'+pct+'%"></span>':"")+
      '<span class="lad-dot" style="left:'+pct+"%;background:"+col+'"></span></span>'+
      '<span class="lad-val mono">'+fx(sc)+"</span></button>";
  }).join("");
  el("ladder").innerHTML='<div class="lad-plot"><div class="lad-bar" aria-hidden="true"><b>fires at 0.50</b></div>'+rows+"</div>"+
    '<div class="lad-key"><span><i style="background:var(--pos)"></i>long</span>'+
    '<span><i style="background:var(--neg)"></i>short</span>'+
    '<span><i class="con"></i>contested</span>'+
    '<span>solid fill = past the bar</span>'+
    '<span>click a row for its dossier</span></div>';
}

// ===========================================================================
// DOSSIER SHEET -- the ladder and the all-dossiers table are both summaries,
// and until now the evidence under a score was reachable only by reading the
// JSON on disk. Clicking a row opens the dossier it summarises.
//
// Fetched per symbol rather than carried on the 10s status payload: evidence
// is the largest thing this system stores, and shipping every dossier's items
// on every refresh to render a panel that is usually shut is exactly the
// mistake _status_payload already had to undo for the graph's by_symbol map.
// ===========================================================================
var SHEET = { seq:0, from:null };

// esc() covers innerHTML; an ATTRIBUTE also has to survive a quote, and both
// are reachable from model-extracted text (headlines, relationship notes), so
// neither is theoretical. split/join rather than .replace(/"/g,...) because a
// regex literal containing a quote character reads as an unterminated string
// to the guard in test_webapp_html.py -- which is the only thing standing
// between this file and a syntax error that silently kills the dashboard.
function escAttr(s){ return esc(s).split('"').join("&quot;").split("'").join("&#39;"); }
// Evidence URLs come from news feeds and model extraction, i.e. not from us.
// Anything that is not plainly http(s) does not become an href.
function safeUrl(u){ u=String(u||""); return /^https?:\\/\\//i.test(u)?u:""; }

function openDossier(sym, origin){
  if(!sym) return;
  var seq=++SHEET.seq; SHEET.from=origin||null;
  el("dossierTitle").textContent=sym;
  el("dossierSub").textContent="";
  el("dossierBody").innerHTML='<div class="lad-empty">Loading dossier&hellip;</div>';
  el("dossierSheet").hidden=false;
  document.body.style.overflow="hidden";
  el("dossierClose").focus();
  fetch(API_BASE+"dossier/"+encodeURIComponent(sym))
    .then(function(r){ return r.json().then(function(j){ return { ok:r.ok, body:j }; }); })
    .then(function(res){
      // A second row clicked while this one was in flight must not have its
      // panel overwritten when the slower response lands.
      if(seq!==SHEET.seq) return;
      if(!res.ok){ el("dossierBody").innerHTML='<div class="lad-empty">'+esc(res.body.error||"Could not load this dossier.")+"</div>"; return; }
      renderDossierSheet(res.body);
    })
    .catch(function(err){ if(seq===SHEET.seq) el("dossierBody").innerHTML='<div class="lad-empty">Failed to load: '+esc(err)+"</div>"; });
}

function closeDossier(){
  var sheet=el("dossierSheet"); if(sheet.hidden) return;
  SHEET.seq++; sheet.hidden=true; document.body.style.overflow="";
  // Only if it is still on the page -- the row that opened this may have been
  // replaced by a refresh while the sheet was up.
  if(SHEET.from && document.contains(SHEET.from)) SHEET.from.focus();
  SHEET.from=null;
}

function statTile(label, value, colour){
  return '<div class="dsh-s"><div class="l">'+label+'</div><div class="v mono"'+(colour?' style="color:'+colour+'"':"")+">"+value+"</div></div>";
}

function renderDossierSheet(x){
  var sc=x.confidence*x.magnitude, fires=sc>=0.5, con=x.mass_opposing>0.3;
  // Same fallback as the ladder: anything that is not LONG or SHORT gets the
  // neutral colour rather than defaulting to green.
  var col=x.direction==="SHORT"?"var(--neg)":(x.direction==="LONG"?"var(--pos)":"var(--faint)");
  el("dossierSub").innerHTML='<span class="dir '+(CO[x.direction]||"")+'">'+esc(x.direction)+"</span>";

  var head='<div class="dsh-score"><span class="n mono" style="color:'+col+'">'+fx(sc)+"</span>"+
    '<span class="c">'+fx(x.confidence)+" conf &times; "+fx(x.magnitude)+" mag"+"</span>"+
    // Coloured by DIRECTION rather than with .pill.sig, which is the accent
    // the SIGNALED pill right next to it already uses -- two accent pills side
    // by side read as one repeated fact instead of two different ones.
    '<span class="pill"'+(fires?' style="color:'+col+";border-color:"+col+'"':"")+">"+
      (fires?"above the 0.50 bar":"below the 0.50 bar")+"</span>"+
    (x.status==="SIGNALED"?'<span class="pill sig">SIGNALED</span>':'<span class="pill">'+esc(x.status)+"</span>")+
    (con?'<span class="pill" style="color:var(--warn);border-color:var(--warn)">contested</span>':"")+"</div>";

  var tiles=statTile("Sources",x.independent_source_count)+
    statTile("Evidence",x.evidence_count)+
    statTile("Distinct facts",x.distinct_fact_count||x.distinct_fact_keys||0)+
    statTile("Horizon",(x.horizon_days||0)+"d")+
    statTile("Mass agree",fx(x.mass_agree),"var(--pos)")+
    statTile("Mass opposing",fx(x.mass_opposing),con?"var(--warn)":"")+
    statTile("Updated",timeAgo(x.updated_at).t)+
    (x.signaled_at?statTile("Signal price",x.signaled_price==null?"&ndash;":fx(x.signaled_price)):"");

  var thesis=x.thesis_summary?'<div class="dsh-h">Thesis</div><div class="dsh-p">'+esc(x.thesis_summary)+"</div>":"";

  // Why the number moved. A 0.000 from the whole-body pass and a 0.000 from
  // decay are the same digit and completely different situations.
  var syn="";
  if(x.synthesis_at){
    var flags=[];
    if(x.already_priced_in) flags.push("already priced in");
    if(x.redundant_evidence) flags.push("redundant evidence");
    syn='<div class="dsh-h">Whole-body synthesis</div><div class="dsh-p">Scored '+fx(x.pre_synthesis_score)+
      " before the pass, "+fx(sc)+" after ("+fx(x.synthesis_confidence)+" conf &times; "+fx(x.synthesis_magnitude)+" mag)"+
      (flags.length?", flagged "+esc(flags.join(" and ")):"")+". Last run "+esc((x.synthesis_at||"").slice(0,16).replace("T"," "))+".</div>";
  }

  var shown=x.evidence_shown===undefined?(x.evidence||[]).length:x.evidence_shown;
  var evHead='<div class="dsh-h">Evidence &middot; '+shown+(x.evidence_count>shown?" of "+x.evidence_count+" (newest)":"")+"</div>";
  el("dossierBody").innerHTML=head+'<div class="dsh-grid">'+tiles+"</div>"+thesis+syn+evHead+dossierEvidence(x);
}

function dossierEvidence(x){
  var ev=x.evidence||[];
  if(!ev.length) return '<div class="lad-empty">No evidence items on this dossier.</div>';
  return ev.map(function(e){
    var url=safeUrl(e.url), title=esc(e.headline||"(no headline)");
    var tags='<span class="dir '+(CO[e.direction]||"")+'">'+esc(e.direction)+"</span>"+
      (e.is_propagated?'<span class="dsh-via">via '+esc(e.origin_symbol)+"</span>":"");
    var meta=[esc(e.source_type||"")+(e.source_name?" &middot; "+esc(e.source_name):""),
              esc((e.published_at||"").slice(0,10)),
              "conf "+fx(e.confidence)+" &middot; mag "+fx(e.magnitude),
              e.horizon_days?e.horizon_days+"d horizon":"",
              e.fact_key?"fact: "+esc(e.fact_key):""]
      .filter(Boolean).map(function(t){ return "<span>"+t+"</span>"; }).join("");
    return '<div class="dsh-ev"><div class="dsh-tags">'+tags+"</div>"+
      '<div class="dsh-ev-h">'+(url?'<a href="'+escAttr(url)+'" target="_blank" rel="noopener noreferrer">'+title+"</a>":title)+"</div>"+
      '<div class="dsh-ev-m">'+meta+"</div>"+
      (e.relationship_note?'<div class="dsh-ev-n">'+esc(e.relationship_note)+
        (e.relationship_confidence==null?"":" (edge "+fx(e.relationship_confidence)+")")+"</div>":"")+
      (e.reasoning?'<div class="dsh-ev-n">'+esc(e.reasoning)+"</div>":"")+
      (e.skeptic_note?'<div class="dsh-ev-n dsh-skep">'+esc(e.skeptic_note)+"</div>":"")+"</div>";
  }).join("");
}

function renderOpen(d){
  var open=d.open_paper_trades||[]; el("openCount").textContent=open.length+" open";
  el("openTable").innerHTML='<thead><tr><th>Sym</th><th>Dir</th><th class="num">Entry</th><th class="num">Last</th><th class="num">Unreal R</th><th class="num">Unreal &euro;</th><th>Marked</th></tr></thead><tbody>'+
    (open.length?open.map(function(t){ var fr=timeAgo(t.last_marked_at);
      return "<tr><td class=\\"sym mono\\">"+esc(t.symbol)+'</td><td><span class="dir '+CO[t.direction]+'">'+t.direction+"</span></td>"+
        '<td class="num mono">'+fx(t.entry_price)+'</td><td class="num mono">'+fx(t.last_price)+'</td>'+
        '<td class="num mono rcell '+cls(t.unrealized_r)+'">'+(t.unrealized_r==null?"–":sgn(t.unrealized_r))+"</td>"+
        '<td class="num mono '+cls(t.unrealized_currency)+'">'+(t.unrealized_currency==null?"–":sgn(t.unrealized_currency,0))+"</td>"+
        '<td class="'+(fr.stale?"stale":"fresh")+'">'+fr.t+"</td></tr>";
    }).join(""):'<tr><td colspan="7" style="color:var(--faint)">None open.</td></tr>')+"</tbody>";
}

function renderSignals(d){
  var s=d.recent_signals||[];
  el("sigTable").innerHTML='<thead><tr><th>When</th><th>Sym</th><th>Dir</th><th class="num">Conf</th><th class="num">Mag</th><th class="num">Src</th><th>Thesis</th></tr></thead><tbody>'+
    s.slice().reverse().map(function(x){ return '<tr><td class="mono" style="color:var(--muted)">'+esc((x.generated_at||"").slice(0,16).replace("T"," "))+"</td>"+
      '<td class="sym mono">'+esc(x.symbol)+'</td><td><span class="dir '+CO[x.direction]+'">'+x.direction+"</span></td>"+
      '<td class="num mono">'+fx(x.confidence)+'</td><td class="num mono">'+fx(x.magnitude)+'</td><td class="num mono">'+x.independent_source_count+"</td>"+
      '<td class="thesis">'+esc(x.thesis_summary)+"</td></tr>";
    }).join("")+"</tbody>";
}

function renderDetail(d){
  var c=d.closed_paper_trades||[];
  el("closedTable").innerHTML='<thead><tr><th>Closed</th><th>Sym</th><th>Dir</th><th>Out</th><th class="num">Entry</th><th class="num">Exit</th><th class="num">R</th></tr></thead><tbody>'+
    c.slice().reverse().map(function(t){ return "<tr><td class=\\"mono\\" style=\\"color:var(--muted)\\">"+esc((t.closed_at||"").slice(0,10))+"</td>"+
      '<td class="sym mono">'+esc(t.symbol)+'</td><td><span class="dir '+CO[t.direction]+'">'+t.direction+"</span></td>"+
      '<td><span class="pill '+(t.status==="WIN"?"win":"loss")+'">'+t.status+"</span></td>"+
      '<td class="num mono">'+fx(t.entry_price)+'</td><td class="num mono">'+fx(t.exit_price)+'</td>'+
      '<td class="num mono rcell '+cls(t.r_multiple)+'">'+(t.r_multiple==null?"–":fx(t.r_multiple)+"R")+"</td></tr>";
    }).join("")+"</tbody>";
  var ds=d.dossiers||[]; el("dossCount").textContent=ds.length+" active";
  el("dossTable").innerHTML='<thead><tr><th>Sym</th><th>Dir</th><th class="num">Score</th><th class="num">Src</th><th>Status</th></tr></thead><tbody>'+
    ds.map(function(x){ var sc=x.confidence*x.magnitude;
      return '<tr data-sym="'+escAttr(x.symbol)+'" title="Click for the dossier"><td class="sym mono">'+esc(x.symbol)+
        '</td><td><span class="dir '+CO[x.direction]+'">'+x.direction+"</span></td>"+
        '<td class="num mono '+(sc>=0.5?"acc":"")+'">'+fx(sc)+(x.mass_opposing>0.3?' <span style="color:var(--warn)">◌</span>':"")+"</td>"+
        '<td class="num mono">'+x.independent_source_count+"</td>"+
        "<td>"+(x.status==="SIGNALED"?'<span class="pill sig">SIGNALED</span>':'<span class="pill">'+x.status+"</span>")+"</td></tr>";
    }).join("")+"</tbody>";
}

// ===========================================================================
// LIVE WIRE -- canvas graph + news ticker (driven by recent_signals). Lives
// outside the refresh cycle: a continuous animation loop, updated via
// updateWire(data). Signals flow as pulses from the signaled tradeable out to
// its graph neighbours; the active ticker item lights its path.
// ===========================================================================
var WIRE = { nodes:[], edges:[], pos:{}, feed:[], activeKey:"", t0:0, layoutKey:"", dirty:true };

// The stylesheet has always honoured prefers-reduced-motion for the two CSS
// pulse dots; the animation that actually moves -- travelling pulses, a beating
// selection ring, a full canvas repaint 60 times a second -- never consulted it.
// Under the preference the wire now holds still: no auto-advancing ticker (an
// auto-rotating list is the exact pattern the preference exists to stop),
// no travelling dots, and a selection ring at a fixed phase. Clicking a feed
// row still moves the selection, so nothing becomes unreachable.
var RMQ = window.matchMedia ? window.matchMedia("(prefers-reduced-motion: reduce)") : null;
var REDUCED = !!(RMQ && RMQ.matches);
if (RMQ && RMQ.addEventListener) RMQ.addEventListener("change", function(e){ REDUCED = e.matches; WIRE.dirty = true; });
var cv, ctx, dpr=Math.max(1,Math.min(2,window.devicePixelRatio||1)), cw=700, ch=460, scale=1, VW=1000, VH=560;

var TCK = {};
function tok(name){ return TCK[name] !== undefined ? TCK[name] : getComputedStyle(document.documentElement).getPropertyValue(name).trim(); }
function refreshTokens(){ ["--gc-cust","--gc-supp","--gc-comp","--gc-reg","--gc-eco","--gn-anchor","--pos","--neg","--faint","--gn-stroke","--gn-txt","--warn","--muted"]
  .forEach(function(k){ TCK[k]=getComputedStyle(document.documentElement).getPropertyValue(k).trim(); }); }
// The four REL_TYPES and their channel colours. Anything else cannot reach the
// canvas today -- RelationshipGraph drops an unknown rel_type on load -- but it
// is drawn dashed in the fallback colour below rather than silently borrowing a
// real channel's, so a new relationship type shows up as obviously unkeyed.
var GC={customer:"--gc-cust",supplier:"--gc-supp",competitor:"--gc-comp",regulator:"--gc-reg"};
function gvColorTok(type){ return GC[type]||"--gc-eco"; }
function gvR(n){ if(n.kind==="anchor") return 13; if(n.score!=null) return 6+n.score*10; return 6; }
function gvFill(n){ if(n.kind==="anchor") return "--gn-anchor"; if(n.dir==="LONG") return "--pos"; if(n.dir==="SHORT") return "--neg"; return "--faint"; }

function layoutWire(){
  var nodes=WIRE.nodes, edges=WIRE.edges, i,j, seed=1234567;
  function rnd(){ seed=(seed*1664525+1013904223)%4294967296; return seed/4294967296; }
  var pos={}; nodes.forEach(function(n,k){ var a=k*2*Math.PI/nodes.length; pos[n.id]={x:VW/2+(170+rnd()*50)*Math.cos(a),y:VH/2+(140+rnd()*50)*Math.sin(a),vx:0,vy:0}; });
  for(var it=0;it<250;it++){
    for(i=0;i<nodes.length;i++) for(j=i+1;j<nodes.length;j++){ var pa=pos[nodes[i].id],pb=pos[nodes[j].id];
      var dx=pa.x-pb.x,dy=pa.y-pb.y,d2=dx*dx+dy*dy+0.01,d=Math.sqrt(d2),rep=5200/d2,ux=dx/d,uy=dy/d;
      pa.vx+=ux*rep;pa.vy+=uy*rep;pb.vx-=ux*rep;pb.vy-=uy*rep; }
    edges.forEach(function(e){ var pa=pos[e[0]],pb=pos[e[1]]; if(!pa||!pb) return;
      var dx=pb.x-pa.x,dy=pb.y-pa.y,d=Math.sqrt(dx*dx+dy*dy)+0.01,f=(d-92)*0.02*(0.5+e[3]),ux=dx/d,uy=dy/d;
      pa.vx+=ux*f;pa.vy+=uy*f;pb.vx-=ux*f;pb.vy-=uy*f; });
    nodes.forEach(function(n){ var p=pos[n.id]; p.vx+=(VW/2-p.x)*0.006;p.vy+=(VH/2-p.y)*0.006;p.vx*=0.85;p.vy*=0.85;p.x+=p.vx;p.y+=p.vy;
      p.x=Math.max(38,Math.min(VW-38,p.x));p.y=Math.max(32,Math.min(VH-32,p.y)); });
  }
  for(var pass=0;pass<9;pass++) for(i=0;i<nodes.length;i++) for(j=i+1;j<nodes.length;j++){ var qa=pos[nodes[i].id],qb=pos[nodes[j].id];
    var ex=qb.x-qa.x,ey=qb.y-qa.y,ed=Math.sqrt(ex*ex+ey*ey)+0.01,need=gvR(nodes[i])+gvR(nodes[j])+13;
    if(ed<need){ var pu=(need-ed)/2,nx=ex/ed,ny=ey/ed; qa.x-=nx*pu;qa.y-=ny*pu;qb.x+=nx*pu;qb.y+=ny*pu; } }
  WIRE.pos=pos;
}

// ---- the feed's derived list ----------------------------------------------
// ONE ordered, de-duplicated list drives the ticker, the feed highlight and the
// canvas -- not three separate readings of the raw log window.
//
// Two things are wrong with rendering data.recent_signals directly. It arrives
// OLDEST FIRST: gather_recent_signals returns the tail of an append-only log
// verbatim, so the panel with the pulsing "live" dot opened on the oldest signal
// it held, with the newest below the fold of the scroll list (the Recent signals
// table below reverses for exactly this reason; this panel did not). And signal
// evaluation is deliberately status-blind (see signals.py's module docstring):
// one signal EPISODE re-logs a row on every newly accepted piece of corroborating
// evidence, so the raw window can be the same name five times over and the ticker
// would dwell on it five times. Rows carry an `episode` key precisely so that can
// be collapsed -- event_study.collapse_episodes already does it for the analysis.
function sigKeyOf(s){ return s?(s.symbol+"|"+(s.episode||s.generated_at||"")):""; }
function feedRows(sigs){
  var byKey={}, keys=[];
  (sigs||[]).forEach(function(s){
    if(!s||!s.symbol) return;
    var k=sigKeyOf(s);
    if(!byKey[k]){ byKey[k]=s; keys.push(k); }
    else if((s.generated_at||"")>=(byKey[k].generated_at||"")) byKey[k]=s;  // the latest re-log wins
  });
  return keys.map(function(k){ return byKey[k]; })
    .sort(function(a,b){ var x=a.generated_at||"", y=b.generated_at||""; return x<y?1:(x>y?-1:0); });
}
// Position is not identity. The window slides as new signals fire, so the index
// that meant INTT on one refresh means ASYS on the next -- the operator's
// selection silently moved to a different company under them every time the
// engine logged a signal. Selection is held as symbol+episode and resolved back
// to a position on each rebuild; -1 means "the selected episode is gone".
function activeIndex(){
  for(var i=0;i<WIRE.feed.length;i++) if(sigKeyOf(WIRE.feed[i])===WIRE.activeKey) return i;
  return -1;
}
function activeSig(){ var i=activeIndex(); return i<0?(WIRE.feed[0]||null):WIRE.feed[i]; }
function sigEdges(sym){ return WIRE.edges.filter(function(e){ return e[0]===sym||e[1]===sym; }); }
// Whether anything on the canvas is actually in motion this frame. Only the
// pulses and the selection ring ever move, and both need a selected node with
// at least one edge to travel along -- so a wire showing an unconnected signal,
// or no signal at all, has nothing to redraw and is left alone until something
// marks it dirty. This is the difference between a dashboard left open on a
// wall repainting 60 times a second forever and repainting when there is news.
function wireAnimating(){
  if(REDUCED) return false;
  var sig=activeSig();
  return !!(sig && WIRE.pos[sig.symbol] && sigEdges(sig.symbol).length);
}

function drawWire(now){
  if(!ctx) return;
  ctx.setTransform(dpr,0,0,dpr,0,0); ctx.clearRect(0,0,cw,ch);
  // A deployment with no graph yet (a custom SYMBOLS universe whose filings have
  // not been read, or a first run) has nothing to draw. Two empty rectangles
  // under a pulsing "live" dot read as a broken page -- every table on this
  // dashboard carries an empty-state line and this panel carried none.
  if(!WIRE.nodes.length){
    var es=Math.max(scale,0.6);
    ctx.textAlign="center"; ctx.textBaseline="middle";
    ctx.fillStyle=tok("--muted"); ctx.font="600 "+(13*es)+"px -apple-system,sans-serif";
    ctx.fillText("No relationships extracted yet", cw/2, ch/2-9*es);
    ctx.fillStyle=tok("--faint"); ctx.font=(11.5*es)+"px -apple-system,sans-serif";
    ctx.fillText("Edges appear as the engine reads each symbol's latest filings.", cw/2, ch/2+10*es);
    return;
  }
  var s=scale, sig=activeSig(), hotSym=sig?sig.symbol:null, hot={};
  if(hotSym) sigEdges(hotSym).forEach(function(e){ hot[e[0]+">"+e[1]]=1; });
  // edges
  WIRE.edges.forEach(function(e){ var pa=WIRE.pos[e[0]],pb=WIRE.pos[e[1]]; if(!pa||!pb) return;
    var isHot=hot[e[0]+">"+e[1]], col=tok(gvColorTok(e[2]));
    ctx.globalAlpha=isHot?0.95:0.34; ctx.strokeStyle=col; ctx.lineWidth=Math.max(0.6,(0.6+e[3]*2.2))*s*(isHot?1.3:1);
    if(!GC[e[2]]) ctx.setLineDash([3.2*s,4.5*s]); else ctx.setLineDash([]);
    ctx.beginPath(); ctx.moveTo(pa.x*s,pa.y*s); ctx.lineTo(pb.x*s,pb.y*s); ctx.stroke();
  });
  ctx.setLineDash([]); ctx.globalAlpha=1;
  // pulses along hot edges
  // Pulses travel INWARD -- from the counterparty to the signalled name. This
  // band is titled "News -> supply chain" and that is the direction the
  // mechanism runs: an anchor's news propagates across an edge into a thinly
  // covered tradeable, accumulates as evidence, and eventually crosses the
  // conviction bar. Running the dots outward drew the opposite claim -- a
  // small-cap broadcasting at the giants that actually fed it.
  if(hotSym && !REDUCED){ var pn=WIRE.pos[hotSym]; sigEdges(hotSym).forEach(function(e){ var other=e[0]===hotSym?e[1]:e[0], po=WIRE.pos[other]; if(!pn||!po) return;
    var col=tok(gvColorTok(e[2]));
    for(var k=0;k<3;k++){ var t=((now/1200)+k/3)%1, px=(po.x+(pn.x-po.x)*t)*s, py=(po.y+(pn.y-po.y)*t)*s, al=Math.sin(t*Math.PI);
      ctx.globalAlpha=al; ctx.fillStyle=col; ctx.shadowColor=col; ctx.shadowBlur=8*s; ctx.beginPath(); ctx.arc(px,py,3*s,0,7); ctx.fill(); }
  }); ctx.shadowBlur=0; ctx.globalAlpha=1; }
  // nodes
  WIRE.nodes.forEach(function(n){ var p=WIRE.pos[n.id]; if(!p) return; var x=p.x*s,y=p.y*s,r=gvR(n)*s;
    var isHot=(n.id===hotSym);
    // The selection ring clears the unlinked marker below rather than beating
    // through it -- two rings at the same radius read as one smeared ring.
    if(isHot){ var beat=REDUCED?0.5:(0.5+0.5*Math.sin(now/260)), gap=(n.kind==="unlinked"?9.5:4);
      ctx.globalAlpha=0.9; ctx.strokeStyle=tok(gvFill(n)); ctx.lineWidth=2;
      ctx.beginPath(); ctx.arc(x,y,r+(gap+beat*5)*s,0,7); ctx.stroke(); ctx.globalAlpha=1; ctx.shadowColor=tok(gvFill(n)); ctx.shadowBlur=(8+beat*10)*s; }
    // An `external` node is a graph endpoint that is not a universe member at
    // all -- today that is exclusively the regulator pseudo-symbols. Drawn
    // hollow so it cannot be mistaken for a thesis-less tradeable, which shares
    // its size and its grey.
    var isExt=(n.kind==="external");
    ctx.beginPath(); ctx.arc(x,y,r,0,7);
    ctx.fillStyle=isExt?tok("--gn-stroke"):tok(gvFill(n)); ctx.globalAlpha=n.kind==="anchor"?0.92:1; ctx.fill(); ctx.globalAlpha=1;
    ctx.shadowBlur=0; ctx.lineWidth=1.5*s; ctx.strokeStyle=isExt?tok("--faint"):tok("--gn-stroke"); ctx.stroke();
    // A signal with no disclosed path is MARKED, not merely drawn alone -- an
    // isolated dot reads as a layout accident, a dashed warning ring reads as the
    // statement it is: this thesis came from the company's own filings and no
    // relationship carried it.
    if(n.kind==="unlinked"){ ctx.save(); ctx.setLineDash([3*s,3*s]); ctx.lineWidth=1.5*s;
      ctx.strokeStyle=tok("--warn"); ctx.beginPath(); ctx.arc(x,y,r+4.5*s,0,7); ctx.stroke(); ctx.restore(); }
    // Every label sits ABOVE its node, anchors included. Printing the anchor's
    // name inside the disc put --gn-txt (a token tuned for the panel ground) on
    // --gn-anchor grey: 2.6:1 in dark and 3.1:1 in light, against the 4.5:1 that
    // 11px bold text needs to be legible. Above the disc the same text reads
    // 15:1 and up, and it no longer paints over the edges passing behind it.
    ctx.fillStyle=tok("--gn-txt"); ctx.textAlign="center"; ctx.textBaseline="middle"; ctx.font="700 "+(11*s)+"px -apple-system,sans-serif";
    ctx.fillText(n.id, x, y-r-6*s);
    if(n.kind!=="anchor"&&n.score!=null&&r>9*s){ ctx.fillStyle=tok("--gn-stroke"); ctx.font="700 "+(8*s)+"px ui-monospace,monospace"; ctx.fillText(n.score.toFixed(2),x,y); }
  });
  // ...and say it in words when that is the name currently being shown, so a
  // canvas with nothing moving on it is explained rather than just empty.
  if(hotSym){
    var hn=null; WIRE.nodes.forEach(function(n){ if(n.id===hotSym) hn=n; });
    var hp=WIRE.pos[hotSym];
    if(hn&&hp&&hn.kind==="unlinked"){
      ctx.fillStyle=tok("--warn"); ctx.textAlign="center"; ctx.textBaseline="top";
      ctx.font="600 "+(10*s)+"px -apple-system,sans-serif";
      ctx.fillText("no disclosed path \\u2014 own filings only", hp.x*s, hp.y*s+(gvR(hn)+9)*s);
    }
  }
}

// Setting canvas.width/height CLEARS the canvas, so a resize must always be
// followed by a repaint -- with the idle skip in frame() a still wire would
// otherwise stay blank until the next signal.
function resizeWire(){ if(!cv) return;
  // devicePixelRatio is re-read rather than sampled once at load: a window
  // dragged to a different-density display keeps its old backing store and
  // renders soft until something else forces a resize.
  var d=Math.max(1,Math.min(2,window.devicePixelRatio||1));
  var w=cv.parentNode.clientWidth, h=Math.round(w*VH/VW); if(h>520){h=520;}
  // Idempotent, because the observer below can be triggered by this function's
  // own effect on the panel's height -- and re-assigning canvas.width clears
  // the bitmap, so a non-guarded version would repaint in a loop.
  if(w===cw && h===ch && d===dpr) return;
  dpr=d; cw=w; ch=h; scale=cw/VW;
  cv.width=Math.round(cw*dpr); cv.height=Math.round(ch*dpr); cv.style.height=ch+"px"; WIRE.dirty=true; }

function renderFeed(scrollToActive){
  var s=WIRE.feed; el("feedCount").textContent=s.length+(s.length===1?" signal":" signals");
  var act=activeIndex(); if(act<0) act=0;
  // The dot claims the panel is live; with nothing in the window it should not.
  el("feedDot").className="dotlive"+(s.length?"":" idle");
  if(!s.length){ el("feed").innerHTML='<div class="ev-empty">No signals yet &mdash; nothing has crossed the conviction bar.</div>'; return; }
  el("feed").innerHTML=s.map(function(x,i){
    return '<button class="ev'+(i===act?" on":"")+'" data-i="'+i+'"><div class="ev-top"><span class="ev-sym mono">'+esc(x.symbol)+
      '</span><span class="dir '+CO[x.direction]+'">'+x.direction+'</span><span class="ev-time">'+esc((x.generated_at||"").slice(5,16).replace("T"," "))+
      '</span></div><div class="ev-body">'+esc(x.thesis_summary)+"</div></button>";
  }).join("");
  Array.prototype.forEach.call(el("feed").querySelectorAll(".ev"),function(b){ b.addEventListener("click",function(){
    WIRE.activeKey=sigKeyOf(WIRE.feed[+b.dataset.i]); WIRE.t0=performance.now(); WIRE.dirty=true; renderFeed(true); }); });
  // The list holds ~4 rows and the ticker walks all 25, so from the fifth item
  // on the canvas was pulsing a name whose feed row was below the fold. Done on
  // every selection CHANGE but never on a plain re-render: a refresh landing
  // while the operator is scrolled back through older signals must not snap the
  // list out from under them.
  //
  // Scrolls the CONTAINER, not scrollIntoView: block:"nearest" walks every
  // scrollable ancestor, so an operator reading the trade tables further down
  // the page would be yanked back up here every 4.8 seconds.
  if(scrollToActive){
    var on=el("feed").querySelector(".ev.on"), box=el("feed");
    if(on){
      var r1=on.getBoundingClientRect(), r0=box.getBoundingClientRect();
      if(r1.top<r0.top) box.scrollTop+=r1.top-r0.top;
      else if(r1.bottom>r0.bottom) box.scrollTop+=r1.bottom-r0.bottom;
    }
  }
}

// The full graph is the whole universe (~200 names, hundreds of edges) -- a
// hairball. Focus on what actually drives trades: tradeables carrying a thesis
// (a dossier direction/score) or on the live wire, plus the anchors one hop out
// that feed them. That is the "news -> supply chain" subgraph, ~30 nodes.
function focusGraph(g){
  var nodes=g.nodes||[], edges=g.edges||[], keep={};
  nodes.forEach(function(n){ if(n.kind==="tradeable" && (n.dir || n.score!=null)) keep[n.id]="t"; });
  WIRE.feed.forEach(function(s){ keep[s.symbol]="t"; });
  edges.forEach(function(e){ if(keep[e[0]]==="t" && !keep[e[1]]) keep[e[1]]="a"; if(keep[e[1]]==="t" && !keep[e[0]]) keep[e[0]]="a"; });
  var fnodes=nodes.filter(function(n){ return keep[n.id]; });
  if(fnodes.length<6){
    // nothing has a thesis yet (fresh deploy) -- show the most-connected slice so it isn't blank
    var deg={}; edges.forEach(function(e){ deg[e[0]]=(deg[e[0]]||0)+1; deg[e[1]]=(deg[e[1]]||0)+1; });
    fnodes=nodes.slice().sort(function(a,b){ return (deg[b.id]||0)-(deg[a.id]||0); }).slice(0,40);
    fnodes.forEach(function(n){ keep[n.id]=keep[n.id]||"a"; });
  } else if(fnodes.length>52){
    // still dense -- keep every thesis name, trim anchors to the best-connected
    var dk={}; edges.forEach(function(e){ if(keep[e[0]]&&keep[e[1]]){ dk[e[0]]=(dk[e[0]]||0)+1; dk[e[1]]=(dk[e[1]]||0)+1; } });
    fnodes.sort(function(a,b){ var pa=keep[a.id]==="t"?0:1, pb=keep[b.id]==="t"?0:1; return pa!==pb?pa-pb:((dk[b.id]||0)-(dk[a.id]||0)); });
    fnodes=fnodes.slice(0,52);
  }
  var ids={}; fnodes.forEach(function(n){ ids[n.id]=1; });
  // A signalled tradeable with NO graph edge has no node in the payload at all:
  // status.gather_graph_stats builds its node list purely from edge endpoints, so
  // a name that reaches nothing is simply absent, and filtering `nodes` above can
  // only ever drop it. Synthesizing it here is not cosmetic. That is exactly the
  // population gather_graph_health counts as `disconnected_with_thesis` and warns
  // about two bands down ("their dossier came only from their own filings, so the
  // cross-company mechanism never fired for them") -- the case an operator most
  // needs to see, because it is the one where this system's whole premise did not
  // hold. Without a node the wire went silent precisely when the ticker reached
  // one: no dot, no pulse, and -- having no hot edges either -- a frozen canvas
  // with nothing to explain it.
  //
  // Appended AFTER the trim above on purpose: a name that actually fired must
  // never be dropped for being poorly connected, which is the one property that
  // guarantees it loses a degree-ranked cut. Built from the signal row itself,
  // which already carries the direction and both factors of the score.
  var unlinked=0;
  WIRE.feed.forEach(function(s){
    if(!s.symbol || ids[s.symbol]) return;
    ids[s.symbol]=1; unlinked++;
    fnodes.push({ id:s.symbol, kind:"unlinked", dir:s.direction, name:"", sector:"",
      score:(s.confidence!=null&&s.magnitude!=null)?Math.round(s.confidence*s.magnitude*1000)/1000:null });
  });
  return { nodes:fnodes, edges:edges.filter(function(e){ return ids[e[0]]&&ids[e[1]]; }),
           total:nodes.length+unlinked, unlinked:unlinked };
}

function updateWire(d){
  // Built BEFORE focusGraph: the focus set includes every signalled symbol, so
  // the ticker, the highlight and the canvas must all read the same list.
  WIRE.feed=feedRows(d.recent_signals);
  // Only when the selected episode has fallen out of the window entirely does
  // the selection move -- and then to the newest, not to whatever now occupies
  // the old index.
  if(activeIndex()<0) WIRE.activeKey=sigKeyOf(WIRE.feed[0]);
  var f=focusGraph(d.graph||{});
  var key=JSON.stringify(f.nodes.map(function(n){return n.id;}));
  WIRE.nodes=f.nodes; WIRE.edges=f.edges;
  if(key!==WIRE.layoutKey){ WIRE.layoutKey=key; layoutWire(); }
  // The legend keys exactly what is on screen -- nothing more, nothing less.
  //
  // It used to be a fixed list that named "ecosystem", which is not a
  // relationship type at all (it is an evidence class elsewhere in the system,
  // and RelationshipGraph drops any rel_type outside REL_TYPES on load), while
  // supplier and regulator edges -- drawn constantly -- had no key. It also gave
  // one word, "anchor", to three visually distinct node classes. A legend that
  // describes a channel which cannot occur, and omits two that do, is worse than
  // no legend: the panel's whole claim is that the mechanism can be read off it.
  //
  // Derived rather than hard-coded so it cannot drift again, and so an empty
  // canvas is not captioned with six keys for things it is not drawing.
  WIRE.dirty=true;
  var have={};
  f.edges.forEach(function(e){ have[GC[e[2]]?e[2]:"other"]=1; });
  f.nodes.forEach(function(n){
    if(n.kind==="anchor"){ have.anchor=1; return; }
    if(n.kind==="external"){ have.external=1; return; }
    if(n.kind==="unlinked") have.unlinked=1;
    if(n.dir==="LONG") have.long=1; else if(n.dir==="SHORT") have.short=1; else have.none=1;
  });
  var KEYS=[
    ["customer",  '<i class="gl-l" style="background:var(--gc-cust)"></i>customer'],
    ["supplier",  '<i class="gl-l" style="background:var(--gc-supp)"></i>supplier'],
    ["competitor",'<i class="gl-l" style="background:var(--gc-comp)"></i>competitor'],
    ["regulator", '<i class="gl-l" style="background:var(--gc-reg)"></i>regulator'],
    ["other",     '<i class="gl-l" style="background:var(--gc-eco)"></i>other'],
    ["anchor",    '<i class="gl-d" style="background:var(--gn-anchor)"></i>anchor'],
    ["long",      '<i class="gl-d" style="background:var(--pos)"></i>long'],
    ["short",     '<i class="gl-d" style="background:var(--neg)"></i>short'],
    ["none",      '<i class="gl-d small" style="background:var(--faint)"></i>no thesis yet'],
    ["external",  '<i class="gl-d hollow"></i>off-universe'],
    ["unlinked",  '<i class="gl-d none"></i>no disclosed path']
  ];
  el("wireLeg").innerHTML=KEYS.filter(function(k){ return have[k[0]]; })
      .map(function(k){ return '<span class="gl">'+k[1]+"</span>"; }).join("")+
    '<span class="gl" style="margin-left:auto;color:var(--faint)">'+
      f.nodes.length+' of '+f.total+' names &middot; thesis + the anchors feeding them</span>';
  // ...and no empty bordered strip under an empty canvas.
  el("wireLeg").style.display=f.nodes.length?"":"none";
  renderFeed();
}

// wire hover
(function(){ var tip=el("tip");
  function nodeAt(mx,my){ var best=null,bd=1e9; WIRE.nodes.forEach(function(n){ var p=WIRE.pos[n.id]; if(!p) return;
    var dx=mx-p.x*scale,dy=my-p.y*scale,d=Math.sqrt(dx*dx+dy*dy); if(d<gvR(n)*scale+5&&d<bd){bd=d;best=n;} }); return best; }
  document.addEventListener("mousemove",function(ev){ if(!cv) return; var rect=cv.getBoundingClientRect();
    if(ev.clientX<rect.left||ev.clientX>rect.right||ev.clientY<rect.top||ev.clientY>rect.bottom){ tip.style.opacity="0"; return; }
    var n=nodeAt(ev.clientX-rect.left,ev.clientY-rect.top);
    if(!n){ tip.style.opacity="0"; cv.style.cursor="default"; return; } cv.style.cursor="pointer";
    function dirWord(x){ return x.dir==="LONG"?"long":(x.dir==="SHORT"?"short":"no"); }
    var thesis = n.kind==="anchor" ? "anchor &middot; news source"
      : n.kind==="external" ? "off-universe &middot; a graph endpoint that is neither a trade target nor an anchor"
      : n.kind==="unlinked" ? (dirWord(n)+" thesis, score "+(n.score!=null?n.score.toFixed(2):"&ndash;")+
          " &mdash; signalled with NO disclosed relationship path; this one came from its own filings")
      : (n.score!=null?(dirWord(n)+" thesis, score "+n.score.toFixed(2)):"no thesis yet");
    var name=n.name||"", info=DESC[n.id]||n.sector||"";
    tip.innerHTML='<div class="h">'+esc(n.id)+(name?' &middot; '+esc(name):"")+"</div>"+(info?'<div class="d">'+esc(info)+"</div>":"")+'<div class="d">'+thesis+"</div>";
    tip.style.opacity="1"; tip.style.left=(ev.clientX+14)+"px"; tip.style.top=(ev.clientY+12)+"px";
  });
})();

// ===========================================================================
// render everything + animation loop
// ===========================================================================
function renderAll(d){
  renderCaps(d); renderPnl(d); renderWinRate(d); renderExposure(d); renderExpectancy(d);
  renderGenrec(d); renderFunnel(d); renderBudget(d); renderGraphHealth(d); renderLadder(d);
  renderOpen(d); renderSignals(d); renderDetail(d);
  updateWire(d);
}

function frame(now){
  var s=WIRE.feed;
  if(!REDUCED && s.length && now-WIRE.t0>4800){
    var i=activeIndex();
    WIRE.activeKey=sigKeyOf(s[(i<0?0:i+1)%s.length]); WIRE.t0=now; WIRE.dirty=true; renderFeed(true);
  }
  // The rAF chain keeps ticking (an empty callback costs nothing, and a loop
  // that stops has to be restarted correctly from six places -- a missed one
  // leaves a permanently frozen canvas); it is the REPAINT that is skipped.
  if(WIRE.dirty || wireAnimating()){ drawWire(now); WIRE.dirty=false; }
  requestAnimationFrame(frame);
}

// theme toggle (2-state, seeded from OS)
(function(){ var root=document.documentElement, btn=el("tgl");
  var dark=!!(window.matchMedia&&window.matchMedia("(prefers-color-scheme: dark)").matches);
  function apply(){ root.setAttribute("data-theme",dark?"dark":"light"); btn.textContent=dark?"☾ Dark":"☀ Light"; refreshTokens(); WIRE.dirty=true; }
  btn.addEventListener("click",function(){ dark=!dark; apply(); }); apply();
})();

// Delegated, because both containers have their innerHTML replaced wholesale
// every 10 seconds -- a listener bound to the rows themselves would have to be
// re-bound on every render, and would be silently lost the first time someone
// forgot.
el("ladder").addEventListener("click", function(ev){
  var row=ev.target&&ev.target.closest?ev.target.closest(".lad-row"):null;
  if(row) openDossier(row.getAttribute("data-sym"), row);
});
el("dossTable").addEventListener("click", function(ev){
  var tr=ev.target&&ev.target.closest?ev.target.closest("tr[data-sym]"):null;
  if(tr) openDossier(tr.getAttribute("data-sym"), null);
});
el("dossierBack").addEventListener("click", closeDossier);
el("dossierClose").addEventListener("click", closeDossier);
document.addEventListener("keydown", function(ev){ if(ev.key==="Escape") closeDossier(); });

cv=el("wireCanvas"); ctx=cv.getContext("2d");
window.addEventListener("resize",function(){ resizeWire(); });
// A window resize is not the only thing that changes this panel's width: the
// legend wrapping to a second line, the grid stacking at a breakpoint, or the
// "Full detail" section opening all resize it with the window untouched.
if(window.ResizeObserver){ new ResizeObserver(function(){ resizeWire(); }).observe(cv.parentNode); }
refreshTokens(); resizeWire(); WIRE.t0=(window.performance&&performance.now)?performance.now():0; requestAnimationFrame(frame);
refresh(); setInterval(refresh, 10000);
</script>

</body>
</html>
"""


class _CachedDossiers:
    """One disk read per dossier per payload, instead of two or three.

    Four gatherers below want dossiers, and each was calling `store.load()`
    independently -- gather_dossiers over every symbol, gather_graph_stats over
    every symbol that appears in the graph, gather_graph_health over the
    disconnected ones. Each call is a file read plus a JSON parse, and every one
    of them happens on the ENGINE'S OWN EVENT LOOP (see handle_status), every
    10 seconds, per open browser tab. Measured on a small install: 38 loads for
    19 dossiers on disk.

    Deliberately a wrapper rather than a change to DossierStore: the cache must
    live exactly as long as one payload. The store itself is shared with the
    engine, which mutates dossiers between awaits -- caching there would hand
    the polling loop a stale thesis, which is a correctness bug rather than a
    slow dashboard. Read-only by construction: nothing here writes.
    """

    def __init__(self, store):
        self._store = store
        self._cache: dict = {}

    def all_symbols(self) -> list[str]:
        return self._store.all_symbols()

    def load(self, symbol: str):
        if symbol not in self._cache:
            self._cache[symbol] = self._store.load(symbol)
        return self._cache[symbol]


async def _status_payload(engine) -> dict:
    settings = engine.settings
    log_dir = Path(settings.log_dir)
    dossiers = _CachedDossiers(engine.dossiers)
    paper_stats, closed_trades = gather_paper_trade_stats(
        log_dir / "paper_trades.jsonl",
        settings.initial_trading_capital, settings.trading_currency,
        settings.max_concurrent_positions,
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

    graph = gather_graph_stats(engine.graph, engine.universe, dossiers)
    # `by_symbol` is the per-filer grouping the ORIGINAL relationship tables
    # rendered. The redesign replaced those tables with the Live Wire canvas,
    # which reads only nodes/edges, and nothing on the page has touched the
    # field since -- but it is the single largest thing in the payload: 24.7 KB
    # of 66 KB, 37% of every 10-second refresh, serialized for no reader.
    # gather_graph_stats still returns it (it is part of that function's
    # contract and its tests); it just is not shipped to the browser.
    graph.pop("by_symbol", None)

    return {
        "version": os.environ.get("SMARTBOI_VERSION", ""),
        "capabilities": {
            "edgar": engine.edgar_client is not None,
            "news": engine.finnhub is not None,
            "anthropic": engine.updater is not None,
            "ib": engine.price_feed is not None,
        },
        "universe_size": len(engine.symbol_list),
        "coverage": gather_coverage(engine.universe, engine.graph, dossiers),
        "dossiers": gather_dossiers(dossiers),
        "graph": graph,
        "graph_health": gather_graph_health(
            engine.graph, engine.universe, dossiers,
            backfill_state=engine.backfill_state.data,
            last_refresh=engine.periodic_state.get("graph_refresh", "") or "",
            last_research=engine.periodic_state.get("supplier_research", "") or "",
            researched_anchor_count=len(researched_anchors(engine.candidates, engine.research_state)),
            refresh_per_day=(settings.graph_refresh_symbols_per_day
                             if settings.enable_graph_refresh else 0),
            audit=engine.audit_state.get("last"),
            candidates=engine.candidates.data,
        ),
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
        # The page polls this every 10 seconds forever, and between engine ticks
        # the answer is usually byte-identical -- the payload carries no clock of
        # its own, so an unchanged system really does serialize to the same
        # bytes. An ETag turns those repeats into a 304 with no body: the
        # browser serves fetch() the cached copy, so the JS needs no change at
        # all, and a dashboard left open all day stops re-sending an unchanged
        # graph over the LAN.
        #
        # Cache-Control: no-cache is the point -- it means "store it, but always
        # revalidate", which is exactly this endpoint. Without it the browser
        # would be free to serve a stale payload from cache without asking.
        body = json.dumps(data).encode()
        etag = '"' + hashlib.sha256(body).hexdigest()[:32] + '"'
        headers = {"ETag": etag, "Cache-Control": "no-cache"}
        if request.headers.get("If-None-Match") == etag:
            return web.Response(status=304, headers=headers)
        return web.Response(body=body, content_type="application/json", headers=headers)

    async def handle_dossier(request: web.Request) -> web.Response:
        """One dossier with its evidence, fetched when a row is clicked.

        Split out of /api/status rather than folded into it: evidence is by
        far the largest thing the system stores, and the page re-polls status
        every 10 seconds. Shipping every dossier's evidence on that cycle to
        render a panel that is usually closed is how the payload got trimmed
        in the first place (see _status_payload on `by_symbol`).

        The symbol is pattern-checked here and membership-checked in
        gather_dossier_detail, because DossierStore turns the string into a
        filesystem path -- a bare `dir / f"{symbol}.json"` with an unvalidated
        segment is a traversal, even from a GET that only reads.
        """
        symbol = (request.match_info.get("symbol") or "").upper()
        if not _TICKER_RE.match(symbol):
            return web.json_response({"error": "not a ticker"}, status=400)
        try:
            detail = await asyncio.wait_for(
                asyncio.to_thread(gather_dossier_detail, engine.dossiers, symbol),
                timeout=_STATUS_TIMEOUT_SEC,
            )
        except asyncio.TimeoutError:
            return web.json_response({"error": "dossier read timed out"}, status=504)
        except Exception as exc:  # noqa: BLE001 - a bad dossier must not 500 the page
            log.exception("Dashboard dossier query failed for %s", symbol)
            return web.json_response({"error": str(exc)}, status=500)
        if detail is None:
            return web.json_response({"error": f"no dossier for {symbol}"}, status=404)
        return web.json_response(detail, headers={"Cache-Control": "no-cache"})

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

    async def handle_tool_edgar_search(request: web.Request) -> web.Response:
        """Asks EDGAR which OTHER filers name each anchor
        (smartboi.tools.run_edgar_supplier_search).

        Same candidate-only discipline as web research, for a stricter
        reason: a full-text hit is not a disclosure ABOUT the anchor at all,
        it is a third party's filing that happens to mention it. Route it to
        evidence and the system would be scoring one company's 10-K against
        another company's dossier. Candidates only; the edge is created later
        from the accepted symbol's own filings, or not at all."""
        async def run() -> str:
            return await run_edgar_supplier_search(engine)

        return await _run_tool(run)

    async def handle_tool_graph_maintenance(request: web.Request) -> web.Response:
        """Audit, clean and grow the graph in one fixed sequence
        (smartboi.tools.run_graph_maintenance).

        The order is the point -- growing before cleaning re-admits the symbol
        just removed -- which is why this replaces pressing four buttons in the
        right order. `apply` gates only the destructive half: without it the
        audit and the (candidate-only) search still run and the clean and
        reconcile report what they would do.

        Quarantine never deletes and never touches a symbol with an open paper
        trade, so even the apply path cannot strand a position."""
        try:
            body = await request.json()
        except Exception:
            body = {}
        apply = bool(body.get("apply"))

        async def run() -> str:
            return await run_graph_maintenance(engine, apply=apply)

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

    async def handle_tool_full_diagnostics(request: web.Request) -> web.Response:
        """Every runtime file needed to diagnose this deployment remotely, as
        one redacted zip download (smartboi.tools.collect_full_diagnostics).

        The text bundle above is a summary, and a summary is what a NOVEL
        problem hides in. Diagnosing the last round of failures here needed
        the raw logs, signals.jsonl, paper_trades.jsonl,
        periodic_pass_state.json and the dossiers -- each fetched by hand from
        the Home Assistant share, over several rounds. This is that, in one
        click. No .env and no /data/options.json: the archive leaves the
        machine, and nothing in it is worth the API keys.

        POST, unlike its read-only siblings, and the reason is the payload
        rather than the side effect (there is none). The CSRF guard exempts
        GET on the stated grounds that "every GET here is a pure read with no
        side effect" -- true of this one too, but that rule was written when
        the richest GET was a status payload, and the dashboard binds 0.0.0.0
        with no auth of its own (see the module header). This endpoint hands
        back every dossier, the whole graph, the entire trade record and the
        logs, so it gets the same protection as the endpoints that change
        things, not the one the auto-refresh poll gets."""
        # Behind the same single-flight lock every other tool endpoint uses,
        # rather than _run_tool itself (which wraps its result in JSON, and
        # this one returns bytes). Without it the HEAVIEST tool in the app --
        # it reads the whole retained log history and every dossier -- was the
        # only one that could run concurrently with itself, so two impatient
        # clicks meant two simultaneous 30MB reads on a Raspberry Pi.
        if tool_lock.locked():
            return web.json_response(
                {"error": "Another tool run is already in progress -- wait for it to finish."},
                status=409,
            )
        async with tool_lock:
            try:
                payload = await asyncio.to_thread(collect_full_diagnostics, engine)
            except Exception as exc:  # noqa: BLE001 - a failed download must not kill the dashboard
                log.exception("Full diagnostics bundle failed.")
                return web.json_response({"error": redact_token(exc)}, status=500)
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        return web.Response(
            body=payload,
            content_type="application/zip",
            headers={"Content-Disposition":
                     f'attachment; filename="smartboi-diagnostics-{stamp}.zip"'},
        )

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

    async def handle_reset_runtime(request: web.Request) -> web.Response:
        """Starts a clean measurement window: archives all OPEN paper trades
        and resets every dossier's signal/synthesis episode to ACTIVE (see
        engine.reset_runtime_state). Keeps evidence, the graph, the universe
        and the version-stamped forward logs. Runs on the event loop, not a
        worker thread, for the same reason the accepted-reset does: it mutates
        live engine state the polling coroutines read between awaits."""
        result = engine.reset_runtime_state()
        return web.json_response({"ok": True, **result})

    async def handle_rebuild_graph(request: web.Request) -> web.Response:
        """Re-extracts relationships from every tradeable's latest 10-K (see
        engine.rebuild_relationship_graph). Additive only -- graph.add
        dedupes, so this cannot remove an edge or touch a dossier, a trade,
        or a captured log. Runs on the event loop for the same reason the
        reset does: it mutates engine state the polling coroutines read."""
        result = engine.rebuild_relationship_graph()
        return web.json_response({"ok": True, **result})

    async def handle_reconcile_connectivity(request: web.Request) -> web.Response:
        """Connectivity reconcile of the anchor set: grow with candidates that
        land connected to a tradeable, prune runtime-accepted anchors that are
        inert (see engine.reconcile_universe_connectivity). POST {"apply":true}
        to mutate; the default ({} / apply omitted) is a dry run that returns
        exactly what it would do. Runs on the event loop like the other
        universe mutators."""
        try:
            body = await request.json()
        except Exception:
            body = {}
        result = await engine.reconcile_universe_connectivity(apply=bool(body.get("apply")))
        return web.json_response({"ok": True, **result})

    app = web.Application(middlewares=[_require_csrf_header])
    app.router.add_get("/", handle_index)
    app.router.add_get("/api/status", handle_status)
    app.router.add_get("/api/dossier/{symbol}", handle_dossier)
    app.router.add_post("/api/candidates/accept", handle_accept_candidate)
    app.router.add_post("/api/tools/screen", handle_tool_screen)
    app.router.add_post("/api/tools/supplier-research", handle_tool_supplier_research)
    app.router.add_post("/api/tools/edgar-search", handle_tool_edgar_search)
    app.router.add_post("/api/tools/graph-maintenance", handle_tool_graph_maintenance)
    app.router.add_post("/api/tools/forward-returns", handle_tool_forward_returns)
    app.router.add_post("/api/tools/event-study", handle_tool_event_study)
    app.router.add_post("/api/tools/exit-analysis", handle_tool_exit_analysis)
    app.router.add_post("/api/tools/diagnostics", handle_tool_diagnostics)
    app.router.add_post("/api/tools/full-diagnostics", handle_tool_full_diagnostics)
    app.router.add_post("/api/universe/reset-accepted", handle_reset_accepted)
    app.router.add_post("/api/runtime/reset", handle_reset_runtime)
    app.router.add_post("/api/universe/rebuild-graph", handle_rebuild_graph)
    app.router.add_post("/api/universe/reconcile-connectivity", handle_reconcile_connectivity)
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
