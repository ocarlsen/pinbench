"""Score a run.json: label agreement, vote tally, cost and latency stats.

Refuses to do: treat an arm's error as anything but a miss for labels and an exclusion
from latency stats; refuses an unknown normalize step name.
"""

from __future__ import annotations

import re
import string
from dataclasses import dataclass
from typing import Optional

_PUNCT_TABLE = str.maketrans("", "", string.punctuation)


def normalize(text: str, steps) -> str:
    out = text
    for step in steps:
        if step == "strip":
            out = out.strip()
        elif step == "casefold":
            out = out.casefold()
        elif step == "strip_punct":
            out = out.translate(_PUNCT_TABLE)
        else:
            raise ValueError(f"unknown normalize step: {step}")
    return out


def label_match(output: Optional[str], expect: str, mode: str, normalize_steps) -> bool:
    if output is None:
        return False
    norm_output = normalize(output, normalize_steps)
    norm_expect = normalize(expect, normalize_steps)
    if mode == "exact":
        return norm_output == norm_expect
    if mode == "contains":
        return norm_expect in norm_output
    if mode == "regex":
        return re.search(expect, output) is not None
    raise ValueError(f"unknown labels mode: {mode}")


@dataclass
class LabelAgreement:
    matched: int
    total: int

    @property
    def pct(self) -> float:
        return 0.0 if self.total == 0 else 100.0 * self.matched / self.total


def label_agreement(run: dict, arm: str, mode: str, normalize_steps) -> LabelAgreement:
    matched = 0
    total = 0
    for item in run["items"]:
        arm_result = item.get(arm)
        if arm_result is None or item.get("expect") is None:
            continue
        total += 1
        if label_match(arm_result.get("output"), item["expect"], mode, normalize_steps):
            matched += 1
    return LabelAgreement(matched=matched, total=total)


def error_count(run: dict, arm: str) -> int:
    return sum(1 for item in run["items"] if item.get(arm) and item[arm].get("error"))


def first_error(run: dict, arm: str) -> Optional[str]:
    for item in run["items"]:
        arm_result = item.get(arm)
        if arm_result and arm_result.get("error"):
            return arm_result["error"]
    return None


def cost_per_item(run: dict, arm: str) -> Optional[float]:
    costs = [item[arm]["cost_usd"] for item in run["items"] if item.get(arm) and item[arm].get("cost_usd") is not None]
    if not costs:
        return None
    return sum(costs) / len(costs)


def latency_stats(run: dict, arm: str) -> dict:
    """Mean latency over non-error completions; excludes errors per spec section 4.4."""
    values = [
        item[arm]["latency_ms"]
        for item in run["items"]
        if item.get(arm) and not item[arm].get("error") and item[arm].get("latency_ms") is not None
    ]
    if not values:
        return {"mean_ms": None, "n": 0}
    return {"mean_ms": sum(values) / len(values), "n": len(values)}


@dataclass
class VoteTally:
    candidate_wins: int
    incumbent_wins: int
    ties: int
    n: int


def unblind_votes(votes: dict, mapping: dict) -> VoteTally:
    """votes: {item_id: {vote, note}}; mapping: {item_id: {A: side, B: side}}."""
    candidate_wins = 0
    incumbent_wins = 0
    ties = 0
    n = 0
    for item_id, vote_rec in votes.items():
        if item_id not in mapping:
            raise ValueError(f"votes.json has unknown item id: {item_id}")
        vote = vote_rec.get("vote")
        if vote not in ("A", "B", "tie"):
            raise ValueError(f"votes.json has unknown vote value for {item_id}: {vote!r}")
        n += 1
        if vote == "tie":
            ties += 1
            continue
        winner_side = mapping[item_id][vote]
        if winner_side == "candidate":
            candidate_wins += 1
        else:
            incumbent_wins += 1
    return VoteTally(candidate_wins=candidate_wins, incumbent_wins=incumbent_wins, ties=ties, n=n)
