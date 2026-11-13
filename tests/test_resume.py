"""Offline tests for crash-safe sync runs and `run --resume`: per-item checkpoints, signals,
run-name safety, 429 / quota handling, resume rules and judge-only resume of a recorded batch.

Each test is written to fail if the behavior it names is reverted (proven in the PR by
reverting each feature in a scratch copy outside the worktree)."""

from __future__ import annotations

import json
import os
import signal

import pytest
import yaml

from test_batch import (  # noqa: F401 - the fixtures must be importable to be used
    FakeBatchHTTP, _batch_obj, _run_cli, fake_http, no_sleep,
)
from test_batch_judge import (  # noqa: F401
    GEMINI, _script_judge_batch, JUDGE, JUDGE_BATCH, _args, _judge_lines, _prices_file, _suite_file, gemini_env,
)

from pinbench import cli as cli_mod
from pinbench import cost as cost_mod
from pinbench import judge as judge_mod
from pinbench import prices as prices_mod
from pinbench import providers
from pinbench import report as report_mod
from pinbench import resume as resume_mod
from pinbench import runner as runner_mod
from pinbench import suite as suite_mod

INC = "claude-sonnet-5-5"
CAND = GEMINI
VERDICT = json.dumps({"hit": [], "extra": 0, "notes": "ok"})


class Script:
    """Wraps the fake adapter: logs every call, answers the judge with a valid verdict, and
    lets a test intercept a call by (model, input) via hooks."""

    def __init__(self, adapter):
        self.adapter = adapter
        self.arm_calls = []  # (model, input) for every non-judge call
        self.judge_calls = 0
        self.hooks = []
        self.judge_hooks = []  # callables (user_input) -> Reply | None
        orig = adapter.call

        def call(model_id, system, user_input, max_tokens, api_key, params=None, timeout=60):
            if model_id == JUDGE:
                self.judge_calls += 1
                for hook in list(self.judge_hooks):
                    reply = hook(user_input)
                    if reply is not None:
                        return reply
                return providers.Reply(output=VERDICT, input_tokens=100, output_tokens=10, latency_ms=1, error=None, stop_reason="end_turn")
            self.arm_calls.append((model_id, user_input))
            for hook in list(self.hooks):
                reply = hook(model_id, user_input)
                if reply is not None:
                    return reply
            return orig(model_id, system, user_input, max_tokens, api_key, params=params, timeout=timeout)

        adapter.call = call

    def on(self, model, text, fn):
        self.hooks.append(lambda m, u: fn() if (m == model and u == text) else None)

    def reset(self):
        self.arm_calls.clear()
        self.judge_calls = 0
        self.hooks.clear()
        self.judge_hooks.clear()


@pytest.fixture
def script(gemini_env):
    return Script(gemini_env)


def _crash():
    raise KeyboardInterrupt


class Env:
    def __init__(self, tmp_path, n=4, judge=True):
        self.tmp_path = tmp_path
        self.suite_path = _suite_file(tmp_path, n=n, judge=judge)
        self.prices_path = _prices_file(tmp_path)
        self.state = str(tmp_path / ".pinbench")

    def suite(self):
        return suite_mod.load(self.suite_path)

    def prices(self):
        return prices_mod.load(self.prices_path)

    def run(self, name="r", cap=5.0, suite=None, prices=None, incumbent=INC, candidate=CAND, **kw):
        return runner_mod.run(
            suite or self.suite(), incumbent, candidate, prices or self.prices(), cap_usd=cap, name=name,
            state_dir=self.state, suite_path=str(self.suite_path), **kw,
        )

    def load(self, name="r"):
        return json.loads((self.tmp_path / ".pinbench" / "runs" / name / "run.json").read_text())


def _crashed_run(env, script, name="r", crash_on="Q2?", **kw):
    """A run killed (KeyboardInterrupt from inside a call) on the candidate call for `crash_on`."""
    script.on(CAND, crash_on, _crash)
    with pytest.raises(KeyboardInterrupt):
        env.run(name=name, **kw)
    script.reset()


# --- 1. checkpoints -------------------------------------------------------------------------

def test_run_json_is_checkpointed_after_each_item(tmp_path, script):
    env = Env(tmp_path)
    seen = {}

    def peek():
        seen["rec"] = env.load()
        return None

    script.on(INC, "Q2?", peek)  # the first call of the third item
    env.run()
    rec = seen["rec"]
    assert rec["status"] == "in_progress"
    assert [i["id"] for i in rec["items"]] == ["q00", "q01"]
    assert rec["items_completed"] == 2 and rec["finished_at"] is None
    assert all(i["judge"]["candidate"]["error"] is None for i in rec["items"])  # whole items, judge included
    final = env.load()
    assert final["status"] == "complete" and final["items_completed"] == 4 and final["finished_at"]


def test_crash_keeps_every_finished_item(tmp_path, script):
    env = Env(tmp_path)
    script.on(CAND, "Q2?", _crash)
    with pytest.raises(KeyboardInterrupt):
        env.run()
    rec = env.load()
    whole = [i for i in rec["items"] if i["candidate"] is not None and i["candidate"]["output"]]
    assert len(whole) == 2
    assert rec["status"] == "stopped" and "interrupted" in rec["stop_reason"]
    assert rec["items"][2]["incumbent"]["output"] == "echo:Q2?" and rec["items"][2]["candidate"] is None  # paid half kept
    assert rec["spent_usd"]["total"] > 0


# --- 2. signals -----------------------------------------------------------------------------

def test_signal_guard_graceful_sets_flag_then_second_signal_abandons():
    guard = resume_mod.SignalGuard(graceful=True, install=False)
    guard.handle(signal.SIGTERM)  # the call in flight is allowed to finish
    assert guard.requested == signal.SIGTERM and guard.name == "SIGTERM"
    with pytest.raises(resume_mod.RunInterrupted) as info:
        guard.handle(signal.SIGINT)
    assert info.value.signum == signal.SIGINT and isinstance(info.value, KeyboardInterrupt)


def test_signal_guard_batch_wait_raises_on_first_signal():
    guard = resume_mod.SignalGuard(graceful=False, install=False)
    with pytest.raises(resume_mod.RunInterrupted):
        guard.handle(signal.SIGHUP)


