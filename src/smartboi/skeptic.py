"""Adversarial second pass: given a proposed dossier update, tries to
refute it before it's allowed to count -- see README point 5 ("make the LLM
adversarial to itself"). Hallucinated or over-read theses are the main
failure mode of an LLM-driven strategy; nothing merges into a dossier
without surviving this (see engine.py).

Calibrated deliberately asymmetrically between direct and propagated
evidence (see _SYSTEM_PROMPT) -- an earlier version held both to the same
bar ("is the causal link too weak/indirect to trust"), which in practice
refuted essentially 100% of propagated evidence: "indirect, not yet
confirmed by the target's own numbers" is true of ALL propagated evidence
by definition, since the entire point of this strategy (README point 2) is
trading the lag before the market connects a linked company's news to a
second-order name. Demanding that confirmation up front before evidence
counts would mean the propagation mechanism could structurally never
produce a signal, regardless of how good the underlying pipeline is.

Also carries `adjusted_magnitude` alongside `adjusted_confidence` -- a
second real-world run showed the recalibrated skeptic reasoning correctly
(origin fact real, but proposed size too large for how loosely the
relationship is disclosed) and then still refusing outright, because a
too-large MAGNITUDE had no accept-but-shrink option, only accept-as-is or
refuse. That forced good-but-oversized propagated evidence to be thrown
away rather than scaled down and counted for what it's actually worth."""
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
                    "True if the evidence is generic/non-specific filler, promotional or "
                    "speculative language, old/already-priced-in news being treated as new, "
                    "a mechanical/non-discretionary event with no real signal (e.g. tax-"
                    "withholding RSU vesting, a routine pre-planned 10b5-1 sale), or reasoning "
                    "that isn't actually specific to this company. Being propagated (about a "
                    "linked company rather than this one) or lacking confirmation that the "
                    "second-order effect has already occurred is NOT by itself a reason to "
                    "refute -- that is the normal, expected state of evidence this strategy is "
                    "built to act on before the market catches up. If the ONLY problem is that "
                    "the proposed magnitude/confidence is too large for how loosely the "
                    "disclosed relationship connects the two companies (the origin fact and "
                    "reasoning are otherwise fine), do NOT refute -- set refuted=false and "
                    "scale adjusted_magnitude/adjusted_confidence down instead."
                ),
            },
            "reasoning": {"type": "string", "description": "One or two sentences."},
            "adjusted_confidence": {
                "type": "number", "minimum": 0, "maximum": 1,
                "description": "Your own confidence in the proposed direction, which may be lower than the original proposal's.",
            },
            "adjusted_magnitude": {
                "type": "number", "minimum": 0, "maximum": 1,
                "description": (
                    "Your own view of the magnitude, which may be lower than the original proposal's. "
                    "Use this -- accepting with a SMALLER magnitude, not refuted=true -- when the origin "
                    "fact is real and the direction is right, but the proposed size is too large for how "
                    "loosely/genericly the disclosed relationship actually connects the two companies. "
                    "Refusing real-but-modest evidence outright throws away exactly the kind of small, "
                    "accumulating corroboration this strategy is built to combine over time; scaling it "
                    "down lets it count for what it's actually worth instead."
                ),
            },
        },
        "required": ["refuted", "reasoning", "adjusted_confidence", "adjusted_magnitude"],
    },
}

