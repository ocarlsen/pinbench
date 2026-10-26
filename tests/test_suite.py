import json

import pytest

from pinbench import suite


def _write(tmp_path, text, name="suite.yaml"):
    path = tmp_path / name
    path.write_text(text)
    return path


def test_load_valid_yaml(tmp_path):
    path = _write(
        tmp_path,
        """\
version: 1
name: demo
task: "Answer briefly."
max_output_tokens: 32
items:
  - id: a1
    input: "hi"
    expect: "hello"
""",
    )
    s = suite.load(path)
    assert s.name == "demo"
    assert len(s.items) == 1
    assert s.items[0].expect == "hello"
    assert s.policy.min_items == 10  # default


def test_load_valid_json(tmp_path):
    data = {
        "version": 1,
        "name": "demo",
        "task": "Answer briefly.",
        "max_output_tokens": 32,
        "items": [{"id": "a1", "input": "hi"}],
        "scoring": {"labels": "none"},
    }
    path = tmp_path / "suite.json"
    path.write_text(json.dumps(data))
    s = suite.load(path)
    assert s.name == "demo"
    assert s.scoring.labels == "none"


def test_duplicate_ids_refused(tmp_path):
    path = _write(
        tmp_path,
        """\
version: 1
name: demo
task: "x"
max_output_tokens: 8
items:
  - id: a1
    input: "hi"
    expect: "x"
  - id: a1
    input: "bye"
    expect: "y"
""",
    )
    with pytest.raises(suite.SuiteError):
        suite.load(path)


def test_bad_id_chars_refused(tmp_path):
    path = _write(
        tmp_path,
        """\
version: 1
name: demo
task: "x"
max_output_tokens: 8
items:
  - id: "a 1"
    input: "hi"
    expect: "x"
""",
    )
    with pytest.raises(suite.SuiteError):
        suite.load(path)


def test_empty_input_refused(tmp_path):
    path = _write(
        tmp_path,
        """\
version: 1
name: demo
task: "x"
max_output_tokens: 8
items:
  - id: a1
    input: ""
    expect: "x"
""",
    )
    with pytest.raises(suite.SuiteError):
        suite.load(path)


def test_mixed_labeled_refused(tmp_path):
    path = _write(
        tmp_path,
        """\
version: 1
name: demo
task: "x"
max_output_tokens: 8
scoring:
  labels: exact
items:
  - id: a1
    input: "hi"
    expect: "x"
  - id: a2
    input: "bye"
""",
    )
    with pytest.raises(suite.SuiteError):
        suite.load(path)


def test_labels_none_allows_missing_expect(tmp_path):
    path = _write(
        tmp_path,
        """\
version: 1
name: demo
task: "x"
max_output_tokens: 8
scoring:
  labels: none
items:
  - id: a1
    input: "hi"
""",
    )
    s = suite.load(path)
    assert s.items[0].expect is None


def test_freeze_hash_stable_and_sensitive(tmp_path):
    path = _write(
        tmp_path,
        """\
version: 1
name: demo
task: "x"
max_output_tokens: 8
scoring:
  labels: none
items:
  - id: a1
    input: "hi"
""",
    )
    s1 = suite.load(path)
    s2 = suite.load(path)
    assert suite.freeze_hash(s1) == suite.freeze_hash(s2)

    path2 = _write(
        tmp_path,
        """\
version: 1
name: demo
task: "x"
max_output_tokens: 8
scoring:
  labels: none
items:
  - id: a1
    input: "hi there"
""",
        name="suite2.yaml",
    )
    s3 = suite.load(path2)
    assert suite.freeze_hash(s1) != suite.freeze_hash(s3)


def _judge_suite_text(rubric_points_block=None, judge_block=None):
    points = rubric_points_block or '      - id: p1\n        description: "point one"'
    judge = judge_block if judge_block is not None else "judge:\n  model: judge-model\n  rubric: \"grade it\"\n"
    return f"""\
version: 1
name: review
task: "Review it."
max_output_tokens: 32
scoring:
  labels: judge
policy:
  min_items: 1
{judge}items:
  - id: r1
    input: "an artifact"
    rubric_points:
{points}
"""


def test_judge_suite_loads(tmp_path):
    path = _write(tmp_path, _judge_suite_text())
    s = suite.load(path)
    assert s.scoring.labels == "judge"
    assert s.judge.model == "judge-model"
    assert s.judge.rubric == "grade it"
    assert s.items[0].rubric_points[0].id == "p1"
    assert s.policy.recall_tolerance_points == 0.0
    assert s.policy.extra_tolerance == 0.5


def test_judge_suite_requires_judge_block(tmp_path):
    path = _write(tmp_path, _judge_suite_text(judge_block=""))
    with pytest.raises(suite.SuiteError, match="judge"):
        suite.load(path)


def test_judge_suite_item_requires_rubric_points(tmp_path):
    text = """\
version: 1
name: review
task: "Review it."
max_output_tokens: 32
scoring:
  labels: judge
judge:
  model: judge-model
  rubric: "grade it"
items:
  - id: r1
    input: "an artifact"
"""
    path = _write(tmp_path, text)
    with pytest.raises(suite.SuiteError, match="rubric_points"):
        suite.load(path)


def test_judge_suite_does_not_require_expect(tmp_path):
    path = _write(tmp_path, _judge_suite_text())
    s = suite.load(path)
    assert s.items[0].expect is None  # no "mixed suite" refusal for judge mode


def test_judge_suite_rejects_duplicate_rubric_point_ids(tmp_path):
    dup_points = '      - id: p1\n        description: "point one"\n      - id: p1\n        description: "dup"'
    path = _write(tmp_path, _judge_suite_text(rubric_points_block=dup_points))
    with pytest.raises(suite.SuiteError, match="duplicate rubric point"):
        suite.load(path)


def test_freeze_hash_sensitive_to_rubric_points(tmp_path):
    path1 = _write(tmp_path, _judge_suite_text(), name="a.yaml")
    other_points = '      - id: p1\n        description: "a different description"'
    path2 = _write(tmp_path, _judge_suite_text(rubric_points_block=other_points), name="b.yaml")
    assert suite.freeze_hash(suite.load(path1)) != suite.freeze_hash(suite.load(path2))
