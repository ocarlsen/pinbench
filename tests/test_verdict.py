from pinbench import suite as suite_mod
from pinbench import verdict

NORM = ["strip", "casefold", "strip_punct"]


class FakeSuite:
    """Defaults to an unlabeled suite that does NOT require votes, so single checks can be isolated."""

    def __init__(
        self, labels="none", votes="optional", min_items=10, label_tol=0.0, cost_tol=0.25, normalize=None,
        require_votes_when_unlabeled=False, recall_tol=0.0, extra_tol=0.5,
    ):
        self.scoring = suite_mod.Scoring(labels=labels, normalize=normalize or NORM, votes=votes)
        self.policy = suite_mod.Policy(
            min_items=min_items,
            label_tolerance_points=label_tol,
            cost_tolerance=cost_tol,
            require_votes_when_unlabeled=require_votes_when_unlabeled,
            recall_tolerance_points=recall_tol,
            extra_tolerance=extra_tol,
        )


def _items(n, inc_cost=0.01, cand_cost=0.01, inc_err=None, cand_err=None, matched_labels=None, expect="X"):
    """matched_labels: list of bool, one per item, controls candidate output matching `expect`."""
    items = []
    for i in range(n):
        matched = matched_labels[i] if matched_labels else True
        items.append(
            {
                "id": f"q{i}",
                "expect": expect,
                "incumbent": {"output": expect, "error": inc_err, "cost_usd": inc_cost, "latency_ms": 100},
                "candidate": {
                    "output": expect if matched else "WRONG",
                    "error": cand_err(i) if callable(cand_err) else cand_err,
                    "cost_usd": cand_cost,
                    "latency_ms": 100,
                },
            }
        )
    return items


def _run(n, **kwargs):
    stopped_early = kwargs.pop("stopped_early", False)
    items = _items(n, **kwargs)
    return {
        "items_completed": n,
        "items_total": n,
        "stopped_early": stopped_early,
        "items": items,
    }


def test_check1_incomplete_on_stopped_early():
    run = _run(12, stopped_early=True)
    s = FakeSuite(min_items=10)
    result = verdict.decide(run, s)
    assert result.verdict == "INCOMPLETE"
    assert result.checks[0].name == "completeness"
    assert not result.checks[0].passed


def test_check1_incomplete_on_too_few_items():
    run = _run(5)
    s = FakeSuite(min_items=10)
    result = verdict.decide(run, s)
    assert result.verdict == "INCOMPLETE"
    assert "n=5 < 10" in result.checks[0].detail


def test_check2_incomplete_votes_required_missing():
    run = _run(12)
    s = FakeSuite(votes="required", min_items=10)
    result = verdict.decide(run, s, votes=None, mapping=None)
    assert result.verdict == "INCOMPLETE"
    assert result.checks[-1].name == "votes_present"


def test_check2_unlabeled_suite_needs_votes_by_default_policy():
    # labels: none + votes: optional + no votes.json would otherwise RECOMMEND on cost alone.
    run = _run(12)
    s = FakeSuite(labels="none", votes="optional", min_items=10, require_votes_when_unlabeled=True)
    result = verdict.decide(run, s, votes=None, mapping=None)
    assert result.verdict == "INCOMPLETE"
    assert result.checks[-1].name == "votes_present"
    assert "require_votes_when_unlabeled" in result.checks[-1].detail


def test_check2_unlabeled_suite_with_votes_proceeds():
    run = _run(12)
    s = FakeSuite(labels="none", votes="optional", min_items=10, require_votes_when_unlabeled=True)
    votes = {f"q{i}": {"vote": "tie", "note": ""} for i in range(12)}
    mapping = {f"q{i}": {"A": "candidate", "B": "incumbent"} for i in range(12)}
    result = verdict.decide(run, s, votes=votes, mapping=mapping)
    assert result.verdict == "RECOMMEND"


def test_check3_incomplete_on_candidate_errors():
    run = _run(12, cand_err="boom")
    s = FakeSuite(min_items=10)
    result = verdict.decide(run, s)
    assert result.verdict == "INCOMPLETE"
    errors_check = [c for c in result.checks if c.name == "errors"][0]
    assert not errors_check.passed
    assert "boom" in errors_check.detail


