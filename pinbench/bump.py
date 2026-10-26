"""Edit a pin, gated on a RECOMMEND report for that exact (incumbent, candidate) pair.

Refuses to do: edit pins.yaml without a RECOMMEND report matching the slot's current pin
and the requested id, unless `--force "reason"` is given, in which case the reason is
written into the commit message as the recorded escape hatch.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from . import registry as registry_mod
from . import runner as runner_mod


class BumpRefused(ValueError):
    pass


@dataclass
class BumpResult:
    branch: Optional[str]
    committed: bool
    message: str


def _read_verdict(report_path: Path) -> Optional[str]:
    for line in report_path.read_text().splitlines():
        line = line.strip()
        if line.startswith("Verdict:"):
            return line.split(":", 1)[1].strip()
        if line.startswith("## Verdict:"):
            return line.split(":", 1)[1].strip()
    return None


def _check_gate(pins_path, state_dir, slot_name, to_id, run_name) -> None:
    reg = registry_mod.load(pins_path)
    slot = reg.slot(slot_name)
    report_path = Path(state_dir) / "runs" / run_name / "report.md"
    if not report_path.exists():
        raise BumpRefused(f"no report.md for run {run_name}; run `pinbench report {run_name}` first")

    run = runner_mod.load_run(state_dir, run_name)
    if run["incumbent"] != slot.model:
        raise BumpRefused(
            f"run {run_name} incumbent '{run['incumbent']}' does not match slot {slot_name}'s current pin '{slot.model}'"
        )
    if run["candidate"] != to_id:
        raise BumpRefused(f"run {run_name} candidate '{run['candidate']}' does not match --to {to_id}")

    verdict = _read_verdict(report_path)
    if verdict != "RECOMMEND":
        raise BumpRefused(f"run {run_name} report verdict is {verdict!r}, not RECOMMEND")


def bump(pins_path, slot_name, to_id, run_name, state_dir=".pinbench", no_git=False, force_reason=None, repo_dir=None) -> BumpResult:
    old_id = registry_mod.load(pins_path).slot(slot_name).model
    if force_reason:
        # The recorded escape hatch: the verdict gate is skipped and the reason goes in the commit.
        message = f"bump {slot_name}: {old_id} -> {to_id} (forced: {force_reason}, run {run_name})"
    else:
        _check_gate(pins_path, state_dir, slot_name, to_id, run_name)
        message = f"bump {slot_name}: {old_id} -> {to_id} (pinbench RECOMMEND, run {run_name})"

    registry_mod.set_model(pins_path, slot_name, to_id)

    if no_git:
        return BumpResult(branch=None, committed=False, message=message)

    repo_dir = repo_dir or Path(pins_path).resolve().parent
    branch = f"bump/{slot_name}-{to_id}"
    report_rel = str(Path(state_dir) / "runs" / run_name / "report.md")
    pins_rel = str(Path(pins_path).resolve().relative_to(Path(repo_dir).resolve()))

    _git(repo_dir, "checkout", "-b", branch)
    _git(repo_dir, "add", pins_rel, report_rel)
    _git(repo_dir, "commit", "-m", message)

    return BumpResult(branch=branch, committed=True, message=message)


def _git(repo_dir, *args):
    subprocess.run(["git", "-C", str(repo_dir), *args], check=True, capture_output=True, text=True)
