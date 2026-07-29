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
        # Only where the parameter still exists. Pinned to 0 there for the
        # original reason: these scores gate trades at a hard threshold,
        # and the API default of 1.0 made some threshold crossings sampling
        # noise rather than evidence.
        kwargs["temperature"] = 0
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


def first_tool_use(response) -> dict | None:
    """The first tool_use block's input, or None.

    Skips thinking blocks, which now precede the tool call on every
    thinking-capable model -- code that assumed `content[0]` was the tool
    call reads a ThinkingBlock instead and silently finds no tool use."""
    for block in response.content:
        if block.type == "tool_use":
            return block.input
    return None
