"""Score a run.json: label agreement, vote tally, judge recall/extra, cost and latency stats.

Refuses to do: treat an arm's error as anything but a miss for labels and an exclusion
from latency stats; refuses an unknown normalize step name; refuses to call a truncated
output "complete" without saying so (is_truncated/truncated_count); refuses to count a
rubric point an item never declared, or to let a judge error on an item silently drop that
item's rubric points from the recall denominator (a judge error makes the run INCOMPLETE,
scored separately, never averaged away). A judge call that was never made (skipped because
the arm's own output errored, was empty, or was truncated) is scored like a judge error in
judge_recall (points kept, zero hits: an ungraded output earns nothing) and excluded from
judge_extra_mean, but is never a judge error in judge_error_count -- a skip is a different
thing from a judge that was called and then errored.
"""

from __future__ import annotations

import re
import string
from dataclasses import dataclass
from typing import Optional

_PUNCT_TABLE = str.maketrans("", "", string.punctuation)

# The vendor stop/finish reasons that mean "cut off by the output ceiling, not a natural
# stop": Anthropic's `stop_reason` and OpenAI's `finish_reason` use different words for the
# same event. This is the one place that mapping lives; every other module imports it.
TRUNCATION_STOP_REASONS = {"max_tokens", "length"}


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


def is_truncated(arm_result: Optional[dict]) -> bool:
    """True when this arm's own stop/finish reason says the output ceiling cut it off.

    False for an arm that was never attempted (None) or that errored for any other reason
    (a missing stop_reason there is not truncation, it is unknown).
    """
    return bool(arm_result) and arm_result.get("stop_reason") in TRUNCATION_STOP_REASONS


def truncated_count(run: dict, arm: str) -> int:
    return sum(1 for item in run["items"] if is_truncated(item.get(arm)))


def first_truncated_item_id(run: dict, arm: str) -> Optional[str]:
    for item in run["items"]:
        if is_truncated(item.get(arm)):
            return item["id"]
    return None


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
class JudgeRecall:
    hits: int
    points: int

    @property
    def pct(self) -> float:
        return 0.0 if self.points == 0 else 100.0 * self.hits / self.points


def judge_recall(run: dict, arm: str) -> JudgeRecall:
    """Total rubric points hit for `arm` over total rubric points declared, across every
    item -- an item whose judge call errored contributes its points to the denominator but
    zero hits (the error itself fails the run via judge_error_count, this just keeps the
    recall math honest rather than silently shrinking n). An item whose judge call was
    SKIPPED (the arm's own output errored, was empty, or was truncated -- judge.py never
    called the judge at all) is scored the same way: its points stay in the denominator
    with zero hits. An ungraded output earns nothing. Dropping the item instead would let
    an arm that truncates on the hardest item (within policy.max_truncated) post its
    recall over the easy ones and reach RECOMMEND on a smaller denominator than the other
    arm's.
    """
    hits = 0
    points = 0
    for item in run["items"]:
        item_points = len(item.get("rubric_points") or [])
        points += item_points
        judge = item.get("judge") or {}
        arm_result = judge.get(arm)
        if arm_result and not arm_result.get("error") and not arm_result.get("skipped"):
            hits += len(arm_result.get("hit") or [])
    return JudgeRecall(hits=hits, points=points)


def judge_extra_mean(run: dict, arm: str) -> Optional[float]:
    """Mean "extra" (spurious findings) count over items the judge actually scored for this
    arm; None when nothing was scored. Items with a judge error, or whose judge call was
    skipped (see judge_recall), are excluded, same as a latency stat excludes an arm error.
    """
    values = []
    for item in run["items"]:
        judge = item.get("judge") or {}
        arm_result = judge.get(arm)
        if arm_result and not arm_result.get("error") and not arm_result.get("skipped"):
            values.append(arm_result.get("extra", 0))
    if not values:
        return None
    return sum(values) / len(values)


def judge_error_count(run: dict) -> int:
    """Count of (item, arm) judge calls that errored, across both arms. A skipped slot
    (never called at all) carries error=None, so it is never counted here -- a skip is not
    a judge error, it is the judge refusing to spend on an answer that was already doomed.
    """
    count = 0
    for item in run["items"]:
        judge = item.get("judge") or {}
        for arm in ("incumbent", "candidate"):
            arm_result = judge.get(arm)
            if arm_result and arm_result.get("error"):
                count += 1
    return count


def judge_skipped_count(run: dict, arm: Optional[str] = None) -> int:
    """Count of (item, arm) judge slots recorded as skipped -- the judge was never called
    because that arm's own output errored, was empty, or was truncated. `arm` narrows the
    count to one side; omitted, it counts both.
    """
    arms = (arm,) if arm else ("incumbent", "candidate")
    count = 0
    for item in run["items"]:
        judge = item.get("judge") or {}
        for a in arms:
            arm_result = judge.get(a)
            if arm_result and arm_result.get("skipped"):
                count += 1
    return count


def judge_arms_fully_skipped(run: dict) -> list:
    """Arm names (subset of ["incumbent", "candidate"]) where every attempted judge slot was
    skipped and none was actually judged -- that arm's judge metrics have no data at all,
    not just degraded data, and a caller should say so loudly rather than let it look clean.
    Counts attempted slots, not items_total, so a run the cap stopped early still warns.
    """
    return [
        arm for arm in ("incumbent", "candidate")
        if judge_judged_count(run, arm) == 0 and judge_skipped_count(run, arm) > 0
    ]


def judge_judged_count(run: dict, arm: Optional[str] = None) -> int:
    """Count of (item, arm) judge slots where the judge was actually called (whether it
    then scored cleanly or errored) -- the complement of judge_skipped_count among slots
    that were attempted at all (present in the item's "judge" dict).
    """
    arms = (arm,) if arm else ("incumbent", "candidate")
    count = 0
    for item in run["items"]:
        judge = item.get("judge") or {}
        for a in arms:
            arm_result = judge.get(a)
            if arm_result and not arm_result.get("skipped"):
                count += 1
    return count


def first_judge_error(run: dict) -> Optional[str]:
    for item in run["items"]:
        judge = item.get("judge") or {}
        for arm in ("incumbent", "candidate"):
            arm_result = judge.get(arm)
            if arm_result and arm_result.get("error"):
                return arm_result["error"]
    return None


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
