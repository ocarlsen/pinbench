"""Shared test fixtures: a fake provider adapter, and a hard no-network env guard."""

from __future__ import annotations

import os

import pytest

from pinbench import providers


@pytest.fixture(autouse=True)
def no_network_env(monkeypatch):
    """Every test runs with PINBENCH_NO_NETWORK=1 and no real API keys."""
    monkeypatch.setenv("PINBENCH_NO_NETWORK", "1")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    yield


class FakeAdapter:
    """A stand-in for a providers.<name> module: .call() and .list_models()."""

    def __init__(self):
        self.call_count = 0
        self.calls = []
        self.replies = {}  # (model_id, item_input) -> Reply
        self.default_output_tokens = None  # override output_tokens for every reply
        self.models = []

    def set_reply(self, model_id, item_input, reply):
        self.replies[(model_id, item_input)] = reply

    def call(self, model_id, system, user_input, max_tokens, api_key, params=None, timeout=60):
        self.call_count += 1
        self.calls.append((model_id, user_input))
        if not api_key:
            raise ValueError("missing api key")
        key = (model_id, user_input)
        if key in self.replies:
            return self.replies[key]
        out_tokens = self.default_output_tokens if self.default_output_tokens is not None else min(max_tokens, 4)
        return providers.Reply(
            output=f"echo:{user_input}",
            input_tokens=len(system) + len(user_input),
            output_tokens=out_tokens,
            latency_ms=5,
            error=None,
        )

    def list_models(self, api_key, timeout=20):
        if not api_key:
            raise ValueError("missing api key")
        return list(self.models)


@pytest.fixture
def fake_adapter(monkeypatch):
    adapter = FakeAdapter()

    def fake_route(model_id):
        providers.provider_for_id(model_id)  # still validates the prefix
        return adapter

    monkeypatch.setattr(providers, "route", fake_route)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-anthropic-key")
    monkeypatch.setenv("OPENAI_API_KEY", "test-openai-key")
    return adapter
