"""Provider backed by the installed Claude Code CLI in headless mode.

This is the default because it needs no API key: it runs on the Claude Code
subscription already installed on the machine. That makes the whole eval
runnable at no additional cost, which is the difference between a results
table and a promise of one.

Two things to be honest about, both reported rather than hidden:

1. `total_cost_usd` is Claude Code's own estimate at *list API prices*. On a
   subscription you are not billed that amount. It is the right number for
   comparing configurations against each other, and the wrong number to
   quote as "what this costs to run".
2. Every call carries Claude Code's harness overhead - roughly 900-4000
   cached tokens of scaffolding even with the system prompt fully replaced.
   It applies equally to all three configurations, so the comparison holds,
   but absolute per-incident cost is inflated against a bare API call.

The provider only has to complete a prompt. Tool use is driven by
`triage.agent`, which owns the MCP client - see that module for why.

Reproducibility: each call disables settings sources, slash commands,
session persistence and every built-in tool, so a run here matches a run on
another machine with different local configuration.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import time

from .base import ModelResult, ProviderError

DEFAULT_MODEL = "sonnet"
TIMEOUT_SECONDS = 600


def _claude_binary() -> str:
    found = shutil.which("claude")
    if not found:
        raise ProviderError(
            "The `claude` CLI is not on PATH. Install Claude Code, or run the "
            "eval with --provider anthropic (needs ANTHROPIC_API_KEY) or "
            "--provider offline."
        )
    return found


class ClaudeCLIProvider:
    name = "claude_cli"

    def __init__(self, model: str = DEFAULT_MODEL, effort: str | None = None):
        self.model = model
        self.effort = effort
        self._binary = _claude_binary()

    def complete(self, system: str, prompt: str,
                 json_schema: dict | None = None) -> ModelResult:
        argv = [
            self._binary,
            "--print",
            "--model", self.model,
            "--system-prompt", system,
            "--output-format", "json",
            # No built-in tools: no Bash, no Read, no WebSearch. The only
            # tools in this project are the MCP server's, and those are
            # called by the harness rather than from inside Claude Code.
            "--tools", "",
            "--setting-sources", "",
            "--disable-slash-commands",
            "--no-session-persistence",
            "--strict-mcp-config",
        ]
        if self.effort:
            argv += ["--effort", self.effort]
        if json_schema:
            argv += ["--json-schema", json.dumps(json_schema)]
        argv.append(prompt)

        started = time.perf_counter()
        try:
            proc = subprocess.run(
                argv,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=TIMEOUT_SECONDS,
                # Inherit the environment so existing authentication is
                # picked up without being handled here.
                env=os.environ.copy(),
            )
        except subprocess.TimeoutExpired:
            return ModelResult(
                text="", latency_s=float(TIMEOUT_SECONDS),
                error=f"claude timed out after {TIMEOUT_SECONDS}s",
            )
        latency = time.perf_counter() - started

        if proc.returncode != 0:
            detail = (proc.stderr or proc.stdout or "").strip()[:500]
            return ModelResult(
                text="", latency_s=latency,
                error=f"claude exited {proc.returncode}: {detail}",
            )

        try:
            final = json.loads(proc.stdout)
        except json.JSONDecodeError as exc:
            return ModelResult(
                text="", latency_s=latency,
                error=f"could not parse claude output: {exc}; "
                      f"got {proc.stdout[:300]!r}",
            )
        return _result_from(final, latency)


def _result_from(final: dict, latency: float) -> ModelResult:
    usage = final.get("usage") or {}
    # Cached reads and writes are real input tokens. Counting only
    # `input_tokens` would make every configuration look nearly free.
    input_tokens = (
        int(usage.get("input_tokens") or 0)
        + int(usage.get("cache_read_input_tokens") or 0)
        + int(usage.get("cache_creation_input_tokens") or 0)
    )

    error = None
    if final.get("is_error"):
        error = str(
            final.get("result") or final.get("subtype")
            or "claude reported an error"
        )
    if final.get("stop_reason") == "refusal":
        error = (error + "; " if error else "") + "model refused"

    return ModelResult(
        text=str(final.get("result") or ""),
        latency_s=round(final.get("duration_ms", latency * 1000) / 1000.0, 3),
        input_tokens=input_tokens,
        output_tokens=int(usage.get("output_tokens") or 0),
        cost_usd=float(final.get("total_cost_usd") or 0.0),
        num_turns=int(final.get("num_turns") or 1),
        error=error,
        raw={
            "session_id": final.get("session_id"),
            "stop_reason": final.get("stop_reason"),
            "subtype": final.get("subtype"),
        },
    )
