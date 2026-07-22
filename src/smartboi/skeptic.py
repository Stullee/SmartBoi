"""Adversarial second pass: given a proposed dossier update, tries to
refute it before it's allowed to count -- see README point 5 ("make the LLM
adversarial to itself"). Hallucinated or over-read theses are the main
failure mode of an LLM-driven strategy; nothing merges into a dossier
without surviving this (see engine.py)."""
from __future__ import annotations

import logging

from anthropic import AsyncAnthropic

from smartboi.usage import UsageTracker

log = logging.getLogger(__name__)

_TOOL = {
    "name": "skeptic_verdict",
    "description": "Try to refute a proposed trading-thesis update. Default to refuting when unsure.",
    "input_schema": {
        "type": "object",
        "properties": {
            "refuted": {
                "type": "boolean",
                "description": (
                    "True if the evidence doesn't actually support the proposed direction/"
                    "magnitude, is too speculative, is old/rehashed, or the causal link to "
                    "this company is too weak/indirect to trust."
                ),
            },
            "reasoning": {"type": "string", "description": "One or two sentences."},
            "adjusted_confidence": {
                "type": "number", "minimum": 0, "maximum": 1,
                "description": "Your own confidence in the proposed direction, which may be lower than the original proposal's.",
            },
        },
        "required": ["refuted", "reasoning", "adjusted_confidence"],
    },
}

_SYSTEM_PROMPT = (
    "You are a skeptical second reviewer for an equity research thesis. Someone else has "
    "proposed a direction/magnitude/confidence for a piece of evidence. Try to refute it: "
    "is the evidence actually specific to this company, or generic filler? Is the causal "
    "chain plausible or a stretch (especially for evidence about a linked company rather "
    "than this one directly)? Is the language promotional/speculative rather than "
    "factual? Is old or already-priced-in news being treated as new? Default to "
    "refuted=true when genuinely unsure -- a missed real signal costs nothing, a false "
    "positive costs a bad trade."
)


class Skeptic:
    def __init__(self, api_key: str, model: str, usage: UsageTracker):
        self._client = AsyncAnthropic(api_key=api_key)
        self._model = model
        self._usage = usage

    async def review(self, evidence_text: str, proposed: dict) -> dict | None:
        """Returns the verdict dict, or None on a transient API failure OR
        an exhausted daily LLM call budget (usage.py) -- the caller
        (engine.py) then leaves the evidence unregistered so a later poll
        retries it, rather than permanently discarding evidence because of
        a network blip or a paused budget. Evidence still never merges
        without an actual verdict."""
        if not self._usage.budget_remaining():
            log.info("Daily LLM call budget reached -- deferring skeptic review.")
            return None
        prompt = (
            f"Proposed update: direction={proposed.get('direction')}, "
            f"magnitude={proposed.get('magnitude')}, confidence={proposed.get('confidence')}, "
            f"reasoning: {proposed.get('reasoning')}\n\n"
            f"Underlying evidence:\n{evidence_text}"
        )
        try:
            response = await self._client.messages.create(
                model=self._model,
                max_tokens=300,
                system=_SYSTEM_PROMPT,
                tools=[_TOOL],
                tool_choice={"type": "tool", "name": "skeptic_verdict"},
                messages=[{"role": "user", "content": prompt}],
            )
        except Exception as exc:  # noqa: BLE001 - fail safe: nothing merges without a real verdict
            log.warning("Skeptic review failed (%s) -- will retry this evidence on a later poll.", exc)
            return None
        self._usage.record(response.usage.input_tokens, response.usage.output_tokens)
        for block in response.content:
            if block.type == "tool_use":
                return block.input
        return {"refuted": True, "reasoning": "model returned no verdict", "adjusted_confidence": 0.0}

    async def aclose(self) -> None:
        await self._client.close()
