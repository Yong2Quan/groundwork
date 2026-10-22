"""The output contract every configuration must satisfy.

All three configurations answer in the same shape, against the same closed
list of root causes. That is what makes scoring an exact string comparison
instead of a judgement call, and it removes "the agent wrote a longer answer
so it looked better" from the results.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

from . import store

UNKNOWN = "unknown"


class Verdict(BaseModel):
    """A configuration's answer for one incident."""

    root_cause_id: str = Field(
        description="The single best-supported root cause, from the allowed list."
    )
    reasoning: str = Field(
        description="Two or three sentences: the evidence that rules the "
                    "alternatives out, not just the one that fits."
    )
    confidence: float = Field(
        ge=0.0, le=1.0,
        description="0.0 to 1.0. Be honest; a low score here is more useful "
                    "than a confident wrong answer.",
    )
    evidence_used: list[str] = Field(
        default_factory=list,
        description="Specific facts relied on, e.g. a metric series name and "
                    "what it did, or a runbook section id.",
    )


def allowed_causes() -> list[str]:
    """The closed answer set, plus an explicit escape hatch.

    `unknown` matters. Without it a model that cannot tell two causes apart
    will guess, and the accuracy number stops distinguishing "knew it" from
    "got lucky". With it, we can report an abstention rate alongside accuracy.
    """
    return sorted(store.taxonomy()) + [UNKNOWN]


def json_schema() -> dict[str, Any]:
    """JSON Schema for the verdict, for `--json-schema` / `output_config`."""
    return {
        "type": "object",
        "properties": {
            "root_cause_id": {
                "type": "string",
                "enum": allowed_causes(),
                "description": "The single best-supported root cause.",
            },
            "reasoning": {
                "type": "string",
                "description": "Two or three sentences, including what you "
                               "ruled out and why.",
            },
            "confidence": {
                "type": "number",
                "minimum": 0.0,
                "maximum": 1.0,
            },
            "evidence_used": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Specific facts relied on.",
            },
        },
        "required": ["root_cause_id", "reasoning", "confidence", "evidence_used"],
        "additionalProperties": False,
    }


def causes_block() -> str:
    """The taxonomy rendered for a prompt."""
    lines = [f"  {cause}: {desc}" for cause, desc in sorted(store.taxonomy().items())]
    lines.append(f"  {UNKNOWN}: The evidence available does not identify a "
                 f"single cause.")
    return "\n".join(lines)


def parse_verdict(text: str) -> Verdict:
    """Parse a model response into a Verdict, tolerantly.

    Structured output should make this trivial, but a model can still return
    prose around the JSON, or stop early. A parse failure is a real result -
    it becomes an `unknown` verdict and is counted as a failure, never
    retried silently, because silent retries would hide a reliability
    difference between the configurations.
    """
    import json
    import re

    candidate = text.strip()
    if not candidate:
        return _unparseable("empty response")

    # Fenced block, then bare object, then the whole string.
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", candidate, re.DOTALL)
    if fenced:
        candidate = fenced.group(1)
    elif not candidate.startswith("{"):
        brace = re.search(r"\{.*\}", candidate, re.DOTALL)
        if brace:
            candidate = brace.group(0)

    try:
        data = json.loads(candidate)
    except json.JSONDecodeError as exc:
        return _unparseable(f"invalid JSON: {exc}")

    if not isinstance(data, dict):
        return _unparseable("response was not a JSON object")

    cause = str(data.get("root_cause_id", "")).strip()
    if cause not in allowed_causes():
        return _unparseable(f"root_cause_id {cause!r} is not in the allowed list")

    try:
        confidence = float(data.get("confidence", 0.0))
    except (TypeError, ValueError):
        confidence = 0.0

    evidence = data.get("evidence_used") or []
    if not isinstance(evidence, list):
        evidence = [str(evidence)]

    return Verdict(
        root_cause_id=cause,
        reasoning=str(data.get("reasoning", "")),
        confidence=max(0.0, min(1.0, confidence)),
        evidence_used=[str(e) for e in evidence],
    )


def _unparseable(why: str) -> Verdict:
    return Verdict(
        root_cause_id=UNKNOWN,
        reasoning=f"[harness] could not parse a verdict: {why}",
        confidence=0.0,
        evidence_used=[],
    )
