"""Cost estimation (before spend) and the in-run cost meter (during spend).

Refuses to do: fabricate a $0 cost for an unpriced model or a response with missing usage
(missing usage is charged at the estimate, never at zero); refuses to let a run exceed its
cap, checked before every single call, not just at the start. A judge model's calls are
estimated and metered exactly like an arm's -- there is no separate, softer cap for judge
spend.
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

    items = suite.items[:limit] if limit is not None else suite.items
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
    judge: Optional[ArmEstimate] = None

    @property
    def total_usd(self) -> float:
        total = self.incumbent.cost_usd + self.candidate.cost_usd
        if self.judge is not None:
            total += self.judge.cost_usd
        return total


def _judge_char_estimate_input_tokens(suite, item) -> int:
    """Input tokens for ONE judge call grading one arm's output on this item: the rubric,
    this item's rubric point descriptions, the item input, and an assumed output the size
    of the arm's own output ceiling -- the ceiling, not a guess, same conservatism as the
    arm estimate above."""
    rubric_points_chars = sum(len(p.description) for p in item.rubric_points)
    chars = len(suite.judge.rubric) + rubric_points_chars + len(item.input) + suite.max_output_tokens * 4
    return math.ceil(chars / CHARS_PER_TOKEN) + INPUT_TOKEN_PAD


def estimate_judge_call_cost(suite, item, price) -> float:
    """Cost of ONE judge call (grading one arm's output on one item). Raises ValueError if
    price is None, same fail-closed rule as estimate_arm."""
    if price is None:
        raise ValueError(f"no price row for judge model '{suite.judge.model}'")
    in_tok = _judge_char_estimate_input_tokens(suite, item)
    out_tok = suite.judge.max_output_tokens
    return in_tok * price.input / 1e6 + out_tok * price.output / 1e6


def estimate_judge_run(suite, judge_id, price, limit=None, arms=("incumbent", "candidate")) -> ArmEstimate:
    """Estimate every judge call a run would make: two per item (one per arm's output).
    `arms` narrows that for a reused incumbent, whose judge calls are never made."""
    if price is None:
        raise ValueError(f"no price row for {judge_id}")
    items = suite.items[:limit] if limit is not None else suite.items
    estimates = []
    total = 0.0
    for item in items:
        cost_each = estimate_judge_call_cost(suite, item, price)
        for arm in arms:
            in_tok = _judge_char_estimate_input_tokens(suite, item)
            estimates.append(
                ItemEstimate(item_id=f"{item.id}:{arm}", input_tokens=in_tok, output_tokens=suite.judge.max_output_tokens, cost_usd=cost_each, measured=False)
            )
            total += cost_each
    total *= ESTIMATE_HEADROOM
    return ArmEstimate(model_id=judge_id, items=estimates, cost_usd=total, measured_count=0)


def actual_call_cost(reply, price, estimate_usd=None) -> Optional[float]:
    """What to charge the meter for one reply. None means "charge the estimate" (an unknown
    outcome); never a fabricated $0. Shared by the arm runner, the batch path and the judge
    stage: one cost rule, not two.

    Rule: never charge less than the tokens a reply reports as billed.

    - Success with usage: tokens at price. Success with missing usage: None (the estimate).
    - Errored reply carrying input and/or output tokens (a Gemini continuation that failed
      after paid hops, or a single call whose usage was parsed from a real response, such as
      an empty-text truncation):
        * HTTP 4xx, or a reply with a stop_reason: the outcome is known (the server refused
          the next request, or answered completely), so the cost is exactly tokens at price.
        * Any other error (a 5xx, a timeout, a dropped connection; stop_reason is None): the
          final hop's outcome is unknown and may have billed more than was reported, so the
          cost is max(tokens at price, estimate_usd). Callers pass the estimate they would
          hand to CostMeter.charge; with no estimate given the token cost is returned as a
          floor, which callers on the metered path must not rely on.
      A single-call empty-text truncation used to be charged the estimate; it now costs its
      real tokens, because its usage is exact and the estimate (which prices the suite's
      max_output_tokens) can be above or below what was billed.
    - Errored reply with neither token count: unchanged. An HTTP 4xx (bad request, auth,
      not-found, rate-limited) never generated, so $0 (known, not unknown); any other error
      leaves the outcome unknown, so None (the estimate).
    """
    if reply.error is not None:
        is_4xx = reply.status_code is not None and 400 <= reply.status_code < 500
        if reply.input_tokens is None and reply.output_tokens is None:
            return 0.0 if is_4xx else None
        # At least one count was reported. Price what is present; a missing count is 0 here
        # ONLY because this is a floor for tokens known to be billed, never a statement that
        # the other side billed nothing.
        token_cost = (reply.input_tokens or 0) * price.input / 1e6 + (reply.output_tokens or 0) * price.output / 1e6
        if is_4xx or reply.stop_reason is not None:
            return token_cost
        return token_cost if estimate_usd is None else max(token_cost, estimate_usd)
    if reply.input_tokens is None or reply.output_tokens is None:
        return None
    return reply.input_tokens * price.input / 1e6 + reply.output_tokens * price.output / 1e6


def estimate_run(suite, incumbent_id, candidate_id, prices, limit=None, state_dir=None, judge_id=None, reuse_incumbent=False) -> RunEstimate:
    """`reuse_incumbent=True` (run --reuse-incumbent): the incumbent arm and its judge calls
    are never made, so both estimate at $0 with no items."""
    from pinbench import suite as suite_mod  # local import avoids a cycle at module load

    suite_hash = suite_mod.freeze_hash(suite)
    inc_price = prices.rows.get(incumbent_id)
    cand_price = prices.rows.get(candidate_id)
    if reuse_incumbent:
        inc = ArmEstimate(model_id=incumbent_id, items=[], cost_usd=0.0, measured_count=0)
    else:
        inc = estimate_arm(suite, incumbent_id, inc_price, limit=limit, state_dir=state_dir, suite_hash=suite_hash)
    cand = estimate_arm(suite, candidate_id, cand_price, limit=limit, state_dir=state_dir, suite_hash=suite_hash)
    judge = None
    if judge_id is not None:
        judge_arms = ("candidate",) if reuse_incumbent else ("incumbent", "candidate")
        judge = estimate_judge_run(suite, judge_id, prices.rows.get(judge_id), limit=limit, arms=judge_arms)
    return RunEstimate(incumbent=inc, candidate=cand, judge=judge)


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
