"""The agent configuration: a ReAct loop over our own MCP client.

Why the loop lives here rather than in the model harness
--------------------------------------------------------
The original design let Claude Code attach the MCP server itself. On this
machine that is blocked by a remote organization policy:

    Warning: MCP server blocked by enterprise policy: triage

Rather than work around the policy, the harness owns the MCP client. We
connect to the server with the official Python SDK over stdio, list its tools,
and drive the loop ourselves, using the provider purely as a text completion
endpoint. Three things fall out of that, and two of them are improvements:

* It is provider-agnostic. The same agent runs on the Claude Code CLI, on the
  Anthropic API, or on anything else that can complete a prompt.
* The loop is inspectable. Every step is recorded in the trace, which is what
  makes the failure analysis in the README possible.
* It is stateless per step: the transcript is re-sent each turn, so the agent
  arm pays for its own history. Native tool calling would amortise that
  through prompt caching. The cost column reports what this architecture
  actually costs, and the README says so rather than quietly comparing an
  agent against a baseline on different billing terms.
"""

from __future__ import annotations

import asyncio
import json
import os
from dataclasses import dataclass, field

from . import schema
from .providers import ModelResult

MAX_OBSERVATION_CHARS = 3500
DEFAULT_MAX_STEPS = 8


@dataclass
class Step:
    n: int
    tool: str | None
    arguments: dict
    observation: str
    raw_reply: str
    error: str | None = None


@dataclass
class AgentRun:
    verdict: schema.Verdict
    steps: list[Step] = field(default_factory=list)
    cost_usd: float = 0.0
    latency_s: float = 0.0
    input_tokens: int = 0
    output_tokens: int = 0
    error: str | None = None

    @property
    def tool_calls(self) -> int:
        return sum(1 for s in self.steps if s.tool)

    @property
    def tool_call_names(self) -> list[str]:
        return [s.tool for s in self.steps if s.tool]


