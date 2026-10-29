"""Google Gemini adapter: POST /v1beta/interactions for a call, GET /v1beta/models for the catalog.

Refuses to do: open a socket when PINBENCH_NO_NETWORK=1, or call without an api_key, or
count an empty (or whitespace-only) text output as a success -- same principle as the other
adapters. Gemini thinking models draw their thought tokens from the same max_output_tokens
budget as the visible reply, so a tight ceiling can come back "incomplete" with little or no
text; that status is recorded as the stop reason and scoring treats it as truncation.

Two Gemini-specific shapes:
- Thinking is set with `generation_config.thinking_level`, and which levels a model accepts
  varies by model (Gemini 4 Argon takes low/medium/high, default medium, and cannot turn
  thinking off; ai.google.dev/gemini-api/docs/latest-model, read 2026-10-29). That is a
  per-model `request_defaults` row in prices.yaml, never a default here. Because the setting
  is nested, a `generation_config` mapping in `params` is merged INTO the body's
  generation_config (so it keeps max_output_tokens); every other key goes on the body as is.
- Billed output is visible output plus thought tokens. Google reports them separately
  (`usage.total_output_tokens`, `usage.total_thought_tokens`) and bills both at the output
  rate (ai.google.dev/gemini-api/docs/pricing: "every token the model processes is counted
  and billed, including thinking tokens"), so `output_tokens` here is their sum.

`store` is sent as False: the Interactions API stores a request by default, and pinbench
sends your own frozen inputs.
"""

from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request

API_BASE = "https://generativelanguage.googleapis.com/v1beta"


class NoNetwork(RuntimeError):
    pass


def _guard_network():
    if os.environ.get("PINBENCH_NO_NETWORK") == "1":
        raise NoNetwork("PINBENCH_NO_NETWORK=1: refusing to open a socket")


def _build_body(model_id, system, user_input, max_tokens, params):
    body = {
        "model": model_id,
        "system_instruction": system,
        "input": user_input,
        "store": False,
        "generation_config": {"max_output_tokens": max_tokens},
    }
    for key, value in (params or {}).items():
        if key == "generation_config" and isinstance(value, dict):
            body["generation_config"].update(value)
        else:
            body[key] = value
    return body


def _extract_text(interaction):
    """Visible text from the model_output steps; `output_text` only when there are no steps."""
    steps = interaction.get("steps")
    if steps:
        parts = []
        for step in steps:
            if step.get("type") != "model_output":
                continue
            for block in step.get("content") or []:
                if block.get("type") == "text":
                    parts.append(block.get("text", ""))
        return "".join(parts)
    return interaction.get("output_text") or ""


def call(model_id, system, user_input, max_tokens, api_key, params=None, timeout=60):
    from . import Reply

    _guard_network()
    if not api_key:
        raise ValueError("GEMINI_API_KEY is not set")

    body = _build_body(model_id, system, user_input, max_tokens, params)
    req = urllib.request.Request(
        f"{API_BASE}/interactions",
        data=json.dumps(body).encode("utf-8"),
        method="POST",
        headers={
            "content-type": "application/json",
            "x-goog-api-key": api_key,
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

    # The reference documents the interaction nested under "interaction"; responses observed
    # from this endpoint carry it at the top level. Read either.
    interaction = data.get("interaction") if isinstance(data.get("interaction"), dict) else data
    output = _extract_text(interaction)
    # Status is an enum ("completed", "incomplete", ...); the docs print it upper-case and the
    # API has returned it lower-case, so it is lower-cased here and recorded as the stop reason.
    status = interaction.get("status")
    stop_reason = status.lower() if isinstance(status, str) else None
    usage = data.get("usage") or interaction.get("usage") or {}
    input_tokens = usage.get("total_input_tokens")
    visible = usage.get("total_output_tokens")
    thoughts = usage.get("total_thought_tokens")
    output_tokens = None if visible is None and thoughts is None else (visible or 0) + (thoughts or 0)

    if not output.strip():
        return Reply(
            output=None,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            latency_ms=latency_ms,
            error=f"empty text output (status={status!r}, thought_tokens={thoughts!r})",
            stop_reason=stop_reason,
        )
    if stop_reason not in ("completed", "incomplete"):
        return Reply(
            output=None,
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            latency_ms=latency_ms,
            error=f"interaction status={status!r}",
            stop_reason=stop_reason,
        )

    return Reply(
        output=output,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        latency_ms=latency_ms,
        error=None,
        stop_reason=stop_reason,
    )


def list_models(api_key, timeout=20):
    """Return a sorted list of bare model ids ("models/" prefix stripped), paginating on nextPageToken."""
    _guard_network()
    if not api_key:
        raise ValueError("GEMINI_API_KEY is not set")

    ids = []
    page_token = None
    while True:
        url = f"{API_BASE}/models?pageSize=1000"
        if page_token:
            url += f"&pageToken={page_token}"
        req = urllib.request.Request(url, method="GET", headers={"x-goog-api-key": api_key})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        for model in data.get("models", []):
            name = model.get("name", "")
            ids.append(name[len("models/"):] if name.startswith("models/") else name)
        page_token = data.get("nextPageToken")
        if not page_token:
            break
    return sorted(ids)
