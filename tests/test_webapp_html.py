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