def step_schema(tool_names: list[str]) -> dict:
    """One object per turn: either a tool call or the final answer."""
    return {
        "type": "object",
        "properties": {
            "action": {"type": "string", "enum": ["call_tool", "answer"]},
            "tool": {"type": "string", "enum": tool_names + [""]},
            "arguments": {"type": "object"},
            "root_cause_id": {
                "type": "string",
                "enum": schema.allowed_causes() + [""],
            },
            "reasoning": {"type": "string"},
            "confidence": {"type": "number", "minimum": 0.0, "maximum": 1.0},
            "evidence_used": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["action", "tool", "arguments", "root_cause_id",
                     "reasoning", "confidence", "evidence_used"],
        "additionalProperties": False,
    }


def tool_catalogue(tools) -> str:
    lines = []
    for tool in tools:
        params = (tool.input_schema or {}).get("properties", {})
        required = set((tool.input_schema or {}).get("required", []))
        arg_bits = []
        for name, spec in params.items():
            flag = "" if name in required else " (optional)"
            arg_bits.append(f"{name}: {spec.get('type', 'string')}{flag}")
        summary = (tool.description or "").strip().splitlines()
        lines.append(
            f"- {tool.name}({', '.join(arg_bits)})\n"
            f"    {summary[0] if summary else ''}"
        )
    return "\n".join(lines)


PROTOCOL = """\
Work one step at a time. On each turn reply with exactly one JSON object.

To use a tool:
  {"action": "call_tool", "tool": "<name>", "arguments": {...},
   "root_cause_id": "", "reasoning": "<why this call>", "confidence": 0,
   "evidence_used": []}

To finish:
  {"action": "answer", "tool": "", "arguments": {},
   "root_cause_id": "<one of the allowed causes>",
   "reasoning": "<what you concluded and what you ruled out>",
   "confidence": <0.0-1.0>, "evidence_used": ["<specific facts>"]}

You have at most $MAX_STEPS steps. Gather the evidence that separates the \
candidate causes before answering - several of them produce identical \
symptoms and differ only in the metrics."""


async def _call_tool(session, name: str, arguments: dict) -> tuple[str, str | None]:
    try:
        result = await session.call_tool(name, arguments)
    except Exception as exc:  # noqa: BLE001 - surfaced into the trace
        return "", f"{type(exc).__name__}: {exc}"
    parts = []
    for block in result.content:
        text = getattr(block, "text", None)
        if text:
            parts.append(text)
    observation = "\n".join(parts)
    if len(observation) > MAX_OBSERVATION_CHARS:
        observation = (
            observation[:MAX_OBSERVATION_CHARS]
            + f"\n... [truncated at {MAX_OBSERVATION_CHARS} characters]"
        )
    return observation, ("tool reported an error" if result.is_error else None)


def _render_transcript(ticket: str, catalogue: str, steps: list[Step],
                       max_steps: int) -> str:
    out = [
        ticket,
        "",
        "=== Tools available ===",
        catalogue,
        "",
        PROTOCOL.replace("$MAX_STEPS", str(max_steps)),
    ]
    if steps:
        out += ["", "=== Investigation so far ==="]
        for step in steps:
            if step.tool:
                out.append(f"[{step.n}] {step.tool}({json.dumps(step.arguments)})")
                if step.error:
                    out.append(f"    ERROR: {step.error}")
                out.append(f"    -> {step.observation}")
            else:
                # A step that never reached a tool. Say so plainly: rendering
                # it as `None({})` reads like a tool that returned nothing,
                # and the model will try to interpret the empty result
                # instead of correcting the malformed call.
                out.append(f"[{step.n}] no tool was called")
                out.append(f"    ERROR: {step.error}")
                out.append("    -> nothing was retrieved; this step was wasted")
    remaining = max_steps - len(steps)
    out += ["", f"Steps remaining: {remaining}."]
    if remaining <= 1:
        out.append("This is your last step. Answer now with your best verdict.")
    out.append("Reply with one JSON object.")
    return "\n".join(out)


async def _run(provider, system: str, ticket: str, mcp_config: dict,
               max_steps: int) -> AgentRun:
    from mcp import ClientSession
    from mcp.client.stdio import StdioServerParameters, stdio_client

    server = next(iter(mcp_config["mcpServers"].values()))
    params = StdioServerParameters(
        command=server["command"],
        args=server.get("args", []),
        env={**os.environ, **server.get("env", {})},
    )

    run = AgentRun(verdict=schema.Verdict(
        root_cause_id=schema.UNKNOWN, reasoning="", confidence=0.0
    ))

    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()
            tools = (await session.list_tools()).tools
            names = [t.name for t in tools]
            catalogue = tool_catalogue(tools)
            sch = step_schema(names)

            for n in range(1, max_steps + 1):
                prompt = _render_transcript(ticket, catalogue, run.steps, max_steps)
                result: ModelResult = await asyncio.to_thread(
                    provider.complete, system, prompt, sch
                )
                run.cost_usd += result.cost_usd
                run.latency_s += result.latency_s
                run.input_tokens += result.input_tokens
                run.output_tokens += result.output_tokens

                if result.error:
                    run.error = result.error
                    break

                reply = _parse_step(result.text)
                if reply is None:
                    run.steps.append(Step(
                        n=n, tool=None, arguments={},
                        observation="", raw_reply=result.text,
                        error="could not parse the step as JSON",
                    ))
                    continue

                if reply.get("action") == "answer":
                    run.verdict = schema.parse_verdict(json.dumps(reply))
                    break

                tool = (reply.get("tool") or "").strip()
                args = reply.get("arguments") or {}
                if tool not in names:
                    run.steps.append(Step(
                        n=n, tool=None, arguments=args, observation="",
                        raw_reply=result.text,
                        error=f"no such tool: {tool!r}; available: {names}",
                    ))
                    continue

                observation, err = await _call_tool(session, tool, args)
                run.steps.append(Step(
                    n=n, tool=tool, arguments=args,
                    observation=observation or "(empty)",
                    raw_reply=result.text, error=err,
                ))
            else:
                # Ran out of steps without an answer.
                run.verdict = schema.Verdict(
                    root_cause_id=schema.UNKNOWN,
                    reasoning=f"[harness] exhausted {max_steps} steps without "
                              f"producing a verdict.",
                    confidence=0.0,
                )
    return run


def _parse_step(text: str) -> dict | None:
    import re
    candidate = (text or "").strip()
    if not candidate:
        return None
    fenced = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", candidate, re.DOTALL)
    if fenced:
        candidate = fenced.group(1)
    elif not candidate.startswith("{"):
        brace = re.search(r"\{.*\}", candidate, re.DOTALL)
        if not brace:
            return None
        candidate = brace.group(0)
    try:
        data = json.loads(candidate)
    except json.JSONDecodeError:
        return None
    return data if isinstance(data, dict) else None


def run_agent_loop(provider, system: str, ticket: str, mcp_config: dict,
                   max_steps: int = DEFAULT_MAX_STEPS) -> AgentRun:
    return asyncio.run(_run(provider, system, ticket, mcp_config, max_steps))
