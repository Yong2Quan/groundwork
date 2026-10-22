"""Provider interface shared by every configuration.

A provider answers two questions: "complete this prompt" and "run this prompt
with these MCP tools available". Everything measured by the eval - latency,
tool calls, tokens, cost - comes back on ModelResult, so the harness never has
to know which provider produced it.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable


@dataclass
class ModelResult:
    """One model invocation, with everything the eval needs to score it."""

    text: str
    latency_s: float
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    num_turns: int = 1
    tool_calls: int = 0
    tool_call_names: list[str] = field(default_factory=list)
    error: str | None = None
    raw: dict = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.error is None


@runtime_checkable
class Provider(Protocol):
    """What a configuration is allowed to assume about a provider."""

    name: str
    model: str

    def complete(self, system: str, prompt: str,
                 json_schema: dict | None = None) -> ModelResult:
        """Single turn, no tools.

        This is the only thing a provider has to do. The agent loop lives in
        triage.agent and drives tool use itself, so swapping providers never
        changes the agent's behaviour - only the model behind it.
        """
        ...


class ProviderError(RuntimeError):
    pass
