import json

import pytest

from pinbench import blind, judge, report, suite


def _suite_path(tmp_path, n=12, labels="exact", votes="none", min_items=10):
    items = "\n".join(
        f'  - id: q{i:02d}\n    input: "Q{i}?"\n    expect: "A{i}"' for i in range(n)
    )
    expect_block = items if labels != "none" else "\n".join(
        f'  - id: q{i:02d}\n    input: "Q{i}?"' for i in range(n)
    )
    path = tmp_path / "suite.yaml"
    path.write_text(
        f"""\
version: 1
name: demo
task: "Answer briefly."
max_output_tokens: 16
scoring:
  labels: {labels}
  votes: {votes}
policy:
  min_items: {min_items}
items:
{expect_block}
"""
    )
    return path


def _run(n, suite_path, labels_match_all=True):
    items = []
    for i in range(n):
        out = f"A{i}" if labels_match_all else "WRONG"
        items.append(
            {
                "id": f"q{i:02d}",
                "expect": f"A{i}",
                "incumbent": {"output": f"A{i}", "input_tokens": 10, "output_tokens": 2, "cost_usd": 0.01, "latency_ms": 100, "error": None},
                "candidate": {"output": out, "input_tokens": 10, "output_tokens": 2, "cost_usd": 0.005, "latency_ms": 100, "error": None},
            }
        )
    return {
        "run_name": "r1",
        "suite": str(suite_path),
        "suite_hash": "sha256:x",
        "incumbent": "incumbent-id",
        "candidate": "candidate-id",
        "cap_usd": 1.0,
        "estimate_usd": {"incumbent": 0.1, "candidate": 0.05, "total": 0.15},
        "spent_usd": {"incumbent": 0.09, "candidate": 0.04, "total": 0.13},
        "stopped_early": False,
        "items_completed": n,
        "items_total": n,
        "items": items,
    }


def test_render_text_contains_verdict_and_rule(tmp_path):
    suite_path = _suite_path(tmp_path, n=12)
    s = suite.load(suite_path)
    run = _run(12, suite_path)
    result = report.build_verdict(run, s, None, None)
    text = report.render(run, s, result, fmt="text")
    assert "Verdict: RECOMMEND" in text
    assert "Rule:" in text


def test_render_markdown_has_table(tmp_path):
    suite_path = _suite_path(tmp_path, n=12)
    s = suite.load(suite_path)
    run = _run(12, suite_path)
    result = report.build_verdict(run, s, None, None)
    md = report.render(run, s, result, fmt="md")
    assert "| check | result | detail |" in md
    assert "## Verdict: RECOMMEND" in md


def test_render_json_is_parseable(tmp_path):
    suite_path = _suite_path(tmp_path, n=12)
    s = suite.load(suite_path)
    run = _run(12, suite_path)
    result = report.build_verdict(run, s, None, None)
    payload = json.loads(report.render(run, s, result, fmt="json"))
    assert payload["verdict"] == "RECOMMEND"
    assert payload["run_name"] == "r1"


def test_render_text_shows_config_path_and_hash_when_used(tmp_path):
    suite_path = _suite_path(tmp_path, n=12)
    s = suite.load(suite_path)
    run = _run(12, suite_path)
    run["effective_config"] = {"config_path": "/some/pinbench.yaml", "config_hash": "sha256:abc"}
    result = report.build_verdict(run, s, None, None)
    text = report.render(run, s, result, fmt="text")
    assert "Config: /some/pinbench.yaml  (sha256:abc)" in text


def test_render_markdown_shows_config_path_and_hash_when_used(tmp_path):
    suite_path = _suite_path(tmp_path, n=12)
    s = suite.load(suite_path)
    run = _run(12, suite_path)
    run["effective_config"] = {"config_path": "/some/pinbench.yaml", "config_hash": "sha256:abc"}
    result = report.build_verdict(run, s, None, None)
    md = report.render(run, s, result, fmt="md")
    assert "Config: /some/pinbench.yaml  (sha256:abc)" in md


def test_render_json_includes_config_path_and_hash(tmp_path):
    suite_path = _suite_path(tmp_path, n=12)
    s = suite.load(suite_path)
    run = _run(12, suite_path)
    run["effective_config"] = {"config_path": "/some/pinbench.yaml", "config_hash": "sha256:abc"}
    result = report.build_verdict(run, s, None, None)
    payload = json.loads(report.render(run, s, result, fmt="json"))
    assert payload["config_path"] == "/some/pinbench.yaml"
    assert payload["config_hash"] == "sha256:abc"


def test_render_text_omits_config_line_when_no_config_was_used(tmp_path):
    suite_path = _suite_path(tmp_path, n=12)
    s = suite.load(suite_path)
    run = _run(12, suite_path)  # no effective_config key at all -- a pre-config run.json
    result = report.build_verdict(run, s, None, None)
    text = report.render(run, s, result, fmt="text")
    assert "Config:" not in text


def test_votes_json_bad_item_id_refused(tmp_path):
    suite_path = _suite_path(tmp_path, n=12, votes="optional")
    s = suite.load(suite_path)
    run = _run(12, suite_path)
    mapping = blind.build_mapping(run)
    bad_votes = {"ghost-item": {"vote": "A"}}
    with pytest.raises(ValueError):
        report.build_verdict(run, s, bad_votes, mapping)