def test_sigterm_persists_then_exits_143(tmp_path, monkeypatch, script, capsys):
    env = Env(tmp_path)
    before = signal.getsignal(signal.SIGTERM)
    script.on(INC, "Q1?", lambda: os.kill(os.getpid(), signal.SIGTERM))  # during the second item's first call
    rc = _run_cli(tmp_path, monkeypatch, _args(env.suite_path, env.prices_path, name="sig"))
    assert rc == 143
    rec = env.load("sig")
    assert rec["status"] == "stopped" and rec["stop_reason"] == "signal:SIGTERM" and rec["stop_signal"] == "SIGTERM"
    assert rec["items"][0]["candidate"]["output"] == "echo:Q0?"
    # the call in flight returned and was kept; no further call was made
    assert rec["items"][1]["incumbent"]["output"] == "echo:Q1?" and rec["items"][1]["candidate"] is None
    assert (CAND, "Q1?") not in script.arm_calls
    assert "--resume sig" in capsys.readouterr().err
    assert signal.getsignal(signal.SIGTERM) is before  # the handler was restored


# --- 3. run-name safety ---------------------------------------------------------------------

def test_existing_name_is_refused_without_resume(tmp_path, script):
    env = Env(tmp_path)
    env.run(name="dup")
    before = (tmp_path / ".pinbench" / "runs" / "dup" / "run.json").read_text()
    script.reset()
    with pytest.raises(runner_mod.PlanRefused, match="already exists"):
        env.run(name="dup")
    assert script.arm_calls == []
    assert (tmp_path / ".pinbench" / "runs" / "dup" / "run.json").read_text() == before  # not overwritten


def test_default_names_never_collide_in_the_same_second(tmp_path, script, monkeypatch):
    env = Env(tmp_path, n=1)
    monkeypatch.setattr(runner_mod, "_stamp", lambda: "20261113T000000Z")
    draws = iter(["aaaaaa", "aaaaaa", "bbbbbb"])
    monkeypatch.setattr(runner_mod.secrets, "token_hex", lambda n: next(draws))
    first = env.run(name=None)
    second = env.run(name=None)
    assert first["run_name"] != second["run_name"]
    assert first["run_name"].endswith("aaaaaa") and second["run_name"].endswith("bbbbbb")


def test_resume_name_must_exist_and_agree_with_name(tmp_path, script):
    env = Env(tmp_path)
    with pytest.raises(runner_mod.PlanRefused, match="no .*run.json"):
        env.run(name=None, resume="ghost")
    _crashed_run(env, script)
    with pytest.raises(runner_mod.PlanRefused, match="disagree"):
        env.run(name="other", resume="r")


# --- 4. 429 / quota -------------------------------------------------------------------------

def _rate_limited(billed=True):
    return providers.Reply(
        output=None, input_tokens=100 if billed else None, output_tokens=7 if billed else None, latency_ms=1,
        error="HTTP 429: RESOURCE_EXHAUSTED quota", status_code=429,
    )


def test_429_is_retried_with_backoff_then_the_call_succeeds(tmp_path, script):
    env = Env(tmp_path, n=2)
    fails = {"n": 2}

    def flaky():
        if fails["n"]:
            fails["n"] -= 1
            return _rate_limited(billed=False)
        return None

    script.on(CAND, "Q0?", flaky)
    sleeps = []
    rec = env.run(sleep_fn=sleeps.append)
    assert sleeps == [resume_mod.backoff_s(1), resume_mod.backoff_s(2)]
    assert rec["status"] == "complete" and rec["items_completed"] == 2
    assert rec["items"][0]["candidate"]["error"] is None


def test_exhausted_429_stops_the_run_and_charges_billed_tokens(tmp_path, script):
    env = Env(tmp_path)
    script.on(CAND, "Q2?", _rate_limited)
    sleeps = []
    rec = env.run(sleep_fn=sleeps.append)
    assert [c for c in script.arm_calls if c[1] == "Q2?" and c[0] == CAND] == [(CAND, "Q2?")] * resume_mod.RATE_LIMIT_ATTEMPTS
    assert sleeps == [resume_mod.backoff_s(k) for k in range(1, resume_mod.RATE_LIMIT_ATTEMPTS)]
    assert rec["status"] == "stopped" and rec["stopped_early"] is True and rec["stop_reason"] == "quota_or_rate_limit"
    assert not any(i == "q03" for i in (r["id"] for r in rec["items"]))  # nothing after the stop
    assert (INC, "Q3?") not in script.arm_calls
    # every failed attempt carried billed tokens (100 in, 7 out at 1.0 / 4.0 per M): all charged
    row = env.prices().rows[CAND]
    per_call = 100 * row.input / 1e6 + 7 * row.output / 1e6
    ok_cost = sum(i["candidate"]["cost_usd"] for i in rec["items"][:2])
    assert rec["spent_usd"]["candidate"] == pytest.approx(ok_cost + per_call * resume_mod.RATE_LIMIT_ATTEMPTS)
    assert rec["items"][2]["candidate"]["cost_usd"] == pytest.approx(per_call * resume_mod.RATE_LIMIT_ATTEMPTS)


def test_exhausted_incumbent_429_stops_the_run(tmp_path, script):
    env = Env(tmp_path)
    script.on(INC, "Q1?", lambda: _rate_limited(billed=False))
    rec = env.run(sleep_fn=lambda s: None)
    assert rec["stop_reason"] == "quota_or_rate_limit" and rec["status"] == "stopped"
    assert [i["id"] for i in rec["items"]] == ["q00", "q01"]
    assert rec["items"][1]["incumbent"]["error"].startswith("HTTP 429") and rec["items"][1]["candidate"] is None
    assert script.arm_calls.count((INC, "Q1?")) == resume_mod.RATE_LIMIT_ATTEMPTS and (INC, "Q2?") not in script.arm_calls
    script.reset()
    done = env.run(resume="r")  # the errored incumbent is re-run on resume
    assert done["status"] == "complete" and done["items"][1]["incumbent"]["error"] is None


def test_exhausted_judge_429_stops_the_run(tmp_path, script, monkeypatch):
    env = Env(tmp_path)
    orig = script.adapter.call

    def limited_judge(model_id, system, user_input, max_tokens, api_key, params=None, timeout=60):
        if model_id == JUDGE and "Q1?" in user_input:
            script.judge_calls += 1
            return _rate_limited(billed=False)
        return orig(model_id, system, user_input, max_tokens, api_key, params=params, timeout=timeout)

    script.adapter.call = limited_judge
    rec = env.run(sleep_fn=lambda s: None)
    assert rec["stop_reason"] == "quota_or_rate_limit" and rec["status"] == "stopped"
    assert [i["id"] for i in rec["items"]] == ["q00", "q01"]  # q01 kept with its failed judge record, nothing after
    judge = rec["items"][1]["judge"]
    assert list(judge) == ["call_order", judge["call_order"][0]] and "429" in judge[judge["call_order"][0]]["error"]


