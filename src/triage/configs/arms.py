"""The three configurations under test, plus the rules control.

Each one takes a provider and an incident and returns a Verdict alongside the
raw ModelResult, so the harness can score the answer and record what it cost.
"""

from __future__ import annotations

import os
import sys
from dataclasses import dataclass
from pathlib import Path

from .. import agent, retrieval, schema
from ..providers import ModelResult, Provider
from . import common

REPO_ROOT = Path(__file__).resolve().parents[3]

MCP_TOOLS = [
    "mcp__triage__search_runbooks",
    "mcp__triage__get_ticket",
    "mcp__triage__get_metrics",
    "mcp__triage__find_similar_incidents",
]


@dataclass
class ArmOutput:
    verdict: schema.Verdict
    result: ModelResult


def mcp_config() -> dict:
    """Launch our MCP server over stdio, using this interpreter.

    PYTHONPATH is set explicitly: the server is launched by the model harness
    from an arbitrary working directory, so `src` being importable cannot be
    assumed.
    """
    return {
        "mcpServers": {
            "triage": {
                "command": sys.executable,
                "args": ["-m", "triage.mcp_server"],
                "env": {
                    "PYTHONPATH": str(REPO_ROOT / "src"),
                    "PYTHONIOENCODING": "utf-8",
                },
            }
        }
    }


# --------------------------------------------------------------------------
# (a) The model alone
# --------------------------------------------------------------------------

def run_baseline(provider: Provider, incident: dict) -> ArmOutput:
    prompt = common.render_ticket(incident)
    result = provider.complete(
        system=common.system_prompt("baseline"),
        prompt=prompt,
        json_schema=schema.json_schema(),
    )
    return ArmOutput(schema.parse_verdict(result.text), result)


# --------------------------------------------------------------------------
# (b) Retrieval over the runbooks, no tools
# --------------------------------------------------------------------------

_retriever: retrieval.BM25Retriever | None = None


def _get_retriever() -> retrieval.BM25Retriever:
    global _retriever
    if _retriever is None:
        _retriever = retrieval.BM25Retriever()
    return _retriever


def run_rag(provider: Provider, incident: dict, top_k: int = 4) -> ArmOutput:
    hits = _get_retriever().search(common.search_query(incident), k=top_k)
    if hits:
        context = "\n\n---\n\n".join(chunk.render() for chunk, _ in hits)
    else:
        context = "(no runbook section matched this incident)"

    prompt = (
        f"{common.render_ticket(incident)}\n\n"
        f"=== Retrieved runbook sections (top {len(hits)}) ===\n\n{context}"
    )
    result = provider.complete(
        system=common.system_prompt("rag"),
        prompt=prompt,
        json_schema=schema.json_schema(),
    )
    output = ArmOutput(schema.parse_verdict(result.text), result)
    # Recorded so a wrong answer can be attributed to retrieval or to
    # reasoning, which is the first question anyone asks of a RAG result.
    output.result.raw["retrieved"] = [chunk.id for chunk, _ in hits]
    return output


# --------------------------------------------------------------------------
# (c) Agent looping over the MCP tools
# --------------------------------------------------------------------------

def run_agent(provider: Provider, incident: dict,
              max_steps: int = agent.DEFAULT_MAX_STEPS) -> ArmOutput:
    run = agent.run_agent_loop(
        provider=provider,
        system=common.system_prompt("agent"),
        ticket=common.render_ticket(incident),
        mcp_config=mcp_config(),
        max_steps=max_steps,
    )
    result = ModelResult(
        text="",
        latency_s=round(run.latency_s, 3),
        input_tokens=run.input_tokens,
        output_tokens=run.output_tokens,
        cost_usd=round(run.cost_usd, 6),
        num_turns=len(run.steps) + 1,
        tool_calls=run.tool_calls,
        tool_call_names=run.tool_call_names,
        error=run.error,
        raw={
            "steps": [
                {
                    "n": s.n,
                    "tool": s.tool,
                    "arguments": s.arguments,
                    "error": s.error,
                    "observation_chars": len(s.observation),
                }
                for s in run.steps
            ],
        },
    )
    return ArmOutput(run.verdict, result)


ARMS = {
    "baseline": run_baseline,
    "rag": run_rag,
    "agent": run_agent,
}

ARM_LABELS = {
    "baseline": "LLM alone",
    "rag": "RAG over runbooks",
    "agent": "Agent over MCP tools",
}
