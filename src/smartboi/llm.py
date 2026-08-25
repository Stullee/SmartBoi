"""Model-aware request construction for the three Claude call sites
(graph.RelationshipExtractor, dossier.DossierUpdater, skeptic.Skeptic).

This exists because the Anthropic request surface is NOT uniform across
model generations, and this codebase's three call sites all hard-coded one
generation's shape. Getting it wrong is not a soft failure: every call site
wraps its API call in `except Exception` and returns None on error, which
this engine correctly interprets as "transient -- retry later". A request
that is permanently malformed for the configured model therefore produces
no crash, no signal, and an infinite retry loop. Changing a model name in
.env could silently stop the system from ever scoring evidence again.

The three differences that matter, current as of the 2026-07 refresh:

- `temperature` (and top_p/top_k) is REMOVED on Opus 5 / Sonnet 5 / Opus
  4.8 / 4.7 and Fable 5 -- sending it returns HTTP 400. It is still
  accepted on Haiku 4.5 and the 4.6 family. Determinism now comes from
  effort plus a tight prompt, not a sampling parameter (and temperature=0
  never guaranteed identical outputs anyway).
- `output_config.effort` is how thinking depth is controlled on 4.6+. It
  ERRORS on Haiku 4.5, which predates it.
- Extended thinking's `budget_tokens` is removed on 4.7+; `thinking:
  {"type": "adaptive"}` replaces it. On Opus 5 thinking is ON BY DEFAULT,
  which makes max_tokens a shared ceiling over thinking AND the response --
  a max_tokens sized for a 500-token tool call truncates before the
  tool_use block is ever emitted, and a truncated response yields no tool
  use at all. Every max_tokens here is sized with that headroom.

`request_kwargs` returns the correct shape for whichever model is
configured, so switching models is a .env edit rather than a silent
breakage. Anything not recognised falls back to the most conservative
shape (no effort, no thinking, no temperature), which is valid on every
model in the table."""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger(__name__)

# Model families, matched as a PREFIX of the configured model id so both the
# alias ("claude-haiku-4-5") and the dated snapshot
# ("claude-haiku-4-5-20251001") resolve to the same capabilities.
#
#   effort   -- accepts output_config.effort
#   thinking -- accepts thinking={"type": "adaptive"}
#   temp     -- accepts temperature
_CAPABILITIES: tuple[tuple[str, bool, bool, bool], ...] = (
    # prefix,                effort, thinking, temperature
    ("claude-fable-5",        True,  True,  False),
    ("claude-mythos-5",       True,  True,  False),
    ("claude-opus-5",         True,  True,  False),
    ("claude-sonnet-5",       True,  True,  False),
    ("claude-opus-4-8",       True,  True,  False),
    ("claude-opus-4-7",       True,  True,  False),
    ("claude-opus-4-6",       True,  True,  True),
    ("claude-sonnet-4-6",     True,  True,  True),
    ("claude-opus-4-5",       True,  False, True),
    ("claude-haiku-4-5",      False, False, True),
    ("claude-sonnet-4-5",     False, False, True),
)

# Per-million-token prices, (input, output), for the daily USD budget (see
# usage.py). A model absent from this table is priced at the most expensive
# entry rather than zero -- an unknown model must never look free, because
# the budget is the only thing standing between a model-string typo and an
# unbounded bill.
MODEL_PRICES_PER_MTOK: dict[str, tuple[float, float]] = {
    "claude-fable-5": (10.0, 50.0),
    "claude-mythos-5": (10.0, 50.0),
    "claude-opus-5": (5.0, 25.0),
    "claude-opus-4-8": (5.0, 25.0),
    "claude-opus-4-7": (5.0, 25.0),
    "claude-opus-4-6": (5.0, 25.0),
    "claude-opus-4-5": (5.0, 25.0),
    "claude-sonnet-5": (3.0, 15.0),
    "claude-sonnet-4-6": (3.0, 15.0),
    "claude-sonnet-4-5": (3.0, 15.0),
    "claude-haiku-4-5": (1.0, 5.0),
}
_UNKNOWN_MODEL_PRICE = max(MODEL_PRICES_PER_MTOK.values())


def _capabilities(model: str) -> tuple[bool, bool, bool]:
    for prefix, effort, thinking, temperature in _CAPABILITIES:
        if model.startswith(prefix):
            return effort, thinking, temperature
    # Unrecognised (newer than this table, or a typo): the conservative
    # shape. Every parameter this omits is optional on every known model,
    # so the request stays valid rather than 400-ing on an unknown field.
    return False, False, False


