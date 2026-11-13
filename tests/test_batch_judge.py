"""Offline tests for `--batch-judge`: arms run synchronously (a fake adapter stands in for
Anthropic and Gemini), every judge call goes out as ONE Message Batch (the `FakeBatchHTTP`
stub from tests/test_batch.py), priced at the judge row's batch_multiplier.

Each test is written to fail if the behavior it names is reverted in pinbench/batch.py,
runner.py, report.py or cli.py (proven in the PR by reverting each in a scratch copy)."""

from __future__ import annotations

import io
import json
import urllib.error

import pytest
import yaml

from test_batch import (  # noqa: F401 - the fixtures must be importable to be used
    FakeBatchHTTP, _FakeResponse, _batch_obj, _run_cli, _succeeded_line, _write_prices, _write_suite,
    fake_http, no_sleep,
)

from pinbench import batch as batch_mod
from pinbench import cost as cost_mod
from pinbench import judge as judge_mod
from pinbench import prices as prices_mod
from pinbench import providers
from pinbench import runner as runner_mod
from pinbench import suite as suite_mod
from pinbench.providers import anthropic as anthropic_provider

JUDGE = "claude-haiku-4-5"
JUDGE_BATCH = "msgbatch_judge"
GEMINI = "gemini-4-carbon"
JUDGE_DEFAULTS = {"thinking": {"type": "between_tools"}}


def _verdict(i, extra=1, notes="fine"):
    return json.dumps({"hit": [f"p{i}"], "extra": extra, "notes": notes})


VERDICT = _verdict(0)


def _prices_file(tmp_path, judge_multiplier=0.5, gpt_multiplier=None):
    judge_row = {"input": 0.1, "output": 0.5, "source": "https://v", "request_defaults": JUDGE_DEFAULTS}
    if judge_multiplier is not None:
        judge_row["batch_multiplier"] = judge_multiplier
    return _write_prices(tmp_path, {
        GEMINI: {"input": 1.0, "output": 4.0, "source": "https://g"},
        # priced for batch, so only the provider check can refuse it as a judge
        "gpt-5-mini": {"input": 0.25, "output": 2.0, "source": "https://w", "batch_multiplier": 0.5},
        JUDGE: judge_row,
    })


def _suite_file(tmp_path, n=2, judge_model=None, judge=True):
    path = _write_suite(tmp_path, n=n, judge=judge)
    if judge_model is not None:
        data = yaml.safe_load(path.read_text())
        data["judge"]["model"] = judge_model
        path.write_text(yaml.safe_dump(data, sort_keys=False))
    return path


def _args(suite_path, prices_path, *extra, incumbent="claude-sonnet-5-5", candidate=GEMINI, name="bj"):
    return ["run", str(suite_path), "--incumbent", incumbent, "--candidate", candidate,
            "--cap", "5.0", "--prices", str(prices_path), "--name", name, *extra]


def _judge_lines(n, arms=("incumbent", "candidate"), input_tokens=100, output_tokens=10, extra=1, notes="fine", per_arm=None):
    """`per_arm` {arm: (extra, notes)} gives the two arms different verdicts."""
    def text(i, arm):
        e, nt = (per_arm or {}).get(arm, (extra, notes))
        return _verdict(i, e, nt)

    return "\n".join(
        _succeeded_line(f"{i:04d}-judge-{arm}", text(i, arm), input_tokens=input_tokens, output_tokens=output_tokens, model=JUDGE)
        for i in range(n) for arm in arms
    )


def _script_judge_batch(http, n=2, batch_id=JUDGE_BATCH, arms=("incumbent", "candidate"), **kw):
    http.create_responses.append(_batch_obj(batch_id, "in_progress", {"processing": n * len(arms)}))
    http.status_sequences[batch_id] = [_batch_obj(batch_id, "ended", {"succeeded": n * len(arms)})]
    http.results[batch_id] = _judge_lines(n, arms, **kw)


@pytest.fixture
def gemini_env(monkeypatch, fake_adapter):
    monkeypatch.setenv("GEMINI_API_KEY", "test-gemini-key")
    return fake_adapter


def _load(tmp_path, name="bj"):
    return json.loads((tmp_path / ".pinbench" / "runs" / name / "run.json").read_text())


# --- refusals, all before any call or POST --------------------------------------------------

def _assert_refused_clean(rc, http, adapter):
    assert rc == 2
    assert http.create_posts == []
    assert adapter.call_count == 0


def test_refuses_with_batch(tmp_path, monkeypatch, fake_http, gemini_env):
    rc = _run_cli(tmp_path, monkeypatch, _args(_suite_file(tmp_path), _prices_file(tmp_path), "--batch", "--batch-judge",
                                               candidate="claude-haiku-5-5"))
    _assert_refused_clean(rc, fake_http, gemini_env)


def test_refuses_suite_without_judge(tmp_path, monkeypatch, fake_http, gemini_env):
    rc = _run_cli(tmp_path, monkeypatch, _args(_suite_file(tmp_path, judge=False), _prices_file(tmp_path), "--batch-judge"))
    _assert_refused_clean(rc, fake_http, gemini_env)


