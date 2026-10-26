import json
from pathlib import Path

import pytest

from pinbench.providers import anthropic as anthropic_provider
from pinbench.providers import openai as openai_provider

FIXTURES = Path(__file__).parent / "fixtures"


class _FakeResponse:
    def __init__(self, body: bytes):
        self._body = body

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_no_network_guard_raises(monkeypatch):
    monkeypatch.setenv("PINBENCH_NO_NETWORK", "1")
    with pytest.raises(anthropic_provider.NoNetwork):
        anthropic_provider.call("claude-sonnet-4-5", "sys", "hi", 16, "key")
    with pytest.raises(openai_provider.NoNetwork):
        openai_provider.call("gpt-4o-mini", "sys", "hi", 16, "key")


def test_missing_api_key_raises(monkeypatch):
    monkeypatch.delenv("PINBENCH_NO_NETWORK", raising=False)
    with pytest.raises(ValueError):
        anthropic_provider.call("claude-sonnet-4-5", "sys", "hi", 16, None)
    with pytest.raises(ValueError):
        openai_provider.call("gpt-4o-mini", "sys", "hi", 16, None)


def test_anthropic_call_shape_and_parsing(monkeypatch):
    monkeypatch.delenv("PINBENCH_NO_NETWORK", raising=False)
    body = (FIXTURES / "anthropic_message_response.json").read_bytes()
    captured = {}

    def fake_urlopen(req, timeout=None):
        captured["url"] = req.full_url
        captured["method"] = req.get_method()
        captured["headers"] = {k.lower(): v for k, v in req.headers.items()}
        captured["body"] = json.loads(req.data.decode("utf-8"))
        return _FakeResponse(body)

    monkeypatch.setattr(anthropic_provider.urllib.request, "urlopen", fake_urlopen)

    reply = anthropic_provider.call("claude-sonnet-4-5", "Answer only.", "What is the capital of France?", 64, "test-key")

    assert captured["method"] == "POST"
    assert captured["url"] == "https://api.anthropic.com/v1/messages"
    assert captured["headers"]["x-api-key"] == "test-key"
    assert captured["headers"]["anthropic-version"]
    assert captured["body"]["model"] == "claude-sonnet-4-5"
    assert captured["body"]["max_tokens"] == 64
    assert captured["body"]["messages"][0]["content"] == "What is the capital of France?"

    assert reply.output == "Paris"
    assert reply.input_tokens == 23
    assert reply.output_tokens == 2
    assert reply.error is None


def test_openai_call_shape_and_parsing(monkeypatch):
    monkeypatch.delenv("PINBENCH_NO_NETWORK", raising=False)
    body = (FIXTURES / "openai_chat_response.json").read_bytes()
    captured = {}

    def fake_urlopen(req, timeout=None):
        captured["url"] = req.full_url
        captured["method"] = req.get_method()
        captured["headers"] = {k.lower(): v for k, v in req.headers.items()}
        captured["body"] = json.loads(req.data.decode("utf-8"))
        return _FakeResponse(body)

    monkeypatch.setattr(openai_provider.urllib.request, "urlopen", fake_urlopen)

    reply = openai_provider.call("gpt-4o-mini", "Answer only.", "What is the capital of France?", 64, "test-key")

    assert captured["method"] == "POST"
    assert captured["url"] == "https://api.openai.com/v1/chat/completions"
    assert captured["headers"]["authorization"] == "Bearer test-key"
    assert captured["body"]["model"] == "gpt-4o-mini"
    assert captured["body"]["messages"][1]["content"] == "What is the capital of France?"

    assert reply.output == "Paris."
    assert reply.input_tokens == 21
    assert reply.output_tokens == 3
    assert reply.error is None


def test_anthropic_http_error_becomes_reply_error(monkeypatch):
    import io
    import urllib.error

    monkeypatch.delenv("PINBENCH_NO_NETWORK", raising=False)

    def fake_urlopen(req, timeout=None):
        raise urllib.error.HTTPError(req.full_url, 429, "rate limited", {}, io.BytesIO(b"rate limited"))

    monkeypatch.setattr(anthropic_provider.urllib.request, "urlopen", fake_urlopen)
    reply = anthropic_provider.call("claude-sonnet-4-5", "sys", "hi", 16, "key")
    assert reply.output is None
    assert reply.error is not None
    assert "429" in reply.error
