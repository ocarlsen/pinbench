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


def test_continuation_without_token_keeps_unreported_output_count_none(monkeypatch):
    monkeypatch.delenv("PINBENCH_NO_NETWORK", raising=False)
    body = {"id": "i", "status": "requires_continuation", "steps": [], "usage": {"total_input_tokens": 7}}
    monkeypatch.setattr(gemini.urllib.request, "urlopen", lambda req, timeout=None: _Resp(body))
    r = gemini.call("gemini-4-carbon", "S", "U", 64000, "k", params={"generation_config": {}})
    assert r.error and "continuation_token" in r.error
    assert r.input_tokens == 7 and r.output_tokens is None
    assert _charge(r) == pytest.approx(EST)


EST = 0.5  # an estimate far above the $0.000007 an input-only reading would give
INPUT_ONLY_PRICE = prices.PriceRow(model="x", input=1.0, output=10.0, source="x")


def _charge(reply):
    meter = cost.CostMeter(cap_usd=100.0)
    return meter.charge(cost.actual_call_cost(reply, INPUT_ONLY_PRICE, EST), EST)


def test_gemini_empty_text_incomplete_with_input_only_usage_charges_the_estimate(monkeypatch):
    monkeypatch.delenv("PINBENCH_NO_NETWORK", raising=False)
    body = {"id": "i", "status": "incomplete", "steps": [], "usage": {"total_input_tokens": 7}}
    monkeypatch.setattr(gemini.urllib.request, "urlopen", lambda req, timeout=None: _Resp(body))
    r = gemini.call("gemini-4-carbon", "S", "U", 64000, "k", params={"generation_config": {}})
    assert r.error and "empty text" in r.error and r.stop_reason == "incomplete"
    assert r.input_tokens == 7 and r.output_tokens is None
    assert _charge(r) == pytest.approx(EST)


def test_anthropic_empty_text_max_tokens_with_input_only_usage_charges_the_estimate():
    from pinbench.providers import anthropic

    data = {"content": [], "stop_reason": "max_tokens", "usage": {"input_tokens": 7}}
    r = anthropic._reply_from_message(data, latency_ms=1)
    assert r.error and r.stop_reason == "max_tokens" and r.output_tokens is None
    assert _charge(r) == pytest.approx(EST)


# ---- each metering call site passes ITS OWN estimate (the max branch) ----
# An errored reply with a few tokens, no status and no stop_reason is an unknown outcome, so it
# must meter at the estimate (above its tiny token cost). A control reply with no tokens at all
# and a 500 meters at the estimate by the old rule; the two must match to the cent.

def _write_prices_suite(tmp_path, extra_models):
    suite_path = tmp_path / "s.yaml"
    items = "".join(f'  - id: q0{i}\n    input: "Q{i}?"\n    expect: "A{i}"\n' for i in range(2))
    suite_path.write_text(
        'version: 1\nname: demo\ntask: "Answer."\nmax_output_tokens: 16\npolicy:\n  min_items: 1\n'
        "items:\n" + items
    )
    p_path = tmp_path / "prices.yaml"
    p_path.write_text(
        "version: 1\nchecked: 2026-10-26\nmodels:\n"
        '  claude-sonnet-4-5: {input: 3.0, output: 15.0, source: "https://x"}\n'
        '  gpt-4o-mini: {input: 0.15, output: 0.6, source: "https://y"}\n'
        + extra_models
    )
    return suite_path, p_path


@pytest.mark.parametrize("failure", ["http500", "timeout"])
def test_runner_candidate_hop_two_unknown_failure_meters_the_estimate(tmp_path, fake_adapter, monkeypatch, failure):
    monkeypatch.delenv("PINBENCH_NO_NETWORK", raising=False)
    monkeypatch.setenv("GEMINI_API_KEY", "g")
    hop1 = {
        "id": "i", "status": "requires_continuation", "continuation_token": "t",
        "steps": [{"type": "thought", "content": [{"type": "text", "text": "hidden"}]}],
        "usage": {"total_input_tokens": 10, "total_output_tokens": 0, "total_thought_tokens": 10},
    }
    if failure == "http500":
        err = urllib.error.HTTPError("u", 500, "x", {}, None)
        err.read = lambda: b"boom"
    else:
        err = TimeoutError("timed out")
    queue = [_Resp(hop1), err]

    def fake_urlopen(req, timeout=None):
        item = queue.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    monkeypatch.setattr(gemini.urllib.request, "urlopen", fake_urlopen)
    real_route = providers.route
    monkeypatch.setattr(providers, "route", lambda mid: gemini if mid.startswith("gemini") else real_route(mid))
    monkeypatch.setattr(providers, "adapter_for", lambda p: gemini if p == "gemini" else fake_adapter)

    suite_path, p_path = _write_prices_suite(tmp_path, '  gemini-4-carbon: {input: 1.0, output: 10.0, source: "https://y"}\n')
    s = suite.load(suite_path)
    prc = prices.load(p_path)
    queue.append(err)  # q01: the first hop fails outright (no tokens), the control
    record = runner.run(
        s, "claude-sonnet-4-5", "gemini-4-carbon", prc, cap_usd=5.0,
        state_dir=tmp_path / "state", suite_path=suite_path, name="r1",
    )
    est = cost.estimate_arm(s, "gemini-4-carbon", prc.rows["gemini-4-carbon"]).items[0].cost_usd
    token_cost = 10 * 1.0 / 1e6 + 10 * 10.0 / 1e6
    assert est > token_cost
    by_id = {i["id"]: i["candidate"] for i in record["items"]}
    assert by_id["q00"]["cost_usd"] == pytest.approx(est)
    assert by_id["q01"]["cost_usd"] == pytest.approx(est)  # control: no tokens, estimate
    assert record["spent_usd"]["candidate"] == pytest.approx(2 * est)


