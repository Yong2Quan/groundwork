"""MCP server behaviour, exercised through the server's own tool dispatch.

These complement the MCP Inspector (`triage inspect`): the Inspector is for
looking at the server by hand, these keep it honest in CI.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from triage import mcp_server, store

EXPECTED_TOOLS = {
    "search_runbooks", "get_ticket", "get_metrics", "find_similar_incidents",
}


def call(name: str, arguments: dict) -> str:
    async def go():
        result = await mcp_server.mcp.call_tool(name, arguments)
        return "\n".join(
            block.text for block in result.content
            if getattr(block, "text", None)
        )
    return asyncio.run(go())


def test_exactly_four_tools_are_exposed():
    tools = asyncio.run(mcp_server.mcp.list_tools())
    assert {t.name for t in tools} == EXPECTED_TOOLS


def test_every_tool_documents_its_arguments():
    """An undocumented argument is one the model will get wrong."""
    for tool in asyncio.run(mcp_server.mcp.list_tools()):
        assert tool.description, f"{tool.name} has no description"
        properties = (tool.input_schema or {}).get("properties", {})
        assert properties, f"{tool.name} exposes no arguments"
        for name, spec in properties.items():
            assert spec.get("description"), (
                f"{tool.name}.{name} has no description"
            )


def test_get_ticket_returns_the_public_record():
    payload = json.loads(call("get_ticket", {"incident_id": "INC-020"}))
    assert payload["id"] == "INC-020"
    assert payload["service"] == "checkout-api"
    assert payload["timeline"]
    assert "ground_truth" not in payload


def test_get_ticket_rejects_an_unknown_id():
    assert "No such incident" in call("get_ticket", {"incident_id": "INC-999"})


def test_no_tool_response_contains_a_ground_truth_label():
    """The eval is void if any tool hands over the answer."""
    for incident_id in store.incident_ids():
        incident = store.public_incident(incident_id)
        truth = store.ground_truth_for(incident_id)["root_cause_id"]
        window = incident["metric_window"]

        responses = [
            call("get_ticket", {"incident_id": incident_id}),
            call("get_metrics", {
                "service": incident["service"],
                "window_start": window["start"],
                "window_end": window["end"],
            }),
            call("search_runbooks", {"query": incident["description"]}),
        ]
        for response in responses:
            assert truth not in response, (
                f"{incident_id}: a tool response contained the label {truth!r}"
            )


def test_get_metrics_refuses_rather_than_guessing_a_window():
    """checkout-api has five retained windows; picking one would hand the
    agent metrics from an unrelated outage."""
    payload = json.loads(call("get_metrics", {"service": "checkout-api"}))
    assert "error" in payload
    assert len(payload["retained_windows"]) == 5


def test_get_metrics_returns_the_right_window():
    window = store.public_incident("INC-020")["metric_window"]
    payload = json.loads(call("get_metrics", {
        "service": "checkout-api",
        "window_start": window["start"],
        "window_end": window["end"],
        "series": "db_active_connections",
    }))
    assert payload["window_start"] == window["start"]
    assert len(payload["summary"]) == 20
    assert len(payload["raw"]["db_active_connections"]) == 36
    assert payload["summary"]["db_active_connections"]["incident_avg"] > 95


def test_get_metrics_reports_unknown_series_instead_of_failing():
    window = store.public_incident("INC-020")["metric_window"]
    payload = json.loads(call("get_metrics", {
        "service": "checkout-api",
        "window_start": window["start"],
        "window_end": window["end"],
        "series": "not_a_real_series",
    }))
    assert payload["unknown_series"] == ["not_a_real_series"]
    assert "available_series" in payload


def test_search_runbooks_finds_the_pool_exhaustion_section():
    response = call("search_runbooks", {
        "query": "latency high but database query times normal", "limit": 3,
    })
    assert "RB-003" in response or "pool" in response.lower()


def test_find_similar_incidents_returns_differing_causes():
    """If every neighbour shared a cause, similarity search would be an
    answer key rather than a hint, and the agent arm would look good for a
    reason that would not survive a real incident database."""
    payload = json.loads(call("find_similar_incidents", {
        "symptom": "latency degraded with timeouts and errors",
    }))
    assert len(payload) >= 2
    assert len({p["confirmed_root_cause"] for p in payload}) >= 2


def test_find_similar_incidents_handles_no_match():
    response = call("find_similar_incidents", {"symptom": "zzzz qqqq"})
    assert "No similar past incident" in response or response.startswith("[")
