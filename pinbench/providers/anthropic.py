"""Anthropic adapter: POST /v1/messages for a call, GET /v1/models for the catalog.

Refuses to do: open a socket when PINBENCH_NO_NETWORK=1, or call without an api_key; count
an empty (or whitespace-only) extracted text as a success -- current-generation Claude
models run extended thinking by default (platform.claude.com/docs, Models overview /
Messages API / extended thinking), and a thinking block's tokens count against a low
max_tokens ceiling, so the model can hit the ceiling before writing any visible text and
still return 200 with usage.output_tokens == max_tokens. This adapter never guesses or
hard-codes a per-model request shape -- how (or whether) a given model turns thinking off
is model-specific (one Claude generation took `{"type": "disabled"}`, the next 400s on
that exact value and wants `{"type": "between_tools"}` instead) and lives as that model's
`request_defaults` row in prices.yaml, merged in by the caller before `params` is passed
here. This adapter treats empty extracted text as a failure, never a success, recording
the vendor's `stop_reason` either way.
"""

from __future__ import annotations

import http.client
import json
import os
import time
import urllib.error
import urllib.request

API_BASE = "https://api.anthropic.com"
# CONFIRMED (platform.claude.com/docs/en/build-with-claude/batch-processing, read 2026-11-10):
# the single-call Messages endpoint accepts anthropic-version: 2023-06-01 (used below since
# pinbench's first release). UNCONFIRMED for the batch endpoints (create/retrieve/results/
# cancel): every curl example on that page uses "anthropic-version: 2026-10-21"; the page
# never states whether 2023-06-01 is also accepted there, and pinbench cannot call the real
# API to check (PINBENCH_NO_NETWORK=1 in every test/offline run). The batch functions below
# reuse this same constant on the stated assumption that the version header is global, not
# per-endpoint; if a live smoke 400s on it, bump this constant (or split a
# BATCH_ANTHROPIC_VERSION) in a one-line follow-up PR -- do not silently swallow that 400.
ANTHROPIC_VERSION = "2023-06-01"
BATCHES_PATH = "/v1/messages/batches"


class NoNetwork(RuntimeError):
    pass


class BatchError(RuntimeError):
    """Raised on a non-2xx response from a batch endpoint (create/retrieve/results/cancel),
    or when the request never got a response (timeout, connection drop).

    `status` is the HTTP code, or None when there was no response. `transient` is True for
    the failures worth retrying: 429, any 5xx (529 included) and no response at all. Every
    other 4xx is a real refusal and is never retried."""

    def __init__(self, message, status=None, permanent=False):
        super().__init__(message)
        self.status = status
        self.permanent = permanent  # a malformed result that retrying cannot change

    @property
    def transient(self) -> bool:
        return not self.permanent and (self.status is None or self.status == 429 or 500 <= self.status <= 599)


def _guard_network():
    if os.environ.get("PINBENCH_NO_NETWORK") == "1":
        raise NoNetwork("PINBENCH_NO_NETWORK=1: refusing to open a socket")


def build_params(model_id, system, user_input, max_tokens, params=None) -> dict:
    """The Messages API request body for one call -- the exact dict `call()` sends, and the
    exact dict a batch request's "params" carries (see `batch.build_arm_requests` /
    `judge.judge_request_params`). The one place this body is assembled, so the sync and
    batch paths can never drift apart on it.
    """
    params = params or {}
    body = {
        "model": model_id,
        "system": system,
        "messages": [{"role": "user", "content": user_input}],
        "max_tokens": max_tokens,
    }
    # No model-specific defaults (e.g. how to turn thinking off) are set here -- the caller
    # merges the model's own request_defaults (prices.yaml) with the suite's params before
    # calling this function, so whatever lands in `params` is sent verbatim.
    body.update(params)
    return body


def _reply_from_message(data, latency_ms):
    """Parse a Messages API response body into a Reply. `data` is the full Message object:
    the top-level response to a sync call, or `result["message"]` from one `succeeded`
    batch result line (CONFIRMED same shape, platform.claude.com/docs/en/build-with-claude/
    batch-processing, read 2026-11-10 -- the doc's own example line is
    `{"custom_id":...,"result":{"type":"succeeded","message":{"id":...,"content":[...],
    "stop_reason":...,"usage":{...}}}}`). `latency_ms` is the caller's measured wall time,
    or None when there is no meaningful per-call latency (every batch reply).
    """
    from . import Reply

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


def _batch_error_detail(result: dict) -> str:
    """A short human-readable string for an errored/canceled/expired batch result line.

    CONFIRMED (same doc, "If your result has an error, its result.error will be set to the
    standard error shape", and the SDK samples' `result.result.error.error.type`): an
    `errored` result's `error` field is `{"type": "error", "error": {"type": "...",
    "message": "..."}}` -- the same nested envelope a synchronous HTTP error body uses.
    `canceled`/`expired` carry no `error` field at all (the vendor table: "User canceled...",
    "Batch reached its 24-hour expiration..."), so those fall back to the bare type.
    """
    err = result.get("error")
    if isinstance(err, dict):
        inner = err.get("error")
        if isinstance(inner, dict):
            return f"{inner.get('type', 'unknown')}: {inner.get('message', '')}"
        return json.dumps(err)
    return str(result.get("type", "unknown"))


