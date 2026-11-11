"""Proves the full CLI pipeline never opens a socket: socket.socket is patched to raise,
and every command still runs end to end against the fake provider."""

import json
import socket

import pytest
import yaml

from pinbench import cli
from pinbench.providers import anthropic as anthropic_provider


@pytest.fixture(autouse=True)
def _forbid_real_sockets(monkeypatch):
    def _raise(*a, **k):
        raise AssertionError("a test tried to open a real socket")

    monkeypatch.setattr(socket, "socket", _raise)
    yield


def _write(path, text):
    path.write_text(text)
    return path


def test_full_cli_pipeline_never_touches_network(tmp_path, fake_adapter, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)

    pins_path = _write(
        tmp_path / "pins.yaml",
        """\
version: 1
slots:
  classifier:
    model: gpt-5-mini
    provider: openai
    owner: owen
""",
    )
    prices_path = _write(
        tmp_path / "prices.yaml",
        """\
version: 1
checked: 2026-10-26
models:
  gpt-4o-mini: {input: 0.15, output: 0.60, source: "https://x"}
  gpt-5-mini: {input: 0.25, output: 2.00, source: "https://y"}
""",
    )
    suite_path = _write(
        tmp_path / "suite.yaml",
        """\
version: 1
name: demo
task: "Answer briefly."
max_output_tokens: 16
scoring:
  labels: none
  votes: none
policy:
  min_items: 2
  require_votes_when_unlabeled: false   # this smoke test exercises bump, which needs a RECOMMEND
items:
  - id: q01
    input: "Q1?"
  - id: q02
    input: "Q2?"
""",
    )
    state_dir = tmp_path / ".pinbench"
    fake_adapter.models = ["gpt-4o-mini", "gpt-5-mini", "gpt-5-nano"]

    rc = cli.main(
        [
            "plan",
            str(suite_path),
            "--incumbent",
            "gpt-5-mini",
            "--candidate",
            "gpt-4o-mini",
            "--cap",
            "1.0",
            "--prices",
            str(prices_path),
            "--state",
            str(state_dir),
        ]
    )
    assert rc == 0

    rc = cli.main(
        [
            "run",
            str(suite_path),
            "--incumbent",
            "gpt-5-mini",
            "--candidate",
            "gpt-4o-mini",
            "--cap",
            "1.0",
            "--prices",
            str(prices_path),
            "--state",
            str(state_dir),
            "--name",
            "r1",
        ]
    )
    assert rc == 0
    assert fake_adapter.call_count == 4  # 2 items x 2 arms

    rc = cli.main(["vote", "r1", "--state", str(state_dir)])
    assert rc == 0
    assert (state_dir / "runs" / "r1" / "vote.html").exists()

    rc = cli.main(["report", "r1", "--state", str(state_dir), "--format", "text"])
    assert rc == 0
    assert (state_dir / "runs" / "r1" / "report.md").exists()

    rc = cli.main(["watch", "--pins", str(pins_path), "--state", str(state_dir)])
    assert rc == 0  # first run: seeds every provider pins.yaml references

    rc = cli.main(
        [
            "bump",
            "classifier",
            "--to",
            "gpt-4o-mini",
            "--run",
            "r1",
            "--pins",
            str(pins_path),
            "--state",
            str(state_dir),
            "--no-git",
        ]
    )
    assert rc == 0


