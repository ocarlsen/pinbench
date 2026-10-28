"""Edit a pin, gated on a RECOMMEND verdict for that exact (incumbent, candidate) pair.

Refuses to do: edit pins.yaml without a RECOMMEND verdict matching the slot's current pin
and the requested id, unless `--force "reason"` is given, in which case the reason is
written into the commit message as the recorded escape hatch. The gate never trusts
report.md's text: it reads verdict.json, checks every input against the hash recorded
there, and re-derives the verdict from those inputs. Leaves no half-done bump behind: the
branch is created before the pins file is touched, and any failure after that restores the
file and the original branch.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from . import registry as registry_mod
from . import report as report_mod
from . import runner as runner_mod


class BumpRefused(ValueError):
    pass


@dataclass
class BumpResult:
    branch: Optional[str]
    committed: bool
    message: str


def _check_gate(pins_path, state_dir, slot_name, to_id, run_name) -> None:
    reg = registry_mod.load(pins_path)
    slot = reg.slot(slot_name)
    run_dir = Path(state_dir) / "runs" / run_name
    if not (run_dir / "report.md").exists():
        raise BumpRefused(f"no report.md for run {run_name}; run `pinbench report {run_name}` first")
    verdict_path = run_dir / "verdict.json"
    if not verdict_path.exists():
        raise BumpRefused(f"no verdict.json for run {run_name}; run `pinbench report {run_name}` again")

    run = runner_mod.load_run(state_dir, run_name)
    if run["incumbent"] != slot.model:
        raise BumpRefused(
            f"run {run_name} incumbent '{run['incumbent']}' does not match slot {slot_name}'s current pin '{slot.model}'"
        )
    if run["candidate"] != to_id:
        raise BumpRefused(f"run {run_name} candidate '{run['candidate']}' does not match --to {to_id}")

    record = json.loads(verdict_path.read_text())
    votes_path = record.get("votes_path")
    current = {
        "run.json": report_mod.sha256_file(run_dir / "run.json"),
        "suite": report_mod.sha256_file(run["suite"]),
        "votes": report_mod.sha256_file(votes_path) if votes_path else None,
        "mapping.json": report_mod.sha256_file(run_dir / "mapping.json") if votes_path else None,
        "report.md": report_mod.sha256_file(run_dir / "report.md"),
    }
    for name, recorded in (record.get("sha256") or {}).items():
        if current.get(name) != recorded:
            raise BumpRefused(
                f"run {run_name}: {name} changed since `pinbench report` (or is missing); "
                f"run `pinbench report {run_name}` again"
            )

    votes = report_mod.load_votes(votes_path) if votes_path else None
    mapping = report_mod.load_mapping(run_dir / "mapping.json") if votes_path else None
    result = report_mod.build_verdict(run, report_mod.load_suite_for_run(run), votes, mapping)
    if result.verdict != record.get("verdict"):
        raise BumpRefused(
            f"run {run_name}: verdict.json says {record.get('verdict')!r} but the inputs give "
            f"{result.verdict!r}; run `pinbench report {run_name}` again"
        )
    if result.verdict != "RECOMMEND":
        raise BumpRefused(f"run {run_name} report verdict is {result.verdict!r}, not RECOMMEND")


def bump(pins_path, slot_name, to_id, run_name, state_dir=".pinbench", no_git=False, force_reason=None, repo_dir=None) -> BumpResult:
    old_id = registry_mod.load(pins_path).slot(slot_name).model
    if force_reason:
        # The recorded escape hatch: the verdict gate is skipped and the reason goes in the commit.
        message = f"bump {slot_name}: {old_id} -> {to_id} (forced: {force_reason}, run {run_name})"
    else:
        _check_gate(pins_path, state_dir, slot_name, to_id, run_name)
        message = f"bump {slot_name}: {old_id} -> {to_id} (pinbench RECOMMEND, run {run_name})"

    if no_git:
        registry_mod.set_model(pins_path, slot_name, to_id)
        return BumpResult(branch=None, committed=False, message=message)

    repo_dir = repo_dir or Path(pins_path).resolve().parent
    branch = f"bump/{slot_name}-{to_id}"
    report_rel = str(Path(state_dir) / "runs" / run_name / "report.md")
    pins_rel = str(Path(pins_path).resolve().relative_to(Path(repo_dir).resolve()))
    original_branch = _git(repo_dir, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    original_pins = Path(pins_path).read_bytes()

    # Branch first: if this fails (say the branch already exists), nothing has been edited yet.
    try:
        _git(repo_dir, "checkout", "-b", branch)
    except subprocess.CalledProcessError as exc:
        raise BumpRefused(f"could not create branch {branch}: {exc.stderr.strip()}") from exc

    try:
        registry_mod.set_model(pins_path, slot_name, to_id)
        _git(repo_dir, "add", pins_rel, report_rel)
        _git(repo_dir, "commit", "-m", message)
    except (subprocess.CalledProcessError, registry_mod.RegistryError) as exc:
        Path(pins_path).write_bytes(original_pins)
        _git(repo_dir, "reset", "-q", "--", pins_rel, report_rel)
        _git(repo_dir, "checkout", "-q", original_branch)
        _git(repo_dir, "branch", "-D", branch)
        detail = exc.stderr.strip() if isinstance(exc, subprocess.CalledProcessError) else str(exc)
        raise BumpRefused(f"bump failed and was rolled back (pins file and branch restored): {detail}") from exc

    return BumpResult(branch=branch, committed=True, message=message)


def _git(repo_dir, *args):
    return subprocess.run(["git", "-C", str(repo_dir), *args], check=True, capture_output=True, text=True)