def reply_from_batch_result(line: dict):
    """One parsed JSONL result line (`{"custom_id": ..., "result": {"type": ...}}`) -> Reply.

    `succeeded` reuses `_reply_from_message` on `result["message"]` with latency_ms=None (no
    per-call timing exists in a batch). `errored`/`canceled`/`expired` become an arm error
    with status_code=400 so cost.actual_call_cost charges $0 -- the vendor's own words: "You
    will not be billed for these requests." Any other `result.type` is unconfirmed shape and
    fails loudly (BatchError) rather than guessing.
    """
    from . import Reply

    result = line.get("result") or {}
    rtype = result.get("type")
    if rtype == "succeeded":
        message = result.get("message")
        if not isinstance(message, dict):
            raise BatchError(
                f"batch result {line.get('custom_id')!r}: type 'succeeded' with no 'message' object "
                f"(unconfirmed shape, refusing to guess): {result!r}",
                permanent=True,
            )
        return _reply_from_message(message, latency_ms=None)
    if rtype in ("errored", "canceled", "expired"):
        detail = _batch_error_detail(result)
        return Reply(
            output=None, input_tokens=None, output_tokens=None, latency_ms=None,
            error=f"batch {rtype}: {detail[:200]}", status_code=400,
        )
    raise BatchError(f"batch result {line.get('custom_id')!r}: unknown result type {rtype!r} (refusing to guess)", permanent=True)


def _batch_headers(api_key):
    return {"content-type": "application/json", "x-api-key": api_key, "anthropic-version": ANTHROPIC_VERSION}


def _open_batch_request(req, timeout, action):
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise BatchError(f"{action} HTTP {exc.code}: {detail[:200]}", status=exc.code) from exc
    except urllib.error.URLError as exc:
        raise BatchError(f"{action}: {exc}") from exc
    except (OSError, ValueError, http.client.HTTPException) as exc:
        # read timeout, truncated body (IncompleteRead) or non-JSON body
        raise BatchError(f"{action}: {type(exc).__name__}: {exc}") from exc


def create_batch(requests, api_key, timeout=60) -> dict:
    """POST /v1/messages/batches with {"requests": [{"custom_id", "params"}, ...]}.

    Returns the created Message Batch object (id, processing_status, request_counts, ...).
    Raises BatchError on any non-2xx response.
    """
    _guard_network()
    if not api_key:
        raise ValueError("ANTHROPIC_API_KEY is not set")
    body = {"requests": requests}
    req = urllib.request.Request(
        f"{API_BASE}{BATCHES_PATH}",
        data=json.dumps(body).encode("utf-8"),
        method="POST",
        headers=_batch_headers(api_key),
    )
    return _open_batch_request(req, timeout, "create_batch")


def get_batch(batch_id, api_key, timeout=30) -> dict:
    """GET /v1/messages/batches/{id}: the current Message Batch object."""
    _guard_network()
    if not api_key:
        raise ValueError("ANTHROPIC_API_KEY is not set")
    req = urllib.request.Request(
        f"{API_BASE}{BATCHES_PATH}/{batch_id}",
        method="GET",
        headers=_batch_headers(api_key),
    )
    return _open_batch_request(req, timeout, "get_batch")


def fetch_results(batch_id, api_key, timeout=120) -> list:
    """GET /v1/messages/batches/{id}/results: the JSONL results file, split into one parsed
    dict per non-blank line. Blank lines are skipped; a line that is not valid JSON raises
    (json.JSONDecodeError), never silently dropped.
    """
    _guard_network()
    if not api_key:
        raise ValueError("ANTHROPIC_API_KEY is not set")
    req = urllib.request.Request(
        f"{API_BASE}{BATCHES_PATH}/{batch_id}/results",
        method="GET",
        headers=_batch_headers(api_key),
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise BatchError(f"fetch_results HTTP {exc.code}: {detail[:200]}", status=exc.code) from exc
    except urllib.error.URLError as exc:
        raise BatchError(f"fetch_results: {exc}") from exc
    except (OSError, http.client.HTTPException) as exc:
        # a read timeout or a dropped connection while reading the results body
        raise BatchError(f"fetch_results: {type(exc).__name__}: {exc}") from exc
    results = []
    for raw_line in raw.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        try:
            results.append(json.loads(line))
        except json.JSONDecodeError as exc:
            raise BatchError(f"fetch_results: malformed JSONL line: {exc}", permanent=True) from exc
    return results


def cancel_batch(batch_id, api_key, timeout=30) -> dict:
    """POST /v1/messages/batches/{id}/cancel."""
    _guard_network()
    if not api_key:
        raise ValueError("ANTHROPIC_API_KEY is not set")
    req = urllib.request.Request(
        f"{API_BASE}{BATCHES_PATH}/{batch_id}/cancel",
        data=b"",
        method="POST",
        headers=_batch_headers(api_key),
    )
    return _open_batch_request(req, timeout, "cancel_batch")


def call(model_id, system, user_input, max_tokens, api_key, params=None, timeout=60):
    _guard_network()
    if not api_key:
        raise ValueError("ANTHROPIC_API_KEY is not set")

    body = build_params(model_id, system, user_input, max_tokens, params)

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

    from . import Reply

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
    except (OSError, ValueError, http.client.HTTPException) as exc:
        # A read timeout (TimeoutError / socket.timeout) or a body that is not valid JSON
        # surfaces from resp.read() / json.loads, not from urlopen; either is a failed call.
        latency_ms = int((time.monotonic() - started) * 1000)
        return Reply(
            output=None, input_tokens=None, output_tokens=None, latency_ms=latency_ms,
            error=f"{type(exc).__name__}: {exc}",
        )
    latency_ms = int((time.monotonic() - started) * 1000)

    return _reply_from_message(data, latency_ms)


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