def test_refuses_non_anthropic_judge(tmp_path, monkeypatch, fake_http, gemini_env):
    suite_path = _suite_file(tmp_path, judge_model="gpt-5-mini")
    rc = _run_cli(tmp_path, monkeypatch, _args(suite_path, _prices_file(tmp_path), "--batch-judge"))
    _assert_refused_clean(rc, fake_http, gemini_env)


def test_refuses_judge_row_without_batch_multiplier(tmp_path, monkeypatch, fake_http, gemini_env):
    rc = _run_cli(tmp_path, monkeypatch, _args(_suite_file(tmp_path), _prices_file(tmp_path, judge_multiplier=None), "--batch-judge"))
    _assert_refused_clean(rc, fake_http, gemini_env)


def test_plan_refuses_the_same_way(tmp_path, monkeypatch, fake_http, gemini_env):
    suite_path = _suite_file(tmp_path)
    prices_path = _prices_file(tmp_path, judge_multiplier=None)
    rc = _run_cli(tmp_path, monkeypatch, ["plan", str(suite_path), "--incumbent", "claude-sonnet-5-5", "--candidate", GEMINI,
                                          "--cap", "5.0", "--prices", str(prices_path), "--batch-judge"])
    assert rc == 2


# --- plan estimates the judge at the batch price, arms at the sync price --------------------

def test_plan_prices_only_the_judge_at_batch(tmp_path, gemini_env):
    prices = prices_mod.load(_prices_file(tmp_path))
    suite = suite_mod.load(_suite_file(tmp_path))
    sync = runner_mod.plan(suite, "claude-sonnet-5-5", GEMINI, prices)
    bj = runner_mod.plan(suite, "claude-sonnet-5-5", GEMINI, prices, batch_judge=True)
    assert bj.estimate.judge.cost_usd == pytest.approx(sync.estimate.judge.cost_usd * 0.5)
    assert bj.estimate.incumbent.cost_usd == sync.estimate.incumbent.cost_usd
    assert bj.estimate.candidate.cost_usd == sync.estimate.candidate.cost_usd


# --- sync Gemini candidate + batched judge, end to end -------------------------------------

def test_sync_gemini_arms_with_batched_judge(tmp_path, monkeypatch, fake_http, gemini_env, no_sleep, capsys):
    _script_judge_batch(fake_http)
    rc = _run_cli(tmp_path, monkeypatch, _args(_suite_file(tmp_path), _prices_file(tmp_path), "--batch-judge"))
    assert rc == 0
    # arms: 2 items x 2 arms made synchronously; the judge made NO sync call
    assert gemini_env.call_count == 4
    assert {m for m, _ in gemini_env.calls} == {"claude-sonnet-5-5", GEMINI}
    # exactly one POST, four judge requests, none for an arm
    assert len(fake_http.create_posts) == 1
    ids = sorted(r["custom_id"] for r in fake_http.create_posts[0]["requests"])
    assert ids == ["0000-judge-candidate", "0000-judge-incumbent", "0001-judge-candidate", "0001-judge-incumbent"]
    assert {r["params"]["model"] for r in fake_http.create_posts[0]["requests"]} == {JUDGE}

    run = _load(tmp_path)
    assert "batch" not in run  # the arms were NOT batch-priced
    assert run["judge_batch"]["id"] == JUDGE_BATCH
    assert run["judge_batch"]["price_multiplier"] == 0.5
    per_call = (100 * 0.1 * 0.5 + 10 * 0.5 * 0.5) / 1e6
    assert run["spent_usd"]["judge"] == pytest.approx(4 * per_call)
    assert run["items"][0]["judge"]["incumbent"]["hit"] == ["p0"]
    assert run["items"][0]["judge"]["incumbent"]["cost_usd"] == pytest.approx(per_call)
    # arm cost is the sync price: sonnet 2.0/10.0 per Mtok on the fake adapter's tokens
    inc = run["items"][0]["incumbent"]
    assert inc["cost_usd"] == pytest.approx(inc["input_tokens"] * 2.0 / 1e6 + inc["output_tokens"] * 10.0 / 1e6)
    batch_json = json.loads((tmp_path / ".pinbench" / "runs" / "bj" / "batch.json").read_text())
    assert batch_json["judge"] == {"id": JUDGE_BATCH}

    capsys.readouterr()
    assert _run_cli(tmp_path, monkeypatch, ["report", "bj", "--state", str(tmp_path / ".pinbench")]) == 0
    out = capsys.readouterr().out
    assert f"judge: batched (id {JUDGE_BATCH})" in out


def test_poll_interval_and_max_wait_are_used(tmp_path, monkeypatch, fake_http, gemini_env, no_sleep):
    fake_http.create_responses.append(_batch_obj(JUDGE_BATCH, "in_progress", {"processing": 4}))
    fake_http.status_sequences[JUDGE_BATCH] = [_batch_obj(JUDGE_BATCH, "in_progress", {"processing": 4})]
    rc = _run_cli(tmp_path, monkeypatch, _args(_suite_file(tmp_path), _prices_file(tmp_path), "--batch-judge",
                                               "--poll-interval", "7", "--max-wait", "7"))
    assert rc == 5
    assert no_sleep == [7]  # one sleep of the given interval, then the max-wait gives up