_SYSTEM_PROMPT = (
    "You are a skeptical second reviewer for an equity research thesis. Someone else has "
    "proposed a direction/magnitude/confidence for a piece of evidence. Try to refute it -- "
    "but calibrate the bar differently depending on whether the evidence is DIRECT (about "
    "this company itself) or PROPAGATED (about a linked customer/supplier/competitor/"
    "regulator; you'll be told the disclosed relationship, already verified from a filing, "
    "when it applies).\n\n"
    "For DIRECT evidence, hold a high bar: mechanical/non-discretionary events (RSU vesting "
    "tax withholding, routine pre-planned 10b5-1 sales, option exercises, a governance change "
    "with no accompanying strategic or financial catalyst) carry near-zero signal regardless "
    "of size or dollar value -- refute these. Promotional/vague language and old or already-"
    "priced-in news being treated as new are refuted regardless of direct or propagated.\n\n"
    "For PROPAGATED evidence, remember why this exists: the market reprices a linked "
    "company's news within minutes but rarely connects it to THIS company for days or weeks "
    "-- capturing that lag is the entire strategy. 'The second-order effect hasn't shown up "
    "in this company's own numbers yet' is therefore the NORMAL, EXPECTED state of "
    "propagated evidence, not a reason to refute on its own -- do not refute merely because "
    "the causal chain is indirect or unconfirmed; that confirmation is what the lag is for. "
    "Instead judge propagated evidence on: (1) is the ORIGIN news itself a concrete, specific "
    "fact (an actual order, contract, capacity change, guidance revision, disclosed financial "
    "figure) rather than generic sector sentiment, momentum-chasing, or analyst commentary "
    "with no hard data -- refute this case, it has nothing real underneath it regardless of "
    "size; (2) is the proposed magnitude/confidence proportionate to how DIRECTLY the "
    "disclosed relationship connects the two companies (a named customer representing a "
    "stated revenue concentration deserves more weight than a vague thematic/sector-exposure "
    "framing) -- when given the relationship's own extracted confidence number, weigh your "
    "proportionality judgment against it directly rather than only re-inferring directness from "
    "the note's wording each time; when the origin fact IS real but the size is too large for how weak or "
    "generic the disclosed relationship actually is, do NOT refute this case: instead accept "
    "it (refuted=false) with adjusted_magnitude scaled down to something proportionate to the "
    "relationship's actual strength. A real fact filtered through a loose relationship is "
    "still worth something, just less than proposed -- it's exactly the kind of small, "
    "accumulating corroboration this strategy combines over time (see README point 3), and "
    "refusing it outright throws that away; (3) is the reasoning actually specific to the "
    "target company, or could it be pasted onto any company in the sector unchanged -- refute "
    "the latter, no amount of magnitude-scaling fixes reasoning that isn't really about this "
    "company.\n\n"
    "Default to refuted=true only when genuinely unsure after weighing the above -- a missed "
    "real signal costs nothing, a false positive costs a bad trade, but refuting every "
    "propagated item on principle means this strategy's core thesis never gets tested either "
    "way. When you're refuting SOLELY because of size/proportionality (point 2) rather than "
    "because the origin fact or reasoning itself is weak (points 1 and 3), prefer scaling "
    "adjusted_magnitude down over refusing outright."
)


class Skeptic:
    def __init__(self, api_key: str, model: str, usage: UsageTracker):
        self._client = AsyncAnthropic(api_key=api_key)
        self._model = model
        self._usage = usage

    async def review(
        self, evidence_text: str, proposed: dict, relationship_note: str = "",
        relationship_confidence: float | None = None,
    ) -> dict | None:
        """Returns the verdict dict, or None on a transient API failure OR
        an exhausted daily LLM call budget (usage.py) -- the caller
        (engine.py) then leaves the evidence unregistered so a later poll
        retries it, rather than permanently discarding evidence because of
        a network blip or a paused budget. Evidence still never merges
        without an actual verdict.

        `relationship_note` is the graph's own disclosed-relationship text
        (see graph.py), passed through UNFILTERED rather than relying on
        the dossier updater's paraphrase of it -- lets the skeptic judge
        how directly the relationship actually connects the two companies
        (see _SYSTEM_PROMPT point 2) from the original extracted text.
        `relationship_confidence` is that same edge's own numeric confidence
        (extraction time), given directly rather than left for the skeptic
        to re-infer proportionality purely from the wording of the note
        every time."""
        if not self._usage.budget_remaining():
            log.info("Daily LLM call budget reached -- deferring skeptic review.")
            return None
        confidence_suffix = (
            f" [relationship confidence: {relationship_confidence:.2f}]" if relationship_confidence is not None else ""
        )
        relationship_line = (
            f"Disclosed relationship (verified from a filing, not asserted by the proposer): "
            f"{relationship_note}{confidence_suffix}\n"
            if relationship_note
            else "This evidence is about the company directly (not propagated).\n"
        )
        prompt = (
            f"Proposed update: direction={proposed.get('direction')}, "
            f"magnitude={proposed.get('magnitude')}, confidence={proposed.get('confidence')}, "
            f"reasoning: {proposed.get('reasoning')}\n"
            f"{relationship_line}\n"
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
        return {"refuted": True, "reasoning": "model returned no verdict", "adjusted_confidence": 0.0, "adjusted_magnitude": 0.0}

    async def aclose(self) -> None:
        await self._client.close()
