import json
import subprocess

import pytest

from pinbench import bump, registry


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
    run_dir = repo / ".pinbench" / "runs" / run_name
    run_dir.mkdir(parents=True)
    run_record = {
        "run_name": run_name,
        "incumbent": incumbent,
        "candidate": candidate,
        "items_completed": 12,
        "items_total": 12,
    }
    (run_dir / "run.json").write_text(json.dumps(run_record))
    heading = "## Verdict:" if fmt == "md" else "Verdict:"
    (run_dir / "report.md").write_text(f"{heading} {verdict_text}\n\nRule: ...\n")
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
