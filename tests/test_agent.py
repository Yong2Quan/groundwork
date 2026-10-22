"""The agent loop, driven by a scripted provider instead of a model.

No network, no model, no cost. The point is to pin the loop's control flow:
tool dispatch, bad tool names, unparseable replies, and the step ceiling.
Those are the paths that turn a working agent into a silently bad one.
"""

from __future__ import annotations

import json

from triage import agent, schema
from triage.configs import arms, common
from triage.providers.base import ModelResult


class ScriptedProvider:
    """Replays a fixed list of replies, one per call."""

    name = "scripted"
    model = "scripted"

    def __init__(self, replies: list[str]):
        self.replies = list(replies)
        self.prompts: list[str] = []

    def complete(self, system, prompt, json_schema=None) -> ModelResult:
        self.prompts.append(prompt)
        reply = self.replies.pop(0) if self.replies else ""
        return ModelResult(
            text=reply, latency_s=0.01, input_tokens=100,
            output_tokens=20, cost_usd=0.001,
        )


def tool_call(tool: str, arguments: dict) -> str:
    return json.dumps({
        "action": "call_tool", "tool": tool, "arguments": arguments,
        "root_cause_id": "", "reasoning": "looking", "confidence": 0,
        "evidence_used": [],
    })


def answer(cause: str, confidence: float = 0.9) -> str:
    return json.dumps({
        "action": "answer", "tool": "", "arguments": {},
        "root_cause_id": cause, "reasoning": "because the evidence says so",
        "confidence": confidence, "evidence_used": ["db_active_connections"],
    })


def run(replies: list[str], incident_id: str = "INC-020", max_steps: int = 8):
    from triage import store
    provider = ScriptedProvider(replies)
    incident = store.public_incident(incident_id)
    result = agent.run_agent_loop(
        provider=provider,
        system=common.system_prompt("agent"),
        ticket=common.render_ticket(incident),
        mcp_config=arms.mcp_config(),
        max_steps=max_steps,
    )
    return result, provider


def test_answering_immediately_uses_no_tools():
    result, _ = run([answer("connection_pool_exhaustion")])
    assert result.verdict.root_cause_id == "connection_pool_exhaustion"
    assert result.tool_calls == 0


def test_a_tool_call_reaches_the_server_and_comes_back():
    window = {"service": "checkout-api",
              "window_start": "2026-03-08T10:00:00Z",
              "window_end": "2026-03-08T12:55:00Z"}
    result, _ = run([
        tool_call("get_metrics", window),
        answer("connection_pool_exhaustion"),
    ])
    assert result.tool_calls == 1
    assert result.tool_call_names == ["get_metrics"]
    step = result.steps[0]
    assert step.error is None
    assert "db_active_connections" in step.observation


def test_observations_are_fed_back_into_the_next_prompt():
    """A loop that forgets its observations is an expensive single call."""
    result, provider = run([
        tool_call("get_ticket", {"incident_id": "INC-020"}),
        answer("connection_pool_exhaustion"),
    ])
    assert len(provider.prompts) == 2
    assert "Investigation so far" in provider.prompts[1]
    assert "get_ticket" in provider.prompts[1]


def test_an_unknown_tool_is_reported_and_the_loop_continues():
    result, _ = run([
        tool_call("delete_production", {}),
        answer("disk_full"),
    ])
    assert result.tool_calls == 0
    assert "no such tool" in result.steps[0].error
    assert result.verdict.root_cause_id == "disk_full"


def test_an_unparseable_reply_is_recorded_not_crashed_on():
    result, _ = run(["I'm not going to answer in JSON, sorry.",
                     answer("memory_leak_oom")])
    assert result.steps[0].error == "could not parse the step as JSON"
    assert result.verdict.root_cause_id == "memory_leak_oom"


def test_running_out_of_steps_yields_unknown_not_a_guess():
    replies = [tool_call("get_ticket", {"incident_id": "INC-020"})] * 3
    result, _ = run(replies, max_steps=3)
    assert result.verdict.root_cause_id == schema.UNKNOWN
    assert "exhausted" in result.verdict.reasoning


def test_cost_and_tokens_accumulate_across_steps():
    result, _ = run([
        tool_call("get_ticket", {"incident_id": "INC-020"}),
        tool_call("search_runbooks", {"query": "pool"}),
        answer("connection_pool_exhaustion"),
    ])
    assert result.tool_calls == 2
    assert result.cost_usd == 0.003
    assert result.input_tokens == 300
    assert result.output_tokens == 60


def test_a_failing_tool_call_does_not_end_the_run():
    result, _ = run([
        tool_call("get_metrics", {"service": "checkout-api"}),  # no window
        answer("connection_pool_exhaustion"),
    ])
    assert result.tool_calls == 1
    assert "error" in result.steps[0].observation
    assert result.verdict.root_cause_id == "connection_pool_exhaustion"


def test_the_last_step_warns_the_model_to_answer():
    replies = [tool_call("get_ticket", {"incident_id": "INC-020"}),
               answer("connection_pool_exhaustion")]
    _, provider = run(replies, max_steps=2)
    assert "last step" in provider.prompts[-1]
