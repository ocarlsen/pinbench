"""Hermetic coverage for per-model `request_defaults` (prices.yaml) reaching the request
each arm actually sends, with the suite's own `params` winning on any key both set, and a
model with no request_defaults row sending no extra key at all. This is the fix for the
third live run (.pinbench/runs/summaries-v3, 2026-10-26): the adapter hard-coded
`thinking: {"type": "disabled"}` for every Claude model, and claude-sonnet-5-5 rejected it
with an HTTP 400 asking for `{"type": "between_tools"}` instead -- a per-model fact that
now lives in prices.yaml, not in the adapter.
"""

from __future__ import annotations

import json

from pinbench import prices, providers, runner, suite


def _suite(tmp_path, n=2, extra_params=""):
    items = "\n".join(
        f'  - id: q{i:02d}\n    input: "Q{i}?"\n    expect: "A{i}"' for i in range(n)
    )
    path = tmp_path / "demo.yaml"
    path.write_text(
        f"""\
version: 1
name: demo
task: "Answer briefly."
max_output_tokens: 16
policy:
  min_items: 1
{extra_params}items:
{items}
"""
    )
    return path


def _prices_with_defaults(tmp_path):
    path = tmp_path / "prices.yaml"
    path.write_text(
        """\
version: 1
checked: 2026-10-26
models:
  claude-sonnet-5-5:
    input: 2.0
    output: 10.0
    source: "https://x"
    request_defaults:
      thinking: {type: between_tools}
  claude-haiku-5-5:
    input: 0.1
    output: 0.5
    source: "https://y"
    request_defaults:
      thinking: {type: disabled}
  gpt-5-mini:
    input: 0.25
    output: 2.0
    source: "https://z"
"""
    )
    return prices.load(path)


def _install_capturing_call(fake_adapter):
    """Record every `params` dict each arm actually received, keyed by model id."""
    seen = {}

    def fake_call(model_id, system, user_input, max_tokens, api_key, params=None, timeout=60):
        fake_adapter.call_count += 1
        fake_adapter.calls.append((model_id, user_input))
        seen.setdefault(model_id, []).append(dict(params or {}))
        if not api_key:
            raise ValueError("missing api key")
        return providers.Reply(output="ok", input_tokens=10, output_tokens=2, latency_ms=5, error=None)

    fake_adapter.call = fake_call
    return seen


def test_per_model_defaults_reach_the_request_body(tmp_path, fake_adapter):
    suite_path = _suite(tmp_path, n=2)
    s = suite.load(suite_path)
    p = _prices_with_defaults(tmp_path)
    seen = _install_capturing_call(fake_adapter)

    record = runner.run(
        s, "claude-sonnet-5-5", "claude-haiku-5-5", p, cap_usd=1.0,
        state_dir=tmp_path / "state", suite_path=suite_path, name="r1",
    )

    # Each arm got its OWN model's request_defaults, not the other arm's.
    for params in seen["claude-sonnet-5-5"]:
        assert params == {"thinking": {"type": "between_tools"}}
    for params in seen["claude-haiku-5-5"]:
        assert params == {"thinking": {"type": "disabled"}}

    # And run.json recorded exactly what was sent, per arm.
    assert record["request_params"]["incumbent"] == {
        "max_output_tokens": 16, "thinking": {"type": "between_tools"},
    }
    assert record["request_params"]["candidate"] == {
        "max_output_tokens": 16, "thinking": {"type": "disabled"},
    }


def test_suite_params_override_model_request_defaults(tmp_path, fake_adapter):
    suite_path = _suite(tmp_path, n=1, extra_params="params:\n  thinking: {type: adaptive}\n")
    s = suite.load(suite_path)
    p = _prices_with_defaults(tmp_path)
    seen = _install_capturing_call(fake_adapter)

    record = runner.run(
        s, "claude-sonnet-5-5", "claude-haiku-5-5", p, cap_usd=1.0,
        state_dir=tmp_path / "state", suite_path=suite_path, name="r2",
    )

    # The suite explicitly set `thinking`, so it wins over BOTH models' own defaults.
    for params in seen["claude-sonnet-5-5"]:
        assert params == {"thinking": {"type": "adaptive"}}
    for params in seen["claude-haiku-5-5"]:
        assert params == {"thinking": {"type": "adaptive"}}

    assert record["request_params"]["incumbent"]["thinking"] == {"type": "adaptive"}
    assert record["request_params"]["candidate"]["thinking"] == {"type": "adaptive"}


def test_model_with_no_request_defaults_sends_no_extra_key(tmp_path, fake_adapter):
    suite_path = _suite(tmp_path, n=1)
    s = suite.load(suite_path)
    p = _prices_with_defaults(tmp_path)  # gpt-5-mini has no request_defaults row
    seen = _install_capturing_call(fake_adapter)

    record = runner.run(
        s, "claude-sonnet-5-5", "gpt-5-mini", p, cap_usd=1.0,
        state_dir=tmp_path / "state", suite_path=suite_path, name="r3",
    )

    for params in seen["gpt-5-mini"]:
        assert params == {}
    assert record["request_params"]["candidate"] == {"max_output_tokens": 16}
    # The incumbent is unaffected by the candidate having no defaults.
    for params in seen["claude-sonnet-5-5"]:
        assert params == {"thinking": {"type": "between_tools"}}


def test_live_run_shape_claude_sonnet_5_5_rejects_disabled_claude_haiku_accepts(tmp_path, fake_adapter):
    """Reproduces the exact failure that motivated this fix (summaries-v3, 2026-10-26):
    claude-sonnet-5-5 400s on `thinking: {"type": "disabled"}`. With the model's correct
    request_defaults (`between_tools`) now sourced from prices.yaml instead of a hard-coded
    adapter default, the call that used to 400 now succeeds."""
    suite_path = _suite(tmp_path, n=1)
    s = suite.load(suite_path)
    p = _prices_with_defaults(tmp_path)

    def fake_call(model_id, system, user_input, max_tokens, api_key, params=None, timeout=60):
        fake_adapter.call_count += 1
        fake_adapter.calls.append((model_id, user_input))
        if not api_key:
            raise ValueError("missing api key")
        thinking = (params or {}).get("thinking")
        if model_id == "claude-sonnet-5-5" and thinking == {"type": "disabled"}:
            return providers.Reply(
                output=None, input_tokens=None, output_tokens=None, latency_ms=50,
                error=(
                    'HTTP 400: {"type":"error","error":{"type":"invalid_request_error",'
                    '"message":"To turn thinking off on this model, send \\"thinking\\": '
                    '{\\"type\\": \\"between_tools\\"} instead of {\\"type\\": \\"disabled\\"}."}}'
                ),
                status_code=400,
            )
        return providers.Reply(output="ok", input_tokens=10, output_tokens=2, latency_ms=5, error=None, stop_reason="end_turn")

    fake_adapter.call = fake_call

    record = runner.run(
        s, "claude-sonnet-5-5", "claude-haiku-5-5", p, cap_usd=1.0,
        state_dir=tmp_path / "state", suite_path=suite_path, name="r4",
    )

    assert record["items_succeeded"]["incumbent"] == 1  # no longer 400s
    assert record["items_succeeded"]["candidate"] == 1
    on_disk = json.loads((tmp_path / "state" / "runs" / "r4" / "run.json").read_text())
    for item in on_disk["items"]:
        assert item["incumbent"]["error"] is None
        assert item["candidate"]["error"] is None
