"""Cost estimation (before spend) and the in-run cost meter (during spend).

Refuses to do: fabricate a $0 cost for an unpriced model or a response with missing usage
(missing usage is charged at the estimate, never at zero); refuses to let a run exceed its
cap, checked before every single call, not just at the start.
"""

from __future__ import annotations

import json
import math
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

CHARS_PER_TOKEN = 3.5
INPUT_TOKEN_PAD = 20
ESTIMATE_HEADROOM = 1.10


def _char_estimate_input_tokens(task: str, item_input: str) -> int:
    chars = len(task) + len(item_input)
    return math.ceil(chars / CHARS_PER_TOKEN) + INPUT_TOKEN_PAD


def _prior_measured_input_tokens(state_dir, suite_hash: str, model_id: str) -> dict:
    """item_id -> max input_tokens seen for model_id in a prior run of this suite_hash.

    Scans runs/*/run.json under state_dir. Returns {} when state_dir is None or nothing
    matches; never raises on a missing or malformed state dir.
    """
    if state_dir is None:
        return {}
    runs_dir = Path(state_dir) / "runs"
    if not runs_dir.is_dir():
        return {}
    best: dict = {}
    for run_json in runs_dir.glob("*/run.json"):
        try:
            data = json.loads(run_json.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        if data.get("suite_hash") != suite_hash:
            continue
        for arm_name, arm_model in (("incumbent", data.get("incumbent")), ("candidate", data.get("candidate"))):
            if arm_model != model_id:
                continue
            for item in data.get("items", []):
                arm = item.get(arm_name) or {}
                tok = arm.get("input_tokens")
                if tok is None:
                    continue
                item_id = item["id"]
                if item_id not in best or tok > best[item_id]:
                    best[item_id] = tok
    return best


@dataclass
class ItemEstimate:
    item_id: str
    input_tokens: int
    output_tokens: int
    cost_usd: float
    measured: bool


@dataclass
class ArmEstimate:
    model_id: str
    items: list
    cost_usd: float
    measured_count: int


def estimate_arm(suite, model_id: str, price, limit=None, state_dir=None, suite_hash=None) -> ArmEstimate:
    """Estimate one arm's cost over suite.items[:limit].

    `price` is a prices.PriceRow. Raises ValueError if price is None (unpriced refuses).
    """
    if price is None:
        raise ValueError(f"no price row for {model_id}")

    items = suite.items[:limit] if limit else suite.items
    prior = _prior_measured_input_tokens(state_dir, suite_hash, model_id) if suite_hash else {}

    estimates = []
    total = 0.0
    for item in items:
        measured = item.id in prior
        in_tok = prior[item.id] if measured else _char_estimate_input_tokens(suite.task, item.input)
        out_tok = suite.max_output_tokens
        cost = in_tok * price.input / 1e6 + out_tok * price.output / 1e6
        estimates.append(
            ItemEstimate(item_id=item.id, input_tokens=in_tok, output_tokens=out_tok, cost_usd=cost, measured=measured)
        )
        total += cost

    total *= ESTIMATE_HEADROOM
    measured_count = sum(1 for e in estimates if e.measured)
    return ArmEstimate(model_id=model_id, items=estimates, cost_usd=total, measured_count=measured_count)


@dataclass
class RunEstimate:
    incumbent: ArmEstimate
    candidate: ArmEstimate

    @property
    def total_usd(self) -> float:
        return self.incumbent.cost_usd + self.candidate.cost_usd


def estimate_run(suite, incumbent_id, candidate_id, prices, limit=None, state_dir=None) -> RunEstimate:
    from pinbench import suite as suite_mod  # local import avoids a cycle at module load

    suite_hash = suite_mod.freeze_hash(suite)
    inc_price = prices.rows.get(incumbent_id)
    cand_price = prices.rows.get(candidate_id)
    inc = estimate_arm(suite, incumbent_id, inc_price, limit=limit, state_dir=state_dir, suite_hash=suite_hash)
    cand = estimate_arm(suite, candidate_id, cand_price, limit=limit, state_dir=state_dir, suite_hash=suite_hash)
    return RunEstimate(incumbent=inc, candidate=cand)


class CapExceeded(Exception):
    """Raised by CostMeter when the next call would push spend over the cap."""


@dataclass
class CostMeter:
    """Tracks spend during a run and refuses a call that would push spend over cap."""

    cap_usd: float
    spent_usd: float = 0.0
    stopped_early: bool = False

    def check(self, est_next_call_usd: float) -> bool:
        """True if the next call may proceed; sets stopped_early and returns False otherwise."""
        if self.spent_usd + est_next_call_usd > self.cap_usd:
            self.stopped_early = True
            return False
        return True

    def charge(self, actual_cost_usd: Optional[float], est_cost_usd: float) -> float:
        """Add the actual cost to spent (or the estimate when actual is unknown)."""
        cost = actual_cost_usd if actual_cost_usd is not None else est_cost_usd
        self.spent_usd += cost
        return cost