def test_votes_json_bad_vote_value_refused(tmp_path):
    suite_path = _suite_path(tmp_path, n=12, votes="optional")
    s = suite.load(suite_path)
    run = _run(12, suite_path)
    mapping = blind.build_mapping(run)
    item_id = run["items"][0]["id"]
    bad_votes = {item_id: {"vote": "C"}}
    with pytest.raises(ValueError):
        report.build_verdict(run, s, bad_votes, mapping)


def test_incomplete_when_votes_required_and_missing(tmp_path):
    suite_path = _suite_path(tmp_path, n=12, labels="none", votes="required")
    s = suite.load(suite_path)
    run = _run(12, suite_path)
    result = report.build_verdict(run, s, None, None)
    assert result.verdict == "INCOMPLETE"


def _judge_suite_path(tmp_path, n=6):
    items = "\n".join(
        f'  - id: q{i:02d}\n    input: "Q{i}?"\n    rubric_points:\n      - id: p{i}\n        description: "point {i}"'
        for i in range(n)
    )
    path = tmp_path / "judge_suite.yaml"
    path.write_text(
        f"""\
version: 1
name: review
task: "Review."
max_output_tokens: 16
scoring:
  labels: judge
policy:
  min_items: {n}
judge:
  model: judge-model
  rubric: "grade strictly"
items:
{items}
"""
    )
    return path


def _judge_run(n, suite_path):
    items = []
    for i in range(n):
        items.append(
            {
                "id": f"q{i:02d}",
                "rubric_points": [{"id": f"p{i}", "description": f"point {i}"}],
                "incumbent": {"output": "out", "cost_usd": 0.01, "latency_ms": 100, "error": None},
                "candidate": {"output": "out", "cost_usd": 0.005, "latency_ms": 100, "error": None},
                "judge": {
                    "call_order": ["incumbent", "candidate"],
                    "incumbent": {"hit": [f"p{i}"], "extra": 0, "notes": "", "error": None},
                    "candidate": {"hit": [f"p{i}"], "extra": 0, "notes": "", "error": None},
                },
            }
        )
    return {
        "run_name": "jr1",
        "suite": str(suite_path),
        "suite_hash": "sha256:x",
        "incumbent": "incumbent-id",
        "candidate": "candidate-id",
        "judge_model": "judge-model",
        "judge_prompt_hash": judge.prompt_hash("grade strictly"),
        "cap_usd": 1.0,
        "estimate_usd": {"incumbent": 0.1, "candidate": 0.05, "judge": 0.2, "total": 0.35},
        "spent_usd": {"incumbent": 0.09, "candidate": 0.04, "judge": 0.18, "total": 0.31},
        "stopped_early": False,
        "items_completed": n,
        "items_total": n,
        "items": items,
    }


def test_judge_report_text_header_shows_model_and_prompt_hash(tmp_path):
    suite_path = _judge_suite_path(tmp_path)
    s = suite.load(suite_path)
    run = _judge_run(6, suite_path)
    result = report.build_verdict(run, s, None, None)
    text = report.render(run, s, result, fmt="text")
    assert "Judge: judge-model" in text
    assert run["judge_prompt_hash"] in text
    assert "judge_recall" in text
    assert result.verdict == "RECOMMEND"


def test_judge_report_markdown_header_shows_model_and_prompt_hash(tmp_path):
    suite_path = _judge_suite_path(tmp_path)
    s = suite.load(suite_path)
    run = _judge_run(6, suite_path)
    result = report.build_verdict(run, s, None, None)
    md = report.render(run, s, result, fmt="md")
    assert "Judge: `judge-model`" in md
    assert run["judge_prompt_hash"] in md


def test_judge_report_json_includes_judge_fields(tmp_path):
    suite_path = _judge_suite_path(tmp_path)
    s = suite.load(suite_path)
    run = _judge_run(6, suite_path)
    result = report.build_verdict(run, s, None, None)
    payload = json.loads(report.render(run, s, result, fmt="json"))
    assert payload["judge_model"] == "judge-model"
    assert payload["judge_prompt_hash"] == run["judge_prompt_hash"]


def test_judge_report_incomplete_on_judge_error(tmp_path):
    suite_path = _judge_suite_path(tmp_path)
    s = suite.load(suite_path)
    run = _judge_run(6, suite_path)
    run["items"][0]["judge"]["candidate"]["error"] = "malformed judge JSON: boom"
    result = report.build_verdict(run, s, None, None)
    assert result.verdict == "INCOMPLETE"
    text = report.render(run, s, result, fmt="text")
    assert "judge_errors" in text


def test_assert_comparable_refuses_across_different_judge_runs(tmp_path):
    suite_path = _judge_suite_path(tmp_path)
    run_a = _judge_run(6, suite_path)
    run_b = _judge_run(6, suite_path)
    run_b["judge_model"] = "a-different-judge"
    with pytest.raises(judge.JudgeMismatch):
        judge.assert_comparable(run_a, run_b)
