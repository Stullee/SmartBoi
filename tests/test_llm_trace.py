"""What each pass was SHOWN, not just what it decided.

The system records every judgement -- direction, magnitude, the skeptic's
note, the synthesis verdict and its flags -- and recorded nothing about the
prompt that produced them. That is the wrong half when a judgement looks
wrong: when fact_key came back empty on 970 consecutive items, the stored
output could not distinguish a model that never emitted the field from a
pipeline that dropped it. It was the pipeline, and the other one was assumed
first."""
from __future__ import annotations

import json

from smartboi.llm import LLMTrace


def _rows(path):
    return [json.loads(line) for line in path.read_text().splitlines()]


def test_a_traced_call_records_the_prompt_and_the_tool_call(tmp_path):
    path = tmp_path / "llm_trace.jsonl"
    LLMTrace(path).record(
        "synthesis", "claude-opus-5", "AOSL",
        "Company: AOSL\nPRICE (daily closes...)", {"already_priced_in": False},
        1200, 300, system="you are...",
    )
    row, = _rows(path)
    assert row["category"] == "synthesis"
    assert row["symbol"] == "AOSL"
    assert "PRICE (daily closes" in row["prompt"], "the prompt is the whole point"
    assert row["response"] == {"already_priced_in": False}
    assert row["input_tokens"] == 1200 and row["output_tokens"] == 300
    # The system prompt is a per-pass constant that would dominate the file;
    # its LENGTH is enough to notice a change, and the text lives in source.
    assert row["system_chars"] == len("you are...")
    assert "system" not in row


def test_a_call_that_returned_no_tool_use_is_recorded_as_such(tmp_path):
    """None is the interesting case, not a gap -- it means the model emitted
    no tool call at all, which is invisible in every other log."""
    path = tmp_path / "llm_trace.jsonl"
    LLMTrace(path).record("dossier", "haiku", "BWEN", "prompt", None)
    row, = _rows(path)
    assert row["response"] is None


def test_the_high_volume_passes_are_sampled_and_synthesis_is_not(tmp_path):
    """~700 per-item calls a day against ~30 synthesis calls. Tracing both in
    full would make the trace the largest thing in a diagnostics bundle by an
    order of magnitude, for the pass that matters least per call."""
    path = tmp_path / "llm_trace.jsonl"
    trace = LLMTrace(path, sample={"dossier": 20, "synthesis": 1})
    for i in range(60):
        trace.record("dossier", "haiku", f"S{i}", "p", {})
    for i in range(3):
        trace.record("synthesis", "opus", f"T{i}", "p", {})
    rows = _rows(path)
    assert sum(1 for r in rows if r["category"] == "dossier") == 3    # 60 / 20
    assert sum(1 for r in rows if r["category"] == "synthesis") == 3  # all of them


def test_sampling_is_shared_across_passes_that_bill_to_one_category(tmp_path):
    """The updater and the skeptic both bill to `dossier` and share one
    tracer, so 1-in-N is 1-in-N of their COMBINED calls. A tracer per client
    would quietly double the intended volume."""
    path = tmp_path / "llm_trace.jsonl"
    trace = LLMTrace(path, sample={"dossier": 4})
    for i in range(8):
        trace.record("dossier", "haiku", "updater", "p", {})   # pass A
        trace.record("dossier", "haiku", "skeptic", "p", {})   # pass B
    assert len(_rows(path)) == 4   # 16 calls / 4, not 8


def test_the_trace_is_bounded_on_disk(tmp_path):
    """Prompts carry the whole evidence digest. Unbounded, this is the file
    that fills the disk."""
    path = tmp_path / "llm_trace.jsonl"
    trace = LLMTrace(path, max_bytes=2000)
    for i in range(200):
        trace.record("synthesis", "opus", f"S{i}", "x" * 200, {})
    assert path.stat().st_size < 2000 + 1000, "current generation exceeded its cap"
    assert path.with_suffix(".jsonl.1").exists(), "rotated generation missing"
    # One backup only -- two generations, not an unbounded chain.
    assert not path.with_suffix(".jsonl.2").exists()


def test_tracing_never_breaks_ingestion(tmp_path):
    """A diagnostic that can kill the poll loop is not a diagnostic. Every
    failure here is swallowed."""
    trace = LLMTrace(tmp_path / "nope" / "deeper" / "x.jsonl")
    trace.record("dossier", "haiku", "S", "p", {})   # unwritable parent chain
    # An unserialisable response must not raise either.
    LLMTrace(tmp_path / "t.jsonl").record("dossier", "haiku", "S", "p", object())


def test_disabled_writes_nothing(tmp_path):
    path = tmp_path / "llm_trace.jsonl"
    LLMTrace(path, enabled=False).record("synthesis", "opus", "S", "p", {})
    assert not path.exists()
