"""Provider registry."""

from __future__ import annotations

from .base import ModelResult, Provider, ProviderError

PROVIDERS = ("claude_cli", "anthropic", "offline")


def get_provider(name: str, model: str | None = None,
                 effort: str | None = None) -> Provider:
    if name == "claude_cli":
        from .claude_cli import DEFAULT_MODEL, ClaudeCLIProvider
        return ClaudeCLIProvider(model=model or DEFAULT_MODEL, effort=effort)
    if name == "anthropic":
        from .anthropic_api import DEFAULT_MODEL, AnthropicProvider
        return AnthropicProvider(model=model or DEFAULT_MODEL,
                                 effort=effort or "high")
    if name == "offline":
        from .offline import OfflineRulesProvider
        return OfflineRulesProvider()
    raise ProviderError(
        f"unknown provider {name!r}; choose one of {', '.join(PROVIDERS)}"
    )


__all__ = ["ModelResult", "Provider", "ProviderError", "PROVIDERS", "get_provider"]
