"""CLI-level tests for pinbench.yaml: the `config` subcommand, --config, walk-up discovery,
cap-required-everywhere, and the report header's config path/hash."""

import json

from pinbench import cli


def _write(path, text):
    path.write_text(text)
    return path


def _suite(tmp_path, name="suite.yaml", extra=""):
    return _write(
        tmp_path / name,
        f"""\
version: 1
name: demo
task: "Answer briefly."
max_output_tokens: 16
scoring:
  labels: none
  votes: none
policy:
  min_items: 2
  require_votes_when_unlabeled: false
{extra}items:
  - id: q01
    input: "Q1?"
  - id: q02
    input: "Q2?"
""",
    )


def _prices(tmp_path):
    return _write(
        tmp_path / "prices.yaml",
        """\
version: 1
checked: 2026-10-26
models:
  gpt-4o-mini: {input: 0.15, output: 0.60, source: "https://x"}
  gpt-5-mini: {input: 0.25, output: 2.00, source: "https://y"}
""",
    )


def test_config_command_no_file_shows_builtin_defaults(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    rc = cli.main(["config"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "none found" in out
    assert "cap_usd: None  (source: default)" in out
    assert "model: 'claude-opus-5-5'  (source: default)" in out


def test_config_command_walks_up_to_find_pinbench_yaml(tmp_path, monkeypatch, capsys):
    _write(tmp_path / "pinbench.yaml", "cap_usd: 0.75\n")
    nested = tmp_path / "a" / "b"
    nested.mkdir(parents=True)
    monkeypatch.chdir(nested)
    rc = cli.main(["config"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "cap_usd: 0.75  (source: config)" in out
    # Printed relative to the cwd, never as a machine-specific absolute path.
    assert "config file: ../../pinbench.yaml" in out
    assert str(tmp_path) not in out


def test_config_command_explicit_flag_overrides_walk_up(tmp_path, monkeypatch, capsys):
    _write(tmp_path / "pinbench.yaml", "cap_usd: 0.75\n")
    other = _write(tmp_path / "other.yaml", "cap_usd: 9.0\n")
    monkeypatch.chdir(tmp_path)
    rc = cli.main(["config", "--config", str(other)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "cap_usd: 9.0  (source: config)" in out


def test_config_command_explicit_flag_missing_file_refused(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    rc = cli.main(["config", "--config", str(tmp_path / "nope.yaml")])
    assert rc == 1
    err = capsys.readouterr().err
    assert "refused" in err


def test_config_command_unknown_key_refused(tmp_path, monkeypatch, capsys):
    _write(tmp_path / "bad.yaml", "bogus_key: 1\n")
    monkeypatch.chdir(tmp_path)
    rc = cli.main(["config", "--config", str(tmp_path / "bad.yaml")])
    assert rc == 1
    err = capsys.readouterr().err
    assert "bogus_key" in err


def test_config_command_with_suite_shows_suite_sourced_values(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    suite_path = _suite(tmp_path, extra="cap_usd: 3.5\n")
    rc = cli.main(["config", "--suite", str(suite_path)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "cap_usd: 3.5  (source: suite)" in out


def test_run_refuses_with_no_cap_anywhere(tmp_path, monkeypatch, fake_adapter, capsys):
    monkeypatch.chdir(tmp_path)
    suite_path = _suite(tmp_path)
    prices_path = _prices(tmp_path)
    rc = cli.main(
        [
            "run", str(suite_path), "--incumbent", "gpt-5-mini", "--candidate", "gpt-4o-mini",
            "--prices", str(prices_path), "--state", str(tmp_path / ".pinbench"),
        ]
    )
    assert rc == 2
    err = capsys.readouterr().err
    assert "cap" in err
    assert fake_adapter.call_count == 0


def test_run_picks_up_cap_from_pinbench_yaml(tmp_path, monkeypatch, fake_adapter, capsys):
    monkeypatch.chdir(tmp_path)
    _write(tmp_path / "pinbench.yaml", "cap_usd: 1.0\n")
    suite_path = _suite(tmp_path)
    prices_path = _prices(tmp_path)
    rc = cli.main(
        [
            "run", str(suite_path), "--incumbent", "gpt-5-mini", "--candidate", "gpt-4o-mini",
            "--prices", str(prices_path), "--state", str(tmp_path / ".pinbench"), "--name", "r1",
        ]
    )
    assert rc == 0
    assert fake_adapter.call_count == 4


def test_suite_cap_usd_wins_over_pinbench_yaml(tmp_path, monkeypatch, fake_adapter):
    monkeypatch.chdir(tmp_path)
    _write(tmp_path / "pinbench.yaml", "cap_usd: 0.00001\n")  # would refuse if it won
    suite_path = _suite(tmp_path, extra="cap_usd: 1.0\n")
    prices_path = _prices(tmp_path)
    rc = cli.main(
        [
            "run", str(suite_path), "--incumbent", "gpt-5-mini", "--candidate", "gpt-4o-mini",
            "--prices", str(prices_path), "--state", str(tmp_path / ".pinbench"), "--name", "r1",
        ]
    )
    assert rc == 0


def test_cli_cap_wins_over_suite_and_config(tmp_path, monkeypatch, fake_adapter):
    monkeypatch.chdir(tmp_path)
    _write(tmp_path / "pinbench.yaml", "cap_usd: 0.00001\n")
    suite_path = _suite(tmp_path, extra="cap_usd: 0.00001\n")
    prices_path = _prices(tmp_path)
    rc = cli.main(
        [
            "run", str(suite_path), "--incumbent", "gpt-5-mini", "--candidate", "gpt-4o-mini",
            "--cap", "1.0",
            "--prices", str(prices_path), "--state", str(tmp_path / ".pinbench"), "--name", "r1",
        ]
    )
    assert rc == 0


def test_report_header_names_config_path_and_hash(tmp_path, monkeypatch, fake_adapter, capsys):
    monkeypatch.chdir(tmp_path)
    config_path = _write(tmp_path / "pinbench.yaml", "cap_usd: 1.0\n")
    suite_path = _suite(tmp_path)
    prices_path = _prices(tmp_path)
    state_dir = tmp_path / ".pinbench"
    rc = cli.main(
        [
            "run", str(suite_path), "--incumbent", "gpt-5-mini", "--candidate", "gpt-4o-mini",
            "--prices", str(prices_path), "--state", str(state_dir), "--name", "r1",
        ]
    )
    assert rc == 0
    capsys.readouterr()

    rc = cli.main(["report", "r1", "--state", str(state_dir)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "Config: pinbench.yaml  (sha256:" in out
    # report.md is what `bump` commits: no absolute path may reach it or run.json.
    assert str(tmp_path) not in out

    run_json = json.loads((state_dir / "runs" / "r1" / "run.json").read_text())
    assert run_json["effective_config"]["config_path"] == "pinbench.yaml"
    assert run_json["effective_config"]["config_hash"].startswith("sha256:")
    assert config_path.is_file()


def test_config_sourced_paths_persist_relative_to_cwd(tmp_path, monkeypatch, fake_adapter, capsys):
    """A paths entry is resolved against pinbench.yaml's directory, then recorded relative
    to the cwd: usable from here, and free of any absolute local path in run.json."""
    monkeypatch.chdir(tmp_path)
    suite_path = _suite(tmp_path)
    _prices(tmp_path)
    _write(tmp_path / "pinbench.yaml", "cap_usd: 1.0\npaths:\n  prices: prices.yaml\n  runs: state\n")
    rc = cli.main(["run", str(suite_path), "--incumbent", "gpt-5-mini", "--candidate", "gpt-4o-mini", "--name", "r1"])
    assert rc == 0, capsys.readouterr().err
    run_json = json.loads((tmp_path / "state" / "runs" / "r1" / "run.json").read_text())
    paths = run_json["effective_config"]["paths"]
    assert paths["prices"] == {"value": "prices.yaml", "source": "config"}
    assert paths["runs"] == {"value": "state", "source": "config"}
    assert str(tmp_path) not in json.dumps(run_json["effective_config"])


def test_config_command_malformed_yaml_refused(tmp_path, monkeypatch, capsys):
    bad = _write(tmp_path / "bad.yaml", "cap_usd: [unclosed\n")
    monkeypatch.chdir(tmp_path)
    rc = cli.main(["config", "--config", str(bad)])
    assert rc == 1
    assert "refused: " in capsys.readouterr().err


def test_report_reloads_suite_using_runs_own_recorded_judge_defaults(tmp_path, monkeypatch, fake_adapter, capsys):
    """A judge suite that relies on pinbench.yaml for judge.model must still `report`
    correctly even if pinbench.yaml is later edited or deleted -- the run recorded its own
    resolved judge fallback and `report` must reuse exactly that, never re-resolve config."""
    monkeypatch.chdir(tmp_path)
    _write(tmp_path / "pinbench.yaml", "cap_usd: 1.0\njudge:\n  model: claude-config-judge\n")
    suite_path = _write(
        tmp_path / "suite.yaml",
        """\
version: 1
name: review
task: "Review it."
max_output_tokens: 32
scoring:
  labels: judge
  votes: none
policy:
  min_items: 1
  require_votes_when_unlabeled: false
judge:
  rubric: "grade it"
items:
  - id: r1
    input: "an artifact"
    rubric_points:
      - id: p1
        description: "point one"
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
  claude-config-judge: {input: 0.10, output: 0.10, source: "https://z"}
""",
    )
    state_dir = tmp_path / ".pinbench"
    rc = cli.main(
        [
            "run", str(suite_path), "--incumbent", "gpt-5-mini", "--candidate", "gpt-4o-mini",
            "--prices", str(prices_path), "--state", str(state_dir), "--name", "r1",
        ]
    )
    assert rc == 0

    # pinbench.yaml is now gone -- report must still succeed, using run.json's own record.
    (tmp_path / "pinbench.yaml").unlink()
    capsys.readouterr()
    rc = cli.main(["report", "r1", "--state", str(state_dir)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "claude-config-judge" in out
