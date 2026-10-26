"""The decision rule: RECOMMEND / HOLD / INCOMPLETE, with every check's detail line.

Refuses to do: hard-code a number in the printed rule; every number is read from the
suite's `policy`, so changing policy changes what the report says the rule is. Also refuses
to call a run complete when either arm has more truncated outputs than
`policy.max_truncated` (default 0) -- a comparison built on cut-off answers is INCOMPLETE,
same bucket as an arm error. For a judge-scored suite (scoring.labels == "judge"), any judge
error makes the run INCOMPLETE the same way an arm error does, and the labels check is
replaced by two judge checks (recall, extra findings) instead of exact/contains/regex match.
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
    if suite.scoring.labels in ("none", "judge"):
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


def _check_judge_errors(run: dict, suite) -> Optional[Check]:
    """INCOMPLETE-causing, same bucket as an arm error: a malformed judge reply or a judge
    call that itself failed means this item was never actually graded, not that the
    candidate looked bad.
    """
    if suite.scoring.labels != "judge":
        return None
    n = scoring_mod.judge_error_count(run)
    if n == 0:
        return Check("judge_errors", True, "0 judge errors")
    return Check("judge_errors", False, f"{n} judge error(s); first: {scoring_mod.first_judge_error(run)}")


def _check_judge_recall(run: dict, suite) -> Optional[Check]:
    if suite.scoring.labels != "judge":
        return None
    cand = scoring_mod.judge_recall(run, "candidate")
    inc = scoring_mod.judge_recall(run, "incumbent")
    tol = suite.policy.recall_tolerance_points
    passed = cand.pct >= inc.pct - tol
    detail = (
        f"candidate recall {cand.pct:.1f}% ({cand.hits}/{cand.points}) vs incumbent "
        f"{inc.pct:.1f}% ({inc.hits}/{inc.points}); tolerance {tol} points; {'PASS' if passed else 'FAIL'}"
    )
    return Check("judge_recall", passed, detail)


def _check_judge_extra(run: dict, suite) -> Optional[Check]:
    """HOLD-causing: the candidate's mean "extra" (spurious finding) count may not exceed
    the incumbent's by more than policy.extra_tolerance per item -- a judge's extra-findings
    count is a precision proxy, so this is the half of the judge check that recall alone
    cannot see (a model that hits every rubric point by also flagging everything in sight
    is not an improvement).
    """
    if suite.scoring.labels != "judge":
        return None
    cand_extra = scoring_mod.judge_extra_mean(run, "candidate")
    inc_extra = scoring_mod.judge_extra_mean(run, "incumbent")
    tol = suite.policy.extra_tolerance
    if cand_extra is None or inc_extra is None:
        return Check("judge_extra", False, "missing judge extra-findings data")
    passed = cand_extra <= inc_extra + tol
    detail = (
        f"candidate extra {cand_extra:.2f}/item vs incumbent {inc_extra:.2f}/item; "
        f"tolerance {tol}/item; {'PASS' if passed else 'FAIL'}"
    )
    return Check("judge_extra", passed, detail)


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


def _check_cost(run: dict, suite, strict_win: bool) -> Check:
    """`strict_win` is pre-combined by the caller: (label strict win AND vote strict win)
    for a labeled/voted suite, or a judge strict win (recall strictly better AND extra no
    worse) for a judge suite -- whichever pair of independent signals this suite has agree
    the candidate is strictly better, not just non-inferior.
    """
    cand_cost = scoring_mod.cost_per_item(run, "candidate")
    inc_cost = scoring_mod.cost_per_item(run, "incumbent")
    tol = suite.policy.cost_tolerance
    if cand_cost is None or inc_cost is None or inc_cost == 0:
        return Check("cost", False, "missing cost data")
    ratio_ok = cand_cost <= inc_cost * (1 + tol)
    passed = ratio_ok or strict_win
    detail = (
        f"candidate ${cand_cost:.6f}/item vs incumbent ${inc_cost:.6f}/item "
        f"(ratio {cand_cost / inc_cost:.2f}, allowed {1 + tol:.2f}); {'PASS' if passed else 'FAIL'}"
    )
    return Check("cost", passed, detail)


def _label_strict_win(run, suite) -> bool:
    if suite.scoring.labels in ("none", "judge"):
        return False
    cand = scoring_mod.label_agreement(run, "candidate", suite.scoring.labels, suite.scoring.normalize)
    inc = scoring_mod.label_agreement(run, "incumbent", suite.scoring.labels, suite.scoring.normalize)
    return cand.pct > inc.pct + suite.policy.label_tolerance_points


def _judge_strict_win(run, suite) -> bool:
    """Recall strictly better (beyond tolerance) counts as a judge "strict win" for the
    cost-tolerance override, same role _label_strict_win plays for labeled suites. Extra
    findings are checked for PASS, not for a strict win -- fewer extra findings is a tie-
    breaker, not itself grounds to waive the cost tolerance.
    """
    if suite.scoring.labels != "judge":
        return False
    cand = scoring_mod.judge_recall(run, "candidate")
    inc = scoring_mod.judge_recall(run, "incumbent")
    return cand.pct > inc.pct + suite.policy.recall_tolerance_points


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

    judge_errors_check = _check_judge_errors(run, suite)
    if judge_errors_check is not None:
        checks.append(judge_errors_check)
        if not judge_errors_check.passed:
            return Verdict("INCOMPLETE", checks, _rule_text(suite, n))

    truncation_check = _check_truncation(run, suite)
    checks.append(truncation_check)
    if not truncation_check.passed:
        return Verdict("INCOMPLETE", checks, _rule_text(suite, n))

    votes_present = _check_votes_present(run, suite, votes)
    checks.append(votes_present)
    if not votes_present.passed:
        return Verdict("INCOMPLETE", checks, _rule_text(suite, n))

    hold_checks = _hold_checks(run, suite, votes, mapping)
    checks.extend(hold_checks)
    verdict_str = "RECOMMEND" if all(c.passed for c in hold_checks) else "HOLD"
    return Verdict(verdict_str, checks, _rule_text(suite, n))


def _hold_checks(run: dict, suite, votes, mapping) -> list:
    """Every non-INCOMPLETE check: labels or judge recall/extra, votes, then cost (which may
    waive its own tolerance on a strict win from whichever pair of independent signals this
    suite has). A judge suite with `scoring.votes: none` has only one signal (recall+extra),
    so that signal alone can waive; a judge suite that also collects votes has two, and both
    must agree -- a judge strict win on its own is not enough when a vote signal exists."""
    hold_checks = []
    label_check = _check_labels(run, suite)
    if label_check is not None:
        hold_checks.append(label_check)

    judge_recall_check = _check_judge_recall(run, suite)
    judge_extra_check = _check_judge_extra(run, suite)
    for check in (judge_recall_check, judge_extra_check):
        if check is not None:
            hold_checks.append(check)

    vote_result = _check_votes(run, votes, mapping)
    tally = vote_result[1] if vote_result is not None else None
    if vote_result is not None:
        hold_checks.append(vote_result[0])

    label_strict_win = bool(label_check and _label_strict_win(run, suite))
    vote_strict_win = bool(tally and tally.candidate_wins > tally.incumbent_wins)
    judge_strict_win = bool(
        judge_recall_check and judge_extra_check and judge_recall_check.passed and judge_extra_check.passed
        and _judge_strict_win(run, suite)
    )
    # A judge win alone waives the cost tolerance only when votes:none means the judge is
    # the suite's only signal; once votes are also collected, the waiver needs both halves
    # of that pair (judge_strict_win AND vote_strict_win), matching the docstring above.
    judge_waiver = judge_strict_win if suite.scoring.votes == "none" else (judge_strict_win and vote_strict_win)
    strict_win = (label_strict_win and vote_strict_win) or judge_waiver
    hold_checks.append(_check_cost(run, suite, strict_win))
    return hold_checks


def _rule_text(suite, n: int) -> str:
    """The rule in prose. Thresholds come from policy; the resolution sentence uses this run's n."""
    cost_pct = int(round((1 + suite.policy.cost_tolerance) * 100))
    min_items = suite.policy.min_items
    max_truncated = suite.policy.max_truncated
    points = round(100.0 / n, 1) if n else 100.0
    if suite.scoring.labels == "judge":
        scoring_clause = (
            f"non-inferior on judge recall (within {suite.policy.recall_tolerance_points} points) "
            f"and no worse than {suite.policy.extra_tolerance} extra findings per item than the "
            "incumbent (judge errors make the run INCOMPLETE, never guessed), non-losing on blind "
            "votes (ties excluded)"
        )
    else:
        scoring_clause = (
            f"non-inferior on labels (within {suite.policy.label_tolerance_points} points), "
            "non-losing on blind votes (ties excluded)"
        )
    return (
        f"a candidate is recommended when it is {scoring_clause}, and costs no more than "
        f"{cost_pct}% of the incumbent per item, over at least {min_items} completed items with "
        f"no errors and no more than {max_truncated} truncated output(s) per arm. "
        f"{n} items can show a large regression, not a small one "
        f"(1/n = {points} points); the downside of a wrong RECOMMEND is bounded by `pinbench bump` "
        "being one reverted commit."
    )
