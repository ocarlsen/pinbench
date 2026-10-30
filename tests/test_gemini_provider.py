"""Gemini adapter: request shape, billed-output arithmetic, truncation and empty-text handling.

Response bodies here are hand-written to the Interactions API shape (top-level `status`,
`steps[].content[]` text blocks, `usage.total_*_tokens`); no live response is checked in.
"""

import json

import pytest

from pinbench import providers, scoring
from pinbench.providers import gemini


class _FakeResponse:
    def __init__(self, payload):
        self._body = json.dumps(payload).encode("utf-8")

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _interaction(text, status="completed", out=40, thoughts=25, nested=False):
    steps = [{"type": "thought", "content": [{"type": "text", "text": "hidden reasoning"}]}]
    if text is not None:
        steps.append({"type": "model_output", "content": [{"type": "text", "text": text}]})
    interaction = {"id": "i-1", "status": status, "steps": steps}
    usage = {"total_input_tokens": 120, "total_output_tokens": out, "total_thought_tokens": thoughts}
    if nested:
        return {"interaction": interaction, "usage": usage}
    return {**interaction, "usage": usage}


@pytest.fixture
def capture(monkeypatch):
    monkeypatch.delenv("PINBENCH_NO_NETWORK", raising=False)
    captured = {"payload": _interaction("A short summary.")}

    def fake_urlopen(req, timeout=None):
        captured["url"] = req.full_url
        captured["method"] = req.get_method()
        captured["headers"] = {k.lower(): v for k, v in req.headers.items()}
        captured["body"] = json.loads(req.data.decode("utf-8")) if req.data else None
        return _FakeResponse(captured["payload"])

    monkeypatch.setattr(gemini.urllib.request, "urlopen", fake_urlopen)
    return captured


def test_routing():
    assert providers.provider_for_id("gemini-4-argon") == "gemini"
    assert providers.route("gemini-4-argon") is gemini
    assert providers.ENV_KEYS["gemini"] == "GEMINI_API_KEY"


def test_no_network_and_missing_key(monkeypatch):
    monkeypatch.setenv("PINBENCH_NO_NETWORK", "1")
    with pytest.raises(gemini.NoNetwork):
        gemini.call("gemini-4-argon", "sys", "hi", 16, "key")
    with pytest.raises(gemini.NoNetwork):
        gemini.list_models("key")
    monkeypatch.delenv("PINBENCH_NO_NETWORK")
    with pytest.raises(ValueError):
        gemini.call("gemini-4-argon", "sys", "hi", 16, None)


def test_call_shape_and_billed_output(capture):
    reply = gemini.call("gemini-4-argon", "Summarize.", "Some passage.", 300, "test-key")

    assert capture["method"] == "POST"
    assert capture["url"] == "https://generativelanguage.googleapis.com/v1beta/interactions"
    assert capture["headers"]["x-goog-api-key"] == "test-key"
    assert "test-key" not in capture["url"]  # the key travels in a header, never the URL
    body = capture["body"]
    assert body["model"] == "gemini-4-argon"
    assert body["system_instruction"] == "Summarize."
    assert body["input"] == "Some passage."
    assert body["store"] is False
    assert body["generation_config"] == {"max_output_tokens": 300}

    # Only model_output text counts as output; thought steps never leak into it.
    assert reply.output == "A short summary."
    assert reply.error is None
    assert reply.stop_reason == "completed"
    assert reply.input_tokens == 120
    # Billed output = visible 40 + thoughts 25.
    assert reply.output_tokens == 65


def test_generation_config_params_merge_not_replace(capture):
    gemini.call(
        "gemini-4-argon", "s", "x", 300, "k",
        params={"generation_config": {"thinking_level": "low"}, "temperature": 0.2},
    )
    assert capture["body"]["generation_config"] == {"max_output_tokens": 300, "thinking_level": "low"}
    # Any other key is forwarded as is; the API may ignore or reject it, and a rejection is
    # recorded as an error, never retried.
    assert capture["body"]["temperature"] == 0.2


