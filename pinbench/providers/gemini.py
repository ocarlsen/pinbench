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

Continuation (https://aistudio.google.com/docs/long-decode; ai.google.dev/gemini-api/docs/long-decode
redirects there; read 2026-11-13). A non-streaming hop is capped at 32,768 tokens on gemini-4-carbon
and gemini-4-argon (65,536 on gemini-4-hydrogen), so a long thinking run can end a hop with top-level
`status: "requires_continuation"` and a top-level `continuation_token`. `max_output_tokens` is the
thinking+output limit for the whole interaction. In REST the client loops: while status is
`requires_continuation`, POST again with the same model, system instruction and generation config
plus `continuation_token`; with `store: false` the prompt is resent instead of
`previous_interaction_id`. Every hop is billed ("You pay for every token in every hop, including hops
that contain only thinking"), and each hop re-bills its input. `call()` loops this way:
- Visible text is concatenated across hops; input, visible-output and thought tokens are summed
  across hops (output_tokens = all visible + all thought tokens). latency_ms covers every hop and
  the per-call `timeout` applies to each hop.
- Bounded: the loop stops when summed output reaches max_output_tokens or after
  ceil(max_output_tokens / 32768) + 1 hops, then returns stop_reason "incomplete" (the word
  scoring.TRUNCATION_STOP_REASONS already treats as truncation) instead of looping on a server
  that keeps asking.
- A hop that fails after earlier hops succeeded returns an error Reply that still carries the
  summed tokens of the paid hops.
- AMBIGUITY: the docs name the usage fields `total_input_tokens`, `total_output_tokens`,
  `total_thought_tokens` and describe total_input_tokens as summed "across all hops", which could
  mean a response's usage is already cumulative. This adapter treats each hop response's usage as
  that hop's own and sums them, as the brief for this change specifies; no live multi-hop
  response has been checked. `total_cached_tokens` is ignored (cached input is billed at a lower
  rate; pricing it separately is out of scope), so input cost is an upper bound.

`store` is sent as False: the Interactions API stores a request by default, and pinbench
sends your own frozen inputs.
"""

from __future__ import annotations

import http.client
import json
import math
import os
import time
import urllib.error
import urllib.request

API_BASE = "https://generativelanguage.googleapis.com/v1beta"
# Smallest non-streaming single-hop cap (gemini-4-carbon / gemini-4-argon); the hop bound uses it.
HOP_CAP_TOKENS = 32768


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
    """Visible text from the model_output steps, else `output_text`; thought steps never count."""
    parts = []
    for step in interaction.get("steps") or []:
        if step.get("type") != "model_output":
            continue
        for block in step.get("content") or []:
            if block.get("type") == "text":
                parts.append(block.get("text", ""))
    return "".join(parts) or interaction.get("output_text") or ""


def _post(body, api_key, timeout):
    """One POST. Returns (data, None) or (None, error_kwargs for Reply)."""
    req = urllib.request.Request(
        f"{API_BASE}/interactions",
        data=json.dumps(body).encode("utf-8"),
        method="POST",
        headers={
            "content-type": "application/json",
            "x-goog-api-key": api_key,
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8")), None
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        return None, {"error": f"HTTP {exc.code}: {detail[:200]}", "status_code": exc.code}
    except urllib.error.URLError as exc:
        return None, {"error": str(exc)}
    except (OSError, ValueError, http.client.HTTPException) as exc:
        # A read timeout (TimeoutError / socket.timeout) or a body that is not valid JSON
        # surfaces from resp.read() / json.loads, not from urlopen; either is a failed call.
        return None, {"error": f"{type(exc).__name__}: {exc}"}


def _usage(data, interaction):
    usage = data.get("usage") or interaction.get("usage") or {}
    return (
        usage.get("total_input_tokens"),
        usage.get("total_output_tokens"),
        usage.get("total_thought_tokens"),
    )


def _add(total, value):
    """Running sum that stays None until some hop reports the field."""
    if value is None:
        return total
    return value + (total or 0)


def call(model_id, system, user_input, max_tokens, api_key, params=None, timeout=60):
    """One logical call, which may take several HTTP hops (see the module docstring).

    `timeout` applies to each hop, not to the whole call; `latency_ms` covers all hops.
    """
    from . import Reply

    _guard_network()
    if not api_key:
        raise ValueError("GEMINI_API_KEY is not set")

    base_body = _build_body(model_id, system, user_input, max_tokens, params)
    # The ceiling that bounds the loop is the one actually sent (a params generation_config
    # can override max_output_tokens), falling back to the max_tokens argument.
    ceiling = base_body["generation_config"].get("max_output_tokens") or max_tokens
    max_hops = math.ceil(ceiling / HOP_CAP_TOKENS) + 1 if ceiling else 2

    started = time.monotonic()
    body = base_body
    hops = 0
    texts = []
    input_tokens = None
    visible_total = None
    thoughts_total = None
    status = None
    stop_reason = None
    ran_out = False

    while True:
        data, failure = _post(body, api_key, timeout)
        hops += 1
        if failure is not None:
            latency_ms = int((time.monotonic() - started) * 1000)
            # Earlier hops were billed: keep their usage on the error Reply (hop 1 failing
            # leaves all three as None, exactly as a single-call failure always did).
            billed = None if visible_total is None and thoughts_total is None else (visible_total or 0) + (thoughts_total or 0)
            return Reply(
                output=None, input_tokens=input_tokens, output_tokens=billed,
                latency_ms=latency_ms, **failure,
            )

        # The reference documents the interaction nested under "interaction"; responses observed
        # from this endpoint carry it at the top level. Read either.
        interaction = data.get("interaction") if isinstance(data.get("interaction"), dict) else data
        texts.append(_extract_text(interaction))
        # Status is an enum ("completed", "incomplete", ...); the docs print it upper-case and the
        # API has returned it lower-case, so it is lower-cased here and recorded as the stop reason.
        status = interaction.get("status")
        stop_reason = status.lower() if isinstance(status, str) else None
        in_tok, vis, thought = _usage(data, interaction)
        input_tokens = _add(input_tokens, in_tok)
        visible_total = _add(visible_total, vis)
        thoughts_total = _add(thoughts_total, thought)

        if stop_reason != "requires_continuation":
            break
        # Bound the loop: the summed thinking+output budget is spent, or a server keeps asking.
        if (visible_total or 0) + (thoughts_total or 0) >= ceiling or hops >= max_hops:
            ran_out = True
            break
        token = interaction.get("continuation_token")
        if not token:
            latency_ms = int((time.monotonic() - started) * 1000)
            billed = None if visible_total is None and thoughts_total is None else (visible_total or 0) + (thoughts_total or 0)
            return Reply(
                output=None, input_tokens=input_tokens, output_tokens=billed, latency_ms=latency_ms,
                error="status='requires_continuation' with no continuation_token", stop_reason=stop_reason,
            )
        # store is false, so the prompt is resent (no previous_interaction_id), with the same
        # model, system instruction and generation config, plus the token.
        body = {**base_body, "continuation_token": token}

    latency_ms = int((time.monotonic() - started) * 1000)
    output = "".join(texts)
    if ran_out:
        # Still asking to continue but out of budget or hops: that is a ceiling cut, recorded
        # with the word scoring.TRUNCATION_STOP_REASONS already holds for it.
        status = "incomplete"
        stop_reason = "incomplete"
    output_tokens = None if visible_total is None and thoughts_total is None else (visible_total or 0) + (thoughts_total or 0)
    thoughts = thoughts_total

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