def test_cli_quota_stop_exits_6_and_prints_the_resume_command(tmp_path, monkeypatch, script, capsys):
    env = Env(tmp_path)
    monkeypatch.setattr(resume_mod.time, "sleep", lambda s: None)
    script.on(CAND, "Q1?", _rate_limited)
    rc = _run_cli(tmp_path, monkeypatch, _args(env.suite_path, env.prices_path, "--timeout", "1500", name="q"))
    assert rc == resume_mod.EXIT_QUOTA_STOP == 6
    err = capsys.readouterr().err
    assert f"pinbench run {env.suite_path} --incumbent {INC} --candidate {CAND} --cap 5.0 --prices {env.prices_path} --timeout 1500 --resume q" in err
    script.reset()
    rc = _run_cli(tmp_path, monkeypatch, ["run", str(env.suite_path), "--incumbent", INC, "--candidate", CAND, "--cap", "5.0",
                                          "--prices", str(env.prices_path), "--timeout", "1500", "--resume", "q"])
    assert rc == 0 and env.load("q")["status"] == "complete"


# --- 5. resume ------------------------------------------------------------------------------

def test_resume_skips_done_work_reruns_errored_and_counts_spend(tmp_path, script):
    env = Env(tmp_path)
    est = runner_mod.plan(env.suite(), INC, CAND, env.prices(), cap_usd=5.0, state_dir=env.state).estimate
    est_cand_q02 = {e.item_id: e.cost_usd for e in est.candidate.items}["q02"]
    script.on(CAND, "Q0?", lambda: providers.Reply(output="partial", input_tokens=20, output_tokens=64, latency_ms=1, error=None, stop_reason="max_tokens"))
    bad = providers.Reply(output=None, input_tokens=50, output_tokens=3, latency_ms=1, error="HTTP 400: bad", status_code=400)
    script.on(CAND, "Q1?", lambda: bad)
    script.on(CAND, "Q2?", _crash)
    with pytest.raises(KeyboardInterrupt):
        env.run()
    first = env.load()
    script.reset()

    rec = env.run(resume="r")
    # no call for finished work: q00 (truncated stays as recorded), the kept incumbents of q01 and q02
    assert script.arm_calls == [(CAND, "Q1?"), (CAND, "Q2?"), (INC, "Q3?"), (CAND, "Q3?")]
    assert script.judge_calls == 6  # q01, q02, q03 both arms; q00 was judged before
    assert rec["status"] == "complete" and rec["items_completed"] == 4 and not rec["stopped_early"]
    assert rec["items"][0]["candidate"]["output"] == "partial" and rec["items"][0]["candidate"]["stop_reason"] == "max_tokens"
    assert rec["items"][1]["candidate"]["output"] == "echo:Q1?"
    assert rec["items"][1]["incumbent"] == first["items"][1]["incumbent"]  # kept as recorded
    # the errored attempt's billed tokens are still spent, though its record was replaced
    row = env.prices().rows[CAND]
    billed_bad = 50 * row.input / 1e6 + 3 * row.output / 1e6
    # the call abandoned by the crash (Q2) has an unknown outcome: it stays charged at its estimate
    abandoned = est_cand_q02
    assert rec["spent_usd"]["candidate"] == pytest.approx(sum(i["candidate"]["cost_usd"] for i in rec["items"]) + billed_bad + abandoned)
    # q01 was judged again after its candidate was re-run; the first judgement stays spent
    rejudged = sum(first["items"][1]["judge"][a]["cost_usd"] for a in ("incumbent", "candidate") if a in first["items"][1]["judge"])
    assert rejudged > 0
    assert rec["spent_usd"]["judge"] == pytest.approx(sum(i["judge"][a]["cost_usd"] for i in rec["items"] for a in ("incumbent", "candidate")) + rejudged)
    assert rec["spent_usd"]["total"] > first["spent_usd"]["total"]
    assert rec["resumes"]["count"] == 1 and len(rec["resumes"]["history"]) == 1
    assert rec["started_at"] == first["started_at"]
    assert "Resumed 1 time(s)" in report_mod._resume_line(rec)


def test_resume_cap_covers_the_whole_run(tmp_path, script):
    env = Env(tmp_path)
    _crashed_run(env, script)
    first = env.load()
    est = runner_mod.plan(env.suite(), INC, CAND, env.prices(), cap_usd=5.0, state_dir=env.state).estimate
    inc = {e.item_id: e.cost_usd for e in est.incumbent.items}
    cand = {e.item_id: e.cost_usd for e in est.candidate.items}
    # what the resume still has to do: q02's candidate, both arms of q03, and the judge for those two items
    remaining = cand["q02"] + inc["q03"] + cand["q03"] + est.judge.cost_usd * 2 / 4
    spent = first["spent_usd"]["total"]
    cap = remaining + spent * 0.5  # fits the remaining work alone; does not fit it on top of what is spent
    with pytest.raises(runner_mod.RunCapExceeded, match="already spent"):
        env.run(resume="r", cap=cap)
    assert script.arm_calls == []
    assert env.load()["spent_usd"]["total"] == first["spent_usd"]["total"]


def test_resume_meter_starts_from_spend_so_far(tmp_path, script):
    env = Env(tmp_path, n=2, judge=False)
    _crashed_run(env, script, crash_on="Q1?")
    first = env.load()
    # a cap that fits the remaining call's estimate but not on top of what is already spent
    suite = env.suite()
    spent = first["spent_usd"]["total"]
    est = {e.item_id: e.cost_usd for e in runner_mod.plan(suite, INC, CAND, env.prices(), cap_usd=5.0, state_dir=env.state).estimate.candidate.items}
    cap = spent + est["q01"] * 0.5
    with pytest.raises(runner_mod.RunCapExceeded):
        env.run(resume="r", cap=cap)


def test_resume_refuses_a_complete_run(tmp_path, script):
    env = Env(tmp_path, n=2)
    env.run()
    with pytest.raises(runner_mod.PlanRefused, match="complete"):
        env.run(resume="r")


def test_resume_refuses_a_run_json_without_status(tmp_path, script):
    env = Env(tmp_path, n=2)
    env.run()
    path = tmp_path / ".pinbench" / "runs" / "r" / "run.json"
    rec = json.loads(path.read_text())
    del rec["status"]
    path.write_text(json.dumps(rec))
    with pytest.raises(runner_mod.PlanRefused, match="predates resume"):
        env.run(resume="r")


def _complete_source(env, name="src"):
    return env.run(name=name, candidate=CAND)


def _yaml_edit(path, fn):
    data = yaml.safe_load(path.read_text())
    fn(data)
    path.write_text(yaml.safe_dump(data, sort_keys=False))