def price_per_mtok(model: str) -> tuple[float, float]:
    for prefix, price in MODEL_PRICES_PER_MTOK.items():
        if model.startswith(prefix):
            return price
    return _UNKNOWN_MODEL_PRICE


def cost_usd(model: str, input_tokens: int, output_tokens: int) -> float:
    price_in, price_out = price_per_mtok(model)
    return (input_tokens * price_in + output_tokens * price_out) / 1_000_000


def request_kwargs(model: str, max_tokens: int, effort: str = "high") -> dict:
    """The model-appropriate subset of thinking/effort/temperature kwargs,
    ready to splat into `messages.create`.

    `effort` is a hint, ignored on models that predate the parameter.
    "high" is the default because every call site here gates a trading
    decision; the extraction pass, which is high-volume and mechanical,
    passes something lower."""
    supports_effort, supports_thinking, supports_temperature = _capabilities(model)
    kwargs: dict = {"max_tokens": max_tokens}
    if supports_thinking:
        kwargs["thinking"] = {"type": "adaptive"}
    if supports_effort:
        kwargs["output_config"] = {"effort": effort}
    if supports_temperature:
        # Only where the parameter still exists, and only ever through
        # `extra_body`. The API still accepts temperature on these models;
        # the SDK does not still NAME it. anthropic 1.0.0 removed
        # `temperature` (and top_p/top_k) from AsyncMessages.create()
        # altogether, so passing it as a named kwarg raises TypeError in the
        # client before a request is ever sent -- which is how this system
        # spent four days retrying 109,092 calls that could never succeed.
        # `extra_body` is merged into the request body verbatim and has
        # existed across both SDK generations, so it is the one spelling that
        # survives the next signature change too.
        #
        # Pinned to 0 for the original reason: these scores gate trades at a
        # hard threshold, and the API default of 1.0 made some threshold
        # crossings sampling noise rather than evidence.
        kwargs["extra_body"] = {"temperature": 0}
    return kwargs


def cacheable_system(prompt: str) -> list[dict]:
    """The system prompt as a single cache-marked block.

    Every call here re-sends a system prompt of a thousand-odd tokens that
    never changes, and the per-item content is already confined to
    `messages`, so the prefix is a textbook caching candidate. Harmless
    where it can't apply: a prefix below the model's minimum cacheable
    length simply doesn't cache (no error, no extra charge), so this is
    unconditional rather than another capability branch -- it costs
    nothing on Haiku, and pays for itself immediately on a model with a
    512-token minimum."""
    return [{"type": "text", "text": prompt, "cache_control": {"type": "ephemeral"}}]


# Substrings that identify an ACCOUNT-level API failure -- one that every
# subsequent call will hit identically, no matter the prompt, until a human
# changes something outside this process.
#
# This distinction is load-bearing and its absence was expensive. Every call
# site here wraps the API in `except Exception` and returns None, which the
# engine correctly reads as "transient, retry later" -- correct for a 429, a
# 529 or a dropped connection, and catastrophic for a billing failure, which
# is neither transient nor retryable. Measured live on 2026-08-09: 11,893
# identical "Your credit balance is too low" failures between 06:00 and 08:00
# UTC, 10,102 of them inside a single hour, roughly three requests a second
# against an error that could not succeed until someone topped up an account.
#
# Matched on message text rather than exception type on purpose: the SDK
# raises the same BadRequestError class for a malformed tool schema (a genuine
# per-request bug that must NOT halt the day) as for an exhausted balance, so
# the type alone cannot tell them apart. Anything not listed here stays
# transient, which is the safe direction -- a missed classification costs
# retries, an over-eager one silently stops the system for a day.
# Deliberately narrow. Two entries that look like obvious inclusions are left
# out, because the breaker halts EVERY category on one failure:
#
#   - a bare "billing" substring. It appears in prose ("a billing period"),
#     in URLs the SDK echoes back, and in messages that are per-request
#     rather than account-level -- an unanchored match with a blast radius of
#     the whole day and no coverage the specific phrases below do not give.
#   - permission_error. This system runs FOUR different models (extraction,
#     dossier, skeptic, synthesis), and a key that lacks access to one of
#     them works perfectly for the other three. Halting all four on one
#     model's permission failure would turn a partial outage into a total
#     one; that call site correctly retries and fails, which is loud in the
#     log without stopping anything that still works.
_PERMANENT_API_FAILURES: tuple[tuple[str, str], ...] = (
    ("credit balance is too low", "the Anthropic credit balance is exhausted"),
    ("exceeded your organization's", "an Anthropic organization spend limit was reached"),
    ("authentication_error", "the Anthropic API key was rejected"),
    ("invalid x-api-key", "the Anthropic API key is invalid"),
)