# --- judge bodies are byte-identical to a sync judge call ----------------------------------

def test_judge_bodies_byte_identical_to_sync(tmp_path, monkeypatch, fake_http, gemini_env, no_sleep):
    _script_judge_batch(fake_http)
    suite_path = _suite_file(tmp_path)
    assert _run_cli(tmp_path, monkeypatch, _args(suite_path, _prices_file(tmp_path), "--batch-judge")) == 0
    batch_body = next(r["params"] for r in fake_http.create_posts[0]["requests"] if r["custom_id"] == "0000-judge-incumbent")
    assert "thinking" in batch_body  # the judge row's request_defaults travelled

    suite = suite_mod.load(suite_path)
    prices = prices_mod.load(tmp_path / "prices.yaml")
    captured = {}

    def sync_urlopen(req, timeout=None):
        captured["body"] = json.loads(req.data.decode("utf-8"))
        return _FakeResponse(json.dumps({
            "id": "m", "type": "message", "role": "assistant", "content": [{"type": "text", "text": VERDICT}],
            "stop_reason": "end_turn", "usage": {"input_tokens": 5, "output_tokens": 2},
        }).encode("utf-8"))

    monkeypatch.setattr(anthropic_provider.urllib.request, "urlopen", sync_urlopen)
    monkeypatch.setattr(providers, "route", lambda model_id: providers.ADAPTERS["anthropic"])
    judge_mod.judge_output(JUDGE, suite.items[0], suite, "echo:Q0?", "key", params=prices.rows[JUDGE].request_defaults)
    assert json.dumps(batch_body, sort_keys=True) == json.dumps(captured["body"], sort_keys=True)


# --- arms the judge must skip are recorded, never sent -------------------------------------

def test_skipped_arms_are_recorded_not_sent(tmp_path, monkeypatch, fake_http, gemini_env, no_sleep):
    gemini_env.set_reply(GEMINI, "Q0?", providers.Reply(output="cut off", input_tokens=5, output_tokens=64, latency_ms=1, stop_reason="max_tokens"))
    gemini_env.set_reply("claude-sonnet-5-5", "Q1?", providers.Reply(output=None, input_tokens=None, output_tokens=None, latency_ms=1, error="boom", status_code=500))
    _script_judge_batch(fake_http, arms=("incumbent", "candidate"))
    fake_http.results[JUDGE_BATCH] = "\n".join([
        _succeeded_line("0000-judge-incumbent", VERDICT, model=JUDGE),
        _succeeded_line("0001-judge-candidate", _verdict(1), model=JUDGE),
    ])
    assert _run_cli(tmp_path, monkeypatch, _args(_suite_file(tmp_path), _prices_file(tmp_path), "--batch-judge")) == 0
    sent = sorted(r["custom_id"] for r in fake_http.create_posts[0]["requests"])
    assert sent == ["0000-judge-incumbent", "0001-judge-candidate"]
    run = _load(tmp_path)
    assert run["items"][0]["judge"]["candidate"]["skipped"] == "skipped: arm output truncated"
    assert run["items"][1]["judge"]["incumbent"]["skipped"] == "skipped: arm error"
    assert run["items"][0]["judge"]["candidate"]["cost_usd"] == 0.0


# --- the cap is checked against arms spend + the whole judge batch BEFORE submitting --------

def test_cap_reserve_refuses_the_batch_before_submit(tmp_path, monkeypatch, fake_http, gemini_env, no_sleep):
    # The candidate's actual cost lands arms spend UNDER the cap, but by less than the judge
    # batch's estimate, though the pre-run estimate fit. (Spend already over the cap would
    # refuse even a zero-cost check, so it would not prove the estimate is reserved.)
    suite_path = _suite_file(tmp_path, n=1)
    suite = suite_mod.load(suite_path)
    batch_row = prices_mod.load(_prices_file(tmp_path)).rows[JUDGE].at_batch()
    judge_est = 2 * cost_mod.estimate_judge_call_cost(suite, suite.items[0], batch_row)
    cap = 2.0
    inc_cost = (len("Answer briefly.") + len("Q0?")) * 2.0 / 1e6 + 4 * 10.0 / 1e6  # the fake adapter's default reply
    cand_cost = cap - judge_est / 2 - inc_cost
    gemini_env.set_reply(GEMINI, "Q0?", providers.Reply(output="ok", input_tokens=round(cand_cost * 1e6), output_tokens=0, latency_ms=1, stop_reason="end_turn"))
    args = _args(suite_path, _prices_file(tmp_path), "--batch-judge")
    args[args.index("--cap") + 1] = str(cap)
    assert _run_cli(tmp_path, monkeypatch, args) == 0
    run = _load(tmp_path)
    assert run["spent_usd"]["total"] < cap < run["spent_usd"]["total"] + judge_est  # the setup holds
    assert fake_http.create_posts == []
    assert run["stopped_early"] is True
    assert run["judge_batch"]["status"] == "not_submitted_cap"
    assert "exceed the cap" in run["items"][0]["judge"]["incumbent"]["error"]
    assert run["spent_usd"]["judge"] == 0.0
    assert run["items"][0]["candidate"]["output"] == "ok"  # paid arm output kept


