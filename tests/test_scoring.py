import pytest

from pinbench import scoring

NORM = ["strip", "casefold", "strip_punct"]


def test_normalize_steps():
    assert scoring.normalize("  Paris!  ", NORM) == "paris"
    assert scoring.normalize("CANBERRA.", NORM) == "canberra"


def test_normalize_unknown_step_raises():
    with pytest.raises(ValueError):
        scoring.normalize("x", ["not-a-step"])


def test_label_match_exact():
    assert scoring.label_match("  Paris.", "Paris", "exact", NORM)
    assert not scoring.label_match("London", "Paris", "exact", NORM)
    assert not scoring.label_match(None, "Paris", "exact", NORM)


def test_label_match_contains():
    assert scoring.label_match("The capital is Paris, obviously", "Paris", "contains", NORM)
    assert not scoring.label_match("The capital is London", "Paris", "contains", NORM)


def test_label_match_regex():
    assert scoring.label_match("Paris, France", r"^Paris", "regex", NORM)
    assert not scoring.label_match("not Paris", r"^Paris", "regex", NORM)


def _run(results):
    """results: list of (expect, incumbent_output, incumbent_error, candidate_output, candidate_error)."""
    items = []
    for i, (expect, inc_out, inc_err, cand_out, cand_err) in enumerate(results):
        items.append(
            {
                "id": f"q{i}",
                "expect": expect,
                "incumbent": {"output": inc_out, "error": inc_err, "cost_usd": 0.01, "latency_ms": 100},
                "candidate": {"output": cand_out, "error": cand_err, "cost_usd": 0.02, "latency_ms": 200},
            }
        )
    return {"items": items}


def test_label_agreement_counts_errors_as_miss():
    run = _run(
        [
            ("Paris", "Paris", None, "Paris", None),
            ("Canberra", "Canberra", None, None, "boom"),
        ]
    )
    cand = scoring.label_agreement(run, "candidate", "exact", NORM)
    assert cand.matched == 1
    assert cand.total == 2
    assert cand.pct == 50.0


def test_error_count_and_first_error():
    run = _run(
        [
            ("Paris", "Paris", None, None, "first error"),
            ("Canberra", "Canberra", None, None, "second error"),
        ]
    )
    assert scoring.error_count(run, "candidate") == 2
    assert scoring.first_error(run, "candidate") == "first error"
    assert scoring.error_count(run, "incumbent") == 0


def test_cost_per_item():
    run = _run([("Paris", "Paris", None, "Paris", None), ("Canberra", "Canberra", None, "Canberra", None)])
    assert scoring.cost_per_item(run, "incumbent") == 0.01
    assert scoring.cost_per_item(run, "candidate") == 0.02


def test_latency_stats_excludes_errors():
    run = _run([("Paris", "Paris", None, "Paris", None), ("Canberra", "Canberra", None, None, "boom")])
    stats = scoring.latency_stats(run, "candidate")
    assert stats["n"] == 1
    assert stats["mean_ms"] == 200


def test_unblind_votes_tally():
    votes = {"q0": {"vote": "A"}, "q1": {"vote": "B"}, "q2": {"vote": "tie"}}
    mapping = {
        "q0": {"A": "candidate", "B": "incumbent"},
        "q1": {"A": "incumbent", "B": "candidate"},
        "q2": {"A": "incumbent", "B": "candidate"},
    }
    tally = scoring.unblind_votes(votes, mapping)
    assert tally.candidate_wins == 2
    assert tally.incumbent_wins == 0
    assert tally.ties == 1
    assert tally.n == 3


def test_unblind_votes_unknown_item_id_refused():
    with pytest.raises(ValueError):
        scoring.unblind_votes({"ghost": {"vote": "A"}}, {"q0": {"A": "candidate", "B": "incumbent"}})


def test_unblind_votes_unknown_vote_value_refused():
    with pytest.raises(ValueError):
        scoring.unblind_votes({"q0": {"vote": "C"}}, {"q0": {"A": "candidate", "B": "incumbent"}})


def _judge_run(judge_items):
    """judge_items: list of (judge_dict_for_candidate_arm_or_None_meaning_skipped, points)."""
    items = []
    for i, (cand_judge, points) in enumerate(judge_items):
        items.append(
            {
                "id": f"r{i}",
                "rubric_points": [{"id": p, "description": p} for p in points],
                "judge": {"call_order": ["incumbent", "candidate"], "candidate": cand_judge},
            }
        )
    return {"items": items}


def test_judge_recall_scores_a_skipped_item_as_zero_hits_over_its_points():
    run = _judge_run(
        [
            ({"hit": ["p1"], "extra": 0, "notes": "", "error": None, "skipped": None}, ("p1", "p2")),
            ({"hit": [], "extra": 0, "notes": "", "error": None, "skipped": "skipped: arm output truncated"}, ("p1", "p2")),
        ]
    )
    result = scoring.judge_recall(run, "candidate")
    # Same rule as a judge error: the skipped item's 2 points stay in the denominator with
    # zero hits. Dropping them (1/2 instead of 1/4) would let a tolerated truncation lift
    # this arm's recall by shrinking its denominator.
    assert result.hits == 1
    assert result.points == 4


def test_judge_extra_mean_excludes_skipped_items():
    run = _judge_run(
        [
            ({"hit": [], "extra": 4, "notes": "", "error": None, "skipped": None}, ("p1",)),
            ({"hit": [], "extra": 0, "notes": "", "error": None, "skipped": "skipped: arm error"}, ("p1",)),
        ]
    )
    # If the skipped item's extra=0 were counted, the mean would be (4+0)/2 = 2.0 instead.
    assert scoring.judge_extra_mean(run, "candidate") == 4.0


def test_judge_error_count_excludes_skipped():
    run = _judge_run(
        [
            ({"hit": [], "extra": 0, "notes": "", "error": None, "skipped": "skipped: arm output truncated"}, ("p1",)),
        ]
    )
    assert scoring.judge_error_count(run) == 0


def test_judge_skipped_and_judged_counts():
    run = _judge_run(
        [
            ({"hit": ["p1"], "extra": 0, "notes": "", "error": None, "skipped": None}, ("p1",)),
            ({"hit": [], "extra": 0, "notes": "", "error": None, "skipped": "skipped: arm output truncated"}, ("p1",)),
            ({"hit": [], "extra": 0, "notes": "", "error": "boom", "skipped": None}, ("p1",)),
        ]
    )
    assert scoring.judge_skipped_count(run) == 1
    assert scoring.judge_skipped_count(run, "candidate") == 1
    assert scoring.judge_skipped_count(run, "incumbent") == 0
    assert scoring.judge_judged_count(run) == 2  # the hit and the error, not the skip
    assert scoring.judge_judged_count(run, "candidate") == 2


def test_judge_arms_fully_skipped_counts_attempted_slots_not_items_total():
    # A run the cap stopped early judged fewer items than items_total; an arm whose every
    # ATTEMPTED slot was skipped still has no judge data and must still be named.
    run = _judge_run(
        [
            ({"hit": [], "extra": 0, "notes": "", "error": None, "skipped": "skipped: arm output truncated"}, ("p1",)),
        ]
    )
    run["items_total"] = 3
    run["items_completed"] = 1
    assert scoring.judge_arms_fully_skipped(run) == ["candidate"]
