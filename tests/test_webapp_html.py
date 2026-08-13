"""The dashboard's JavaScript is embedded in a Python string, which means a
Python-level escape can silently corrupt it: `\\n` written in the source
becomes a REAL newline inside _INDEX_HTML, and a real newline inside a JS
string literal is a syntax error.

That is not a cosmetic failure. The browser abandons the whole script, so
NOTHING runs -- no auto-refresh (the page sits on "Loading…" forever) and no
button handlers, even though the static HTML around them still renders and
looks fine. It shipped exactly once, in 0.16.0, and was invisible from the
server side: every endpoint kept answering correctly.

Nothing else in the suite executes this JS, so these are the only tests that
can catch it."""
from __future__ import annotations

import json
import re
import shutil
import subprocess

import pytest

from smartboi.webapp import _INDEX_HTML


def _script_body() -> str:
    match = re.search(r"<script>(.*)</script>", _INDEX_HTML, re.S)
    assert match, "dashboard has no <script> block"
    return match.group(1)


def _unterminated_string_lines(js: str) -> list[str]:
    """Lines where a JS string literal is still open at end of line.

    Tracks the opening quote character and honours backslash escapes, rather
    than counting quotes -- this file legitimately contains double quotes
    inside single-quoted strings ('on">ENABLED') and vice versa, which a
    naive count reports as unbalanced. Template literals are deliberately not
    handled: this dashboard uses none, and they are the one JS string type
    where a raw newline is legal."""
    offenders = []
    for lineno, line in enumerate(js.splitlines(), start=1):
        quote = None
        escaped = False
        for i, char in enumerate(line):
            if escaped:
                escaped = False
                continue
            if char == "\\":
                escaped = True
            elif quote is None and char in "\"'":
                quote = char
            elif quote is None and char == "/" and line[i + 1:i + 2] == "/":
                # Rest of the line is a comment. Skipping it matters: prose
                # apostrophes ("TradingBot's webapp.py") otherwise read as an
                # unterminated single-quoted string. Only checked OUTSIDE a
                # string, so a URL like "http://..." is unaffected.
                break
            elif char == quote:
                quote = None
        if quote is not None:
            offenders.append(f"{lineno}: {line.strip()[:80]}")
    return offenders


def test_no_raw_newline_inside_a_javascript_string_literal():
    """The specific corruption: a string literal left open when its line
    ends. Cheap, dependency-free, and targets exactly the mistake that broke
    0.16.0 -- so it still catches it on a machine with no node."""
    offenders = _unterminated_string_lines(_script_body())
    assert not offenders, "unterminated JS string literal(s):\n" + "\n".join(offenders)


def test_the_unterminated_string_detector_actually_detects():
    """A guard that reports nothing is worse than no guard, and this one is
    the only protection when node is unavailable."""
    # A string broken across a newline flags BOTH lines -- the open one and
    # the orphaned remainder. This is the 0.16.0 bug exactly.
    assert _unterminated_string_lines('var a = "oops\nstill going";') == [
        '1: var a = "oops', '2: still going";',
    ]
    # ...without firing on the patterns this dashboard really uses: mixed
    # quoting, escaped quotes, a URL, or an apostrophe in a comment.
    assert _unterminated_string_lines("""var b = 'on">ENABLED';\nvar c = "it's fine";""") == []
    assert _unterminated_string_lines(r'var d = "an escaped \" quote";') == []
    assert _unterminated_string_lines('var e = "http://example.com";') == []
    assert _unterminated_string_lines("// TradingBot's webapp.py explains why") == []


def test_javascript_parses():
    """Full parse via node when it's available -- catches anything the
    line-level check above can't see."""
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not available to parse-check the dashboard JS")
    result = subprocess.run(
        [node, "--check", "-"], input=_script_body(),
        capture_output=True, text=True,
    )
    assert result.returncode == 0, f"dashboard JS does not parse:\n{result.stderr}"