def permanent_failure_reason(exc: object) -> str:
    """A human-readable reason when `exc` is a failure that retrying cannot
    fix, or "" when it is an ordinary transient error.

    See _PERMANENT_API_FAILURES for why the account-level cases are matched
    on text."""
    # A TypeError never reaches the network. It means the kwargs this module
    # built are not the signature of the SDK that is installed -- a mismatch
    # no retry can clear, and one that a dependency bump can introduce
    # without a line of this repository changing. Classified by TYPE rather
    # than by message because the wording is the SDK's to change: what makes
    # it permanent is that the call failed in the client.
    #
    # This is the exact gap that let anthropic 1.0.0's removal of
    # `temperature` from AsyncMessages.create() run for four days as 109,092
    # "transient" retries with the breaker still reading closed.
    if isinstance(exc, TypeError):
        return (
            f"the installed anthropic SDK rejected a request parameter ({exc}) -- "
            "llm.request_kwargs is building a request shape this SDK version does "
            "not accept, so no retry can succeed. Check the pinned anthropic version "
            "against _CAPABILITIES"
        )
    text = str(exc).lower()
    for needle, reason in _PERMANENT_API_FAILURES:
        if needle in text:
            return reason
    return ""


def first_tool_use(response) -> dict | None:
    """The first tool_use block's input, or None.

    Skips thinking blocks, which now precede the tool call on every
    thinking-capable model -- code that assumed `content[0]` was the tool
    call reads a ThinkingBlock instead and silently finds no tool use."""
    for block in response.content:
        if block.type == "tool_use":
            return block.input
    return None


# --- Tracing what each pass was actually SHOWN ----------------------------

# One backup generation, so the trace is bounded at 2x this on disk. The
# newest file is the one that matters -- this exists to answer "what did the
# model see on the call that produced THAT verdict", and that question is
# always about the recent past.
class LLMTrace:
    """Append-only record of the prompt and the tool call it produced.

    The system already records what every pass DECIDED -- direction,
    magnitude, the skeptic's note, the synthesis verdict and its flags -- and
    none of what it was SHOWN. That asymmetry is expensive in exactly the
    situation the record exists for. When `fact_key` came back empty on 970
    consecutive items, the stored output could say only that the field was
    blank; it could not distinguish a model that never emitted it from a
    pipeline that dropped it, and the wrong one was assumed first. A single
    traced call would have settled it.

    Sampled per category, because the shapes differ by two orders of
    magnitude: the per-item updater and skeptic run ~700 times a day, the
    whole-body synthesis ~30. Synthesis is worth tracing in full -- it is the
    pass that can veto a thesis to zero, and the one whose prompt changes
    most -- while the per-item passes only need enough coverage to answer
    "is the field arriving at all".

    Never raises. A diagnostic that can break ingestion is not a diagnostic;
    every failure here is swallowed and logged once."""

    def __init__(self, path: Path, enabled: bool = True,
                 sample: dict[str, int] | None = None,
                 max_bytes: int = 20_000_000) -> None:
        self.path = Path(path)
        self.enabled = enabled
        self.sample = sample or {}
        self.max_bytes = max_bytes
        self._counts: dict[str, int] = {}
        self._warned = False

    def _due(self, category: str) -> bool:
        n = max(1, int(self.sample.get(category, 1)))
        seen = self._counts.get(category, 0) + 1
        self._counts[category] = seen
        return seen % n == 0

    def record(self, category: str, model: str, symbol: str, prompt: str,
               response: object, input_tokens: int = 0, output_tokens: int = 0,
               system: str = "") -> None:
        if not self.enabled or not self._due(category):
            return
        try:
            # Rotate BEFORE writing, so the cap bounds the file rather than
            # being noticed one row after it is exceeded.
            if self.path.exists() and self.path.stat().st_size >= self.max_bytes:
                backup = self.path.with_suffix(self.path.suffix + ".1")
                backup.unlink(missing_ok=True)
                self.path.rename(backup)
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a") as f:
                f.write(json.dumps({
                    "at": datetime.now(timezone.utc).isoformat(),
                    "category": category,
                    "model": model,
                    "symbol": symbol,
                    # The system prompt is a constant per pass and would
                    # dominate the file; its length is enough to notice a
                    # change, and the text lives in source.
                    "system_chars": len(system),
                    "prompt": prompt,
                    # None is the interesting case, not a gap: it means the
                    # call returned no tool use at all.
                    "response": response,
                    "input_tokens": input_tokens,
                    "output_tokens": output_tokens,
                }, default=str) + "\n")
        except Exception:  # noqa: BLE001 - tracing must never break ingestion
            if not self._warned:
                self._warned = True
                log.exception("LLM trace write failed -- continuing untraced.")