def _set_judge_model(data):
    data["judge"]["model"] = "claude-haiku-5-5"


def _set_rubric(data):
    data["judge"]["rubric"] = "grade leniently"


def _set_input(data):
    data["items"][0]["input"] = "Q0 changed?"


@pytest.mark.parametrize("what, match", [
    ("suite", "suite_hash differs"),
    ("incumbent", r"r: incumbent differs"),
    ("candidate", r"r: candidate differs"),
    ("params", "request params for the candidate differs"),
    ("judge_model", "judge model differs"),
    ("judge_prompt", "judge prompt hash"),
    ("batch_judge", r"--batch-judge differs"),
    ("limit", r"--limit differs"),
    ("reuse", r"--reuse-incumbent differs"),
])
def test_resume_refuses_every_comparability_mismatch(tmp_path, script, what, match):
    env = Env(tmp_path)
    _complete_source(env)  # only the "reuse" case uses it
    script.reset()
    _crashed_run(env, script)
    kw = {}
    if what == "suite":
        _yaml_edit(env.suite_path, _set_input)
    elif what == "incumbent":
        kw["incumbent"] = "claude-haiku-5-5"
    elif what == "candidate":
        kw["candidate"] = "gpt-5-mini"
    elif what == "params":
        _yaml_edit(env.prices_path, lambda d: d["models"][CAND].update(request_defaults={"temperature": 0.5}))
    elif what == "judge_model":
        _yaml_edit(env.suite_path, _set_judge_model)
    elif what == "judge_prompt":
        _yaml_edit(env.suite_path, _set_rubric)
    elif what == "batch_judge":
        kw["batch_judge"] = True
    elif what == "limit":
        kw["limit"] = 3
    elif what == "reuse":
        kw["reuse_incumbent"] = "src"
    with pytest.raises(runner_mod.PlanRefused, match=match):
        env.run(resume="r", **kw)
    assert script.arm_calls == [] and script.judge_calls == 0


def test_resume_refuses_when_reuse_source_changed(tmp_path, script):
    env = Env(tmp_path)
    _complete_source(env)
    _crashed_run(env, script, reuse_incumbent="src")
    # a recorded reuse run must be resumed WITH its reuse source, and the same bytes
    with pytest.raises(runner_mod.PlanRefused, match="--reuse-incumbent differs"):
        env.run(resume="r")
    path = tmp_path / ".pinbench" / "runs" / "src" / "run.json"
    rec = json.loads(path.read_text())
    rec["note"] = "edited"
    path.write_text(json.dumps(rec))
    with pytest.raises(runner_mod.PlanRefused, match="sha256"):
        env.run(resume="r", reuse_incumbent="src")


def test_resume_with_reuse_incumbent_keeps_the_reused_slot(tmp_path, script):
    env = Env(tmp_path)
    src = _complete_source(env)
    script.reset()
    _crashed_run(env, script, reuse_incumbent="src")
    checkpoint = env.load()
    assert [(m, u) for (m, u) in script.arm_calls if m == INC] == []
    rec = env.run(resume="r", reuse_incumbent="src")
    assert [c for c in script.arm_calls if c[0] == INC] == []  # the incumbent is never called, before or after
    assert script.arm_calls == [(CAND, "Q2?"), (CAND, "Q3?")]
    assert rec["status"] == "complete" and rec["items_completed"] == 4
    assert rec["reused_incumbent"]["run"] == "src"
    for got, was, orig in zip(rec["items"], checkpoint["items"], src["items"]):
        assert got["incumbent"] == was["incumbent"]  # the reused slot is the checkpoint's, untouched
        assert got["incumbent"]["output"] == orig["incumbent"]["output"]
        assert got["judge"]["incumbent"] == was["judge"]["incumbent"] if "judge" in was else True
    assert rec["spent_usd"]["incumbent"] == 0.0


# --- 6. judge-only resume of a recorded batch -----------------------------------------------

def _first_batch_run(tmp_path, monkeypatch, *extra):
    env = Env(tmp_path, n=2)
    rc = _run_cli(tmp_path, monkeypatch, _args(env.suite_path, env.prices_path, "--batch-judge", *extra, name="bj"))
    return env, rc


def _resume_args(env, *extra, name="bj"):
    return ["run", str(env.suite_path), "--incumbent", INC, "--candidate", CAND, "--cap", "5.0",
            "--prices", str(env.prices_path), "--batch-judge", "--resume", name, *extra]


def _timed_out_run(tmp_path, monkeypatch, fake_http):
    fake_http.create_responses.append(_batch_obj(JUDGE_BATCH, "in_progress", {"processing": 4}))
    fake_http.status_sequences[JUDGE_BATCH] = [_batch_obj(JUDGE_BATCH, "in_progress", {"processing": 4})]
    env, rc = _first_batch_run(tmp_path, monkeypatch, "--poll-interval", "1", "--max-wait", "1")
    assert rc == 5 and env.load("bj")["judge_batch"]["status"] == "timed_out"
    return env


def test_judge_only_resume_collects_the_recorded_batch_without_a_second_create(tmp_path, monkeypatch, fake_http, gemini_env, script, no_sleep):
    env = _timed_out_run(tmp_path, monkeypatch, fake_http)
    assert len(fake_http.create_posts) == 1
    script.reset()
    orders = [i["judge"]["call_order"] for i in env.load("bj")["items"]]
    fake_http.status_sequences[JUDGE_BATCH] = [_batch_obj(JUDGE_BATCH, "ended", {"succeeded": 4})]
    fake_http.results[JUDGE_BATCH] = _judge_lines(2)
    rc = _run_cli(tmp_path, monkeypatch, _resume_args(env))
    assert rc == 0
    assert len(fake_http.create_posts) == 1  # never re-submitted (an empty create queue would also have raised)
    assert [i["judge"]["call_order"] for i in env.load("bj")["items"]] == orders  # the recorded call order is kept
    assert script.arm_calls == [] and script.judge_calls == 0  # the arms were kept
    rec = env.load("bj")
    assert rec["status"] == "complete" and rec["judge_batch"]["status"] == "ended" and rec["judge_batch"]["id"] == JUDGE_BATCH
    for item in rec["items"]:
        for arm in ("incumbent", "candidate"):
            assert item["judge"][arm]["error"] is None and item["judge"][arm]["cost_usd"] > 0
    assert rec["spent_usd"]["judge"] == pytest.approx(sum(i["judge"][a]["cost_usd"] for i in rec["items"] for a in ("incumbent", "candidate")))
    assert rec["resumes"]["count"] == 1