def test_copy_button_degrades_to_execcommand_for_plain_http():
    """The Copy-report button must work on a plain-HTTP LAN Home Assistant
    install -- which is most of them. navigator.clipboard is undefined in a
    non-secure context, so relying on it alone would make the button silently
    do nothing for exactly this add-on's users. Lock in the execCommand
    fallback so a refactor can't quietly drop it."""
    body = _script_body()
    assert 'getElementById("btn-copy-output")' in body      # the button is wired up
    assert "navigator.clipboard" in body                    # modern API when it's there
    assert 'execCommand("copy")' in body                    # ...but HTTP still copies


def test_every_button_the_script_binds_actually_exists_in_the_html():
    """A renamed or removed element id leaves addEventListener throwing on a
    null at load time, which kills the rest of the script exactly like a
    syntax error would."""
    body = _script_body()
    for element_id in re.findall(r'getElementById\("([^"]+)"\)', body):
        assert f'id="{element_id}"' in _INDEX_HTML, f'script binds #{element_id}, which the HTML never defines'


def _layout_source() -> str:
    """The wire layout's pure functions, lifted out of the dashboard script so
    they can be run headless. Brace-matched rather than regexed, because these
    bodies contain braces."""
    js = _script_body()

    def grab(name: str) -> str:
        start = js.index(f"function {name}(")
        depth, i = 0, start
        while True:
            if js[i] == "{":
                depth += 1
            elif js[i] == "}":
                depth -= 1
                if depth == 0:
                    return js[start:i + 1]
            i += 1

    return "\n".join(grab(n) for n in ("gvR", "gvHW", "gvTop", "gvBot", "layoutWire"))


