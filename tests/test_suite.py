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
