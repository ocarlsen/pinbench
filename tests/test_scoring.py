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
