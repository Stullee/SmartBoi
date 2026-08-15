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


def _cards_source() -> str:
    """boardCards, lifted out of the page so the selection and the fan-out count
    can be exercised directly in node."""
    js = _script_body()
    start = js.index("function boardCards(")
    depth, i, seen = 0, start, False
    while i < len(js):
        if js[i] == "{":
            depth += 1; seen = True
        elif js[i] == "}":
            depth -= 1
            if seen and depth == 0:
                return js[start:i + 1]
        i += 1
    raise AssertionError("boardCards never closes")


def test_a_card_is_built_for_every_name_on_the_board_and_shared_anchors_are_counted():
    """The card grid replaced a force-directed canvas because the data is not a
    hairball: a name on this board carries a MEDIAN OF 2 disclosed counterparties.
    Two properties have to hold for the grid to be honest.

    Every name on the board gets a card, including one with no disclosed link at
    all -- those are the population gather_graph_health calls disconnected_with_
    thesis, and the canvas used to drop them silently or strand them as unlabelled
    dots. And an anchor feeding SEVERAL theses at once has to be counted, because
    that is the "one macro fact restated through unrelated names" pattern the
    synthesis pass vetoes on: on the live board BIS fed all five semis names as a
    0.60 regulator edge, which read as five independent corroborations."""
    node = shutil.which("node")
    if node is None:
        pytest.skip("node not available to run the card builder")

    harness = "var WIRE={hideTypes:{competitor:1},feed:[]};\n" + _cards_source() + """
    const nodes=[
      {id:"AOSL",kind:"tradeable",dir:"LONG",score:0.27,sector:"semi_equipment"},
      {id:"PLAB",kind:"tradeable",dir:"LONG",score:0.15,sector:"semi_equipment"},
      {id:"INTT",kind:"tradeable",dir:"LONG",score:0.00,sector:"semi_equipment"},
      {id:"HURC",kind:"tradeable",dir:"LONG",score:0.02,sector:"industrial_machinery"},
      {id:"BIS", kind:"anchor",   dir:null,  score:null,sector:"semi_equipment"},
      {id:"DELL",kind:"anchor",   dir:null,  score:null,sector:"semi_equipment"}];
    const edges=[
      ["AOSL","BIS","regulator",0.80], ["PLAB","BIS","regulator",0.60],
      ["INTT","BIS","regulator",0.60], ["AOSL","DELL","customer",0.85],
      // A competitor edge, which is hidden by default and must not mint a row...
      ["PLAB","DELL","competitor",0.95],
      // ...and the same pair disclosed twice: the STRONGER one wins, one row.
      ["AOSL","DELL","supplier",0.40]];
    // MLAB has a thesis and no graph node at all -- gather_graph_stats builds
    // its node list from edge endpoints, so it can only arrive via the dossier
    // rows. It is the case the panel most needs to show.
    const dossiers=[{symbol:"MLAB",direction:"LONG",confidence:0.6,magnitude:0.2},
                    {symbol:"AOSL",direction:"LONG",confidence:0.9,magnitude:0.3}];
    const f=boardCards({nodes,edges}, dossiers);
    console.log(JSON.stringify({
      cards:f.board.map(n=>n.id),
      bis:f.shared["BIS"], dell:f.shared["DELL"],
      aoslLinks:Object.keys(f.feeds["AOSL"]||{}).length,
      aoslDell:(f.feeds["AOSL"]||{})["DELL"],
      hurcLinks:Object.keys(f.feeds["HURC"]||{}).length,
      mlabLinks:Object.keys(f.feeds["MLAB"]||{}).length,
      plabDell:(f.feeds["PLAB"]||{})["DELL"]||null}));
    """
    result = subprocess.run([node, "-e", harness], capture_output=True, text=True)
    assert result.returncode == 0, f"card builder failed to run:\n{result.stderr}"
    out = json.loads(result.stdout.strip().splitlines()[-1])

    assert sorted(out["cards"]) == ["AOSL", "HURC", "INTT", "MLAB", "PLAB"], \
        "every tradeable with a thesis gets a card -- including one with no graph node"
    assert out["hurcLinks"] == 0, "a name with no disclosed link still gets its card"
    assert out["mlabLinks"] == 0, "a name absent from the graph entirely is still on the board"
    assert out["bis"] == 3, "an anchor feeding three theses must be counted three times"
    assert out["dell"] == 1, "the competitor edge must not add PLAB to DELL's fan-out"
    assert out["plabDell"] is None, "a hidden class must not mint a row"
    assert out["aoslLinks"] == 2, "one row per counterparty, not one per disclosure"
    assert out["aoslDell"] == ["customer", 0.85], \
        "the strongest disclosure wins when a pair is linked more than once"


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