# --- arms are on disk before the batch is submitted ----------------------------------------

def test_arms_persisted_before_submit(tmp_path, monkeypatch, fake_http, gemini_env, no_sleep):
    _script_judge_batch(fake_http)
    seen = {}
    inner = fake_http.urlopen

    def spying_urlopen(req, timeout=None):
        if req.get_method() == "POST" and req.full_url.endswith("/v1/messages/batches"):
            seen["run"] = _load(tmp_path)
        return inner(req, timeout)

    monkeypatch.setattr(anthropic_provider.urllib.request, "urlopen", spying_urlopen)
    assert _run_cli(tmp_path, monkeypatch, _args(_suite_file(tmp_path), _prices_file(tmp_path), "--batch-judge")) == 0
    before = seen["run"]
    assert [i["incumbent"]["output"] for i in before["items"]] == ["echo:Q0?", "echo:Q1?"]
    assert [i["candidate"]["output"] for i in before["items"]] == ["echo:Q0?", "echo:Q1?"]
    assert before["judge_batch"]["status"] == "pending"
    assert "not judged" in before["items"][0]["judge"]["incumbent"]["error"]


# --- max-wait timeout: not judged, never guessed, INCOMPLETE ------------------------------

def test_timeout_leaves_unjudged_records_and_incomplete_verdict(tmp_path, monkeypatch, fake_http, gemini_env, no_sleep, capsys):
    fake_http.create_responses.append(_batch_obj(JUDGE_BATCH, "in_progress", {"processing": 4}))
    fake_http.status_sequences[JUDGE_BATCH] = [_batch_obj(JUDGE_BATCH, "in_progress", {"processing": 4})]
    suite_path = _suite_file(tmp_path)
    args = _args(suite_path, _prices_file(tmp_path), "--batch-judge", "--poll-interval", "1", "--max-wait", "1")
    assert _run_cli(tmp_path, monkeypatch, args) == 5  # nonzero: the judge did not finish
    run = _load(tmp_path)
    assert run["stopped_early"] is False  # the arms finished; only the judge is unresolved
    assert run["judge_batch"]["status"] == "timed_out"
    # the batch still bills: its judge spend is the reserved estimate, never 0, computed here
    suite = suite_mod.load(suite_path)
    batch_row = prices_mod.load(tmp_path / "prices.yaml").rows[JUDGE].at_batch()
    reserved = sum(cost_mod.estimate_judge_call_cost(suite, it, batch_row) for it in suite.items) * 2
    assert run["spent_usd"]["judge"] == pytest.approx(reserved)
    assert run["judge_batch"]["spend_basis"] == "reserved_estimate"
    assert run["judge_batch"]["id"] == JUDGE_BATCH
    for item in run["items"]:
        for arm in ("incumbent", "candidate"):
            rec = item["judge"][arm]
            assert rec["hit"] == [] and "not judged" in rec["error"] and rec["cost_usd"] == 0.0
    assert run["items"][0]["candidate"]["output"] == "echo:Q0?"
    assert fake_http.cancel_posts == []  # left running, never canceled
    assert json.loads((tmp_path / ".pinbench" / "runs" / "bj" / "batch.json").read_text())["judge"]["id"] == JUDGE_BATCH
    capsys.readouterr()
    _run_cli(tmp_path, monkeypatch, ["report", "bj", "--state", str(tmp_path / ".pinbench")])
    assert "INCOMPLETE" in capsys.readouterr().out


# --- --reuse-incumbent: only the candidate's judge calls are batched ----------------------

def _source_then_reuse(tmp_path, monkeypatch, fake_http):
    """Run 1: --batch-judge, incumbent sonnet vs gemini. Run 2: reuse sonnet's outputs and
    judge records against a haiku candidate, also --batch-judge."""
    prices_path = _prices_file(tmp_path)
    suite_path = _suite_file(tmp_path)
    _script_judge_batch(fake_http, input_tokens=200, output_tokens=20, extra=3, notes="source")
    assert _run_cli(tmp_path, monkeypatch, _args(suite_path, prices_path, "--batch-judge", name="src")) == 0
    fake_http.create_posts.clear()
    _script_judge_batch(fake_http, batch_id="msgbatch_judge2", arms=("candidate",))
    rc = _run_cli(tmp_path, monkeypatch, _args(suite_path, prices_path, "--batch-judge", "--reuse-incumbent", "src",
                                               candidate="claude-haiku-5-5", name="re"))
    return rc


def test_reuse_incumbent_batches_only_candidate_judge_calls(tmp_path, monkeypatch, fake_http, gemini_env, no_sleep):
    assert _source_then_reuse(tmp_path, monkeypatch, fake_http) == 0
    sent = sorted(r["custom_id"] for r in fake_http.create_posts[0]["requests"])
    assert sent == ["0000-judge-candidate", "0001-judge-candidate"]
    src, new = _load(tmp_path, "src"), _load(tmp_path, "re")
    assert new["judge_batch"]["id"] == "msgbatch_judge2"
    for i in range(2):
        carried = new["items"][i]["judge"]["incumbent"]
        assert carried["extra"] == 3 and carried["notes"] == "source"
        assert carried["reused_cost_usd"] == src["items"][i]["judge"]["incumbent"]["cost_usd"]
        assert new["items"][i]["judge"]["candidate"]["notes"] == "fine"
    assert new["spent_usd"]["judge"] == pytest.approx(sum(i["judge"]["candidate"]["cost_usd"] for i in new["items"]))
    assert new["spent_usd"]["incumbent"] == 0.0


