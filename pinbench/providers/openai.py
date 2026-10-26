"""OpenAI adapter: POST /v1/chat/completions for a call, GET /v1/models for the catalog.

Refuses to do: open a socket when PINBENCH_NO_NETWORK=1, or call without an api_key.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request

API_BASE = "https://api.openai.com"


class NoNetwork(RuntimeError):
    pass


def _guard_network():
    if os.environ.get("PINBENCH_NO_NETWORK") == "1":
        raise NoNetwork("PINBENCH_NO_NETWORK=1: refusing to open a socket")


def call(model_id, system, user_input, max_tokens, api_key, params=None, timeout=60):
    from . import Reply

    _guard_network()
    if not api_key:
        raise ValueError("OPENAI_API_KEY is not set")

    params = params or {}
    body = {
        "model": model_id,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user_input},
        ],
        "max_completion_tokens": max_tokens,  # `max_tokens` is the legacy key; o-series ids reject it
    }
    body.update(params)

    req = urllib.request.Request(
        f"{API_BASE}/v1/chat/completions",
        data=json.dumps(body).encode("utf-8"),
        method="POST",
        headers={
            "content-type": "application/json",
            "authorization": f"Bearer {api_key}",
        },
    )

    started = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        latency_ms = int((time.monotonic() - started) * 1000)
        detail = exc.read().decode("utf-8", errors="replace")
        return Reply(output=None, input_tokens=None, output_tokens=None, latency_ms=latency_ms, error=f"HTTP {exc.code}: {detail[:200]}")
    except urllib.error.URLError as exc:
        latency_ms = int((time.monotonic() - started) * 1000)
        return Reply(output=None, input_tokens=None, output_tokens=None, latency_ms=latency_ms, error=str(exc))
    latency_ms = int((time.monotonic() - started) * 1000)

    choices = data.get("choices") or []
    output = choices[0]["message"].get("content", "") if choices else ""
    usage = data.get("usage") or {}
    return Reply(
        output=output,
        input_tokens=usage.get("prompt_tokens"),
        output_tokens=usage.get("completion_tokens"),
        latency_ms=latency_ms,
        error=None,
    )


def list_models(api_key, timeout=20):
    """Return a sorted list of model ids. OpenAI's /v1/models is not paginated."""
    _guard_network()
    if not api_key:
        raise ValueError("OPENAI_API_KEY is not set")

    req = urllib.request.Request(
        f"{API_BASE}/v1/models",
        method="GET",
        headers={"authorization": f"Bearer {api_key}"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    return sorted(m["id"] for m in data.get("data", []))
