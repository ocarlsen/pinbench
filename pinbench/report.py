"""Build the text/markdown/json report from run.json + mapping.json + votes.json.

Refuses to do: accept a votes.json with an unknown item id or vote value (scoring.unblind_votes
raises; report propagates that as a refusal, never a silent skip).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

from . import scoring as scoring_mod
from . import verdict as verdict_mod


def load_votes(path) -> dict:
    return json.loads(Path(path).read_text())


def load_mapping(path) -> dict:
    return json.loads(Path(path).read_text())


def build_verdict(run: dict, suite, votes: Optional[dict], mapping: Optional[dict]) -> verdict_mod.Verdict:
    return verdict_mod.decide(run, suite, votes=votes, mapping=mapping)


def render_text(run: dict, suite, result: verdict_mod.Verdict) -> str:
    lines = []
    lines.append(f"Run: {run['run_name']}  ({run['incumbent']} vs {run['candidate']})")
    lines.append(
        f"Items: {run['items_completed']}/{run['items_total']} completed"
        + ("  STOPPED EARLY" if run.get("stopped_early") else "")
    )
    lines.append(
        f"Spend: ${run['spent_usd']['total']:.4f} of ${run['cap_usd']:.2f} cap "
        f"(estimate was ${run['estimate_usd']['total']:.4f}, "
        f"spent/estimate = {_ratio(run['spent_usd']['total'], run['estimate_usd']['total']):.2f})"
    )
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
    lines.append(f"Verdict: {result.verdict}")
    lines.append(f"Rule: {result.rule_text}")
    return "\n".join(lines) + "\n"


def render_markdown(run: dict, suite, result: verdict_mod.Verdict) -> str:
    lines = []
    lines.append(f"# pinbench report: {run['run_name']}")
    lines.append("")
    lines.append(f"**{run['incumbent']}** (incumbent) vs **{run['candidate']}** (candidate)")
    lines.append("")
    lines.append(
        f"- Items: {run['items_completed']}/{run['items_total']} completed"
        + (" (stopped early)" if run.get("stopped_early") else "")
    )
    lines.append(
        f"- Spend: ${run['spent_usd']['total']:.4f} of ${run['cap_usd']:.2f} cap "
        f"(estimate ${run['estimate_usd']['total']:.4f})"
    )
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
    lines.append(result.rule_text)
    return "\n".join(lines) + "\n"


def render_json(run: dict, suite, result: verdict_mod.Verdict) -> str:
    payload = {
        "run_name": run["run_name"],
        "incumbent": run["incumbent"],
        "candidate": run["candidate"],
        "items_completed": run["items_completed"],
        "items_total": run["items_total"],
        "stopped_early": run.get("stopped_early", False),
        "spent_usd": run["spent_usd"],
        "estimate_usd": run["estimate_usd"],
        "verdict": result.verdict,
        "checks": [c.__dict__ for c in result.checks],
        "rule": result.rule_text,
    }
    return json.dumps(payload, indent=2) + "\n"


def _ratio(a, b):
    return 0.0 if not b else a / b


RENDERERS = {"text": render_text, "md": render_markdown, "json": render_json}


def render(run: dict, suite, result: verdict_mod.Verdict, fmt: str = "text") -> str:
    return RENDERERS[fmt](run, suite, result)