# --- round trip: a later reuse of a --batch-judge run rescales the judge, not the arms -----

def test_reuse_of_batch_judge_run_rescales_judge_cost_only(tmp_path, monkeypatch, fake_http, gemini_env, no_sleep):
    assert _source_then_reuse(tmp_path, monkeypatch, fake_http) == 0
    src, new = _load(tmp_path, "src"), _load(tmp_path, "re")
    assert "batch" not in src and src["judge_batch"]["price_multiplier"] == 0.5
    for i in range(2):
        src_judge = src["items"][i]["judge"]["incumbent"]["cost_usd"]
        carried = new["items"][i]["judge"]["incumbent"]
        # judge cost is back at the sync price: recorded / the judge batch multiplier
        assert carried["cost_usd"] == pytest.approx(src_judge / 0.5)
        # the arm was sync-priced in the source, so it is repriced from tokens, never rescaled
        src_arm = src["items"][i]["incumbent"]
        assert src_arm["cost_usd"] == pytest.approx(src_arm["input_tokens"] * 2.0 / 1e6 + src_arm["output_tokens"] * 10.0 / 1e6)
        assert new["items"][i]["incumbent"]["cost_usd"] == pytest.approx(src_arm["cost_usd"])
    assert not runner_mod._source_is_batch(src)


def test_reuse_refuses_bad_recorded_judge_batch_multiplier(tmp_path, monkeypatch, fake_http, gemini_env, no_sleep):
    assert _source_then_reuse(tmp_path, monkeypatch, fake_http) == 0
    path = tmp_path / ".pinbench" / "runs" / "src" / "run.json"
    data = json.loads(path.read_text())
    data["judge_batch"]["price_multiplier"] = 2
    path.write_text(json.dumps(data))
    suite = suite_mod.load(tmp_path / "demo.yaml")
    prices = prices_mod.load(tmp_path / "prices.yaml")
    with pytest.raises(runner_mod.PlanRefused, match="judge_batch.price_multiplier"):
        runner_mod.plan(suite, "claude-sonnet-5-5", "claude-haiku-5-5", prices, state_dir=tmp_path / ".pinbench", reuse_incumbent="src")


# --- the judge-batch metering site passes its own estimate to actual_call_cost -------------

def test_judge_batch_site_meters_the_judge_estimate(tmp_path, monkeypatch, fake_http, gemini_env, no_sleep):
    """An errored judge reply that carries tokens and has an unknown outcome (no status, no
    stop_reason) is charged max(tokens, estimate). The estimate is computed here from the
    suite and the batch-priced judge row, not read off another item in the run."""
    _script_judge_batch(fake_http, n=1)
    stub = lambda line: providers.Reply(output=None, input_tokens=2, output_tokens=1, latency_ms=None, error="connection reset")
    monkeypatch.setattr(anthropic_provider, "reply_from_batch_result", stub)
    suite_path = _suite_file(tmp_path, n=1)
    assert _run_cli(tmp_path, monkeypatch, _args(suite_path, _prices_file(tmp_path), "--batch-judge")) == 0

    suite = suite_mod.load(suite_path)
    batch_row = prices_mod.load(tmp_path / "prices.yaml").rows[JUDGE].at_batch()
    estimate = cost_mod.estimate_judge_call_cost(suite, suite.items[0], batch_row)
    token_cost = (2 * batch_row.input + 1 * batch_row.output) / 1e6
    assert estimate > token_cost
    run = _load(tmp_path)
    for arm in ("incumbent", "candidate"):
        assert run["items"][0]["judge"][arm]["cost_usd"] == pytest.approx(estimate)


# ================= round 2: retries, timeout spend, arm-phase reserve, sharper tests =============

def _http_error(code, body=b"overloaded"):
    return urllib.error.HTTPError("https://api.anthropic.com/x", code, "err", {}, io.BytesIO(body))


class Flaky:
    """Wraps FakeBatchHTTP: per-endpoint queues of faults. An entry is an exception (raised
    INSTEAD of the request reaching the fake), ("after", exc) (the fake handles the request,
    the server 'accepted' it, then exc is raised), or None (pass through)."""

    def __init__(self, inner):
        self.inner = inner
        self.faults = {"create": [], "get": [], "results": []}
        self.creates = 0
        self.results_calls = 0

    @staticmethod
    def kind(req):
        url, method = req.full_url, req.get_method()
        if url.endswith("/results"):
            return "results"
        if method == "POST" and url.endswith("/v1/messages/batches"):
            return "create"
        if method == "GET":
            return "get"
        return "other"

    def urlopen(self, req, timeout=None):
        kind = self.kind(req)
        self.creates += kind == "create"
        self.results_calls += kind == "results"
        queue = self.faults.get(kind)
        if queue:
            fault = queue.pop(0)
            if isinstance(fault, tuple):
                self.inner.urlopen(req, timeout)
                raise fault[1]
            if fault is not None:
                raise fault
        return self.inner.urlopen(req, timeout)