def test_judge_resume_submits_when_the_batch_was_never_sent(tmp_path, monkeypatch, fake_http, gemini_env, script, no_sleep):
    env = _timed_out_run(tmp_path, monkeypatch, fake_http)
    path = tmp_path / ".pinbench" / "runs" / "bj" / "run.json"
    rec = json.loads(path.read_text())
    rec["judge_batch"].update(id=None, status="not_submitted_cap")
    path.write_text(json.dumps(rec))
    fake_http.create_responses.append(_batch_obj("msgbatch_new", "in_progress", {"processing": 4}))
    fake_http.status_sequences["msgbatch_new"] = [_batch_obj("msgbatch_new", "ended", {"succeeded": 4})]
    fake_http.results["msgbatch_new"] = _judge_lines(2)
    assert _run_cli(tmp_path, monkeypatch, _resume_args(env)) == 0
    assert len(fake_http.create_posts) == 2 and env.load("bj")["judge_batch"]["id"] == "msgbatch_new"


def _unknown_outcome_run(tmp_path, monkeypatch, fake_http):
    import io
    import urllib.error

    from pinbench.providers import anthropic as anthropic_provider

    def failing(req, timeout=None):
        if req.get_method() == "POST" and req.full_url.endswith("/v1/messages/batches"):
            raise urllib.error.HTTPError(req.full_url, 500, "err", {}, io.BytesIO(b"boom"))
        return fake_http.urlopen(req, timeout)

    monkeypatch.setattr(anthropic_provider.urllib.request, "urlopen", failing)
    env, rc = _first_batch_run(tmp_path, monkeypatch)
    assert rc == 1 and env.load("bj")["judge_batch"]["status"] == "submit_outcome_unknown"
    monkeypatch.setattr(anthropic_provider.urllib.request, "urlopen", fake_http.urlopen)
    return env


def test_submit_outcome_unknown_refuses_without_a_flag(tmp_path, monkeypatch, fake_http, gemini_env, script, no_sleep, capsys):
    env = _unknown_outcome_run(tmp_path, monkeypatch, fake_http)
    script.reset()
    capsys.readouterr()
    assert _run_cli(tmp_path, monkeypatch, _resume_args(env)) == 2
    err = capsys.readouterr().err
    assert "Anthropic console" in err and "resubmit" not in err and "judge-batch-id" not in err
    assert fake_http.create_posts == [] and script.arm_calls == []


def test_pending_status_is_treated_as_an_unknown_outcome(tmp_path, monkeypatch, fake_http, gemini_env, script, no_sleep, capsys):
    env = _timed_out_run(tmp_path, monkeypatch, fake_http)
    path = tmp_path / ".pinbench" / "runs" / "bj" / "run.json"
    rec = json.loads(path.read_text())
    rec["judge_batch"].update(id=None, status="pending")  # killed during the create call
    path.write_text(json.dumps(rec))
    assert _run_cli(tmp_path, monkeypatch, _resume_args(env)) == 2
    assert "Anthropic console" in capsys.readouterr().err
    assert len(fake_http.create_posts) == 1


# --- round 2 --------------------------------------------------------------------------------

def test_report_on_a_checkpoint_is_refused_and_writes_no_verdict(tmp_path, monkeypatch, script, capsys):
    env = Env(tmp_path, n=3)
    env.run(name="cp")
    path = tmp_path / ".pinbench" / "runs" / "cp" / "run.json"
    rec = json.loads(path.read_text())
    rec["status"] = "in_progress"  # what a mid-run checkpoint looks like
    path.write_text(json.dumps(rec))
    capsys.readouterr()
    monkeypatch.chdir(tmp_path)
    rc = cli_mod.main(["report", "cp", "--state", env.state])
    out = capsys.readouterr()
    assert rc != 0 and "Verdict: INCOMPLETE" in out.out and "Verdict: RECOMMEND" not in out.out
    assert "run status is 'in_progress'" in out.out  # the completeness check names the status
    assert not (path.parent / "verdict.json").exists()


def test_report_on_a_stopped_run_writes_no_verdict_json(tmp_path, monkeypatch, script, capsys):
    env = Env(tmp_path, n=3)
    _crashed_run(env, script, crash_on="Q1?")
    capsys.readouterr()
    monkeypatch.chdir(tmp_path)
    cli_mod.main(["report", "r", "--state", env.state])
    out = capsys.readouterr()
    assert "Verdict: INCOMPLETE" in out.out and "run status is 'stopped'" in out.out
    assert not (tmp_path / ".pinbench" / "runs" / "r" / "verdict.json").exists()


def test_run_is_complete_only_when_every_arm_succeeded(tmp_path, script):
    env = Env(tmp_path, n=3)
    script.on(CAND, "Q1?", lambda: providers.Reply(output=None, input_tokens=None, output_tokens=None, latency_ms=1, error="HTTP 500: boom", status_code=500))
    rec = env.run()
    assert rec["status"] == "stopped" and rec["stop_reason"] == "arm_errors" and rec["stopped_early"] is False
    script.reset()
    rec = env.run(resume="r")
    assert rec["status"] == "complete" and script.arm_calls == [(CAND, "Q1?")]
    # a truncation is a result: a run whose only blemish is a truncated output is complete
    script.on(CAND, "Q0?", lambda: providers.Reply(output="cut", input_tokens=5, output_tokens=64, latency_ms=1, error=None, stop_reason="max_tokens"))
    assert env.run(name="trunc")["status"] == "complete"


def test_signal_during_the_last_item_does_not_complete_the_run(tmp_path, script):
    env = Env(tmp_path, n=2)
    script.on(CAND, "Q1?", lambda: os.kill(os.getpid(), signal.SIGTERM))  # the very last call
    rec = env.run()
    assert rec["status"] == "stopped" and rec["stop_reason"] == "signal:SIGTERM"
    assert rec["items_completed"] == 2  # both items were finished; the run still stops, resumably


def test_batch_judge_is_not_submitted_after_a_signal_in_the_last_item(tmp_path, monkeypatch, fake_http, gemini_env, script, no_sleep):
    env = Env(tmp_path, n=2)
    script.on(CAND, "Q1?", lambda: os.kill(os.getpid(), signal.SIGTERM))
    rc = _run_cli(tmp_path, monkeypatch, _args(env.suite_path, env.prices_path, "--batch-judge", name="bj"))
    assert rc == 143
    assert fake_http.create_posts == []  # no batch was created
    rec = env.load("bj")
    assert rec["status"] == "stopped" and rec["stop_reason"] == "signal:SIGTERM" and "judge_batch" not in rec