def test_runner_incumbent_errored_with_tokens_meters_the_incumbent_estimate(tmp_path, fake_adapter):
    suite_path, p_path = _write_prices_suite(tmp_path, "")
    s = suite.load(suite_path)
    prc = prices.load(p_path)
    # q00: tokens, no status, no stop_reason. q01: no tokens, 500 (control, meters the estimate).
    fake_adapter.set_reply("claude-sonnet-4-5", "Q0?", providers.Reply(
        output=None, input_tokens=2, output_tokens=1, latency_ms=1, error="connection reset"))
    fake_adapter.set_reply("claude-sonnet-4-5", "Q1?", providers.Reply(
        output=None, input_tokens=None, output_tokens=None, latency_ms=1, error="HTTP 500: x", status_code=500))
    record = runner.run(
        s, "claude-sonnet-4-5", "gpt-4o-mini", prc, cap_usd=5.0,
        state_dir=tmp_path / "state", suite_path=suite_path, name="r2",
    )
    est = cost.estimate_arm(s, "claude-sonnet-4-5", prc.rows["claude-sonnet-4-5"]).items[0].cost_usd
    assert est > (2 * 3.0 + 1 * 15.0) / 1e6
    by_id = {i["id"]: i["incumbent"] for i in record["items"]}
    assert by_id["q00"]["cost_usd"] == pytest.approx(est)
    assert by_id["q01"]["cost_usd"] == pytest.approx(est)
    assert record["spent_usd"]["incumbent"] == pytest.approx(2 * est)


def test_judge_stage_errored_with_tokens_meters_the_judge_estimate(monkeypatch):
    from pinbench import judge

    item = suite.Item(id="r01", input="some artifact", rubric_points=[suite.RubricPoint(id="a", description="d")])

    class _S:
        task = "Review."
        max_output_tokens = 100
        judge = suite.JudgeConfig(model="judge-model", rubric="Grade strictly.")

    s = _S()
    jprice = prices.PriceRow(model="judge-model", input=1.0, output=2.0, source="x")
    replies = [
        providers.Reply(output=None, input_tokens=3, output_tokens=1, latency_ms=1, error="connection reset"),
        providers.Reply(output=None, input_tokens=None, output_tokens=None, latency_ms=1, error="HTTP 500: x", status_code=500),
    ]

    class _Adapter:
        def call(self, *a, **k):
            return replies.pop(0)

    monkeypatch.setattr(providers, "route", lambda model_id: _Adapter())
    arm = providers.Reply(output="a finished answer", input_tokens=1, output_tokens=1, latency_ms=1, error=None, stop_reason="end_turn")
    meter = cost.CostMeter(cap_usd=5.0)
    record, _ = judge.run_judge_stage(meter, "judge-model", jprice, "key", s, item, {"incumbent": arm, "candidate": arm})
    est = cost.estimate_judge_call_cost(s, item, jprice)
    assert est > (3 * 1.0 + 1 * 2.0) / 1e6
    assert record["incumbent"]["cost_usd"] == pytest.approx(est)
    assert record["candidate"]["cost_usd"] == pytest.approx(est)
    assert meter.spent_usd == pytest.approx(2 * est)


# ---- batch path: both arm sites and the judge site (collected replies stubbed) ----

def _batch_helpers():
    import test_batch as tb  # tests/ is on sys.path (rootdir conftest, no __init__)
    return tb


from test_batch import fake_http, no_sleep  # noqa: E402,F401  (fixtures reused, not redefined)


def _tokens_errored():  # unknown outcome: tokens, no status, no stop_reason
    return providers.Reply(output=None, input_tokens=2, output_tokens=1, latency_ms=None, error="connection reset")