@pytest.fixture
def flaky(monkeypatch, fake_http):
    f = Flaky(fake_http)
    monkeypatch.setattr(anthropic_provider.urllib.request, "urlopen", f.urlopen)
    return f


def _run_default(tmp_path, monkeypatch, *extra, n=2):
    return _run_cli(tmp_path, monkeypatch, _args(_suite_file(tmp_path, n=n), _prices_file(tmp_path), "--batch-judge", *extra))


def _assert_backoff(sleeps, count):
    assert len(sleeps) == count
    for k, delay in enumerate(sleeps, start=1):
        base = batch_mod.RETRY_BACKOFF_BASE_S * 2 ** (k - 1)
        assert base * 0.5 <= delay <= base


def test_create_retries_429_then_succeeds(tmp_path, monkeypatch, fake_http, flaky, gemini_env, no_sleep):
    _script_judge_batch(fake_http)
    flaky.faults["create"] = [_http_error(429), _http_error(429)]
    assert _run_default(tmp_path, monkeypatch) == 0
    assert len(fake_http.create_posts) == 1
    _assert_backoff(no_sleep, 2)


def test_create_does_not_retry_other_4xx(tmp_path, monkeypatch, fake_http, flaky, gemini_env, no_sleep):
    _script_judge_batch(fake_http)
    flaky.faults["create"] = [_http_error(400, b"bad request")]
    assert _run_default(tmp_path, monkeypatch) == 1
    assert no_sleep == [] and fake_http.create_posts == []
    run = _load(tmp_path)
    assert run["judge_batch"]["status"] == "submit_failed"  # persisted, not left "pending"
    assert run["spent_usd"]["judge"] == 0.0
    assert run["items"][0]["candidate"]["output"] == "echo:Q0?"
    assert "submission failed" in run["items"][0]["judge"]["incumbent"]["error"]


@pytest.mark.parametrize("fault", [
    ("after", TimeoutError("timed out")),  # the server accepted it, the reply was lost
    ("after", ConnectionResetError("reset")),
    _http_error(529),
    _http_error(500),
], ids=["timeout-after-accept", "reset-after-accept", "529", "500"])
def test_non_429_create_failure_is_never_resent(tmp_path, monkeypatch, fake_http, flaky, gemini_env, no_sleep, capsys, fault):
    _script_judge_batch(fake_http)
    # a second create would find this queued response and succeed, hiding the bug in rc
    flaky.faults["create"] = [fault]
    assert _run_default(tmp_path, monkeypatch) == 1
    assert flaky.creates == 1  # exactly one create call, ever
    assert no_sleep == []
    run = _load(tmp_path)
    assert run["judge_batch"]["status"] == "submit_outcome_unknown"
    assert run["judge_batch"]["id"] is None
    assert run["judge_batch"]["spend_basis"] == "reserved_estimate" and run["spent_usd"]["judge"] > 0
    assert "a batch may exist" in run["items"][0]["judge"]["incumbent"]["error"]
    assert run["items"][0]["candidate"]["output"] == "echo:Q0?"  # arms kept
    batch_json = json.loads((tmp_path / ".pinbench" / "runs" / "bj" / "batch.json").read_text())
    assert batch_json["judge"]["status"] == "submit_outcome_unknown"
    assert "Anthropic console" in capsys.readouterr().err


def test_429_create_is_retried_up_to_the_limit(tmp_path, monkeypatch, fake_http, flaky, gemini_env, no_sleep):
    _script_judge_batch(fake_http)
    flaky.faults["create"] = [_http_error(429)] * batch_mod.RETRY_ATTEMPTS
    assert _run_default(tmp_path, monkeypatch) == 1
    assert flaky.creates == batch_mod.RETRY_ATTEMPTS
    _assert_backoff(no_sleep, batch_mod.RETRY_ATTEMPTS - 1)
    run = _load(tmp_path)
    assert run["judge_batch"]["status"] == "submit_failed" and run["spent_usd"]["judge"] == 0.0  # a 429 created nothing


def test_permanent_parse_failure_is_not_retried(tmp_path, monkeypatch, fake_http, flaky, gemini_env, no_sleep):
    _script_judge_batch(fake_http)
    fake_http.results[JUDGE_BATCH] = json.dumps({"custom_id": "0000-judge-incumbent", "result": {"type": "weird"}})
    assert _run_default(tmp_path, monkeypatch) == 1
    assert flaky.results_calls == 1 and no_sleep == []
    run = _load(tmp_path)
    assert run["judge_batch"]["status"] == "results_failed" and run["spent_usd"]["judge"] > 0