def test_sends_no_thinking_level_on_its_own(capture):
    gemini.call("gemini-4-argon", "s", "x", 300, "k")
    assert "thinking_level" not in capture["body"]["generation_config"]


def test_nested_interaction_shape_is_read(capture):
    capture["payload"] = _interaction("Nested.", nested=True)
    reply = gemini.call("gemini-4-argon", "s", "x", 300, "k")
    assert reply.output == "Nested."
    assert reply.output_tokens == 65


def test_upper_case_status_is_normalized(capture):
    capture["payload"] = _interaction("Text.", status="COMPLETED")
    assert gemini.call("gemini-4-argon", "s", "x", 300, "k").stop_reason == "completed"


def test_incomplete_with_text_is_recorded_as_truncation(capture):
    capture["payload"] = _interaction("Cut off mid", status="incomplete", out=10, thoughts=290)
    reply = gemini.call("gemini-4-argon", "s", "x", 300, "k")
    assert reply.error is None
    assert reply.output == "Cut off mid"
    assert scoring.is_truncated({"stop_reason": reply.stop_reason})


def test_thinking_ate_the_budget_is_a_failure(capture):
    """The live-run lesson, Gemini edition: every token went to thinking, no visible text."""
    capture["payload"] = _interaction(None, status="incomplete", out=0, thoughts=300)
    reply = gemini.call("gemini-4-argon", "s", "x", 300, "k")
    assert reply.output is None
    assert "empty text output" in reply.error
    assert reply.output_tokens == 300  # still billed
    assert reply.stop_reason == "incomplete"


def test_unknown_status_with_text_is_an_error(capture):
    capture["payload"] = _interaction("Partial.", status="failed")
    reply = gemini.call("gemini-4-argon", "s", "x", 300, "k")
    assert reply.output is None
    assert "status='failed'" in reply.error


def test_http_error_records_status_code(monkeypatch):
    monkeypatch.delenv("PINBENCH_NO_NETWORK", raising=False)
    import io
    import urllib.error

    def fake_urlopen(req, timeout=None):
        raise urllib.error.HTTPError(req.full_url, 400, "Bad Request", {}, io.BytesIO(b'{"error": "bad level"}'))

    monkeypatch.setattr(gemini.urllib.request, "urlopen", fake_urlopen)
    reply = gemini.call("gemini-4-argon", "s", "x", 300, "k")
    assert reply.status_code == 400
    assert reply.error.startswith("HTTP 400")


def test_list_models_strips_prefix_and_paginates(monkeypatch):
    monkeypatch.delenv("PINBENCH_NO_NETWORK", raising=False)
    pages = [
        {"models": [{"name": "models/gemini-4-argon"}, {"name": "models/gemini-3.8-flash"}], "nextPageToken": "p2"},
        {"models": [{"name": "models/text-embedding-005"}]},
    ]
    urls = []

    def fake_urlopen(req, timeout=None):
        urls.append(req.full_url)
        assert req.headers.get("X-goog-api-key") == "k"
        return _FakeResponse(pages[len(urls) - 1])

    monkeypatch.setattr(gemini.urllib.request, "urlopen", fake_urlopen)
    assert gemini.list_models("k") == ["gemini-3.8-flash", "gemini-4-argon", "text-embedding-005"]
    assert "pageToken=p2" in urls[1]


def test_output_text_fallback_when_no_model_output_step(capture):
    capture["payload"] = {
        "status": "completed",
        "steps": [{"type": "thought", "content": [{"type": "text", "text": "hidden"}]}],
        "output_text": "From output_text.",
        "usage": {"total_input_tokens": 5, "total_output_tokens": 3, "total_thought_tokens": 0},
    }
    assert gemini.call("gemini-4-argon", "s", "x", 300, "k").output == "From output_text."