def test_wire_layout_keeps_labels_apart_and_on_the_canvas():
    """The graph's de-overlap pass separated DISCS, and node labels are far
    wider than the discs they sit above -- a 12px dot carries a 40px name. So
    "TCPA" and "SCRNY" printed straight through each other while their discs
    were a comfortable 13px clear, and the panel read as corrupted.

    It also ran after the simulation loop's clamp without re-clamping, so its
    own pushes could walk a node off the canvas, where the label was clipped
    by the edge or piled into a stack with whatever else was shoved there.

    Measured over the real node set at the time: 70 colliding label pairs
    across 50 node-count/edge-density combinations. This runs the actual
    shipped layout and asserts both properties hold."""
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not available to run the wire layout")

    # VHv is the VISIBLE virtual height, which resizeWire sets from the real
    # canvas; at the design aspect it equals VH, which is what the layout is
    # asserted against here.
    harness = "var WIRE, VW=1000, VH=560, VHv=560;\n" + _layout_source() + """
    // Deterministic stand-in for a live focus subgraph. Two properties matter
    // and a uniform graph has neither: symbol lengths span the real range
    // (3-6 chars, so labels are 2-4x wider than their discs), and the edges
    // are HUB-AND-SPOKE. The real graph hangs dozens of tradeables off a
    // handful of anchors -- BA, LMT, NOC, KTOS, BKR -- and it is that pull
    // toward a few points that packs nodes tight enough for their labels to
    // collide. A chain or a uniform mesh spreads out evenly and never
    // reproduces it: an earlier version of this test used one and passed
    // against the very layout it was written to catch.
    const NAMES=["AOSL","BKTI","UFPT","KLXE","HTLD","SRI","PLPC","RFL","SPWR","STRT",
                 "MVST","ESOA","CVV","PLAB","DCO","GTX","MLAB","EPAC","ACDC","ULH",
                 "PUMP","WOLF","SES","SCE-PN","SCRNY","TCPA","NCSM","WLDN","MTRX","BWEN",
                 "INTT","SIF","ASYS","KULR","LMB","NINE","OIS","RJET","VVX","RDW",
                 "THRM","SLDP","IRMD","CDRE","HDSN","ULBI","KODK","TAYD","GRC","HURC",
                 "CVLG","RLGT","MRTN","NVX","WBX","AMSC"];
    const HUBS=["BA","LMT","NOC","KTOS","BKR"];
    let worst={pairs:0,oob:0,at:""};
    for(let count=20; count<=50; count+=5){
      for(let dens=1; dens<=4; dens++){
        const nodes=[];
        for(let i=0;i<count;i++)
          nodes.push({id:NAMES[i%NAMES.length], kind:"tradeable",
                      dir:(i%3?"LONG":"SHORT"), score:((i*37)%100)/100});
        HUBS.forEach(h=>nodes.push({id:h, kind:"anchor", dir:null, score:null}));
        const edges=[];
        for(let i=0;i<count;i++) for(let k=0;k<dens;k++)
          edges.push([nodes[i].id, HUBS[(i+k)%HUBS.length], "customer", 0.8]);
        WIRE={nodes,edges,pos:{}};
        layoutWire();
        const pos=WIRE.pos;
        let oob=0, pairs=0;
        nodes.forEach(n=>{ const p=pos[n.id];
          if(p.x-gvHW(n)<0 || p.x+gvHW(n)>VW || p.y-gvTop(n)<0 || p.y+gvBot(n)>VH) oob++; });
        for(let i=0;i<nodes.length;i++) for(let j=i+1;j<nodes.length;j++){
          const a=nodes[i],b=nodes[j],pa=pos[a.id],pb=pos[b.id];
          // Labels sit at y-r-6, ~11px tall, centred on x.
          if(Math.abs(pa.x-pb.x) < gvHW(a)+gvHW(b) &&
             Math.abs((pa.y-gvR(a))-(pb.y-gvR(b))) < 11){ pairs++; }
        }
        if(pairs>worst.pairs || oob>worst.oob) worst={pairs,oob,at:`n=${count} density=${dens}`};
      }
    }
    console.log(JSON.stringify(worst));
    """
    # layoutWire closes over WIRE/VW/VH as globals in the page; the harness
    # declares the same three so it runs unmodified.
    result = subprocess.run([node, "-e", harness], capture_output=True, text=True)
    assert result.returncode == 0, f"wire layout failed to run:\n{result.stderr}"
    worst = json.loads(result.stdout.strip().splitlines()[-1])
    assert worst["oob"] == 0, f'{worst["oob"]} node(s) laid out off-canvas at {worst["at"]}'
    assert worst["pairs"] == 0, f'{worst["pairs"]} overlapping label pair(s) at {worst["at"]}'


def test_the_signal_bar_is_read_from_config_not_hard_coded():
    """Four sites answer "does this fire?" -- the conviction rule, the ladder
    rows, the dossier sheet and the dossiers table -- and each one hard-coded
    0.5. That is invisible while signal_confidence_threshold sits at its
    default and actively misleading the moment it does not: at 0.25 the panel
    drew its rule at the midpoint of the track, tinted the fill of every name
    that was really firing, and captioned them "below the 0.50 bar".

    A wrong number here is worse than no number, because the panel exists to
    answer exactly this question."""
    body = _script_body()
    assert "current_strategy.signal_confidence_threshold" in body, \
        "the dashboard never reads the configured bar"
    # No comparison against a bare 0.5 anywhere a score is judged.
    assert not re.search(r"sc\s*>=\s*0?\.5\b", body), \
        "a fires/does-not-fire test is still hard-coded to 0.5"
    # No caption asserting a threshold the config may not be set to.
    assert "0.50 bar" not in body and "fires at 0.50" not in body, \
        "a hard-coded 0.50 caption survives; it will lie when the bar is moved"
    # The rule's x-position must track the same value, not sit at the
    # geometric midpoint of the plot the way `.../2` did.
    assert "* var(--lad-bar)" in _INDEX_HTML, \
        "the conviction rule is not positioned from the configured bar"
    assert 'style="--lad-bar:' in body, "nothing sets --lad-bar at render time"
