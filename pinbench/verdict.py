"""The decision rule: RECOMMEND / HOLD / INCOMPLETE, with every check's detail line.

Refuses to do: hard-code a number in the printed rule; every number is read from the
suite's `policy`, so changing policy changes what the report says the rule is. Also refuses
to call a run complete when either arm has more truncated outputs than
`policy.max_truncated` (default 0) -- a comparison built on cut-off answers is INCOMPLETE,
same bucket as an arm error.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from . import scoring as scoring_mod


@dataclass
class Check:
    name: str
    passed: bool
    detail: str


@dataclass
class Verdict:
    verdict: str
    checks: list = field(default_factory=list)
    rule_text: str = ""


def _check_completeness(run: dict, suite) -> Optional[Check]:
    n = run["items_completed"]
    if run.get("stopped_early"):
        return Check("completeness", False, f"stopped_early=true; only {n} of {run['items_total']} items completed")
    if n < suite.policy.min_items:
        points = round(100.0 / n, 1) if n else 100.0
        return Check(
            "completeness",
            False,
            f"n={n} < {suite.policy.min_items}; a suite this small cannot show a regression smaller than 1/n = {points} points",
        )
    return Check("completeness", True, f"n={n} >= {suite.policy.min_items}")


def _votes_required(suite) -> Optional[str]:
    """Why votes are required for this suite, or None when they are not."""
    if suite.scoring.votes == "required":
        return "scoring.votes is 'required'"
    if suite.scoring.labels == "none" and suite.policy.require_votes_when_unlabeled:
        return "the suite is unlabeled and policy.require_votes_when_unlabeled is true"
    return None


def _check_truncation(run: dict, suite) -> Check:
    """INCOMPLETE-causing: more than policy.max_truncated truncated outputs on EITHER arm
    means at least one answer was cut off by max_output_tokens, not a real comparison.
    """
    inc_n = scoring_mod.truncated_count(run, "incumbent")
    cand_n = scoring_mod.truncated_count(run, "candidate")
    allowed = suite.policy.max_truncated
    passed = inc_n <= allowed and cand_n <= allowed
    detail = (
        f"incumbent {inc_n} truncated, candidate {cand_n} truncated; "
        f"allowed {allowed} per arm; {'PASS' if passed else 'FAIL'}"
    )
    return Check("truncation", passed, detail)


def _check_votes_present(run: dict, suite, votes) -> Check:
    n = run["items_completed"]
    why = _votes_required(suite)
    if why is None:
        return Check("votes_present", True, "votes not required for this suite")
    if votes is None:
        return Check("votes_present", False, f"{why} but no votes.json was given")
    covered = sum(1 for item in run["items"] if item.get("candidate") is not None and item["id"] in votes)
    if covered < n:
        return Check("votes_present", False, f"votes cover {covered} of {n} completed items; required votes must cover all")
    return Check("votes_present", True, f"votes cover all {n} completed items")


def _check_no_arm_errors(run: dict) -> Check:
    """INCOMPLETE-causing: either arm having ANY error means the run cannot be judged, not
    just that the candidate looked bad. (A prior version checked only the candidate, so an
    incumbent that errored on every item -- a broken request shape, say -- went unnoticed.)
    """
    inc_count = scoring_mod.error_count(run, "incumbent")
    cand_count = scoring_mod.error_count(run, "candidate")
    if not inc_count and not cand_count:
        return Check("errors", True, "incumbent and candidate both had 0 errors")
    parts = []
    if inc_count:
        parts.append(f"incumbent had {inc_count} error(s); first: {scoring_mod.first_error(run, 'incumbent')}")
    if cand_count:
        parts.append(f"candidate had {cand_count} error(s); first: {scoring_mod.first_error(run, 'candidate')}")
    return Check("errors", False, "; ".join(parts))


def _check_labels(run: dict, suite) -> Optional[Check]:
    if suite.scoring.labels == "none":
        return None
    cand = scoring_mod.label_agreement(run, "candidate", suite.scoring.labels, suite.scoring.normalize)
    inc = scoring_mod.label_agreement(run, "incumbent", suite.scoring.labels, suite.scoring.normalize)
    tol = suite.policy.label_tolerance_points
    passed = cand.pct >= inc.pct - tol
    detail = (
        f"candidate {cand.pct:.1f}% ({cand.matched}/{cand.total}) vs incumbent {inc.pct:.1f}% "
        f"({inc.matched}/{inc.total}); tolerance {tol} points; {'PASS' if passed else 'FAIL'}"
    )
    return Check("labels", passed, detail)


def _check_votes(run: dict, votes, mapping) -> Optional[tuple]:
    """(Check, VoteTally) when votes were given, else None."""
    if votes is None or mapping is None:
        return None
    tally = scoring_mod.unblind_votes(votes, mapping)
    passed = tally.candidate_wins >= tally.incumbent_wins
    detail = (
        f"candidate {tally.candidate_wins}, incumbent {tally.incumbent_wins}, "
        f"ties {tally.ties} of {tally.n}; {'PASS' if passed else 'FAIL'} (non-losing)"
    )
    return Check("votes", passed, detail), tally


def _check_cost(run: dict, suite, label_strict_win: bool, vote_strict_win: bool) -> Check:
    cand_cost = scoring_mod.cost_per_item(run, "candidate")
    inc_cost = scoring_mod.cost_per_item(run, "incumbent")
    tol = suite.policy.cost_tolerance
    if cand_cost is None or inc_cost is None or inc_cost == 0:
        return Check("cost", False, "missing cost data")
    ratio_ok = cand_cost <= inc_cost * (1 + tol)
    passed = ratio_ok or (label_strict_win and vote_strict_win)
    detail = (
        f"candidate ${cand_cost:.6f}/item vs incumbent ${inc_cost:.6f}/item "
        f"(ratio {cand_cost / inc_cost:.2f}, allowed {1 + tol:.2f}); {'PASS' if passed else 'FAIL'}"
    )
    return Check("cost", passed, detail)


def _label_strict_win(run, suite) -> bool:
    if suite.scoring.labels == "none":
        return False
    cand = scoring_mod.label_agreement(run, "candidate", suite.scoring.labels, suite.scoring.normalize)
    inc = scoring_mod.label_agreement(run, "incumbent", suite.scoring.labels, suite.scoring.normalize)
    return cand.pct > inc.pct + suite.policy.label_tolerance_points


def decide(run: dict, suite, votes: Optional[dict] = None, mapping: Optional[dict] = None) -> Verdict:
    checks = []

    n = run["items_completed"]

    completeness = _check_completeness(run, suite)
    checks.append(completeness)
    if not completeness.passed:
        return Verdict("INCOMPLETE", checks, _rule_text(suite, n))

    errors_check = _check_no_arm_errors(run)
    checks.append(errors_check)
    if not errors_check.passed:
        return Verdict("INCOMPLETE", checks, _rule_text(suite, n))

    truncation_check = _check_truncation(run, suite)
    checks.append(truncation_check)
    if not truncation_check.passed:
        return Verdict("INCOMPLETE", checks, _rule_text(suite, n))

    votes_present = _check_votes_present(run, suite, votes)
    checks.append(votes_present)
    if not votes_present.passed:
        return Verdict("INCOMPLETE", checks, _rule_text(suite, n))

    hold_checks = []
    label_check = _check_labels(run, suite)
    if label_check is not None:
        hold_checks.append(label_check)

    vote_result = _check_votes(run, votes, mapping)
    tally = None
    if vote_result is not None:
        vote_check, tally = vote_result
        hold_checks.append(vote_check)

    label_strict_win = bool(label_check and _label_strict_win(run, suite))
    vote_strict_win = bool(tally and tally.candidate_wins > tally.incumbent_wins)
    cost_check = _check_cost(run, suite, label_strict_win, vote_strict_win)
    hold_checks.append(cost_check)

    checks.extend(hold_checks)
    verdict_str = "RECOMMEND" if all(c.passed for c in hold_checks) else "HOLD"
    return Verdict(verdict_str, checks, _rule_text(suite, n))


def _rule_text(suite, n: int) -> str:
    """The rule in prose. Thresholds come from policy; the resolution sentence uses this run's n."""
    tol = suite.policy.label_tolerance_points
    cost_pct = int(round((1 + suite.policy.cost_tolerance) * 100))
    min_items = suite.policy.min_items
    max_truncated = suite.policy.max_truncated
    points = round(100.0 / n, 1) if n else 100.0
    return (
        f"a candidate is recommended when it is non-inferior on labels (within {tol} points), "
        "non-losing on blind votes (ties excluded), and costs no more than "
        f"{cost_pct}% of the incumbent per item, over at least {min_items} completed items with "
        f"no errors and no more than {max_truncated} truncated output(s) per arm. "
        f"{n} items can show a large regression, not a small one "
        f"(1/n = {points} points); the downside of a wrong RECOMMEND is bounded by `pinbench bump` "
        "being one reverted commit."
    )
