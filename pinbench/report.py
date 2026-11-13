"""Build the text/markdown/json report from run.json + mapping.json + votes.json.

Refuses to do: accept a votes.json with an unknown item id or vote value (scoring.unblind_votes
raises; report propagates that as a refusal, never a silent skip). For a judge-scored run,
always prints the judge model id and its prompt hash in the header -- two runs are only the
same measurement when both match (judge.assert_comparable), so the report states both rather
than letting a reader assume. For a judge-scored run, always states how many judge calls were
actually judged vs skipped (truncated/error), so a drop in judge spend is never mistaken for
a drop in judge calls that were graded.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Optional

from . import scoring as scoring_mod
from . import suite as suite_mod
from . import verdict as verdict_mod


def load_votes(path) -> dict:
    return json.loads(Path(path).read_text())


def load_mapping(path) -> dict:
    return json.loads(Path(path).read_text())


def build_verdict(run: dict, suite, votes: Optional[dict], mapping: Optional[dict]) -> verdict_mod.Verdict:
    return verdict_mod.decide(run, suite, votes=votes, mapping=mapping)


def load_suite_for_run(run: dict):
    """Reload the suite with the SAME judge/policy fallbacks the run itself used (recorded in
    run.json), so a verdict built later reproduces the run exactly even if pinbench.yaml has
    since changed or moved. A run.json from before this feature has no "effective_config"
    (both defaults land on None), which is the suite module's own built-in-only behavior.
    """
    effective = run.get("effective_config") or {}
    judge_defaults = (effective.get("judge_defaults") or {}).get("value")
    policy_defaults = (effective.get("policy_defaults") or {}).get("value")
    return suite_mod.load(run["suite"], judge_defaults=judge_defaults, policy_defaults=policy_defaults)


def sha256_file(path) -> Optional[str]:
    path = Path(path)
    if not path.is_file():
        return None
    return "sha256:" + hashlib.sha256(path.read_bytes()).hexdigest()


def write_verdict_record(run_dir, run: dict, result: verdict_mod.Verdict, votes_path=None) -> Path:
    """Write verdict.json beside report.md: the verdict plus a hash of every input it came from.

    `pinbench bump` reads this, not report.md, and re-derives the verdict from the same
    inputs, so editing report.md (or this file's verdict field) cannot open the gate.
    """
    run_dir = Path(run_dir)
    record = {
        "verdict": result.verdict,
        "incumbent": run["incumbent"],
        "candidate": run["candidate"],
        "votes_path": str(Path(votes_path).resolve()) if votes_path else None,
        "sha256": {
            "run.json": sha256_file(run_dir / "run.json"),
            "suite": sha256_file(run["suite"]),
            "votes": sha256_file(votes_path) if votes_path else None,
            "mapping.json": sha256_file(run_dir / "mapping.json") if votes_path else None,
            "report.md": sha256_file(run_dir / "report.md"),
        },
    }
    target = run_dir / "verdict.json"
    target.write_text(json.dumps(record, indent=2) + "\n")
    return target


def _config_line(run: dict) -> Optional[str]:
    """'Config: <path>  (<hash>)' when this run used a pinbench.yaml, else None -- a run
    with no effective_config (pre-config run.json) or no config file found prints nothing."""
    effective = run.get("effective_config") or {}
    config_path = effective.get("config_path")
    if not config_path:
        return None
    return f"Config: {config_path}  ({effective.get('config_hash')})"


def render_text(run: dict, suite, result: verdict_mod.Verdict) -> str:
    lines = []
    lines.append(f"Run: {run['run_name']}  ({run['incumbent']} vs {run['candidate']})")
    config_line = _config_line(run)
    if config_line:
        lines.append(config_line)
    if run.get("judge_model"):
        lines.append(f"Judge: {run['judge_model']}  (prompt {run['judge_prompt_hash']})")
    lines.append(
        f"Items: {run['items_completed']}/{run['items_total']} completed"
        + ("  STOPPED EARLY" if run.get("stopped_early") else "")
    )
    lines.append(
        f"Spend: ${run['spent_usd']['total']:.4f} of ${run['cap_usd']:.2f} cap "
        f"(estimate was ${run['estimate_usd']['total']:.4f}, "
        f"spent/estimate = {_ratio(run['spent_usd']['total'], run['estimate_usd']['total']):.2f})"
    )
    if run.get("judge_model") and "judge" in run.get("spent_usd", {}):
        lines.append(f"  of which judge: ${run['spent_usd']['judge']:.4f} spent, ${run['estimate_usd'].get('judge', 0.0):.4f} estimated")
    judge_calls_line = _judge_calls_line(run)
    if judge_calls_line:
        lines.append(f"  {judge_calls_line}")
    batch_line = _batch_line(run)
    if batch_line:
        lines.append(batch_line)
    for arm in ("incumbent", "candidate"):
        errs = scoring_mod.error_count(run, arm)
        if errs:
            lines.append(f"{arm} errors: {errs}")
    lines.append("")
    lines.append("Checks:")
    for check in result.checks:
        mark = "PASS" if check.passed else "FAIL"
        lines.append(f"  [{mark}] {check.name}: {check.detail}")
    lines.append("")
    reuse_line = _reuse_line(run)
    if reuse_line:
        lines.append(reuse_line)
        lines.append("")
    lines.append(f"Verdict: {result.verdict}")
    lines.append(f"Rule: {result.rule_text}")
    return "\n".join(lines) + "\n"


def render_markdown(run: dict, suite, result: verdict_mod.Verdict) -> str:
    lines = []
    lines.append(f"# pinbench report: {run['run_name']}")
    lines.append("")
    lines.append(f"**{run['incumbent']}** (incumbent) vs **{run['candidate']}** (candidate)")
    config_line = _config_line(run)
    if config_line:
        lines.append("")
        lines.append(config_line)
    if run.get("judge_model"):
        lines.append("")
        lines.append(f"Judge: `{run['judge_model']}` (prompt `{run['judge_prompt_hash']}`)")
    lines.append("")
    lines.append(
        f"- Items: {run['items_completed']}/{run['items_total']} completed"
        + (" (stopped early)" if run.get("stopped_early") else "")
    )
    lines.append(
        f"- Spend: ${run['spent_usd']['total']:.4f} of ${run['cap_usd']:.2f} cap "
        f"(estimate ${run['estimate_usd']['total']:.4f})"
    )
    judge_calls_line = _judge_calls_line(run)
    if judge_calls_line:
        lines.append(f"- {judge_calls_line}")
    batch_line = _batch_line(run)
    if batch_line:
        lines.append(f"- {batch_line}")
    lines.append("")
    lines.append("## Checks")
    lines.append("")
    lines.append("| check | result | detail |")
    lines.append("|---|---|---|")
    for check in result.checks:
        mark = "PASS" if check.passed else "FAIL"
        lines.append(f"| {check.name} | {mark} | {check.detail} |")
    lines.append("")
    lines.append(f"## Verdict: {result.verdict}")
    lines.append("")
    reuse_line = _reuse_line(run)
    if reuse_line:
        lines.append(f"> {reuse_line}")
        lines.append("")
    lines.append(result.rule_text)
    return "\n".join(lines) + "\n"


def render_json(run: dict, suite, result: verdict_mod.Verdict) -> str:
    payload = {
        "run_name": run["run_name"],
        "incumbent": run["incumbent"],
        "candidate": run["candidate"],
        "judge_model": run.get("judge_model"),
        "judge_prompt_hash": run.get("judge_prompt_hash"),
        "config_path": (run.get("effective_config") or {}).get("config_path"),
        "config_hash": (run.get("effective_config") or {}).get("config_hash"),
        "items_completed": run["items_completed"],
        "items_total": run["items_total"],
        "stopped_early": run.get("stopped_early", False),
        # A reuse run counts only the judge calls made this run (candidate); see _judge_calls_line.
        "judge_calls_judged": scoring_mod.judge_judged_count(run, "candidate" if run.get("reused_incumbent") else None) if run.get("judge_model") else None,
        "judge_calls_skipped": scoring_mod.judge_skipped_count(run, "candidate" if run.get("reused_incumbent") else None) if run.get("judge_model") else None,
        "spent_usd": run["spent_usd"],
        "estimate_usd": run["estimate_usd"],
        "reused_incumbent": run.get("reused_incumbent"),
        "verdict": result.verdict,
        "checks": [c.__dict__ for c in result.checks],
        "rule": result.rule_text,
    }
    return json.dumps(payload, indent=2) + "\n"


def _reuse_line(run: dict) -> Optional[str]:
    """Printed beside the verdict for a `--reuse-incumbent` run, else None: the two arms were
    not measured together, and the incumbent's cost is a recomputation."""
    reused = run.get("reused_incumbent")
    if not reused:
        return None
    recorded = (reused.get("source_started_at") or "an unrecorded date")[:10]
    text = (
        f"Incumbent outputs reused from run {reused['run']}, recorded on {recorded}; "
        "the two arms were judged in different sessions. Incumbent arm cost is recomputed "
        "from recorded tokens at this run's prices where the source recorded tokens (an "
        "errored or tokenless arm keeps its recorded cost); a batch source's incumbent judge "
        "cost is rescaled by the source's recorded batch multiplier. Neither is what was paid."
    )
    arm_errs = scoring_mod.error_count(run, "incumbent")
    judge_errs = sum(1 for i in run["items"] if ((i.get("judge") or {}).get("incumbent") or {}).get("error"))
    if arm_errs or judge_errs:
        text += (
            f" Carried-over incumbent errors: {arm_errs} arm, {judge_errs} judge; "
            "they were not retried, so the verdict is INCOMPLETE because of them."
        )
    return text


def _batch_line(run: dict) -> Optional[str]:
    """'Batch: arms msgbatch_x (N succeeded, M errored); judge msgbatch_y (...)' for a
    `--batch` run, else None. Names both batch ids and their final request_counts so a
    reader can tell a batch run from a sync one without opening run.json."""
    batch = run.get("batch")
    if not batch:
        judge_batch = run.get("judge_batch")
        if not judge_batch:
            return None
        # `--batch-judge`: arms ran synchronously, only the judge was batched.
        text = f"judge: batched (id {judge_batch.get('id')})"
        if judge_batch.get("status") not in ("ended", "nothing_to_judge"):
            text += f", {judge_batch.get('status')}: not every judge call has a result"
        return text
    arms = batch.get("arms") or {}
    arm_counts = arms.get("request_counts") or {}
    text = f"Batch: arms {arms.get('id')} ({arm_counts.get('succeeded', 0)} succeeded, {arm_counts.get('errored', 0)} errored)"
    judge = batch.get("judge")
    if judge:
        judge_counts = judge.get("request_counts") or {}
        text += f"; judge {judge.get('id')} ({judge_counts.get('succeeded', 0)} succeeded, {judge_counts.get('errored', 0)} errored)"
    else:
        text += "; judge none"
    return text


def _judge_calls_line(run: dict) -> Optional[str]:
    """'judge calls: N judged, M skipped (truncated/error)' for a judge-scored run, else
    None -- a skipped call is one judge.py never made because the arm's own output errored,
    was empty, or was truncated, so N + M never overstates what was actually billed.
    """
    if not run.get("judge_model"):
        return None
    if run.get("reused_incumbent"):
        # Only calls made in THIS run (candidate); the incumbent's judge records were copied.
        made = scoring_mod.judge_judged_count(run, "candidate")
        skipped = scoring_mod.judge_skipped_count(run, "candidate")
        carried = scoring_mod.judge_judged_count(run, "incumbent") + scoring_mod.judge_skipped_count(run, "incumbent")
        return f"judge calls: {made} judged, {skipped} skipped (truncated/error), all candidate; {carried} incumbent judge records reused, not made this run"
    judged = scoring_mod.judge_judged_count(run)
    skipped = scoring_mod.judge_skipped_count(run)
    return f"judge calls: {judged} judged, {skipped} skipped (truncated/error)"


def _ratio(a, b):
    return 0.0 if not b else a / b


RENDERERS = {"text": render_text, "md": render_markdown, "json": render_json}


def render(run: dict, suite, result: verdict_mod.Verdict, fmt: str = "text") -> str:
    return RENDERERS[fmt](run, suite, result)