def test_collect_with_errored_arms_remaining_keeps_them_and_never_resubmits(tmp_path, monkeypatch, fake_http, gemini_env, script, no_sleep):
    env = _timed_out_run(tmp_path, monkeypatch, fake_http)
    path = tmp_path / ".pinbench" / "runs" / "bj" / "run.json"
    rec = json.loads(path.read_text())
    bad = dict(rec["items"][1]["candidate"], output=None, error="HTTP 500: boom", stop_reason=None)
    rec["items"][1]["candidate"] = bad
    rec["items"][1]["judge"]["candidate"] = {"hit": [], "extra": 0, "notes": "", "error": None, "cost_usd": 0.0, "skipped": "skipped: arm error"}
    path.write_text(json.dumps(rec))
    script.reset()
    # that arm was never part of the batch (skipped), so its custom_id is not in the results
    fake_http.status_sequences[JUDGE_BATCH] = [_batch_obj(JUDGE_BATCH, "ended", {"succeeded": 3})]
    fake_http.results[JUDGE_BATCH] = "\n".join(line for line in _judge_lines(2).split("\n") if "0001-judge-candidate" not in line)
    assert _run_cli(tmp_path, monkeypatch, _resume_args(env)) == 0
    assert len(fake_http.create_posts) == 1 and script.arm_calls == []  # nothing re-run, nothing re-submitted
    out = env.load("bj")
    assert out["judge_batch"]["status"] == "ended" and out["items"][1]["candidate"]["error"] == "HTTP 500: boom"
    assert out["status"] == "stopped" and out["stop_reason"] == "arm_errors_judged"  # not complete, and final


def test_a_failing_checkpoint_does_not_duplicate_the_item(tmp_path, script, monkeypatch):
    env = Env(tmp_path, n=3, judge=False)
    real = runner_mod._write_run_json
    calls = {"n": 0}

    def flaky(state_dir, name, record):
        calls["n"] += 1
        if calls["n"] == 3:  # claim, checkpoint after q00, checkpoint after q01 fails
            raise OSError("disk full")
        return real(state_dir, name, record)

    monkeypatch.setattr(runner_mod, "_write_run_json", flaky)
    with pytest.raises(OSError):
        env.run()
    ids = [i["id"] for i in env.load()["items"]]
    assert ids == ["q00", "q01"]  # q01 once, not twice


def test_an_abandoned_call_is_charged_at_its_estimate(tmp_path, script):
    env = Env(tmp_path, n=2, judge=False)
    est = runner_mod.plan(env.suite(), INC, CAND, env.prices(), cap_usd=5.0, state_dir=env.state).estimate
    q1 = {e.item_id: e.cost_usd for e in est.candidate.items}["q01"]
    script.on(CAND, "Q1?", _crash)
    with pytest.raises(KeyboardInterrupt):
        env.run()
    rec = env.load()
    ok = rec["items"][0]["candidate"]["cost_usd"]
    assert rec["spent_usd"]["candidate"] == pytest.approx(ok + q1)


def test_retry_stops_when_the_meter_has_no_room(tmp_path, script):
    env = Env(tmp_path, n=1, judge=False)
    # one failed attempt bills about $4 against a $0.01 cap: no second attempt may be made
    script.on(CAND, "Q0?", lambda: providers.Reply(output=None, input_tokens=100, output_tokens=1_000_000, latency_ms=1, error="HTTP 429", status_code=429))
    sleeps = []
    rec = env.run(cap=0.01, sleep_fn=sleeps.append)
    assert script.arm_calls.count((CAND, "Q0?")) == 1 and sleeps == []
    assert rec["stop_reason"] == "quota_or_rate_limit"


def test_retry_stops_when_a_signal_is_pending(tmp_path, script):
    env = Env(tmp_path, n=1, judge=False)

    def limited_and_signalled():
        os.kill(os.getpid(), signal.SIGTERM)
        return _rate_limited(billed=False)

    script.on(CAND, "Q0?", limited_and_signalled)
    sleeps = []
    rec = env.run(sleep_fn=sleeps.append)
    assert script.arm_calls.count((CAND, "Q0?")) == 1 and sleeps == []
    assert rec["stop_reason"] == "signal:SIGTERM"


def test_first_signal_says_what_happens_next(capsys):
    guard = resume_mod.SignalGuard(graceful=True, install=False)
    guard.handle(signal.SIGHUP)
    assert capsys.readouterr().err.strip() == "finishing the call in flight, then saving; press again to abandon it (charged at its estimate)"


def test_judge_retry_charges_the_failed_attempts(tmp_path, script):
    env = Env(tmp_path, n=1)
    fails = {"n": 2}

    def limited(_user):
        if fails["n"]:
            fails["n"] -= 1
            return providers.Reply(output=None, input_tokens=100, output_tokens=7, latency_ms=1, error="HTTP 429", status_code=429)
        return None

    script.judge_hooks.append(limited)
    rec = env.run(sleep_fn=lambda s: None)
    row = env.prices().rows[JUDGE]
    failed = 100 * row.input / 1e6 + 7 * row.output / 1e6
    ok = 100 * row.input / 1e6 + 10 * row.output / 1e6
    recorded = sum(rec["items"][0]["judge"][a]["cost_usd"] for a in ("incumbent", "candidate"))
    assert recorded == pytest.approx(2 * ok + 2 * failed)
    assert rec["spent_usd"]["judge"] == pytest.approx(2 * ok + 2 * failed)


def test_checkpoints_keep_the_stored_items_not_reached_yet(tmp_path, script):
    env = Env(tmp_path, n=4, judge=False)
    script.on(CAND, "Q1?", lambda: providers.Reply(output=None, input_tokens=None, output_tokens=None, latency_ms=1, error="HTTP 500: boom", status_code=500))
    env.run()
    script.reset()
    seen = {}
    script.on(CAND, "Q1?", lambda: seen.setdefault("ids", [i["id"] for i in env.load()["items"]]) and None)
    env.run(resume="r")
    assert seen["ids"] == ["q00", "q01", "q02", "q03"]  # q02 and q03 stay on disk while q01 is re-run


def test_sync_judge_spend_is_carried_into_a_resume(tmp_path, script):
    env = Env(tmp_path, n=4)
    _crashed_run(env, script)
    before = env.load()["spent_usd"]["judge"]
    assert before > 0
    rec = env.run(resume="r")
    assert rec["spent_usd"]["judge"] == pytest.approx(sum(i["judge"][a]["cost_usd"] for i in rec["items"] for a in ("incumbent", "candidate")))
    assert rec["spent_usd"]["judge"] > before


