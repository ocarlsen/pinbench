import json
import subprocess

import pytest

from pinbench import bump, registry, report


def _git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True)


def _init_repo(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@example.com")
    _git(repo, "config", "user.name", "test")

    pins_path = repo / "pins.yaml"
    pins_path.write_text(
        """\
version: 1
slots:
  classifier:
    model: gpt-4o-mini
    provider: openai
    owner: owen
"""
    )
    _git(repo, "add", "pins.yaml")
    _git(repo, "commit", "-q", "-m", "init")
    return repo, pins_path


def _write_run_and_report(repo, run_name, incumbent, candidate, verdict_text, fmt="md"):
    """Build a real run + report through the report module, so verdict.json's hashes and the
    re-derived verdict are genuine. verdict_text picks the data: RECOMMEND = candidate matches
    every label at half the cost; HOLD = candidate gets every label wrong."""
    run_dir = repo / ".pinbench" / "runs" / run_name
    run_dir.mkdir(parents=True)
    n = 12
    items_yaml = "\n".join(f'  - id: q{i:02d}\n    input: "Q{i}?"\n    expect: "A{i}"' for i in range(n))
    suite_path = repo / f"suite-{run_name}.yaml"
    suite_path.write_text(
        "version: 1\nname: demo\ntask: \"Answer briefly.\"\nmax_output_tokens: 16\n"
        "scoring:\n  labels: exact\n  votes: none\npolicy:\n  min_items: 10\nitems:\n" + items_yaml + "\n"
    )
    good = verdict_text == "RECOMMEND"
    items = [
        {
            "id": f"q{i:02d}",
            "expect": f"A{i}",
            "incumbent": {"output": f"A{i}", "input_tokens": 10, "output_tokens": 2, "cost_usd": 0.01, "latency_ms": 100, "error": None},
            "candidate": {"output": f"A{i}" if good else "WRONG", "input_tokens": 10, "output_tokens": 2, "cost_usd": 0.005, "latency_ms": 100, "error": None},
        }
        for i in range(n)
    ]
    run_record = {
        "run_name": run_name,
        "suite": str(suite_path),
        "suite_hash": "sha256:x",
        "incumbent": incumbent,
        "candidate": candidate,
        "cap_usd": 1.0,
        "estimate_usd": {"incumbent": 0.1, "candidate": 0.05, "total": 0.15},
        "spent_usd": {"incumbent": 0.09, "candidate": 0.04, "total": 0.13},
        "stopped_early": False,
        "items_completed": n,
        "items_total": n,
        "items": items,
    }
    (run_dir / "run.json").write_text(json.dumps(run_record))
    s = report.load_suite_for_run(run_record)
    result = report.build_verdict(run_record, s, None, None)
    assert result.verdict == verdict_text, result.verdict
    (run_dir / "report.md").write_text(report.render(run_record, s, result, fmt="md"))
    report.write_verdict_record(run_dir, run_record, result)
    return run_dir


def test_bump_refuses_without_report(tmp_path):
    repo, pins_path = _init_repo(tmp_path)
    with pytest.raises(bump.BumpRefused):
        bump.bump(pins_path, "classifier", "gpt-5-mini", "nope", state_dir=repo / ".pinbench", repo_dir=repo)


def test_bump_refuses_without_recommend(tmp_path):
    repo, pins_path = _init_repo(tmp_path)
    _write_run_and_report(repo, "r1", "gpt-4o-mini", "gpt-5-mini", "HOLD")
    with pytest.raises(bump.BumpRefused):
        bump.bump(pins_path, "classifier", "gpt-5-mini", "r1", state_dir=repo / ".pinbench", repo_dir=repo)


def test_bump_refuses_on_pair_mismatch(tmp_path):
    repo, pins_path = _init_repo(tmp_path)
    _write_run_and_report(repo, "r1", "gpt-4o-mini", "gpt-5-nano", "RECOMMEND")
    with pytest.raises(bump.BumpRefused):
        # --to doesn't match the run's candidate
        bump.bump(pins_path, "classifier", "gpt-5-mini", "r1", state_dir=repo / ".pinbench", repo_dir=repo)


def test_bump_succeeds_with_recommend_and_commits_branch(tmp_path):
    repo, pins_path = _init_repo(tmp_path)
    _write_run_and_report(repo, "r1", "gpt-4o-mini", "gpt-5-mini", "RECOMMEND")

    result = bump.bump(pins_path, "classifier", "gpt-5-mini", "r1", state_dir=repo / ".pinbench", repo_dir=repo)

    assert result.branch == "bump/classifier-gpt-5-mini"
    assert result.committed is True

    reg = registry.load(pins_path)
    assert reg.slots["classifier"].model == "gpt-5-mini"

    log = _git(repo, "log", "-1", "--name-only", "--pretty=format:%s")
    assert "bump classifier: gpt-4o-mini -> gpt-5-mini (pinbench RECOMMEND, run r1)" in log.stdout
    assert "pins.yaml" in log.stdout
    assert "report.md" in log.stdout

    branches = _git(repo, "branch", "--show-current").stdout.strip()
    assert branches == "bump/classifier-gpt-5-mini"


def test_bump_force_bypasses_gate_and_records_reason(tmp_path):
    repo, pins_path = _init_repo(tmp_path)
    # no run/report at all

    run_dir = repo / ".pinbench" / "runs" / "r9"
    run_dir.mkdir(parents=True)
    (run_dir / "run.json").write_text(json.dumps({"incumbent": "gpt-4o-mini", "candidate": "gpt-5-mini"}))
    (run_dir / "report.md").write_text("## Verdict: HOLD\n")

    result = bump.bump(
        pins_path,
        "classifier",
        "gpt-5-mini",
        "r9",
        state_dir=repo / ".pinbench",
        repo_dir=repo,
        force_reason="Owen's manual override: HOLD was close, shipping anyway",
    )
    assert result.committed is True
    assert "forced: Owen's manual override" in result.message
    reg = registry.load(pins_path)
    assert reg.slots["classifier"].model == "gpt-5-mini"


def test_bump_no_git_edits_only(tmp_path):
    repo, pins_path = _init_repo(tmp_path)
    _write_run_and_report(repo, "r1", "gpt-4o-mini", "gpt-5-mini", "RECOMMEND")

    result = bump.bump(pins_path, "classifier", "gpt-5-mini", "r1", state_dir=repo / ".pinbench", no_git=True, repo_dir=repo)
    assert result.branch is None
    assert result.committed is False

    reg = registry.load(pins_path)
    assert reg.slots["classifier"].model == "gpt-5-mini"

    status = _git(repo, "status", "--porcelain").stdout
    assert "pins.yaml" in status  # uncommitted, since --no-git


def test_bump_refuses_hand_edited_report(tmp_path):
    """Editing report.md to say RECOMMEND over a HOLD must not open the gate."""
    repo, pins_path = _init_repo(tmp_path)
    run_dir = _write_run_and_report(repo, "r1", "gpt-4o-mini", "gpt-5-mini", "HOLD")
    report_md = run_dir / "report.md"
    report_md.write_text(report_md.read_text().replace("## Verdict: HOLD", "## Verdict: RECOMMEND"))
    with pytest.raises(bump.BumpRefused, match="report.md changed"):
        bump.bump(pins_path, "classifier", "gpt-5-mini", "r1", state_dir=repo / ".pinbench", repo_dir=repo)


def test_bump_refuses_hand_edited_verdict_record(tmp_path):
    """Flipping verdict.json's verdict field is caught by re-deriving the verdict from the inputs."""
    repo, pins_path = _init_repo(tmp_path)
    run_dir = _write_run_and_report(repo, "r1", "gpt-4o-mini", "gpt-5-mini", "HOLD")
    record_path = run_dir / "verdict.json"
    record = json.loads(record_path.read_text())
    record["verdict"] = "RECOMMEND"
    record_path.write_text(json.dumps(record))
    with pytest.raises(bump.BumpRefused, match="inputs give 'HOLD'"):
        bump.bump(pins_path, "classifier", "gpt-5-mini", "r1", state_dir=repo / ".pinbench", repo_dir=repo)


def test_bump_refuses_run_edited_after_report(tmp_path):
    repo, pins_path = _init_repo(tmp_path)
    run_dir = _write_run_and_report(repo, "r1", "gpt-4o-mini", "gpt-5-mini", "RECOMMEND")
    run_json = run_dir / "run.json"
    run_json.write_text(run_json.read_text().replace('"stopped_early": false', '"stopped_early": false '))
    with pytest.raises(bump.BumpRefused, match="run.json changed"):
        bump.bump(pins_path, "classifier", "gpt-5-mini", "r1", state_dir=repo / ".pinbench", repo_dir=repo)


def test_bump_refuses_report_without_verdict_record(tmp_path):
    """A report.md from before verdict.json existed is not enough on its own."""
    repo, pins_path = _init_repo(tmp_path)
    run_dir = _write_run_and_report(repo, "r1", "gpt-4o-mini", "gpt-5-mini", "RECOMMEND")
    (run_dir / "verdict.json").unlink()
    with pytest.raises(bump.BumpRefused, match="no verdict.json"):
        bump.bump(pins_path, "classifier", "gpt-5-mini", "r1", state_dir=repo / ".pinbench", repo_dir=repo)


def test_bump_existing_branch_leaves_pins_untouched(tmp_path):
    """If the branch already exists, the refusal comes before the pins file is edited."""
    repo, pins_path = _init_repo(tmp_path)
    _write_run_and_report(repo, "r1", "gpt-4o-mini", "gpt-5-mini", "RECOMMEND")
    _git(repo, "branch", "bump/classifier-gpt-5-mini")
    before = pins_path.read_text()
    with pytest.raises(bump.BumpRefused, match="could not create branch"):
        bump.bump(pins_path, "classifier", "gpt-5-mini", "r1", state_dir=repo / ".pinbench", repo_dir=repo)
    assert pins_path.read_text() == before
    assert _git(repo, "status", "--porcelain", "pins.yaml").stdout == ""


def test_bump_commit_failure_rolls_back(tmp_path):
    """A failure after the edit (here a commit hook that rejects) restores the pins file and branch."""
    repo, pins_path = _init_repo(tmp_path)
    _write_run_and_report(repo, "r1", "gpt-4o-mini", "gpt-5-mini", "RECOMMEND")
    hook = repo / ".git" / "hooks" / "pre-commit"
    hook.write_text("#!/bin/sh\nexit 1\n")
    hook.chmod(0o755)
    start_branch = _git(repo, "branch", "--show-current").stdout.strip()
    before = pins_path.read_text()
    with pytest.raises(bump.BumpRefused, match="rolled back"):
        bump.bump(pins_path, "classifier", "gpt-5-mini", "r1", state_dir=repo / ".pinbench", repo_dir=repo)
    assert pins_path.read_text() == before
    assert _git(repo, "branch", "--show-current").stdout.strip() == start_branch
    assert "bump/classifier-gpt-5-mini" not in _git(repo, "branch").stdout


def test_pin_reads_the_registry(tmp_path):
    import pinbench

    repo, pins_path = _init_repo(tmp_path)
    assert pinbench.pin("classifier", pins_path) == "gpt-4o-mini"
    with pytest.raises(registry.RegistryError):
        pinbench.pin("nope", pins_path)
