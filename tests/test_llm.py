"""llm.py: the per-model request shape. Getting this wrong is silent --
every call site catches the resulting 400 as a transient error and retries
forever, so the engine keeps running and simply never scores anything."""
from smartboi.llm import (
    _CAPABILITIES,
    cost_usd,
    first_tool_use,
    price_per_mtok,
    request_kwargs,
)


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
    # Through extra_body, never as a named kwarg: the API still takes
    # temperature on Haiku, but anthropic 1.0.0 removed it from the client
    # signature. See request_kwargs.
    assert kwargs["extra_body"] == {"temperature": 0}
    assert "temperature" not in kwargs
    assert "output_config" not in kwargs
    assert "thinking" not in kwargs


def test_the_46_family_accepts_both():
    kwargs = request_kwargs("claude-opus-4-6", max_tokens=4000)
    assert kwargs["thinking"] == {"type": "adaptive"}
    assert kwargs["output_config"]["effort"] == "high"
    assert kwargs["extra_body"] == {"temperature": 0}


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


def test_every_emitted_kwarg_is_accepted_by_the_INSTALLED_sdk():
    """The rest of this file tests our MODEL of the API. This tests the thing
    that actually broke: the signature of the SDK that is installed.

    On 2026-08-21 a rebuild resolved `anthropic>=0.40` to 1.0.0, which removed
    `temperature` from `AsyncMessages.create()`. Every haiku call site then
    raised TypeError before a request was ever sent, the engine read that as
    transient, and it retried 109,092 times over four days without scoring one
    piece of evidence. The whole suite stayed green throughout, because every
    test here fakes the client and never consults the real signature.

    So: emit the kwargs for every model in the table and assert the installed
    SDK will actually take them. `extra_body` is the escape hatch for a
    parameter the API still accepts but the SDK no longer names."""
    import inspect

    from anthropic.resources.messages import AsyncMessages

    accepted = set(inspect.signature(AsyncMessages.create).parameters)
    for prefix, _, _, _ in _CAPABILITIES:
        kwargs = request_kwargs(prefix, max_tokens=4000, effort="high")
        unknown = set(kwargs) - accepted
        assert not unknown, (
            f"request_kwargs({prefix!r}) emits {sorted(unknown)}, which the installed "
            f"anthropic SDK's AsyncMessages.create() does not accept. Route it through "
            f"extra_body, or drop it from _CAPABILITIES."
        )


def test_a_client_side_TypeError_is_permanent_not_transient():
    """The failure that cost four days. A kwarg the installed SDK does not
    accept raises before the request is sent, so retrying cannot clear it --
    it has to trip the breaker, not look like a rate limit."""
    from smartboi.llm import permanent_failure_reason

    exc = TypeError("AsyncMessages.create() got an unexpected keyword argument 'temperature'")
    reason = permanent_failure_reason(exc)
    assert reason, "a client-side TypeError must be classified permanent"
    assert "anthropic SDK" in reason
    assert "temperature" in reason


def test_ordinary_transient_errors_are_still_transient():
    """The breaker must not swallow a rate limit or a network blip: those DO
    clear on retry, and halting the engine on one would be the worse bug."""
    from smartboi.llm import permanent_failure_reason

    for exc in (
        RuntimeError("rate_limit_error: too many requests"),
        ConnectionError("connection reset by peer"),
        TimeoutError("request timed out"),
        Exception("overloaded_error"),
    ):
        assert permanent_failure_reason(exc) == "", exc
