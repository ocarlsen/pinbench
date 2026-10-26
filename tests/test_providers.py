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
    assert captured["body"]["max_completion_tokens"] == 64
    assert "max_tokens" not in captured["body"]  # the legacy key, rejected by o-series ids
    assert captured["body"]["messages"][1]["content"] == "What is the capital of France?"

    assert reply.output == "Paris."
    assert reply.input_tokens == 21
    assert reply.output_tokens == 3
    assert reply.error is None


def test_anthropic_call_sends_no_thinking_key_on_its_own(monkeypatch):
    """The adapter carries no per-model request opinion of its own -- how (or whether) a
    given Claude model turns thinking off differs by model (see pinbench/prices.py
    request_defaults / runner._merged_params), so the adapter just forwards whatever
    `params` the caller already merged. With no params it sends no `thinking` key at all."""
    monkeypatch.delenv("PINBENCH_NO_NETWORK", raising=False)
    body = (FIXTURES / "anthropic_message_response.json").read_bytes()
    captured = {}

    def fake_urlopen(req, timeout=None):
        captured["body"] = json.loads(req.data.decode("utf-8"))
        return _FakeResponse(body)

    monkeypatch.setattr(anthropic_provider.urllib.request, "urlopen", fake_urlopen)

    anthropic_provider.call("claude-sonnet-5-5", "Answer only.", "hi", 64, "test-key")
    assert "thinking" not in captured["body"]

    # Whatever the caller passes in `params` (a merged model default, a suite override, or
    # both) is forwarded verbatim.
    anthropic_provider.call(
        "claude-sonnet-5-5", "Answer only.", "hi", 64, "test-key",
        params={"thinking": {"type": "between_tools"}},
    )
    assert captured["body"]["thinking"] == {"type": "between_tools"}


def test_anthropic_truncated_output_is_recorded_not_hidden(monkeypatch):
    """Finding A from the second live run: stop_reason=max_tokens with real (but cut-off)
    text is still a success -- but the adapter must record the stop_reason so callers can
    tell it was truncated."""
    monkeypatch.delenv("PINBENCH_NO_NETWORK", raising=False)
    body = (FIXTURES / "anthropic_truncated_response.json").read_bytes()
    monkeypatch.setattr(anthropic_provider.urllib.request, "urlopen", lambda req, timeout=None: _FakeResponse(body))

    reply = anthropic_provider.call("claude-sonnet-5-5", "Summarize.", "...", 60, "test-key")
    assert reply.error is None
    assert reply.output is not None and reply.output != ""
    assert reply.stop_reason == "max_tokens"


def test_anthropic_empty_text_with_thinking_block_is_a_failure(monkeypatch):
    """Finding B from the second live run: item s04 incumbent came back output_tokens=60,
    text="", stop_reason=max_tokens -- the model spent its whole ceiling on a thinking
    block. This must never be recorded as a success."""
    monkeypatch.delenv("PINBENCH_NO_NETWORK", raising=False)
    body = (FIXTURES / "anthropic_empty_thinking_response.json").read_bytes()
    monkeypatch.setattr(anthropic_provider.urllib.request, "urlopen", lambda req, timeout=None: _FakeResponse(body))

    reply = anthropic_provider.call("claude-sonnet-5-5", "Summarize.", "...", 60, "test-key")
    assert reply.output is None
    assert reply.error is not None
    assert "empty text output" in reply.error
    assert "max_tokens" in reply.error
    assert "thinking" in reply.error  # the content block types list names what came back
    assert reply.stop_reason == "max_tokens"
    assert reply.input_tokens == 314
    assert reply.output_tokens == 60


def test_openai_empty_content_is_a_failure(monkeypatch):
    monkeypatch.delenv("PINBENCH_NO_NETWORK", raising=False)
    body = json.dumps(
        {
            "id": "chatcmpl_empty",
            "choices": [{"index": 0, "message": {"role": "assistant", "content": ""}, "finish_reason": "length"}],
            "usage": {"prompt_tokens": 50, "completion_tokens": 16},
        }
    ).encode("utf-8")
    monkeypatch.setattr(openai_provider.urllib.request, "urlopen", lambda req, timeout=None: _FakeResponse(body))

    reply = openai_provider.call("gpt-5-mini", "Summarize.", "...", 16, "test-key")
    assert reply.output is None
    assert reply.error is not None
    assert "empty text output" in reply.error
    assert reply.stop_reason == "length"


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
