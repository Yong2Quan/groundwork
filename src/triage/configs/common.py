"""Prompt construction shared by all three configurations.

The system prompt is assembled from one fixed body plus a single clause
describing what the configuration can reach. Everything else - the role, the
taxonomy, the output contract, the instruction to abstain - is byte-identical
across the three arms.

That constraint is the whole experiment. If the arms had separately tuned
prompts, the results table would be comparing prompt engineering, and any
claim about RAG versus agents would be unsupported.
"""

from __future__ import annotations

from .. import schema

_SYSTEM_TEMPLATE = """\
You are an incident responder performing root-cause triage. You are given one \
incident and must identify its single most likely root cause.

{access_clause}

Choose exactly one root cause from this closed list:

{causes}

Rules:
- Pick the cause the evidence actually supports, not the one that fits the \
symptom most generally. Several causes in the list produce identical symptoms \
and are separated only by specific evidence.
- Say what you ruled out and why, not only what you chose.
- If the evidence available to you does not single out one cause, answer \
"unknown". An honest "unknown" is more useful than a confident guess.

Respond with a single JSON object matching the required schema, and nothing \
else."""

ACCESS_CLAUSES = {
    "baseline": (
        "You have the incident ticket and nothing else. No runbooks, no "
        "metrics, no history. Reason from the ticket alone."
    ),
    "rag": (
        "You have the incident ticket and the runbook sections retrieved "
        "below. You cannot query metrics or incident history."
    ),
    "agent": (
        "You have the incident ticket and a set of tools for investigating "
        "it: runbook search, full ticket retrieval, metric time series, and "
        "search over previously resolved incidents. Use them. The ticket "
        "alone is usually not enough to separate the candidate causes, and "
        "the metric window you need is on the ticket under 'metric_window'."
    ),
}


def system_prompt(arm: str) -> str:
    return _SYSTEM_TEMPLATE.format(
        access_clause=ACCESS_CLAUSES[arm],
        causes=schema.causes_block(),
    )


def render_ticket(incident: dict, include_timeline: bool = False) -> str:
    """The ticket as presented to a configuration.

    `timeline` is withheld from the non-agent arms on purpose: it is what
    `get_ticket` returns, and handing it over for free would remove one of the
    things the agent arm has to go and fetch.
    """
    lines = [
        f"Incident: {incident['id']}",
        f"Service: {incident['service']}",
        f"Severity: {incident['severity']}",
        f"Opened: {incident['opened_at']}",
        f"Title: {incident['title']}",
        "",
        "Description:",
        incident["description"],
    ]
    if include_timeline:
        lines += ["", "Timeline:"]
        lines += [
            f"  {e['at']}  [{e['source']}] {e['text']}"
            for e in incident.get("timeline", [])
        ]
    window = incident.get("metric_window") or {}
    if window:
        lines += [
            "",
            f"Metric window available: {window.get('start')} to {window.get('end')}",
        ]
    return "\n".join(lines)


def search_query(incident: dict) -> str:
    """The retrieval query used by the RAG arm and by `triage analyze`."""
    return f"{incident['title']} {incident['description']}"