def test_check3_incomplete_on_incumbent_errors():
    # A prior version only looked at the candidate; an incumbent that errors on every call
    # (e.g. a request-shape bug) must also make the verdict INCOMPLETE, never silently pass.
    run = _run(12, inc_err="kaboom")
    s = FakeSuite(min_items=10)
    result = verdict.decide(run, s)
    assert result.verdict == "INCOMPLETE"
    errors_check = [c for c in result.checks if c.name == "errors"][0]
    assert not errors_check.passed
    assert "kaboom" in errors_check.detail


def test_check4_essay_labels_regression_holds():
    # 99 items: incumbent matches all 88 of its own "correct" slots is irrelevant here --
    # we directly control matched_labels per arm via separate incumbent/candidate outputs.
    n = 99
    matched = [True] * 83 + [False] * (n - 83)  # candidate matches 83/99 = 83.8%
    items = []
    for i in range(n):
        inc_matched = i < 88  # incumbent matches 88/99 = 88.9%
        cand_matched = matched[i]
        items.append(
            {
                "id": f"q{i}",
                "expect": "X",
                "incumbent": {"output": "X" if inc_matched else "WRONG", "error": None, "cost_usd": 0.01, "latency_ms": 100},
                "candidate": {"output": "X" if cand_matched else "WRONG", "error": None, "cost_usd": 0.01, "latency_ms": 100},
            }
        )
    run = {"items_completed": n, "items_total": n, "stopped_early": False, "items": items}
    s = FakeSuite(labels="exact", min_items=10)
    result = verdict.decide(run, s)
    label_check = [c for c in result.checks if c.name == "labels"][0]
    assert "83.8%" in label_check.detail
    assert "88.9%" in label_check.detail
    assert not label_check.passed
    assert result.verdict == "HOLD"


def test_check5_votes_half_price_recommend():
    n = 13
    run = _run(n, inc_cost=0.02, cand_cost=0.01)  # candidate half price
    votes = {}
    mapping = {}
    # 5 candidate wins, 4 incumbent wins, 4 ties
    outcomes = ["A"] * 5 + ["B"] * 4 + ["tie"] * 4
    for i, outcome in enumerate(outcomes):
        votes[f"q{i}"] = {"vote": outcome, "note": ""}
        mapping[f"q{i}"] = {"A": "candidate", "B": "incumbent"}
    s = FakeSuite(labels="none", votes="optional", min_items=10)
    result = verdict.decide(run, s, votes=votes, mapping=mapping)
    votes_check = [c for c in result.checks if c.name == "votes"][0]
    assert votes_check.passed
    assert "candidate 5, incumbent 4, ties 4 of 13" in votes_check.detail
    assert result.verdict == "RECOMMEND"


def test_check5_votes_losing_holds():
    n = 13
    run = _run(n, inc_cost=0.01, cand_cost=0.01)
    votes = {}
    mapping = {}
    outcomes = ["A"] * 4 + ["B"] * 6 + ["tie"] * 3
    for i, outcome in enumerate(outcomes):
        votes[f"q{i}"] = {"vote": outcome, "note": ""}
        mapping[f"q{i}"] = {"A": "candidate", "B": "incumbent"}
    s = FakeSuite(labels="none", votes="optional", min_items=10)
    result = verdict.decide(run, s, votes=votes, mapping=mapping)
    votes_check = [c for c in result.checks if c.name == "votes"][0]
    assert not votes_check.passed
    assert "candidate 4, incumbent 6, ties 3 of 13" in votes_check.detail
    assert result.verdict == "HOLD"


def test_check6_cost_holds_when_too_expensive_and_no_strict_wins():
    run = _run(12, inc_cost=0.01, cand_cost=0.02)  # 2x incumbent, no label/vote wins supplied
    s = FakeSuite(labels="none", min_items=10, cost_tol=0.25)
    result = verdict.decide(run, s)
    cost_check = [c for c in result.checks if c.name == "cost"][0]
    assert not cost_check.passed
    assert result.verdict == "HOLD"


def test_check6_cost_passes_within_tolerance():
    run = _run(12, inc_cost=0.01, cand_cost=0.0124)  # within 25%
    s = FakeSuite(labels="none", min_items=10, cost_tol=0.25)
    result = verdict.decide(run, s)
    cost_check = [c for c in result.checks if c.name == "cost"][0]
    assert cost_check.passed


def test_check7_recommend_when_all_hold_checks_pass():
    run = _run(12, inc_cost=0.01, cand_cost=0.01)
    s = FakeSuite(labels="none", min_items=10)
    result = verdict.decide(run, s)
    assert result.verdict == "RECOMMEND"