def test_malformed_jsonl_is_results_failed_at_the_reserved_estimate(tmp_path, monkeypatch, fake_http, flaky, gemini_env, no_sleep):
    _script_judge_batch(fake_http)
    fake_http.results[JUDGE_BATCH] = _judge_lines(1) + "\n{not json"
    assert _run_default(tmp_path, monkeypatch) == 1
    assert flaky.results_calls == 1 and no_sleep == []
    run = _load(tmp_path)
    assert run["judge_batch"]["status"] == "results_failed"  # not left at "submitted"
    assert run["judge_batch"]["spend_basis"] == "reserved_estimate" and run["spent_usd"]["judge"] > 0
    assert "not judged" in run["items"][0]["judge"]["incumbent"]["error"]


def test_poll_and_results_retry_transient_failures(tmp_path, monkeypatch, fake_http, flaky, gemini_env, no_sleep):
    _script_judge_batch(fake_http)
    flaky.faults["get"] = [_http_error(503), TimeoutError("timed out")]
    flaky.faults["results"] = [_http_error(529), ConnectionResetError("reset")]
    assert _run_default(tmp_path, monkeypatch) == 0
    assert _load(tmp_path)["judge_batch"]["status"] == "ended"
    assert len(no_sleep) == 4


def test_poll_retries_exhausted_persists_and_exits_nonzero(tmp_path, monkeypatch, fake_http, flaky, gemini_env, no_sleep, capsys):
    _script_judge_batch(fake_http)
    flaky.faults["get"] = [_http_error(503)] * batch_mod.RETRY_ATTEMPTS
    assert _run_default(tmp_path, monkeypatch) == 1
    run = _load(tmp_path)
    assert run["judge_batch"]["status"] == "poll_failed" and run["judge_batch"]["id"] == JUDGE_BATCH
    assert run["spent_usd"]["judge"] > 0
    assert JUDGE_BATCH in capsys.readouterr().err
    assert len(no_sleep) == batch_mod.RETRY_ATTEMPTS - 1


def test_results_retries_exhausted_persists_and_exits_nonzero(tmp_path, monkeypatch, fake_http, flaky, gemini_env, no_sleep, capsys):
    _script_judge_batch(fake_http)
    flaky.faults["results"] = [_http_error(500)] * batch_mod.RETRY_ATTEMPTS
    assert _run_default(tmp_path, monkeypatch) == 1
    run = _load(tmp_path)
    assert run["judge_batch"]["status"] == "results_failed"
    assert "not judged" in run["items"][0]["judge"]["incumbent"]["error"]
    assert JUDGE_BATCH in capsys.readouterr().err
    assert json.loads((tmp_path / ".pinbench" / "runs" / "bj" / "batch.json").read_text())["judge"]["id"] == JUDGE_BATCH


def test_non_transient_4xx_on_poll_is_not_retried(tmp_path, monkeypatch, fake_http, flaky, gemini_env, no_sleep):
    _script_judge_batch(fake_http)
    flaky.faults["get"] = [_http_error(404, b"not found")]
    assert _run_default(tmp_path, monkeypatch) == 1
    assert no_sleep == []


# --- arm phase leaves room for the judge batch ---------------------------------------------

def test_arm_phase_reserves_the_judge_batch_estimate(tmp_path, monkeypatch, fake_http, gemini_env, no_sleep):
    suite_path = _suite_file(tmp_path, n=2)
    prices_path = _prices_file(tmp_path)
    suite = suite_mod.load(suite_path)
    prices = prices_mod.load(prices_path)
    plan = runner_mod.plan(suite, "claude-sonnet-5-5", GEMINI, prices, batch_judge=True)
    reserve = plan.estimate.judge.cost_usd
    est_cand1 = plan.estimate.candidate.items[1].cost_usd
    cap = 2.0
    ok = lambda i, o: providers.Reply(output="ok", input_tokens=i, output_tokens=o, latency_ms=1, stop_reason="end_turn")
    gemini_env.set_reply("claude-sonnet-5-5", "Q0?", ok(100, 10))
    gemini_env.set_reply(GEMINI, "Q0?", ok(100, 10))
    spent0 = (100 * 2.0 + 10 * 10.0) / 1e6 + (100 * 1.0 + 10 * 4.0) / 1e6
    # After item 1's incumbent, the candidate call fits the cap by itself but not with the
    # judge batch's estimate beside it.
    inc1_cost = cap - est_cand1 - reserve / 2 - spent0
    gemini_env.set_reply("claude-sonnet-5-5", "Q1?", ok(round(inc1_cost / 2.0 * 1e6), 0))
    assert spent0 + inc1_cost + est_cand1 <= cap < spent0 + inc1_cost + est_cand1 + reserve
    _script_judge_batch(fake_http, n=1)
    args = _args(suite_path, prices_path, "--batch-judge")
    args[args.index("--cap") + 1] = str(cap)
    assert _run_cli(tmp_path, monkeypatch, args) == 0
    assert (GEMINI, "Q1?") not in gemini_env.calls  # stopped before the call that would strand the judge
    run = _load(tmp_path)
    assert run["stopped_early"] is True
    assert run["items"][1]["candidate"] is None and run["items"][1]["incumbent"]["output"] == "ok"
    # the finished item was still judged, as one batch of its two arms
    assert sorted(r["custom_id"] for r in fake_http.create_posts[0]["requests"]) == ["0000-judge-candidate", "0000-judge-incumbent"]
    assert run["judge_batch"]["status"] == "ended" and "judge" not in run["items"][1]


