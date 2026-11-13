"""cost.actual_call_cost never charges less than the tokens a reply reports as billed.

Covers an errored reply that carries tokens (a Gemini continuation whose later hop failed,
or a single-call empty-text truncation), and one end-to-end run through runner + the real
Gemini adapter with a stubbed transport (PR #23 follow-up).
"""

import json
import urllib.error

import pytest

from pinbench import cost, prices, providers, runner, suite
from pinbench.providers import gemini

PRICE = prices.PriceRow(model="x", input=1.0, output=1.0, source="x")  # $1 per 1M tokens each way
TOKENS = 1_000_000 + 500_000  # 1M in + 0.5M out -> $1.50


def _reply(**kw):
    base = dict(output=None, input_tokens=1_000_000, output_tokens=500_000, latency_ms=1, error="boom")
    base.update(kw)
    return providers.Reply(**base)


def test_4xx_after_paid_hops_charges_token_cost():
    assert cost.actual_call_cost(_reply(status_code=429), PRICE, 0.01) == pytest.approx(1.5)
    assert cost.actual_call_cost(_reply(status_code=400), PRICE) == pytest.approx(1.5)


@pytest.mark.parametrize("status_code", [500, 503, None])  # None: timeout or dropped connection
def test_unknown_outcome_charges_higher_of_tokens_and_estimate(status_code):
    reply = _reply(status_code=status_code)
    assert cost.actual_call_cost(reply, PRICE, 0.01) == pytest.approx(1.5)  # tokens above estimate
    assert cost.actual_call_cost(reply, PRICE, 9.0) == pytest.approx(9.0)  # estimate above tokens


def test_single_call_errored_reply_with_stop_reason_charges_exact_tokens():
    # Empty-text truncation: a real, complete response, so its usage is exact (no estimate floor).
    reply = _reply(stop_reason="max_tokens", error="empty text output")
    assert cost.actual_call_cost(reply, PRICE, 9.0) == pytest.approx(1.5)
    assert cost.actual_call_cost(reply, PRICE, 0.01) == pytest.approx(1.5)


def test_unchanged_cases():
    no_tokens = dict(input_tokens=None, output_tokens=None)
    assert cost.actual_call_cost(_reply(status_code=404, **no_tokens), PRICE, 9.0) == 0.0
    assert cost.actual_call_cost(_reply(status_code=500, **no_tokens), PRICE, 9.0) is None
    assert cost.actual_call_cost(_reply(status_code=None, **no_tokens), PRICE, 9.0) is None
    ok = _reply(error=None, output="hi")
    assert cost.actual_call_cost(ok, PRICE, 9.0) == pytest.approx(1.5)
    assert cost.actual_call_cost(_reply(error=None, output="hi", **no_tokens), PRICE, 9.0) is None


# ---- end to end: runner + real Gemini adapter, stubbed transport ----

class _Resp:
    def __init__(self, payload):
        self._body = json.dumps(payload).encode("utf-8")

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_runner_meters_hop_one_tokens_after_hop_two_429(tmp_path, fake_adapter, monkeypatch):
    monkeypatch.delenv("PINBENCH_NO_NETWORK", raising=False)
    monkeypatch.setenv("GEMINI_API_KEY", "g")
    hop1 = {
        "id": "i", "status": "requires_continuation", "continuation_token": "t",
        "steps": [{"type": "thought", "content": [{"type": "text", "text": "hidden"}]}],
        "usage": {"total_input_tokens": 100, "total_output_tokens": 0, "total_thought_tokens": 32765},
    }
    err = urllib.error.HTTPError("u", 429, "x", {}, None)
    err.read = lambda: b"rate limited"
    queue = [_Resp(hop1), err]

    def fake_urlopen(req, timeout=None):
        item = queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    monkeypatch.setattr(gemini.urllib.request, "urlopen", fake_urlopen)
    real_route = providers.route
    monkeypatch.setattr(
        providers, "route", lambda mid: gemini if mid.startswith("gemini") else real_route(mid)
    )
    monkeypatch.setattr(
        providers, "adapter_for", lambda p: gemini if p == "gemini" else fake_adapter
    )

    suite_path = tmp_path / "s.yaml"
    suite_path.write_text(
        'version: 1\nname: demo\ntask: "Answer."\nmax_output_tokens: 16\npolicy:\n  min_items: 1\n'
        'items:\n  - id: q00\n    input: "Q0?"\n    expect: "A0"\n'
    )
    p_path = tmp_path / "prices.yaml"
    p_path.write_text(
        "version: 1\nchecked: 2026-10-26\nmodels:\n"
        '  claude-sonnet-4-5: {input: 3.0, output: 15.0, source: "https://x"}\n'
        '  gemini-4-carbon: {input: 1.0, output: 10.0, source: "https://y"}\n'
    )
    s = suite.load(suite_path)
    record = runner.run(
        s, "claude-sonnet-4-5", "gemini-4-carbon", prices.load(p_path), cap_usd=5.0,
        state_dir=tmp_path / "state", suite_path=suite_path, name="r1",
    )

    paid = 100 * 1.0 / 1e6 + 32765 * 10.0 / 1e6  # hop 1 at gemini-4-carbon's price
    est = cost.estimate_arm(s, "gemini-4-carbon", prices.load(p_path).rows["gemini-4-carbon"]).cost_usd
    assert paid > 0.3 and est < paid  # a 4xx used to meter $0; the estimate would not cover it either
    assert record["spent_usd"]["candidate"] == pytest.approx(paid)
    cand_item = record["items"][0]["candidate"]
    assert cand_item["cost_usd"] == pytest.approx(paid)
    assert record["spent_usd"]["total"] >= paid