def test_run_lock_refuses_a_live_pid_and_takes_over_a_stale_one(tmp_path, script, capsys):
    env = Env(tmp_path, n=1, judge=False)
    run_dir = tmp_path / ".pinbench" / "runs" / "lk"
    run_dir.mkdir(parents=True)
    (run_dir / "run.lock").write_text(str(os.getppid()))  # a live process that is not us
    with pytest.raises(runner_mod.PlanRefused, match="locked by live process"):
        env.run(name="lk")
    assert script.arm_calls == []
    import subprocess
    import sys
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait()
    (run_dir / "run.lock").write_text(str(dead.pid))
    env.run(name="lk")
    assert "stale run lock" in capsys.readouterr().err
    assert not (run_dir / "run.lock").exists()  # removed on exit


def test_run_json_is_fsynced_before_the_replace(tmp_path, monkeypatch):
    synced = []
    monkeypatch.setattr(runner_mod.os, "fsync", lambda fd: synced.append(fd))
    runner_mod._write_run_json(str(tmp_path / ".pinbench"), "x", {"a": 1})
    assert len(synced) == 2  # the file, then its directory


def test_call_order_is_kept_on_a_judge_only_resume(tmp_path, monkeypatch, fake_http, gemini_env, script, no_sleep):
    monkeypatch.setattr(judge_mod, "pick_call_order", lambda: ["candidate", "incumbent"])
    env = _timed_out_run(tmp_path, monkeypatch, fake_http)
    monkeypatch.setattr(judge_mod, "pick_call_order", lambda: ["incumbent", "candidate"])  # a re-draw would change it
    fake_http.status_sequences[JUDGE_BATCH] = [_batch_obj(JUDGE_BATCH, "ended", {"succeeded": 4})]
    fake_http.results[JUDGE_BATCH] = _judge_lines(2)
    assert _run_cli(tmp_path, monkeypatch, _resume_args(env)) == 0
    assert [i["judge"]["call_order"] for i in env.load("bj")["items"]] == [["candidate", "incumbent"]] * 2


def test_the_batch_wait_interrupt_message_points_at_resume(tmp_path, monkeypatch, fake_http, gemini_env, script, no_sleep, capsys):
    fake_http.create_responses.append(_batch_obj(JUDGE_BATCH, "in_progress", {"processing": 4}))
    fake_http.status_sequences[JUDGE_BATCH] = [_batch_obj(JUDGE_BATCH, "in_progress", {"processing": 4})]
    sent = []
    fake_http.on_get = lambda _id: None if sent else (sent.append(1), os.kill(os.getpid(), signal.SIGTERM))
    _first_batch_run(tmp_path, monkeypatch)
    err = capsys.readouterr().err
    assert "--resume bj" in err and "re-submits" not in err


# --- round 3 --------------------------------------------------------------------------------

def test_a_resume_killed_after_the_claim_write_does_not_forget_the_recorded_batch(tmp_path, monkeypatch, fake_http, gemini_env, script, no_sleep):
    env = _timed_out_run(tmp_path, monkeypatch, fake_http)
    before = env.load("bj")
    assert before["judge_batch"]["id"] == JUDGE_BATCH and before["spent_usd"]["judge"] > 0
    real = runner_mod._write_run_json
    calls = {"n": 0}

    def killed_after_claim(state_dir, name, record):
        calls["n"] += 1
        if calls["n"] >= 2:
            raise KeyboardInterrupt  # the process dies right after the claim write
        return real(state_dir, name, record)

    monkeypatch.setattr(runner_mod, "_write_run_json", killed_after_claim)
    assert _run_cli(tmp_path, monkeypatch, _resume_args(env)) == 130
    monkeypatch.setattr(runner_mod, "_write_run_json", real)
    claimed = env.load("bj")
    assert claimed["judge_batch"]["id"] == JUDGE_BATCH  # the claim write kept the id
    assert claimed["spent_usd"]["judge"] == before["spent_usd"]["judge"]  # and its reserved spend
    fake_http.status_sequences[JUDGE_BATCH] = [_batch_obj(JUDGE_BATCH, "ended", {"succeeded": 4})]
    fake_http.results[JUDGE_BATCH] = _judge_lines(2)
    assert _run_cli(tmp_path, monkeypatch, _resume_args(env)) == 0
    assert len(fake_http.create_posts) == 1  # exactly one create, ever
    assert env.load("bj")["judge_batch"]["id"] == JUDGE_BATCH and env.load("bj")["status"] == "complete"


def _assert_final_dead_end(tmp_path, monkeypatch, env, capsys, reason):
    rec = env.load("bj")
    assert rec["status"] == "stopped" and rec["stop_reason"] == reason
    capsys.readouterr()
    monkeypatch.chdir(tmp_path)
    cli_mod.main(["report", "bj", "--state", env.state])
    captured = capsys.readouterr()
    out = captured.out
    assert "cannot be resumed" in out and "resume it to finish" not in out  # the report tells the truth
    assert "cannot be resumed" in captured.err and "--resume bj" not in captured.err  # and so does its closing note
    assert _run_cli(tmp_path, monkeypatch, _resume_args(env)) == 2
    assert "already ended" in capsys.readouterr().err  # which is what --resume really does


def test_arm_errors_with_an_ended_batch_are_final_not_resumable(tmp_path, monkeypatch, fake_http, gemini_env, script, no_sleep, capsys):
    env = Env(tmp_path, n=2)
    script.on(CAND, "Q1?", lambda: providers.Reply(output=None, input_tokens=None, output_tokens=None, latency_ms=1, error="HTTP 500: boom", status_code=500))
    fake_http.create_responses.append(_batch_obj(JUDGE_BATCH, "in_progress", {"processing": 3}))
    fake_http.status_sequences[JUDGE_BATCH] = [_batch_obj(JUDGE_BATCH, "ended", {"succeeded": 3})]
    fake_http.results[JUDGE_BATCH] = "\n".join(line for line in _judge_lines(2).split("\n") if "0001-judge-candidate" not in line)
    _run_cli(tmp_path, monkeypatch, _args(env.suite_path, env.prices_path, "--batch-judge", name="bj"))
    _assert_final_dead_end(tmp_path, monkeypatch, env, capsys, "arm_errors_judged")


