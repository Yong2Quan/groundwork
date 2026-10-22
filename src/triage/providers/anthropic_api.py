"""Provider backed by the Anthropic API directly, or by Bedrock.

Selected with `--provider anthropic`. Needs credentials:

    ANTHROPIC_API_KEY=...                      # first-party API
    TRIAGE_BEDROCK_REGION=us-east-1            # or Amazon Bedrock

Pricing below is first-party list price, used to compute cost per incident
from reported token usage. Bedrock bills at AWS rates, so the cost column is
labelled as an estimate when that backend is selected.
"""

from __future__ import annotations

import json
import os
import time

from .base import ModelResult, ProviderError

DEFAULT_MODEL = "claude-sonnet-5-5"

# USD per million tokens, first-party list price.
PRICING = {
    "claude-opus-5-5":   (4.00, 20.00),
    "claude-opus-5":     (5.00, 25.00),
    "claude-sonnet-5-5": (2.00, 10.00),
    "claude-sonnet-5":   (2.00, 10.00),
    "claude-haiku-5-5":  (0.10, 0.50),
}


def _price(model: str, input_tokens: int, output_tokens: int) -> float:
    rates = PRICING.get(model)
    if not rates:
        return 0.0
    in_rate, out_rate = rates
    return round(
        input_tokens / 1e6 * in_rate + output_tokens / 1e6 * out_rate, 6
    )


class AnthropicProvider:
    name = "anthropic"

    def __init__(self, model: str = DEFAULT_MODEL, effort: str = "high"):
        try:
            import anthropic  # noqa: F401
        except ImportError as exc:
            raise ProviderError(
                "The anthropic provider needs the SDK. Install it with:\n"
                "    pip install -e .[api]"
            ) from exc
        self.model = model
        self.effort = effort
        self._region = os.environ.get("TRIAGE_BEDROCK_REGION")
        if not self._region and not os.environ.get("ANTHROPIC_API_KEY"):
            raise ProviderError(
                "No credentials found. Set ANTHROPIC_API_KEY, or set "
                "TRIAGE_BEDROCK_REGION to use Amazon Bedrock, or run with "
                "--provider claude_cli (no key needed) or --provider offline."
            )

    def _sync_client(self):
        import anthropic
        if self._region:
            return anthropic.AnthropicBedrockMantle(aws_region=self._region)
        return anthropic.Anthropic()

    def _async_client(self):
        import anthropic
        if self._region:
            return anthropic.AsyncAnthropicBedrockMantle(aws_region=self._region)
        return anthropic.AsyncAnthropic()

    def _model_id(self) -> str:
        # Bedrock model ids carry an `anthropic.` prefix.
        return f"anthropic.{self.model}" if self._region else self.model

    # -- interface --------------------------------------------------------

    def complete(self, system: str, prompt: str,
                 json_schema: dict | None = None) -> ModelResult:
        import anthropic

        kwargs: dict = {
            "model": self._model_id(),
            "max_tokens": 8000,
            "system": system,
            "messages": [{"role": "user", "content": prompt}],
            "output_config": {"effort": self.effort},
        }
        if json_schema:
            kwargs["output_config"]["format"] = {
                "type": "json_schema", "schema": json_schema
            }

        started = time.perf_counter()
        try:
            response = self._sync_client().messages.create(**kwargs)
        except anthropic.APIError as exc:
            return ModelResult(text="", latency_s=0.0, error=f"{type(exc).__name__}: {exc}")
        latency = time.perf_counter() - started

        if response.stop_reason == "refusal":
            detail = getattr(response.stop_details, "category", None)
            return ModelResult(text="", latency_s=latency,
                               error=f"refusal ({detail})")

        text = "".join(b.text for b in response.content if b.type == "text")
        return _result(text, latency, response.usage, self.model, num_turns=1)

def _result(text: str, latency: float, usage, model: str, num_turns: int) -> ModelResult:
    input_tokens = (
        (usage.input_tokens or 0)
        + (getattr(usage, "cache_read_input_tokens", 0) or 0)
        + (getattr(usage, "cache_creation_input_tokens", 0) or 0)
    )
    output_tokens = usage.output_tokens or 0
    return ModelResult(
        text=text,
        latency_s=round(latency, 3),
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        cost_usd=_price(model, input_tokens, output_tokens),
        num_turns=num_turns,
    )
