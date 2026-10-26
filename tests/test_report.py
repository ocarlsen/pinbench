import json

import pytest

from pinbench import blind, report, suite


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