# --- results map to the right arm; the meter is charged; state is persisted in order --------

def test_each_arm_gets_its_own_verdict(tmp_path, monkeypatch, fake_http, gemini_env, no_sleep):
    _script_judge_batch(fake_http, per_arm={"incumbent": (1, "inc-note"), "candidate": (2, "cand-note")}, output_tokens=10)
    fake_http.results[JUDGE_BATCH] = _judge_lines(2, per_arm={"incumbent": (1, "inc-note"), "candidate": (2, "cand-note")})
    assert _run_default(tmp_path, monkeypatch) == 0
    for item in _load(tmp_path)["items"]:
        assert (item["judge"]["incumbent"]["extra"], item["judge"]["incumbent"]["notes"]) == (1, "inc-note")
        assert (item["judge"]["candidate"]["extra"], item["judge"]["candidate"]["notes"]) == (2, "cand-note")


def test_judge_results_are_charged_against_the_cap(tmp_path, monkeypatch, fake_http, gemini_env, no_sleep, capsys):
    # 200M judge input tokens at the batch price ($0.05/Mtok) = $10 a call, far over the $5 cap
    _script_judge_batch(fake_http, n=1, input_tokens=200_000_000)
    assert _run_default(tmp_path, monkeypatch, n=1) == 0
    run = _load(tmp_path)
    assert run["judge_batch"]["cap_exceeded_after_results"] is True
    assert run["spent_usd"]["judge"] == pytest.approx(20.0, rel=0.01)
    assert "exceeded cap" in capsys.readouterr().err


def test_run_json_carries_the_batch_id_as_soon_as_it_is_known(tmp_path, monkeypatch, fake_http, gemini_env, no_sleep):
    _script_judge_batch(fake_http)
    seen = []
    fake_http.on_get = lambda batch_id: seen.append(_load(tmp_path)["judge_batch"])
    assert _run_default(tmp_path, monkeypatch) == 0
    assert seen[0]["id"] == JUDGE_BATCH and seen[0]["status"] == "submitted"


# --- the real combination: a --batch source reused under --batch-judge ----------------------

def test_batch_source_reused_under_batch_judge(tmp_path, monkeypatch, fake_http, gemini_env, no_sleep):
    prices_path = _prices_file(tmp_path)
    suite_path = _suite_file(tmp_path)
    arms_id = "msgbatch_arms"
    fake_http.create_responses = [_batch_obj(arms_id, "in_progress", {"processing": 4}), _batch_obj(JUDGE_BATCH, "in_progress", {"processing": 4})]
    fake_http.status_sequences[arms_id] = [_batch_obj(arms_id, "ended", {"succeeded": 4})]
    fake_http.status_sequences[JUDGE_BATCH] = [_batch_obj(JUDGE_BATCH, "ended", {"succeeded": 4})]
    fake_http.results[arms_id] = "\n".join(
        _succeeded_line(f"000{i}-{arm}", f"{arm}-{i}", input_tokens=10, output_tokens=5) for i in range(2) for arm in ("incumbent", "candidate")
    )
    fake_http.results[JUDGE_BATCH] = _judge_lines(2, input_tokens=200, output_tokens=20, per_arm={"incumbent": (3, "src-inc"), "candidate": (4, "src-cand")})
    rc = _run_cli(tmp_path, monkeypatch, ["run", str(suite_path), "--incumbent", "claude-sonnet-5-5", "--candidate", "claude-haiku-5-5",
                                          "--cap", "5.0", "--prices", str(prices_path), "--batch", "--name", "src"])
    assert rc == 0
    fake_http.create_posts.clear()
    _script_judge_batch(fake_http, batch_id="msgbatch_judge2", arms=("candidate",), per_arm={"candidate": (9, "new-cand")})
    rc = _run_cli(tmp_path, monkeypatch, _args(suite_path, prices_path, "--batch-judge", "--reuse-incumbent", "src", name="re"))
    assert rc == 0
    src, new = _load(tmp_path, "src"), _load(tmp_path, "re")
    assert src["batch"]["price_multiplier"]["claude-sonnet-5-5"] == 0.5
    assert "batch" not in new and new["judge_batch"]["id"] == "msgbatch_judge2"
    assert sorted(r["custom_id"] for r in fake_http.create_posts[0]["requests"]) == ["0000-judge-candidate", "0001-judge-candidate"]
    for i in range(2):
        carried = new["items"][i]["judge"]["incumbent"]
        assert (carried["extra"], carried["notes"]) == (3, "src-inc")
        # the source's judge was batch-priced (x0.5): back to the sync price
        assert carried["cost_usd"] == pytest.approx(src["items"][i]["judge"]["incumbent"]["cost_usd"] / 0.5)
        # the source's arm was batch-priced too: repriced from tokens at this run's sync rate
        assert new["items"][i]["incumbent"]["cost_usd"] == pytest.approx((10 * 2.0 + 5 * 10.0) / 1e6)
        assert (new["items"][i]["judge"]["candidate"]["extra"], new["items"][i]["judge"]["candidate"]["notes"]) == (9, "new-cand")
