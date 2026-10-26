"""Proves the full CLI pipeline never opens a socket: socket.socket is patched to raise,
and every command still runs end to end against the fake provider."""

import json
import socket

import pytest

from pinbench import cli


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
