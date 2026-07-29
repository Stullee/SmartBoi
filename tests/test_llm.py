"""llm.py: the per-model request shape. Getting this wrong is silent --
every call site catches the resulting 400 as a transient error and retries
forever, so the engine keeps running and simply never scores anything."""
from smartboi.llm import cost_usd, first_tool_use, price_per_mtok, request_kwargs


class _Block:
    def __init__(self, type_, **kw):
        self.type = type_
        for k, v in kw.items():
            setattr(self, k, v)


class _Response:
    def __init__(self, *blocks):
        self.content = list(blocks)


def test_current_models_get_thinking_and_effort_and_no_temperature():
    """temperature/top_p/top_k are REMOVED on these -- sending one is a 400."""
    for model in ("claude-opus-5", "claude-sonnet-5", "claude-opus-4-8", "claude-fable-5"):
        kwargs = request_kwargs(model, max_tokens=4000, effort="high")
        assert kwargs["thinking"] == {"type": "adaptive"}
        assert kwargs["output_config"] == {"effort": "high"}
        assert "temperature" not in kwargs, model


def test_haiku_gets_temperature_and_no_effort():
    """Haiku 4.5 predates output_config.effort and ERRORS on it, but still
    accepts temperature -- the exact inverse of the current models."""
    kwargs = request_kwargs("claude-haiku-4-5-20251001", max_tokens=500)
    assert kwargs["temperature"] == 0
    assert "output_config" not in kwargs
    assert "thinking" not in kwargs


def test_the_46_family_accepts_both():
    kwargs = request_kwargs("claude-opus-4-6", max_tokens=4000)
    assert kwargs["thinking"] == {"type": "adaptive"}
    assert kwargs["output_config"]["effort"] == "high"
    assert kwargs["temperature"] == 0


def test_an_unrecognised_model_gets_the_conservative_shape():
    """Every omitted parameter is optional on every known model, so the
    request stays valid rather than 400-ing on an unknown field."""
    kwargs = request_kwargs("claude-something-newer-than-this-table", max_tokens=4000)
    assert kwargs == {"max_tokens": 4000}


def test_dated_snapshots_resolve_like_their_alias():
    assert request_kwargs("claude-haiku-4-5-20251001", 500) == request_kwargs("claude-haiku-4-5", 500)


def test_pricing_is_per_million_tokens():
    assert price_per_mtok("claude-opus-5") == (5.0, 25.0)
    assert price_per_mtok("claude-haiku-4-5-20251001") == (1.0, 5.0)
    assert cost_usd("claude-sonnet-5", 1_000_000, 1_000_000) == 18.0


def test_unknown_models_are_never_free():
    assert price_per_mtok("claude-nonexistent") == (10.0, 50.0)


def test_first_tool_use_skips_thinking_blocks():
    """Thinking blocks now precede the tool call on every thinking-capable
    model -- code reading content[0] finds a ThinkingBlock and no tool use."""
    response = _Response(
        _Block("thinking", thinking=""),
        _Block("tool_use", input={"direction": "LONG"}),
    )
    assert first_tool_use(response) == {"direction": "LONG"}


def test_first_tool_use_is_none_when_the_model_returned_no_tool_call():
    assert first_tool_use(_Response(_Block("text", text="hi"))) is None