def test_cap_stop_with_an_ended_batch_is_final_not_resumable(tmp_path, monkeypatch, fake_http, gemini_env, script, no_sleep, capsys):
    env = Env(tmp_path, n=2)
    plan = runner_mod.plan(env.suite(), INC, GEMINI, env.prices(), batch_judge=True)
    reserve, est_cand1 = plan.estimate.judge.cost_usd, plan.estimate.candidate.items[1].cost_usd
    cap = 2.0
    ok = lambda i, o: providers.Reply(output="ok", input_tokens=i, output_tokens=o, latency_ms=1, stop_reason="end_turn")
    gemini_env.set_reply(INC, "Q0?", ok(100, 10))
    gemini_env.set_reply(GEMINI, "Q0?", ok(100, 10))
    spent0 = (100 * 2.0 + 10 * 10.0) / 1e6 + (100 * 1.0 + 10 * 4.0) / 1e6
    gemini_env.set_reply(INC, "Q1?", ok(round((cap - est_cand1 - reserve / 2 - spent0) / 2.0 * 1e6), 0))
    _script_judge_batch(fake_http, n=1)
    args = _args(env.suite_path, env.prices_path, "--batch-judge")
    args[args.index("--cap") + 1] = str(cap)
    _run_cli(tmp_path, monkeypatch, args)
    _assert_final_dead_end(tmp_path, monkeypatch, env, capsys, "cap_judged")


def test_collect_with_errored_arms_is_reported_final(tmp_path, monkeypatch, fake_http, gemini_env, script, no_sleep, capsys):
    env = _timed_out_run(tmp_path, monkeypatch, fake_http)
    path = tmp_path / ".pinbench" / "runs" / "bj" / "run.json"
    rec = json.loads(path.read_text())
    rec["items"][1]["candidate"] = dict(rec["items"][1]["candidate"], output=None, error="HTTP 500: boom", stop_reason=None)
    rec["items"][1]["judge"]["candidate"] = {"hit": [], "extra": 0, "notes": "", "error": None, "cost_usd": 0.0, "skipped": "skipped: arm error"}
    path.write_text(json.dumps(rec))
    fake_http.status_sequences[JUDGE_BATCH] = [_batch_obj(JUDGE_BATCH, "ended", {"succeeded": 3})]
    fake_http.results[JUDGE_BATCH] = "\n".join(line for line in _judge_lines(2).split("\n") if "0001-judge-candidate" not in line)
    assert _run_cli(tmp_path, monkeypatch, _resume_args(env)) == 0
    _assert_final_dead_end(tmp_path, monkeypatch, env, capsys, "arm_errors_judged")


def _hold_args(env, *extra, name="bj"):
    return _args(env.suite_path, env.prices_path, "--batch-judge", "--hold-judge-on-errors", *extra, name=name)


def test_hold_judge_on_errors_does_not_submit_over_an_errored_arm(tmp_path, monkeypatch, fake_http, gemini_env, script, no_sleep, capsys):
    env = Env(tmp_path, n=2)
    script.on(CAND, "Q1?", lambda: providers.Reply(output=None, input_tokens=None, output_tokens=None, latency_ms=1, error="HTTP 500: boom", status_code=500))
    rc = _run_cli(tmp_path, monkeypatch, _hold_args(env))
    assert rc == resume_mod.EXIT_QUOTA_STOP
    assert fake_http.create_posts == []
    rec = env.load("bj")
    assert rec["status"] == "stopped" and rec["stop_reason"] == "judge_batch_not_submitted"
    assert rec["judge_batch"] == {"status": "not_submitted", "reason": "arm_errors"}
    err = capsys.readouterr().err
    assert "--batch-judge --hold-judge-on-errors --resume bj" in err  # the printed command repeats the flag


def test_hold_judge_on_errors_resume_fixes_the_arm_then_submits_once(tmp_path, monkeypatch, fake_http, gemini_env, script, no_sleep):
    env = Env(tmp_path, n=2)
    script.on(CAND, "Q1?", lambda: providers.Reply(output=None, input_tokens=None, output_tokens=None, latency_ms=1, error="HTTP 500: boom", status_code=500))
    assert _run_cli(tmp_path, monkeypatch, _hold_args(env)) == 6
    script.reset()
    _script_judge_batch(fake_http, n=2)
    args = ["run", str(env.suite_path), "--incumbent", INC, "--candidate", CAND, "--cap", "5.0", "--prices", str(env.prices_path),
            "--batch-judge", "--hold-judge-on-errors", "--resume", "bj"]
    assert _run_cli(tmp_path, monkeypatch, args) == 0
    assert script.arm_calls == [(CAND, "Q1?")]  # only the errored arm was re-run
    assert len(fake_http.create_posts) == 1
    rec = env.load("bj")
    assert rec["status"] == "complete" and rec["judge_batch"]["status"] == "ended"
    # resuming without the flag it was recorded with is a different measurement
    assert _run_cli(tmp_path, monkeypatch, args[:-3] + ["--resume", "bj"]) == 2


def test_hold_judge_on_errors_needs_batch_judge(tmp_path, monkeypatch, script):
    env = Env(tmp_path, n=1)
    rc = _run_cli(tmp_path, monkeypatch, _args(env.suite_path, env.prices_path, "--hold-judge-on-errors", name="x"))
    assert rc == 2 and script.arm_calls == []
    with pytest.raises(runner_mod.PlanRefused, match="--hold-judge-on-errors applies only"):
        env.run(hold_judge_on_errors=True)


def test_resume_comparability_refuses_a_hold_flag_mismatch(tmp_path, monkeypatch, fake_http, gemini_env, script):
    env = Env(tmp_path, n=2)
    script.on(CAND, "Q1?", lambda: providers.Reply(output=None, input_tokens=None, output_tokens=None, latency_ms=1, error="HTTP 500: boom", status_code=500))
    _run_cli(tmp_path, monkeypatch, _hold_args(env))
    script.reset()
    with pytest.raises(runner_mod.PlanRefused, match="--hold-judge-on-errors differs"):
        env.run(name=None, resume="bj", batch_judge=True)


def test_a_lower_cap_on_resume_still_collects_the_recorded_batch(tmp_path, monkeypatch, fake_http, gemini_env, script, no_sleep):
    env = _timed_out_run(tmp_path, monkeypatch, fake_http)
    rec = env.load("bj")
    arms = rec["spent_usd"]["incumbent"] + rec["spent_usd"]["candidate"]
    reserved = rec["judge_batch"]["reserved_usd"]
    cap = arms + reserved * 0.1  # room for the arms, none for a whole judge batch
    fake_http.status_sequences[JUDGE_BATCH] = [_batch_obj(JUDGE_BATCH, "ended", {"succeeded": 4})]
    fake_http.results[JUDGE_BATCH] = _judge_lines(2)
    args = ["run", str(env.suite_path), "--incumbent", INC, "--candidate", CAND, "--cap", repr(cap), "--prices", str(env.prices_path),
            "--batch-judge", "--resume", "bj"]
    assert _run_cli(tmp_path, monkeypatch, args) == 0
    out = env.load("bj")
    assert len(fake_http.create_posts) == 1 and out["status"] == "complete" and out["judge_batch"]["id"] == JUDGE_BATCH