def test_judge_suite_plan_and_run_never_touch_network(tmp_path, fake_adapter, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)

    prices_path = _write(
        tmp_path / "prices.yaml",
        """\
version: 1
checked: 2026-10-26
models:
  gpt-4o-mini: {input: 0.15, output: 0.60, source: "https://x"}
  gpt-5-mini: {input: 0.25, output: 2.00, source: "https://y"}
  gpt-5-nano: {input: 0.05, output: 0.40, source: "https://z"}
""",
    )
    suite_path = _write(
        tmp_path / "judge_suite.yaml",
        """\
version: 1
name: review
task: "Review this for defects."
max_output_tokens: 16
scoring:
  labels: judge
policy:
  min_items: 2
judge:
  model: gpt-5-nano
  max_output_tokens: 32
  rubric: "grade strictly"
items:
  - id: q01
    input: "Q1?"
    rubric_points:
      - id: p1
        description: "point one"
  - id: q02
    input: "Q2?"
    rubric_points:
      - id: p2
        description: "point two"
""",
    )
    state_dir = tmp_path / ".pinbench"

    rc = cli.main(
        [
            "plan", str(suite_path), "--incumbent", "gpt-5-mini", "--candidate", "gpt-4o-mini",
            "--cap", "5.0", "--prices", str(prices_path), "--state", str(state_dir),
        ]
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "judge" in out
    assert "gpt-5-nano" in out

    rc = cli.main(
        [
            "run", str(suite_path), "--incumbent", "gpt-5-mini", "--candidate", "gpt-4o-mini",
            "--cap", "5.0", "--prices", str(prices_path), "--state", str(state_dir), "--name", "jr1",
        ]
    )
    assert rc == 0
    on_disk = json.loads((state_dir / "runs" / "jr1" / "run.json").read_text())
    assert on_disk["judge_model"] == "gpt-5-nano"
    assert "judge" in on_disk["spent_usd"]
    # 2 items x 2 arms (suite calls) + 2 items x 2 arms (judge calls) = 8
    assert fake_adapter.call_count == 8

    rc = cli.main(["report", "jr1", "--state", str(state_dir), "--format", "text"])
    assert rc == 0
    report_text = capsys.readouterr().out
    assert "Judge: gpt-5-nano" in report_text


def test_run_warns_when_judge_never_called_for_an_entire_arm(tmp_path, fake_adapter, monkeypatch, capsys):
    """When every item's incumbent output is truncated, the judge is skipped on that arm for
    every item; `run` must print a loud, explicit warning rather than leaving a judge metric
    that looks clean (0 data) pass silently."""
    from pinbench import providers

    monkeypatch.chdir(tmp_path)

    prices_path = _write(
        tmp_path / "prices.yaml",
        """\
version: 1
checked: 2026-10-26
models:
  gpt-4o-mini: {input: 0.15, output: 0.60, source: "https://x"}
  gpt-5-mini: {input: 0.25, output: 2.00, source: "https://y"}
  gpt-5-nano: {input: 0.05, output: 0.40, source: "https://z"}
""",
    )
    suite_path = _write(
        tmp_path / "judge_suite.yaml",
        """\
version: 1
name: review
task: "Review this for defects."
max_output_tokens: 16
scoring:
  labels: judge
policy:
  min_items: 1
judge:
  model: gpt-5-nano
  max_output_tokens: 32
  rubric: "grade strictly"
items:
  - id: q01
    input: "Q1?"
    rubric_points:
      - id: p1
        description: "point one"
""",
    )
    state_dir = tmp_path / ".pinbench"
    fake_adapter.set_reply(
        "gpt-5-mini", "Q1?",
        providers.Reply(output="cut off", input_tokens=5, output_tokens=16, latency_ms=1, error=None, stop_reason="max_tokens"),
    )

    rc = cli.main(
        [
            "run", str(suite_path), "--incumbent", "gpt-5-mini", "--candidate", "gpt-4o-mini",
            "--cap", "5.0", "--prices", str(prices_path), "--state", str(state_dir), "--name", "jr2",
        ]
    )
    assert rc == 0
    err = capsys.readouterr().err
    assert "judge was never called for incumbent" in err
    assert "all 1 item(s) skipped" in err


class _FakeResponse:
    def __init__(self, body: bytes):
        self._body = body

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_batch_pipeline_never_touches_network(tmp_path, monkeypatch, capsys):
    """`run --batch` -> `vote` -> `report` -> `bump --no-git`, end to end, with the batch
    endpoints faked at the urllib layer (never through `fake_adapter`, since --batch calls
    `providers/anthropic.py` directly -- it is Anthropic-only) and `socket.socket` forbidden.
    """
    monkeypatch.chdir(tmp_path)
    monkeypatch.delenv("PINBENCH_NO_NETWORK", raising=False)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "test-anthropic-key")

    created = {}

    def fake_urlopen(req, timeout=None):
        url = req.full_url
        method = req.get_method()
        if url.endswith("/v1/messages/batches") and method == "POST":
            created["body"] = json.loads(req.data.decode("utf-8"))
            return _FakeResponse(json.dumps({
                "id": "msgbatch_nn", "processing_status": "in_progress",
                "request_counts": {"processing": 4, "succeeded": 0, "errored": 0, "canceled": 0, "expired": 0},
                "created_at": "2026-11-10T00:00:00Z", "ended_at": None, "expires_at": "2026-11-11T00:00:00Z",
                "cancel_initiated_at": None, "results_url": None,
            }).encode("utf-8"))
        if url.endswith("/v1/messages/batches/msgbatch_nn") and method == "GET":
            return _FakeResponse(json.dumps({
                "id": "msgbatch_nn", "processing_status": "ended",
                "request_counts": {"processing": 0, "succeeded": 4, "errored": 0, "canceled": 0, "expired": 0},
                "created_at": "2026-11-10T00:00:00Z", "ended_at": "2026-11-10T00:01:00Z",
                "expires_at": "2026-11-11T00:00:00Z", "cancel_initiated_at": None,
                "results_url": "https://api.anthropic.com/v1/messages/batches/msgbatch_nn/results",
            }).encode("utf-8"))
        if url.endswith("/v1/messages/batches/msgbatch_nn/results"):
            def line(custom_id, text):
                return json.dumps({"custom_id": custom_id, "result": {"type": "succeeded", "message": {
                    "id": "msg_" + custom_id, "type": "message", "role": "assistant", "model": "claude-sonnet-5-5",
                    "content": [{"type": "text", "text": text}], "stop_reason": "end_turn",
                    "usage": {"input_tokens": 10, "output_tokens": 4},
                }}})
            return _FakeResponse("\n".join([
                line("0000-incumbent", "A0"), line("0000-candidate", "A0 too"),
                line("0001-incumbent", "A1"), line("0001-candidate", "A1 too"),
            ]).encode("utf-8"))
        raise AssertionError(f"unexpected request: {method} {url}")

    monkeypatch.setattr(anthropic_provider.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(anthropic_provider.time, "sleep", lambda s: None)

    pins_path = _write(
        tmp_path / "pins.yaml",
        """\
version: 1
slots:
  classifier:
    model: claude-sonnet-5-5
    provider: anthropic
    owner: owen
""",
    )
    prices_path = tmp_path / "prices.yaml"
    prices_path.write_text(yaml.safe_dump({
        "version": 1, "checked": "2026-10-26",
        "models": {
            "claude-sonnet-5-5": {"input": 2.0, "output": 10.0, "source": "https://x", "batch_multiplier": 0.5},
            "claude-haiku-5-5": {"input": 0.1, "output": 0.5, "source": "https://y", "batch_multiplier": 0.5},
        },
    }, sort_keys=False))
    suite_path = tmp_path / "suite.yaml"
    suite_path.write_text(yaml.safe_dump({
        "version": 1, "name": "demo-batch", "task": "Answer briefly.", "max_output_tokens": 16,
        "scoring": {"labels": "none", "votes": "none"},
        "policy": {"min_items": 2, "require_votes_when_unlabeled": False},
        "items": [{"id": "q01", "input": "Q1?"}, {"id": "q02", "input": "Q2?"}],
    }, sort_keys=False))
    state_dir = tmp_path / ".pinbench"

    rc = cli.main([
        "run", str(suite_path), "--incumbent", "claude-sonnet-5-5", "--candidate", "claude-haiku-5-5",
        "--cap", "1.0", "--prices", str(prices_path), "--state", str(state_dir), "--name", "nn1", "--batch",
    ])
    assert rc == 0
    assert created["body"]["requests"][0]["custom_id"] == "0000-incumbent"

    rc = cli.main(["vote", "nn1", "--state", str(state_dir)])
    assert rc == 0
    assert (state_dir / "runs" / "nn1" / "vote.html").exists()

    rc = cli.main(["report", "nn1", "--state", str(state_dir), "--format", "text"])
    assert rc == 0
    assert (state_dir / "runs" / "nn1" / "report.md").exists()

    rc = cli.main([
        "bump", "classifier", "--to", "claude-haiku-5-5", "--run", "nn1",
        "--pins", str(pins_path), "--state", str(state_dir), "--no-git",
    ])
    assert rc == 0