def test_rule_text_reflects_policy_numbers():
    s = FakeSuite(min_items=20, label_tol=1.5, cost_tol=0.5)
    run = _run(20, inc_cost=0.01, cand_cost=0.01)
    result = verdict.decide(run, s)
    assert "1.5 points" in result.rule_text
    assert "150%" in result.rule_text
    assert "20 completed items" in result.rule_text


def test_rule_text_resolution_uses_this_runs_n_not_min_items():
    s = FakeSuite(min_items=10)
    run = _run(13, inc_cost=0.01, cand_cost=0.01)
    result = verdict.decide(run, s)
    assert "at least 10 completed items" in result.rule_text
    assert "13 items can show a large regression" in result.rule_text
    assert "1/n = 7.7 points" in result.rule_text


def _judge_item(item_id, inc_hit, cand_hit, points=("p1", "p2"), inc_extra=0, cand_extra=0, inc_err=None, cand_err=None, inc_cost=0.01, cand_cost=0.01):
    return {
        "id": item_id,
        "rubric_points": [{"id": p, "description": p} for p in points],
        "incumbent": {"output": "out", "error": None, "cost_usd": inc_cost, "latency_ms": 100},
        "candidate": {"output": "out", "error": None, "cost_usd": cand_cost, "latency_ms": 100},
        "judge": {
            "call_order": ["incumbent", "candidate"],
            "incumbent": {"hit": inc_hit, "extra": inc_extra, "notes": "", "error": inc_err},
            "candidate": {"hit": cand_hit, "extra": cand_extra, "notes": "", "error": cand_err},
        },
    }


def _judge_run(items, stopped_early=False):
    return {"items_completed": len(items), "items_total": len(items), "stopped_early": stopped_early, "items": items}


def test_judge_errors_incomplete():
    items = [_judge_item(f"r{i}", ["p1"], ["p1"]) for i in range(6)]
    items[0]["judge"]["candidate"]["error"] = "malformed judge JSON: boom"
    run = _judge_run(items)
    s = FakeSuite(labels="judge", min_items=6)
    result = verdict.decide(run, s)
    assert result.verdict == "INCOMPLETE"
    judge_errors_check = [c for c in result.checks if c.name == "judge_errors"][0]
    assert not judge_errors_check.passed
    assert "boom" in judge_errors_check.detail


def test_judge_recall_pass_and_recommend():
    items = [_judge_item(f"r{i}", ["p1", "p2"], ["p1", "p2"]) for i in range(6)]
    run = _judge_run(items)
    s = FakeSuite(labels="judge", min_items=6)
    result = verdict.decide(run, s)
    recall_check = [c for c in result.checks if c.name == "judge_recall"][0]
    assert recall_check.passed
    assert "100.0%" in recall_check.detail
    assert result.verdict == "RECOMMEND"


def test_judge_recall_fail_holds():
    items = [_judge_item(f"r{i}", ["p1", "p2"], ["p1"]) for i in range(6)]  # candidate misses p2 every time
    run = _judge_run(items)
    s = FakeSuite(labels="judge", min_items=6)
    result = verdict.decide(run, s)
    recall_check = [c for c in result.checks if c.name == "judge_recall"][0]
    assert not recall_check.passed
    assert result.verdict == "HOLD"


def test_judge_extra_fail_holds_even_with_full_recall():
    items = [
        _judge_item(f"r{i}", ["p1", "p2"], ["p1", "p2"], inc_extra=0, cand_extra=3) for i in range(6)
    ]
    run = _judge_run(items)
    s = FakeSuite(labels="judge", min_items=6, extra_tol=0.5)
    result = verdict.decide(run, s)
    extra_check = [c for c in result.checks if c.name == "judge_extra"][0]
    assert not extra_check.passed
    assert result.verdict == "HOLD"


def test_judge_extra_within_tolerance_passes():
    items = [
        _judge_item(f"r{i}", ["p1", "p2"], ["p1", "p2"], inc_extra=1, cand_extra=1) for i in range(6)
    ]
    run = _judge_run(items)
    s = FakeSuite(labels="judge", min_items=6, extra_tol=0.5)
    result = verdict.decide(run, s)
    extra_check = [c for c in result.checks if c.name == "judge_extra"][0]
    assert extra_check.passed


