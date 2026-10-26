"""Anthropic adapter: POST /v1/messages for a call, GET /v1/models for the catalog.

Refuses to do: open a socket when PINBENCH_NO_NETWORK=1, or call without an api_key; count
an empty (or whitespace-only) extracted text as a success -- current-generation Claude
models run extended thinking by default (platform.claude.com/docs, Models overview /
Messages API / extended thinking), and a thinking block's tokens count against a low
max_tokens ceiling, so the model can hit the ceiling before writing any visible text and
still return 200 with usage.output_tokens == max_tokens. This adapter sends
`thinking: {"type": "disabled"}` by default -- the suite's own `params` can still turn it
back on, since `body.update(params)` applies after -- and treats empty extracted text as a
failure, never a success, recording the vendor's `stop_reason` either way.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request

API_BASE = "https://api.anthropic.com"
ANTHROPIC_VERSION = "2023-06-01"


class NoNetwork(RuntimeError):
    pass


def _guard_network():
    if os.environ.get("PINBENCH_NO_NETWORK") == "1":
        raise NoNetwork("PINBENCH_NO_NETWORK=1: refusing to open a socket")


def call(model_id, system, user_input, max_tokens, api_key, params=None, timeout=60):
    from . import Reply

    _guard_network()
    if not api_key:
        raise ValueError("ANTHROPIC_API_KEY is not set")

    params = params or {}
    body = {
        "model": model_id,
        "system": system,
        "messages": [{"role": "user", "content": user_input}],
        "max_tokens": max_tokens,
        # Off by default: current Claude models think by default, and a low max_tokens
        # ceiling (this suite's) can be spent entirely on an invisible thinking block,
        # leaving no text. A suite that wants thinking sets its own `params.thinking` and
        # the line below is overridden by `body.update(params)`.
        "thinking": {"type": "disabled"},
    }
    body.update(params)

    req = urllib.request.Request(
        f"{API_BASE}/v1/messages",
        data=json.dumps(body).encode("utf-8"),
        method="POST",
        headers={
            "content-type": "application/json",
            "x-api-key": api_key,
            "anthropic-version": ANTHROPIC_VERSION,
        },
    )

    started = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        latency_ms = int((time.monotonic() - started) * 1000)
        detail = exc.read().decode("utf-8", errors="replace")
        return Reply(
            output=None, input_tokens=None, output_tokens=None, latency_ms=latency_ms,
            error=f"HTTP {exc.code}: {detail[:200]}", status_code=exc.code,
        )
    except urllib.error.URLError as exc:
        latency_ms = int((time.monotonic() - started) * 1000)
        return Reply(output=None, input_tokens=None, output_tokens=None, latency_ms=latency_ms, error=str(exc))
    latency_ms = int((time.monotonic() - started) * 1000)

    content = data.get("content", [])
    text_parts = [block.get("text", "") for block in content if block.get("type") == "text"]
    output = "".join(text_parts)
    stop_reason = data.get("stop_reason")
    usage = data.get("usage") or {}

    if not output.strip():
        block_types = [block.get("type") for block in content]
        return Reply(
            output=None,
            input_tokens=usage.get("input_tokens"),
            output_tokens=usage.get("output_tokens"),
            latency_ms=latency_ms,
            error=f"empty text output (stop_reason={stop_reason!r}, content block types={block_types})",
            stop_reason=stop_reason,
        )

    return Reply(
        output=output,
        input_tokens=usage.get("input_tokens"),
        output_tokens=usage.get("output_tokens"),
        latency_ms=latency_ms,
        error=None,
        stop_reason=stop_reason,
    )


def list_models(api_key, timeout=20):
    """Return a sorted list of model ids, paginating on has_more/last_id."""
    _guard_network()
    if not api_key:
        raise ValueError("ANTHROPIC_API_KEY is not set")

    ids = []
    after_id = None
    while True:
        url = f"{API_BASE}/v1/models?limit=100"
        if after_id:
            url += f"&after_id={after_id}"
        req = urllib.request.Request(
            url,
            method="GET",
            headers={"x-api-key": api_key, "anthropic-version": ANTHROPIC_VERSION},
        )
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        ids.extend(m["id"] for m in data.get("data", []))
        if data.get("has_more") and data.get("last_id"):
            after_id = data["last_id"]
        else:
            break
    return sorted(ids)
