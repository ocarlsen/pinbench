"""Route a model id to its provider adapter.

Refuses to do: guess a provider for an id prefix it doesn't recognize, or open a socket
when PINBENCH_NO_NETWORK=1 (each adapter checks that env var itself, as a belt to any stub).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

from . import anthropic as _anthropic
from . import openai as _openai

ENV_KEYS = {
    "anthropic": "ANTHROPIC_API_KEY",
    "openai": "OPENAI_API_KEY",
}


@dataclass
class Reply:
    output: Optional[str]
    input_tokens: Optional[int]
    output_tokens: Optional[int]
    latency_ms: int
    error: Optional[str] = None


class NoAdapterError(ValueError):
    pass


def provider_for_id(model_id: str) -> str:
    if model_id.startswith("claude-"):
        return "anthropic"
    if model_id.startswith("gpt-") or (model_id[:1] == "o" and model_id[1:2].isdigit()):
        return "openai"
    raise NoAdapterError(f"no adapter for id '{model_id}'")


def route(model_id: str):
    provider = provider_for_id(model_id)
    if provider == "anthropic":
        return _anthropic
    if provider == "openai":
        return _openai
    raise NoAdapterError(f"no adapter for id '{model_id}'")
