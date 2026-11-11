"""Offline tests for `pinbench run --batch` (Anthropic Message Batches).

`FakeBatchHTTP` monkeypatches over `anthropic_provider.urllib.request.urlopen`, routing on
`req.full_url`/method the same way `tests/test_providers.py` does for the sync path. Every
test here either proves a refusal happens before any POST (mixed provider, missing
batch_multiplier, cap too low), or scripts a full create/poll/results cycle and checks the
resulting run.json / exit code / warning text. Each test is written to fail if the behavior
it names were reverted on `pinbench/batch.py` / `pinbench/providers/anthropic.py` /
`pinbench/runner.py` (proven by diffing against `git show HEAD:` during review -- see the
PR description).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
import yaml

from pinbench import cli
from pinbench import batch as batch_mod
from pinbench import prices as prices_mod
from pinbench import runner as runner_mod
from pinbench import suite as suite_mod
from pinbench.providers import anthropic as anthropic_provider

ARMS_ID = "msgbatch_arms"
JUDGE_ID = "msgbatch_judge"


class _FakeResponse:
    def __init__(self, body: bytes):
        self._body = body

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


class FakeBatchHTTP:
    """Scripted responses for create/retrieve/results, keyed by the batch id the test
    chooses for each create response. `urlopen` is the monkeypatch target."""

    def __init__(self):
        self.create_responses = []  # queue of dicts, popped in call order
        self.create_posts = []  # bodies of every POST .../batches (not .../cancel)
        self.status_sequences = {}  # batch_id -> list[dict]; last entry repeats once exhausted
        self.results = {}  # batch_id -> jsonl text
        self.cancel_posts = []  # urls of any POST .../cancel (should stay empty in every test)
        self.get_calls = []  # batch_id per GET, in call order
        self.on_get = None  # optional callable(batch_id) for tests to observe ordering

    def urlopen(self, req, timeout=None):
        url = req.full_url
        method = req.get_method()
        if url.endswith("/results"):
            batch_id = url.rsplit("/", 2)[-2]
            return _FakeResponse(self.results[batch_id].encode("utf-8"))
        if url.endswith("/cancel") and method == "POST":
            self.cancel_posts.append(url)
            return _FakeResponse(json.dumps({"id": "x", "processing_status": "canceling"}).encode("utf-8"))
        if url.endswith("/v1/messages/batches") and method == "POST":
            body = json.loads(req.data.decode("utf-8"))
            self.create_posts.append(body)
            resp = self.create_responses.pop(0)
            return _FakeResponse(json.dumps(resp).encode("utf-8"))
        if "/v1/messages/batches/" in url and method == "GET":
            batch_id = url.rsplit("/", 1)[-1]
            self.get_calls.append(batch_id)
            if self.on_get:
                self.on_get(batch_id)
            seq = self.status_sequences[batch_id]
            resp = seq.pop(0) if len(seq) > 1 else seq[0]
            return _FakeResponse(json.dumps(resp).encode("utf-8"))
        raise AssertionError(f"unexpected request: {method} {url}")


@pytest.fixture
def fake_http(monkeypatch):
    http = FakeBatchHTTP()
    monkeypatch.delenv("PINBENCH_NO_NETWORK", raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-anthropic-key")
    monkeypatch.setattr(anthropic_provider.urllib.request, "urlopen", http.urlopen)
    return http


@pytest.fixture
def no_sleep(monkeypatch):
    calls = []

    def fake_sleep(seconds):
        calls.append(seconds)

    monkeypatch.setattr(batch_mod.time, "sleep", fake_sleep)
    return calls


def _write_suite(tmp_path, name="demo", n=2, max_out=64, judge=False, cap_usd=None):
    items = []
    for i in range(n):
        item = {"id": f"q{i:02d}", "input": f"Q{i}?"}
        if judge:
            item["rubric_points"] = [{"id": f"p{i}", "description": f"point {i}"}]
        items.append(item)
    data = {
        "version": 1,
        "name": name,
        "task": "Answer briefly.",
        "max_output_tokens": max_out,
        "scoring": {"labels": "none"},
        "policy": {"min_items": 1},
        "items": items,
    }
    if judge:
        data["scoring"] = {"labels": "judge"}
        data["judge"] = {"model": "claude-haiku-4-5", "max_output_tokens": 64, "rubric": "grade strictly"}
    if cap_usd is not None:
        data["cap_usd"] = cap_usd
    path = tmp_path / f"{name}.yaml"
    path.write_text(yaml.safe_dump(data, sort_keys=False))
    return path


def _write_prices(tmp_path, extra_models=None):
    models = {
        "claude-sonnet-5-5": {
            "input": 2.0, "output": 10.0, "source": "https://x",
            "batch_multiplier": 0.5,
            "request_defaults": {"thinking": {"type": "between_tools"}},
        },
        "claude-haiku-5-5": {
            "input": 0.1, "output": 0.5, "source": "https://y",
            "batch_multiplier": 0.5,
        },
        # No batch_multiplier: used to prove the missing-row refusal.
        "claude-opus-5-5": {"input": 4.0, "output": 20.0, "source": "https://z"},
        "gpt-5-mini": {"input": 0.25, "output": 2.0, "source": "https://w"},
        # A judge model distinct from both arms above (judge.model == an arm refuses as
        # self-judging, unrelated to batch support).
        "claude-haiku-4-5": {"input": 0.1, "output": 0.5, "source": "https://v", "batch_multiplier": 0.5},
    }
    if extra_models:
        models.update(extra_models)
    path = tmp_path / "prices.yaml"
    path.write_text(yaml.safe_dump({"version": 1, "checked": "2026-10-26", "models": models}, sort_keys=False))
    return path


def _succeeded_line(custom_id, text, input_tokens=10, output_tokens=5, stop_reason="end_turn", model="claude-sonnet-5-5"):
    return json.dumps({
        "custom_id": custom_id,
        "result": {
            "type": "succeeded",
            "message": {
                "id": "msg_" + custom_id,
                "type": "message",
                "role": "assistant",
                "model": model,
                "content": [{"type": "text", "text": text}],
                "stop_reason": stop_reason,
                "usage": {"input_tokens": input_tokens, "output_tokens": output_tokens},
            },
        },
    })


def _errored_line(custom_id, error_type="invalid_request_error", message="bad request"):
    return json.dumps({
        "custom_id": custom_id,
        "result": {"type": "errored", "error": {"type": "error", "error": {"type": error_type, "message": message}}},
    })


def _expired_line(custom_id):
    return json.dumps({"custom_id": custom_id, "result": {"type": "expired"}})


def _canceled_line(custom_id):
    return json.dumps({"custom_id": custom_id, "result": {"type": "canceled"}})


def _batch_obj(batch_id, status, counts):
    return {
        "id": batch_id,
        "processing_status": status,
        "request_counts": {"processing": 0, "succeeded": 0, "errored": 0, "canceled": 0, "expired": 0, **counts},
        "created_at": "2026-11-10T00:00:00Z",
        "ended_at": "2026-11-10T00:05:00Z" if status == "ended" else None,
        "expires_at": "2026-11-11T00:00:00Z",
        "cancel_initiated_at": None,
        "results_url": f"https://api.anthropic.com/v1/messages/batches/{batch_id}/results" if status == "ended" else None,
    }


def _run_cli(tmp_path, monkeypatch, args):
    monkeypatch.chdir(tmp_path)
    return cli.main(args)


# --- 1. mixed provider refuses before any spend --------------------------------------------

def test_batch_refuses_mixed_provider_before_spend(tmp_path, monkeypatch, fake_http):
    suite_path = _write_suite(tmp_path)
    prices_path = _write_prices(tmp_path)
    rc = _run_cli(
        tmp_path, monkeypatch,
        ["run", str(suite_path), "--incumbent", "claude-sonnet-5-5", "--candidate", "gpt-5-mini",
         "--cap", "5.0", "--prices", str(prices_path), "--batch"],
    )
    assert rc == 2
    assert fake_http.create_posts == []


# --- 2. missing batch_multiplier refuses before any spend -----------------------------------

def test_batch_refuses_without_batch_multiplier(tmp_path, monkeypatch, fake_http):
    suite_path = _write_suite(tmp_path)
    prices_path = _write_prices(tmp_path)
    rc = _run_cli(
        tmp_path, monkeypatch,
        ["run", str(suite_path), "--incumbent", "claude-sonnet-5-5", "--candidate", "claude-opus-5-5",
         "--cap", "5.0", "--prices", str(prices_path), "--batch"],
    )
    assert rc == 2
    assert fake_http.create_posts == []


# --- 3. plan --batch halves the estimate, arms and judge ------------------------------------

def test_plan_batch_prints_discounted_estimate(tmp_path, fake_http):
    suite_path = _write_suite(tmp_path, judge=True)
    prices = prices_mod.load(_write_prices(tmp_path))
    suite = suite_mod.load(suite_path)

    sync_plan = runner_mod.plan(suite, "claude-sonnet-5-5", "claude-haiku-5-5", prices, batch=False)
    batch_plan = runner_mod.plan(suite, "claude-sonnet-5-5", "claude-haiku-5-5", prices, batch=True)

    assert batch_plan.estimate.total_usd == pytest.approx(sync_plan.estimate.total_usd * 0.5)
    assert batch_plan.estimate.judge is not None
    assert batch_plan.estimate.judge.cost_usd == pytest.approx(sync_plan.estimate.judge.cost_usd * 0.5)


# --- 4. cap refusal uses the discounted estimate ---------------------------------------------

def test_batch_cap_refusal_uses_discounted_estimate(tmp_path, monkeypatch, fake_http, no_sleep):
    suite_path = _write_suite(tmp_path, n=1)
    prices_path = _write_prices(tmp_path)
    prices = prices_mod.load(prices_path)
    suite = suite_mod.load(suite_path)

    sync_total = runner_mod.plan(suite, "claude-sonnet-5-5", "claude-haiku-5-5", prices, batch=False).estimate.total_usd
    batch_total = runner_mod.plan(suite, "claude-sonnet-5-5", "claude-haiku-5-5", prices, batch=True).estimate.total_usd
    assert batch_total < sync_total

    # Below the discounted estimate: refuses, zero POSTs.
    rc = _run_cli(
        tmp_path, monkeypatch,
        ["run", str(suite_path), "--incumbent", "claude-sonnet-5-5", "--candidate", "claude-haiku-5-5",
         "--cap", f"{batch_total * 0.5:.6f}", "--prices", str(prices_path), "--batch"],
    )
    assert rc == 3
    assert fake_http.create_posts == []

    # Between the two estimates: proceeds (scripted to a clean, cheap completion).
    fake_http.create_responses = [_batch_obj(ARMS_ID, "in_progress", {"processing": 2})]
    fake_http.status_sequences[ARMS_ID] = [_batch_obj(ARMS_ID, "ended", {"succeeded": 2})]
    fake_http.results[ARMS_ID] = "\n".join([
        _succeeded_line("0000-incumbent", "A0", input_tokens=5, output_tokens=2),
        _succeeded_line("0000-candidate", "A0 too", input_tokens=5, output_tokens=2, model="claude-haiku-5-5"),
    ])
    cap_between = (batch_total + sync_total) / 2
    rc = _run_cli(
        tmp_path, monkeypatch,
        ["run", str(suite_path), "--incumbent", "claude-sonnet-5-5", "--candidate", "claude-haiku-5-5",
         "--cap", f"{cap_between:.6f}", "--prices", str(prices_path), "--batch", "--name", "cap-ok"],
    )
    assert rc == 0
    assert len(fake_http.create_posts) == 1


# --- 5. batch params byte-identical to the sync body -----------------------------------------

def test_batch_params_byte_identical_to_sync(tmp_path, monkeypatch, fake_http):
    suite_path = _write_suite(tmp_path, n=1)
    prices = prices_mod.load(_write_prices(tmp_path))
    suite = suite_mod.load(suite_path)
    item = suite.items[0]
    inc_price = prices.rows["claude-sonnet-5-5"]

    captured = {}

    def fake_sync_urlopen(req, timeout=None):
        captured["body"] = json.loads(req.data.decode("utf-8"))
        return _FakeResponse(json.dumps({
            "id": "msg1", "type": "message", "role": "assistant",
            "content": [{"type": "text", "text": "hi"}],
            "stop_reason": "end_turn", "usage": {"input_tokens": 5, "output_tokens": 2},
        }).encode("utf-8"))

    monkeypatch.setattr(anthropic_provider.urllib.request, "urlopen", fake_sync_urlopen)
    inc_params = runner_mod._merged_params(inc_price, suite)
    anthropic_provider.call("claude-sonnet-5-5", suite.task, item.input, suite.max_output_tokens, "key", params=inc_params)
    sync_body = captured["body"]
    assert "thinking" in sync_body

    requests, built_inc_params, _ = batch_mod.build_arm_requests(
        suite.items, suite, "claude-sonnet-5-5", "claude-haiku-5-5", inc_price, prices.rows["claude-haiku-5-5"],
    )
    assert built_inc_params == inc_params
    batch_request = next(r for r in requests if r["custom_id"] == "0000-incumbent")
    assert json.dumps(batch_request["params"], sort_keys=True) == json.dumps(sync_body, sort_keys=True)
    assert "thinking" in batch_request["params"]


# --- 6. results map by custom_id, never by line position -------------------------------------

def test_batch_results_map_by_custom_id_out_of_order(tmp_path, monkeypatch, fake_http, no_sleep):
    suite_path = _write_suite(tmp_path, n=3)
    prices_path = _write_prices(tmp_path)
    fake_http.create_responses = [_batch_obj(ARMS_ID, "in_progress", {"processing": 6})]
    fake_http.status_sequences[ARMS_ID] = [_batch_obj(ARMS_ID, "ended", {"succeeded": 6})]
    # Deliberately shuffled, and candidate before incumbent for several items.
    lines = [
        _succeeded_line("0002-candidate", "cand-2"),
        _succeeded_line("0000-incumbent", "inc-0"),
        _succeeded_line("0001-candidate", "cand-1"),
        _succeeded_line("0000-candidate", "cand-0"),
        _succeeded_line("0002-incumbent", "inc-2"),
        _succeeded_line("0001-incumbent", "inc-1"),
    ]
    fake_http.results[ARMS_ID] = "\n".join(lines)

    rc = _run_cli(
        tmp_path, monkeypatch,
        ["run", str(suite_path), "--incumbent", "claude-sonnet-5-5", "--candidate", "claude-haiku-5-5",
         "--cap", "5.0", "--prices", str(prices_path), "--batch", "--name", "shuffled"],
    )
    assert rc == 0
    run = json.loads((tmp_path / ".pinbench" / "runs" / "shuffled" / "run.json").read_text())
    by_id = {item["id"]: item for item in run["items"]}
    assert by_id["q00"]["incumbent"]["output"] == "inc-0"
    assert by_id["q00"]["candidate"]["output"] == "cand-0"
    assert by_id["q01"]["incumbent"]["output"] == "inc-1"
    assert by_id["q01"]["candidate"]["output"] == "cand-1"
    assert by_id["q02"]["incumbent"]["output"] == "inc-2"
    assert by_id["q02"]["candidate"]["output"] == "cand-2"


# --- 7. errored / expired results are arm errors, $0, and block `vote` -----------------------

def test_batch_errored_and_expired_results_are_arm_errors(tmp_path, monkeypatch, fake_http, no_sleep):
    suite_path = _write_suite(tmp_path, n=3)
    prices_path = _write_prices(tmp_path)
    fake_http.create_responses = [_batch_obj(ARMS_ID, "in_progress", {"processing": 6})]
    fake_http.status_sequences[ARMS_ID] = [_batch_obj(ARMS_ID, "ended", {"succeeded": 5, "errored": 1, "expired": 1})]
    fake_http.results[ARMS_ID] = "\n".join([
        _succeeded_line("0000-incumbent", "inc-0"),
        _errored_line("0000-candidate"),
        _succeeded_line("0001-incumbent", "inc-1"),
        _expired_line("0001-candidate"),
        _succeeded_line("0002-incumbent", "inc-2"),
        _succeeded_line("0002-candidate", "cand-2"),  # candidate has ONE success: not a zero-success arm
    ])

    rc = _run_cli(
        tmp_path, monkeypatch,
        ["run", str(suite_path), "--incumbent", "claude-sonnet-5-5", "--candidate", "claude-haiku-5-5",
         "--cap", "5.0", "--prices", str(prices_path), "--batch", "--name", "errs"],
    )
    assert rc == 0
    run = json.loads((tmp_path / ".pinbench" / "runs" / "errs" / "run.json").read_text())
    q00_cand = next(i for i in run["items"] if i["id"] == "q00")["candidate"]
    q01_cand = next(i for i in run["items"] if i["id"] == "q01")["candidate"]
    assert "batch errored" in q00_cand["error"]
    assert q00_cand["cost_usd"] == 0.0
    assert "batch expired" in q01_cand["error"]
    assert q01_cand["cost_usd"] == 0.0
    assert run["items_succeeded"]["candidate"] == 1
    assert run["batch"]["results"]["0000-candidate"]["type"] == "errored"
    assert run["batch"]["results"]["0001-candidate"]["type"] == "expired"

    rc = _run_cli(tmp_path, monkeypatch, ["vote", "errs", "--state", str(tmp_path / ".pinbench")])
    assert rc == 1


# --- 8. an arm with zero successes exits 4 ----------------------------------------------------

def test_batch_all_errored_arm_exits_4(tmp_path, monkeypatch, fake_http, no_sleep):
    suite_path = _write_suite(tmp_path, n=2)
    prices_path = _write_prices(tmp_path)
    fake_http.create_responses = [_batch_obj(ARMS_ID, "in_progress", {"processing": 4})]
    fake_http.status_sequences[ARMS_ID] = [_batch_obj(ARMS_ID, "ended", {"succeeded": 2, "errored": 2})]
    fake_http.results[ARMS_ID] = "\n".join([
        _succeeded_line("0000-incumbent", "inc-0"),
        _errored_line("0000-candidate"),
        _succeeded_line("0001-incumbent", "inc-1"),
        _errored_line("0001-candidate"),
    ])

    rc = _run_cli(
        tmp_path, monkeypatch,
        ["run", str(suite_path), "--incumbent", "claude-sonnet-5-5", "--candidate", "claude-haiku-5-5",
         "--cap", "5.0", "--prices", str(prices_path), "--batch", "--name", "all-errored"],
    )
    assert rc == 4


# --- 9. empty-text and truncation semantics match the sync adapter ---------------------------

def test_batch_empty_text_and_truncation_semantics(tmp_path, monkeypatch, fake_http, no_sleep, capsys):
    suite_path = _write_suite(tmp_path, n=2)
    prices_path = _write_prices(tmp_path)
    fake_http.create_responses = [_batch_obj(ARMS_ID, "in_progress", {"processing": 4})]
    fake_http.status_sequences[ARMS_ID] = [_batch_obj(ARMS_ID, "ended", {"succeeded": 4})]

    def thinking_only_line(custom_id):
        return json.dumps({
            "custom_id": custom_id,
            "result": {
                "type": "succeeded",
                "message": {
                    "id": "msg_" + custom_id, "type": "message", "role": "assistant",
                    "content": [{"type": "thinking", "thinking": "..."}],
                    "stop_reason": "max_tokens", "usage": {"input_tokens": 10, "output_tokens": 64},
                },
            },
        })

    fake_http.results[ARMS_ID] = "\n".join([
        thinking_only_line("0000-incumbent"),  # empty text -> failure, even though "succeeded"
        _succeeded_line("0000-candidate", "ok-0"),
        _succeeded_line("0001-incumbent", "cut off but has text", stop_reason="max_tokens"),
        _succeeded_line("0001-candidate", "ok-1"),
    ])

    rc = _run_cli(
        tmp_path, monkeypatch,
        ["run", str(suite_path), "--incumbent", "claude-sonnet-5-5", "--candidate", "claude-haiku-5-5",
         "--cap", "5.0", "--prices", str(prices_path), "--batch", "--name", "trunc"],
    )
    assert rc == 0
    run = json.loads((tmp_path / ".pinbench" / "runs" / "trunc" / "run.json").read_text())
    q00_inc = next(i for i in run["items"] if i["id"] == "q00")["incumbent"]
    assert q00_inc["output"] is None
    assert "empty text output" in q00_inc["error"]
    q01_inc = next(i for i in run["items"] if i["id"] == "q01")["incumbent"]
    assert q01_inc["output"] == "cut off but has text"
    assert q01_inc["error"] is None
    assert q01_inc["stop_reason"] == "max_tokens"
    err = capsys.readouterr().err
    assert "truncated" in err


# --- 10. judge batch uses the same messages and strict parse; skipped arms never go in --------

def test_judge_batch_same_messages_and_strict_parse(tmp_path, monkeypatch, fake_http, no_sleep):
    suite_path = _write_suite(tmp_path, n=2, judge=True)
    prices_path = _write_prices(tmp_path)
    prices = prices_mod.load(prices_path)
    suite = suite_mod.load(suite_path)

    fake_http.create_responses = [
        _batch_obj(ARMS_ID, "in_progress", {"processing": 4}),
        _batch_obj(JUDGE_ID, "in_progress", {"processing": 1}),
    ]
    fake_http.status_sequences[ARMS_ID] = [_batch_obj(ARMS_ID, "ended", {"succeeded": 4})]
    fake_http.status_sequences[JUDGE_ID] = [_batch_obj(JUDGE_ID, "ended", {"succeeded": 1})]

    def empty_line(custom_id):
        return json.dumps({
            "custom_id": custom_id,
            "result": {
                "type": "succeeded",
                "message": {
                    "id": "msg_" + custom_id, "type": "message", "role": "assistant",
                    "content": [{"type": "text", "text": ""}],
                    "stop_reason": "end_turn", "usage": {"input_tokens": 3, "output_tokens": 0},
                },
            },
        })

    # q00 candidate errors (so its judge call is SKIPPED, never sent); q01 both succeed.
    fake_http.results[ARMS_ID] = "\n".join([
        _succeeded_line("0000-incumbent", "summary A"),
        _errored_line("0000-candidate"),
        _succeeded_line("0001-incumbent", "summary B-inc"),
        _succeeded_line("0001-candidate", "summary B-cand"),
    ])
    # Only one judge call should ever be sent for q00 (incumbent); q01 sends both.
    fake_http.results[JUDGE_ID] = "\n".join([
        json.dumps({"custom_id": "0000-judge-incumbent", "result": {"type": "succeeded", "message": {
            "id": "m1", "type": "message", "role": "assistant", "model": "claude-haiku-4-5",
            "content": [{"type": "text", "text": '{"hit": ["p0"], "extra": 0, "notes": "ok"}'}],
            "stop_reason": "end_turn", "usage": {"input_tokens": 50, "output_tokens": 10},
        }}}),
        json.dumps({"custom_id": "0001-judge-incumbent", "result": {"type": "succeeded", "message": {
            "id": "m2", "type": "message", "role": "assistant", "model": "claude-haiku-4-5",
            "content": [{"type": "text", "text": "not json at all"}],
            "stop_reason": "end_turn", "usage": {"input_tokens": 50, "output_tokens": 10},
        }}}),
        json.dumps({"custom_id": "0001-judge-candidate", "result": {"type": "succeeded", "message": {
            "id": "m3", "type": "message", "role": "assistant", "model": "claude-haiku-4-5",
            "content": [{"type": "text", "text": '{"hit": [], "extra": 1, "notes": "meh"}'}],
            "stop_reason": "end_turn", "usage": {"input_tokens": 50, "output_tokens": 10},
        }}}),
    ])

    rc = _run_cli(
        tmp_path, monkeypatch,
        ["run", str(suite_path), "--incumbent", "claude-sonnet-5-5", "--candidate", "claude-haiku-5-5",
         "--cap", "5.0", "--prices", str(prices_path), "--batch", "--name", "judged"],
    )
    assert rc == 0

    judge_request_ids = {r["custom_id"] for r in fake_http.create_posts[1]["requests"]}
    assert judge_request_ids == {"0000-judge-incumbent", "0001-judge-incumbent", "0001-judge-candidate"}

    judge_price = prices.rows["claude-haiku-4-5"].at_batch()
    item0 = suite.items[1]
    expected_params = __import__("pinbench.judge", fromlist=["judge_request_params"]).judge_request_params(
        "claude-haiku-4-5", item0, suite, "summary B-inc", judge_price,
    )
    actual_params = next(r for r in fake_http.create_posts[1]["requests"] if r["custom_id"] == "0001-judge-incumbent")["params"]
    assert json.dumps(actual_params, sort_keys=True) == json.dumps(expected_params, sort_keys=True)

    run = json.loads((tmp_path / ".pinbench" / "runs" / "judged" / "run.json").read_text())
    q00 = next(i for i in run["items"] if i["id"] == "q00")
    assert q00["judge"]["candidate"]["skipped"] == "skipped: arm error"
    q01 = next(i for i in run["items"] if i["id"] == "q01")
    assert "malformed judge JSON" in q01["judge"]["incumbent"]["error"]
    assert q01["judge"]["candidate"]["hit"] == []
    assert q01["judge"]["candidate"]["extra"] == 1


# --- 11. ids, request_counts, poll count, and batch.json written before the first poll --------

def test_batch_records_ids_counts_and_poll(tmp_path, monkeypatch, fake_http, no_sleep):
    suite_path = _write_suite(tmp_path, n=1)
    prices_path = _write_prices(tmp_path)
    run_dir = tmp_path / ".pinbench" / "runs" / "polled"
    seen_batch_json_on_first_get = []

    def on_get(batch_id):
        if len(fake_http.get_calls) == 1:  # the very first GET of the whole test
            seen_batch_json_on_first_get.append((run_dir / "batch.json").exists())

    fake_http.on_get = on_get
    fake_http.create_responses = [_batch_obj(ARMS_ID, "in_progress", {"processing": 2})]
    fake_http.status_sequences[ARMS_ID] = [
        _batch_obj(ARMS_ID, "in_progress", {"processing": 2}),
        _batch_obj(ARMS_ID, "in_progress", {"processing": 1, "succeeded": 1}),
        _batch_obj(ARMS_ID, "ended", {"succeeded": 2}),
    ]
    fake_http.results[ARMS_ID] = "\n".join([
        _succeeded_line("0000-incumbent", "inc-0"),
        _succeeded_line("0000-candidate", "cand-0"),
    ])

    rc = _run_cli(
        tmp_path, monkeypatch,
        ["run", str(suite_path), "--incumbent", "claude-sonnet-5-5", "--candidate", "claude-haiku-5-5",
         "--cap", "5.0", "--prices", str(prices_path), "--batch", "--name", "polled", "--poll-interval", "10"],
    )
    assert rc == 0
    assert seen_batch_json_on_first_get == [True]

    batch_json = json.loads((run_dir / "batch.json").read_text())
    assert batch_json["arms"]["id"] == ARMS_ID

    run = json.loads((run_dir / "run.json").read_text())
    assert run["batch"]["arms"]["id"] == ARMS_ID
    assert run["batch"]["arms"]["request_counts"]["succeeded"] == 2
    assert run["batch"]["poll"]["polls"] == 3
    assert run["batch"]["poll"]["interval_s"] == 10
    assert run["batch"]["poll"]["max_wait_s"] == 86400
    assert no_sleep == [10, 10]  # one sleep between each of the 3 polls, minus the last


# --- 12. spend over cap after results is recorded, not fatal -----------------------------------

def test_batch_cap_exceeded_after_results_recorded_not_fatal(tmp_path, monkeypatch, fake_http, no_sleep, capsys):
    suite_path = _write_suite(tmp_path, n=1)
    prices_path = _write_prices(tmp_path)
    prices = prices_mod.load(prices_path)
    suite = suite_mod.load(suite_path)
    estimate = runner_mod.plan(suite, "claude-sonnet-5-5", "claude-haiku-5-5", prices, batch=True).estimate.total_usd
    cap = estimate * 1.2  # fits the estimate...

    fake_http.create_responses = [_batch_obj(ARMS_ID, "in_progress", {"processing": 2})]
    fake_http.status_sequences[ARMS_ID] = [_batch_obj(ARMS_ID, "ended", {"succeeded": 2})]
    # ...but actual usage is wildly larger than the estimate assumed.
    fake_http.results[ARMS_ID] = "\n".join([
        _succeeded_line("0000-incumbent", "inc-0", input_tokens=500_000, output_tokens=500_000),
        _succeeded_line("0000-candidate", "cand-0", input_tokens=500_000, output_tokens=500_000),
    ])

    rc = _run_cli(
        tmp_path, monkeypatch,
        ["run", str(suite_path), "--incumbent", "claude-sonnet-5-5", "--candidate", "claude-haiku-5-5",
         "--cap", f"{cap:.6f}", "--prices", str(prices_path), "--batch", "--name", "overspend"],
    )
    assert rc == 0
    run = json.loads((tmp_path / ".pinbench" / "runs" / "overspend" / "run.json").read_text())
    assert run["batch"]["cap_exceeded_after_results"] is True
    err = capsys.readouterr().err
    assert "exceeded cap" in err


# --- 13. max-wait exits leaving the batch running, no cancel ------------------------------------

def test_batch_max_wait_exits_leaving_batch_running(tmp_path, monkeypatch, fake_http, no_sleep, capsys):
    suite_path = _write_suite(tmp_path, n=1)
    prices_path = _write_prices(tmp_path)
    fake_http.create_responses = [_batch_obj(ARMS_ID, "in_progress", {"processing": 2})]
    fake_http.status_sequences[ARMS_ID] = [_batch_obj(ARMS_ID, "in_progress", {"processing": 2})]  # never ends

    rc = _run_cli(
        tmp_path, monkeypatch,
        ["run", str(suite_path), "--incumbent", "claude-sonnet-5-5", "--candidate", "claude-haiku-5-5",
         "--cap", "5.0", "--prices", str(prices_path), "--batch", "--name", "stuck",
         "--poll-interval", "10", "--max-wait", "20"],
    )
    assert rc == 1
    assert fake_http.cancel_posts == []
    err = capsys.readouterr().err
    assert ARMS_ID in err
    assert "still running" in err
    # batch.json is on disk even though the run never finished.
    assert (tmp_path / ".pinbench" / "runs" / "stuck" / "batch.json").exists()


def test_plan_batch_self_judge_discounts_once(tmp_path, fake_http):
    # review B1: a model named twice (judge == an arm) must not be discounted twice, or the
    # cap gate sees a quarter-price estimate.
    suite_path = _write_suite(tmp_path, judge=True)
    prices = prices_mod.load(_write_prices(tmp_path))
    suite = suite_mod.load(suite_path)
    sync_plan = runner_mod.plan(suite, "claude-haiku-4-5", "claude-sonnet-5-5", prices, batch=False, allow_self_judge=True)
    batch_plan = runner_mod.plan(suite, "claude-haiku-4-5", "claude-sonnet-5-5", prices, batch=True, allow_self_judge=True)
    assert batch_plan.estimate.total_usd == pytest.approx(sync_plan.estimate.total_usd * 0.5)


def test_collect_results_refuses_duplicate_custom_id(monkeypatch):
    class FakeAnthropic:
        @staticmethod
        def fetch_results(batch_id, api_key):
            line = {"custom_id": "0000-incumbent", "result": {"type": "expired"}}
            return [line, dict(line)]

        @staticmethod
        def reply_from_batch_result(line):
            return None
    with pytest.raises(ValueError, match="duplicate custom_id"):
        batch_mod.collect_results(FakeAnthropic, None, "b1", "k", ["0000-incumbent"])


def test_wait_for_batch_rejects_nonpositive_poll_interval():
    for bad in (0, -5):
        with pytest.raises(ValueError):
            batch_mod.wait_for_batch(lambda: {"processing_status": "in_progress"}, "b1", bad, 60, lambda s: None, lambda m: None)


def test_batch_limit_zero_is_not_unlimited(tmp_path, monkeypatch, fake_http, no_sleep):
    suite_path = _write_suite(tmp_path, n=3)
    suite = suite_mod.load(suite_path)
    prices = prices_mod.load(_write_prices(tmp_path))
    plan_result = runner_mod.plan(suite, "claude-sonnet-5-5", "claude-haiku-5-5", prices, batch=True, limit=0)

    submitted = []

    class Stop(Exception):
        pass

    def fake_create(requests, api_key, timeout=60):
        submitted.append(requests)
        raise Stop

    monkeypatch.setattr(anthropic_provider, "create_batch", fake_create)
    with pytest.raises(Stop):
        batch_mod.run_batch(
            suite, "claude-sonnet-5-5", "claude-haiku-5-5", prices, plan_result, 1.0, 0, "r0", tmp_path / "state", suite_path,
        )
    # limit=0 means zero items: the batch was reached, and carried no request.
    assert submitted == [[]]


def test_batch_run_refuses_nonpositive_poll_interval_before_any_spend(tmp_path, fake_http, no_sleep):
    suite_path = _write_suite(tmp_path, n=2)
    suite = suite_mod.load(suite_path)
    prices = prices_mod.load(_write_prices(tmp_path))
    for bad in (0, -3):
        with pytest.raises(runner_mod.PlanRefused, match="poll interval"):
            runner_mod.run(
                suite, "claude-sonnet-5-5", "claude-haiku-5-5", prices, cap_usd=1.0, state_dir=tmp_path / "state",
                suite_path=suite_path, name="r0", batch=True, poll_interval_s=bad,
            )
    assert fake_http.create_posts == []
    assert not (tmp_path / "state" / "runs" / "r0").exists()
