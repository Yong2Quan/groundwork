"""MCP server exposing the incident corpus as four tools.

Run directly over stdio:

    python -m triage.mcp_server          # or: triage serve

Inspect it by hand with the MCP Inspector:

    triage inspect                       # prints the npx command

The tool surface is the interesting design decision in this project, so it is
worth stating what it is and is not:

* `get_metrics` returns a per-series summary, not 36 raw samples across 20
  series. Raw is available on request for named series. Returning everything
  costs roughly 15k tokens of mostly-baseline noise per call and crowds out
  the reasoning you are paying for.
* `get_metrics` refuses an ambiguous window instead of picking one. A service
  has several retained windows; guessing would hand the agent metrics from an
  unrelated outage and the eval would score reasoning done over the wrong
  evidence.
* No tool ranks, flags or highlights an anomaly. The agent has to read the
  numbers and decide. A tool that returned "the series that moved most" would
  be doing the diagnosis, and the eval would be measuring the tool.
* No tool can reach `ground_truth`. See `store.public_incident`, and
  `tests/test_mcp_server.py::test_no_tool_response_contains_a_ground_truth_label`.

Argument descriptions live in `Field(...)` rather than in the docstring, so
they end up in each tool's JSON schema where the model actually consumes
them. A docstring `Args:` section reads well to a human and does not.
"""

from __future__ import annotations

import json
from typing import Annotated

from pydantic import Field

from . import retrieval, store

try:
    # mcp 2.x. In 1.x this class was called FastMCP and lived in
    # mcp.server.fastmcp; the decorator and run() surface are otherwise the
    # same, so only this import changes between the two.
    from mcp.server.mcpserver import MCPServer
except ImportError as exc:  # pragma: no cover
    raise SystemExit(
        "The MCP server needs the official SDK (v2 or later). Install it with:\n"
        "    pip install -e .[mcp]"
    ) from exc

mcp = MCPServer("triage")
_retriever = retrieval.BM25Retriever()


@mcp.tool()
def search_runbooks(
    query: Annotated[str, Field(description=(
        "Free text describing the symptom, for example 'latency spike with "
        "normal query times' or 'pods restarting with exit code 137'."
    ))],
    limit: Annotated[int, Field(description=(
        "How many runbook sections to return. Defaults to 3, maximum 8."
    ))] = 3,
) -> str:
    """Search the operational runbooks for sections matching a symptom.

    Runbooks describe how to tell similar-looking causes apart. They do not
    contain any data about this specific incident.
    """
    hits = _retriever.search(query, k=max(1, min(limit, 8)))
    if not hits:
        return "No runbook section matched that query."
    return "\n\n---\n\n".join(
        f"{chunk.render()}\n(relevance {score:.2f})" for chunk, score in hits
    )


@mcp.tool()
def get_ticket(
    incident_id: Annotated[str, Field(description=(
        "The incident identifier, for example 'INC-020'."
    ))],
) -> str:
    """Fetch the full incident ticket, including its event timeline.

    The timeline carries deploy events, which are often the only way to tell
    a release regression apart from a configuration change. It also carries
    the metric window you need for get_metrics.
    """
    try:
        incident = store.public_incident(incident_id)
    except KeyError:
        return f"No such incident: {incident_id}"
    return json.dumps(incident, indent=2)


@mcp.tool()
def get_metrics(
    service: Annotated[str, Field(description=(
        "Service name, for example 'checkout-api'."
    ))],
    window_start: Annotated[str, Field(description=(
        "ISO-8601 window start, for example '2026-03-08T10:00:00Z'. Required "
        "in practice: a service has several retained windows and this call "
        "refuses to guess between them. The window is on the ticket under "
        "'metric_window'."
    ))] = "",
    window_end: Annotated[str, Field(description=(
        "ISO-8601 window end, for example '2026-03-08T12:55:00Z'."
    ))] = "",
    series: Annotated[str, Field(description=(
        "Optional comma-separated series names to return raw per-sample "
        "values for, for example "
        "'db_active_connections,db_query_time_p99_ms'. Omit to get summaries "
        "only."
    ))] = "",
) -> str:
    """Fetch metric series for a service over a time window.

    Returns, for every series, its average before the incident, its average
    during, and the window peak and minimum. Pass `series` to also get raw
    per-sample values for specific series.
    """
    try:
        metrics = store.metrics_for_service_window(
            service, window_start or None, window_end or None
        )
    except store.AmbiguousWindow as exc:
        # Tell the caller how to fix the call rather than dead-ending it. A
        # real metrics API would report its retention range the same way.
        return json.dumps({
            "error": str(exc),
            "retained_windows": exc.available,
        }, indent=2)

    payload: dict = {
        "service": metrics["service"],
        "window_start": metrics["window_start"],
        "window_end": metrics["window_end"],
        "interval_minutes": metrics["interval_minutes"],
        "summary": store.summarise_series(metrics),
    }

    wanted = [s.strip() for s in series.split(",") if s.strip()]
    if wanted:
        raw, unknown = {}, []
        for name in wanted:
            if name in metrics["series"]:
                raw[name] = metrics["series"][name]
            else:
                unknown.append(name)
        payload["timestamps"] = metrics["timestamps"]
        payload["raw"] = raw
        if unknown:
            payload["unknown_series"] = unknown
            payload["available_series"] = sorted(metrics["series"])

    return json.dumps(payload, indent=2)


@mcp.tool()
def find_similar_incidents(
    symptom: Annotated[str, Field(description=(
        "Free-text description of what is happening now."
    ))],
    service: Annotated[str, Field(description=(
        "Optional service name to restrict the search to."
    ))] = "",
    limit: Annotated[int, Field(description=(
        "How many past incidents to return. Defaults to 3, maximum 6."
    ))] = 3,
) -> str:
    """Search previously resolved incidents for similar symptoms.

    Returns past incidents with their confirmed root cause and resolution.
    Symptoms that look alike frequently had different root causes, so treat
    these as candidates to check against the evidence, not as an answer.
    """
    query = set(retrieval.tokenize(f"{symptom} {service}"))
    scored = []
    for past in store.past_incidents():
        if service and past["service"] != service:
            continue
        doc = set(retrieval.tokenize(f"{past['summary']} {past['service']}"))
        overlap = len(query & doc)
        if overlap:
            scored.append((overlap / (len(query | doc) or 1), past))
    scored.sort(key=lambda pair: pair[0], reverse=True)
    if not scored:
        return "No similar past incident found."
    return json.dumps([
        {
            "id": past["id"],
            "service": past["service"],
            "summary": past["summary"],
            "confirmed_root_cause": past["root_cause_id"],
            "resolution": past["resolution"],
            "similarity": round(score, 3),
        }
        for score, past in scored[:max(1, min(limit, 6))]
    ], indent=2)


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