def _no_tokens_500():  # control: the old rule meters the estimate
    return providers.Reply(output=None, input_tokens=None, output_tokens=None, latency_ms=None, error="HTTP 500: x", status_code=500)


def _patch_batch_replies(monkeypatch, overrides):
    from pinbench.providers import anthropic as anth

    real = anth.reply_from_batch_result
    monkeypatch.setattr(
        anth, "reply_from_batch_result",
        lambda line: overrides[line["custom_id"]]() if line["custom_id"] in overrides else real(line),
    )


def test_batch_arm_sites_meter_their_own_estimates(tmp_path, monkeypatch, fake_http, no_sleep):
    tb = _batch_helpers()
    suite_path = tb._write_suite(tmp_path, n=3)
    prices_path = tb._write_prices(tmp_path)
    fake_http.create_responses = [tb._batch_obj(tb.ARMS_ID, "in_progress", {"processing": 6})]
    fake_http.status_sequences[tb.ARMS_ID] = [tb._batch_obj(tb.ARMS_ID, "ended", {"succeeded": 6})]
    fake_http.results[tb.ARMS_ID] = "\n".join(
        tb._succeeded_line(f"000{i}-{arm}", "ok") for i in range(3) for arm in ("incumbent", "candidate")
    )
    _patch_batch_replies(monkeypatch, {
        "0000-incumbent": _tokens_errored, "0000-candidate": _tokens_errored,
        "0001-incumbent": _no_tokens_500, "0001-candidate": _no_tokens_500,
    })
    rc = tb._run_cli(
        tmp_path, monkeypatch,
        ["run", str(suite_path), "--incumbent", "claude-sonnet-5-5", "--candidate", "claude-haiku-5-5",
         "--cap", "5.0", "--prices", str(prices_path), "--batch", "--name", "b1"],
    )
    assert rc == 0
    run = json.loads((tmp_path / ".pinbench" / "runs" / "b1" / "run.json").read_text())
    items = {i["id"]: i for i in run["items"]}
    for arm, rate_in, rate_out in (("incumbent", 2.0, 10.0), ("candidate", 0.1, 0.5)):
        est = items["q01"][arm]["cost_usd"]  # control: meters the estimate
        assert est > (2 * rate_in + 1 * rate_out) * 0.5 / 1e6
        assert items["q00"][arm]["cost_usd"] == pytest.approx(est)
    assert items["q01"]["incumbent"]["cost_usd"] != pytest.approx(items["q01"]["candidate"]["cost_usd"])


def test_batch_judge_site_meters_the_judge_estimate(tmp_path, monkeypatch, fake_http, no_sleep):
    tb = _batch_helpers()
    suite_path = tb._write_suite(tmp_path, n=2, judge=True)
    prices_path = tb._write_prices(tmp_path)
    fake_http.create_responses = [
        tb._batch_obj(tb.ARMS_ID, "in_progress", {"processing": 4}),
        tb._batch_obj(tb.JUDGE_ID, "in_progress", {"processing": 4}),
    ]
    fake_http.status_sequences[tb.ARMS_ID] = [tb._batch_obj(tb.ARMS_ID, "ended", {"succeeded": 4})]
    fake_http.status_sequences[tb.JUDGE_ID] = [tb._batch_obj(tb.JUDGE_ID, "ended", {"succeeded": 4})]
    fake_http.results[tb.ARMS_ID] = "\n".join(
        tb._succeeded_line(f"000{i}-{arm}", "ok") for i in range(2) for arm in ("incumbent", "candidate")
    )
    fake_http.results[tb.JUDGE_ID] = "\n".join(
        tb._succeeded_line(f"000{i}-judge-{arm}", "x", model="claude-haiku-4-5") for i in range(2) for arm in ("incumbent", "candidate")
    )
    _patch_batch_replies(monkeypatch, {
        "0000-judge-incumbent": _tokens_errored, "0000-judge-candidate": _tokens_errored,
        "0001-judge-incumbent": _no_tokens_500, "0001-judge-candidate": _no_tokens_500,
    })
    rc = tb._run_cli(
        tmp_path, monkeypatch,
        ["run", str(suite_path), "--incumbent", "claude-sonnet-5-5", "--candidate", "claude-haiku-5-5",
         "--cap", "5.0", "--prices", str(prices_path), "--batch", "--name", "b2"],
    )
    assert rc == 0
    run = json.loads((tmp_path / ".pinbench" / "runs" / "b2" / "run.json").read_text())
    items = {i["id"]: i for i in run["items"]}
    for arm in ("incumbent", "candidate"):
        est = items["q01"]["judge"][arm]["cost_usd"]
        assert est > (2 * 0.1 + 1 * 0.5) * 0.5 / 1e6
        assert items["q00"]["judge"][arm]["cost_usd"] == pytest.approx(est)