def test_judge_mode_skips_label_check():
    items = [_judge_item(f"r{i}", ["p1", "p2"], ["p1", "p2"]) for i in range(6)]
    run = _judge_run(items)
    s = FakeSuite(labels="judge", min_items=6)
    result = verdict.decide(run, s)
    assert [c for c in result.checks if c.name == "labels"] == []


def test_judge_strict_win_waives_cost_tolerance_when_suite_has_no_votes():
    # candidate strictly beats incumbent on recall and has no more extra findings, at 2x cost.
    # scoring.votes is "none": the judge signal is the only pair this suite has, so it alone
    # waives the cost tolerance.
    items = [
        _judge_item(f"r{i}", ["p1"], ["p1", "p2"], inc_cost=0.01, cand_cost=0.02) for i in range(6)
    ]
    run = _judge_run(items)
    s = FakeSuite(labels="judge", votes="none", min_items=6, cost_tol=0.25)
    result = verdict.decide(run, s)
    cost_check = [c for c in result.checks if c.name == "cost"][0]
    assert cost_check.passed
    assert result.verdict == "RECOMMEND"


def test_judge_strict_win_alone_does_not_waive_when_suite_collects_votes():
    # Same judge strict win as above, but this suite also collects votes (votes: optional)
    # and the actual vote tally is a tie, not a candidate strict win -- the judge signal alone
    # must not waive the cost tolerance here; the docstring's "whichever pair of independent
    # signals this suite has" means BOTH must agree when a vote signal exists.
    items = [
        _judge_item(f"r{i}", ["p1"], ["p1", "p2"], inc_cost=0.01, cand_cost=0.02) for i in range(6)
    ]
    run = _judge_run(items)
    votes = {}
    mapping = {}
    outcomes = ["A"] * 3 + ["B"] * 3
    for i, outcome in enumerate(outcomes):
        votes[f"r{i}"] = {"vote": outcome, "note": ""}
        mapping[f"r{i}"] = {"A": "candidate", "B": "incumbent"}
    s = FakeSuite(labels="judge", votes="optional", min_items=6, cost_tol=0.25)
    result = verdict.decide(run, s, votes=votes, mapping=mapping)
    cost_check = [c for c in result.checks if c.name == "cost"][0]
    assert not cost_check.passed
    assert result.verdict == "HOLD"


def test_judge_strict_win_plus_vote_strict_win_waives_cost_tolerance():
    # Same judge strict win, suite collects votes, and the candidate also strictly wins the
    # vote tally -- both halves of the pair agree, so the waiver applies.
    items = [
        _judge_item(f"r{i}", ["p1"], ["p1", "p2"], inc_cost=0.01, cand_cost=0.02) for i in range(6)
    ]
    run = _judge_run(items)
    votes = {}
    mapping = {}
    outcomes = ["A"] * 4 + ["B"] * 2
    for i, outcome in enumerate(outcomes):
        votes[f"r{i}"] = {"vote": outcome, "note": ""}
        mapping[f"r{i}"] = {"A": "candidate", "B": "incumbent"}
    s = FakeSuite(labels="judge", votes="optional", min_items=6, cost_tol=0.25)
    result = verdict.decide(run, s, votes=votes, mapping=mapping)
    cost_check = [c for c in result.checks if c.name == "cost"][0]
    assert cost_check.passed
    assert result.verdict == "RECOMMEND"


def test_truncated_arm_makes_run_incomplete_via_truncation_not_judge_errors():
    # When one item's incumbent output was truncated, the judge is never called on it
    # (judge.py skip_reason) so its judge record carries "skipped", not "error". The run
    # must still go INCOMPLETE -- but via the truncation check, not judge_errors.
    items = [_judge_item(f"r{i}", ["p1", "p2"], ["p1", "p2"]) for i in range(6)]
    items[0]["incumbent"]["stop_reason"] = "max_tokens"
    items[0]["judge"]["incumbent"] = {"hit": [], "extra": 0, "notes": "", "error": None, "skipped": "skipped: arm output truncated"}
    run = _judge_run(items)
    s = FakeSuite(labels="judge", min_items=6)
    result = verdict.decide(run, s)
    assert result.verdict == "INCOMPLETE"
    truncation_check = [c for c in result.checks if c.name == "truncation"][0]
    assert not truncation_check.passed
    judge_errors_checks = [c for c in result.checks if c.name == "judge_errors"]
    # judge_errors is checked (and PASSES) before the truncation check trips the run
    # INCOMPLETE; the skip must never register there as an error.
    assert judge_errors_checks and judge_errors_checks[0].passed
