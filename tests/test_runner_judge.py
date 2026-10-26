"""Judge-mode integration: runner.run() driving both arms AND the judge stage through the
fake provider, all against the same cap."""

import json

from pinbench import judge, prices, providers, runner, suite
from pinbench import scoring as scoring_mod

INC_ID = "claude-inc"
CAND_ID = "claude-cand"


def _judge_suite(tmp_path, n=2, max_out=16, judge_max_out=32, judge_model="claude-judge"):
    items = "\n".join(
        f'  - id: q{i:02d}\n    input: "Q{i}?"\n    rubric_points:\n      - id: p{i}\n        description: "point {i}"'
        for i in range(n)
    )
    path = tmp_path / "judge_suite.yaml"
    path.write_text(
        f"""\
version: 1
name: review
task: "Review."
max_output_tokens: {max_out}
scoring:
  labels: judge
policy:
  min_items: 1
judge:
  model: {judge_model}
  max_output_tokens: {judge_max_out}
  rubric: "grade strictly"
items:
{items}
"""
    )
    return suite.load(path)


def _prices(tmp_path, judge_model="claude-judge"):
    path = tmp_path / "prices.yaml"
    path.write_text(
        f"""\
version: 1
checked: 2026-10-26
models:
  {INC_ID}: {{input: 1.0, output: 2.0, source: "https://x"}}
  {CAND_ID}: {{input: 0.5, output: 1.0, source: "https://y"}}
  {judge_model}: {{input: 2.0, output: 4.0, source: "https://z"}}
"""
    )
    return prices.load(path)


def _register_judge_replies(fake_adapter, s, judge_model):
    """Register a valid-JSON "hit everything" reply for each item's exact, predictable
    judge prompt (fake_adapter's default arm reply echoes the item input back, so both arms
    produce the same output text and thus the same judge prompt for a given item)."""
    for item in s.items:
        echoed_output = f"echo:{item.input}"
        _system, user = judge.build_messages(item, s, echoed_output)
        raw = json.dumps({"hit": [p.id for p in item.rubric_points], "extra": 0, "notes": "ok"})
        fake_adapter.set_reply(judge_model, user, providers.Reply(output=raw, input_tokens=5, output_tokens=5, latency_ms=1, error=None))


def test_run_judges_both_arms_and_records_results(tmp_path, fake_adapter):
    s = _judge_suite(tmp_path, n=2)
    p = _prices(tmp_path)
    _register_judge_replies(fake_adapter, s, "claude-judge")

    record = runner.run(s, INC_ID, CAND_ID, p, cap_usd=1.0, state_dir=tmp_path / "state", suite_path="x", name="r1")

    assert record["judge_model"] == "claude-judge"
    assert record["judge_prompt_hash"] == judge.prompt_hash(s.judge.rubric)
    for item in record["items"]:
        assert "judge" in item
        assert item["judge"]["incumbent"]["error"] is None
        assert item["judge"]["candidate"]["error"] is None
        assert sorted(item["judge"]["incumbent"]["hit"]) == sorted(p["id"] for p in item["rubric_points"])
    assert record["spent_usd"]["judge"] > 0
    assert record["estimate_usd"]["judge"] > 0
    # 2 items x 2 arms (the suite calls) + 2 items x 2 arms (the judge calls) = 8
    assert fake_adapter.call_count == 8


def test_plan_includes_judge_estimate_and_refuses_unpriced_judge(tmp_path, fake_adapter):
    s = _judge_suite(tmp_path, n=2, judge_model="claude-ghost-judge")
    p = _prices(tmp_path, judge_model="claude-some-other-model")  # the suite's judge never gets a price row
    try:
        runner.plan(s, INC_ID, CAND_ID, p, cap_usd=1.0, state_dir=tmp_path / "state")
        assert False, "expected PlanRefused"
    except runner.PlanRefused:
        pass


def test_self_judge_refused_by_default(tmp_path, fake_adapter):
    s = _judge_suite(tmp_path, n=1, judge_model=INC_ID)  # judge == incumbent arm
    p = _prices(tmp_path, judge_model=INC_ID)
    try:
        runner.plan(s, INC_ID, CAND_ID, p, cap_usd=1.0, state_dir=tmp_path / "state")
        assert False, "expected PlanRefused"
    except runner.PlanRefused as exc:
        assert "judge model" in str(exc)
    assert fake_adapter.call_count == 0


def test_self_judge_allowed_with_flag(tmp_path, fake_adapter):
    s = _judge_suite(tmp_path, n=1, judge_model=INC_ID)
    p = _prices(tmp_path, judge_model=INC_ID)
    result = runner.plan(s, INC_ID, CAND_ID, p, cap_usd=1.0, state_dir=tmp_path / "state", allow_self_judge=True)
    assert result.judge_id == INC_ID


def test_run_stops_early_mid_judge_stage_without_losing_arm_output(tmp_path, fake_adapter):
    # The judge's pre-run ESTIMATE always assumes judge.max_output_tokens as the ceiling
    # (same conservatism as an arm's estimate); if the judge's ACTUAL reported usage runs
    # far past that ceiling (same failure mode as a real model that reports more usage than
    # its own ceiling implied), the meter can blow past the cap mid-judge-stage even though
    # the pre-run total estimate fit comfortably inside it.
    s = _judge_suite(tmp_path, n=3, max_out=16, judge_max_out=16)
    p = _prices(tmp_path)
    for item in s.items:
        echoed_output = f"echo:{item.input}"
        _system, user = judge.build_messages(item, s, echoed_output)
        raw = json.dumps({"hit": [], "extra": 0, "notes": "ok"})
        fake_adapter.set_reply(
            "claude-judge", user, providers.Reply(output=raw, input_tokens=2_000_000, output_tokens=2_000_000, latency_ms=1, error=None)
        )

    plan = runner.plan(s, INC_ID, CAND_ID, p, cap_usd=None, state_dir=tmp_path / "state")
    cap = plan.estimate.total_usd * 1.2  # fits the (ceiling-based) pre-run estimate comfortably

    record = runner.run(s, INC_ID, CAND_ID, p, cap_usd=cap, state_dir=tmp_path / "state", suite_path="x", name="r2")

    assert record["stopped_early"] is True
    assert record["items_completed"] < record["items_total"]
    # Every arm call that was made is still in the record (paid outputs are never lost),
    # even for the item where the judge stage is what tripped the cap.
    for item in record["items"]:
        assert item["incumbent"]["output"] is not None
        assert item["candidate"]["output"] is not None


def test_truncated_arm_output_never_reaches_the_judge(tmp_path, fake_adapter):
    # Regression for the live-run cost waste: a truncated arm output used to still be sent
    # to the judge (~$0.10/call) even though scoring.is_truncated already makes the run's
    # verdict INCOMPLETE over it. Call-count is the load-bearing assertion.
    s = _judge_suite(tmp_path, n=2)
    p = _prices(tmp_path)
    # q00's incumbent reply is truncated (stop_reason=max_tokens); its own echoed text is
    # never used as a judge prompt, so no judge reply needs registering for it.
    fake_adapter.set_reply(
        INC_ID, "Q0?",
        providers.Reply(output="cut off", input_tokens=5, output_tokens=16, latency_ms=1, error=None, stop_reason="max_tokens"),
    )
    _register_judge_replies(fake_adapter, s, "claude-judge")

    record = runner.run(s, INC_ID, CAND_ID, p, cap_usd=1.0, state_dir=tmp_path / "state", suite_path="x", name="r3")

    q0 = next(item for item in record["items"] if item["id"] == "q00")
    q1 = next(item for item in record["items"] if item["id"] == "q01")
    assert q0["incumbent"]["stop_reason"] == "max_tokens"
    assert q0["judge"]["incumbent"]["skipped"] == "skipped: arm output truncated"
    assert q0["judge"]["incumbent"]["cost_usd"] == 0.0
    assert q0["judge"]["incumbent"]["hit"] == []
    # The candidate's clean output on q0, and both arms on q1, were still judged normally.
    assert q0["judge"]["candidate"]["skipped"] is None
    assert q1["judge"]["incumbent"]["skipped"] is None
    assert q1["judge"]["candidate"]["skipped"] is None

    # Judge calls made: q0-candidate, q1-incumbent, q1-candidate = 3 (never q0-incumbent).
    judge_calls = [c for c in fake_adapter.calls if c[0] == "claude-judge"]
    assert len(judge_calls) == 3

    assert scoring_mod.judge_skipped_count(record) == 1
    assert scoring_mod.judge_judged_count(record) == 3
    # judge spend reflects only the 3 real calls, never a 4th for the skipped one.
    assert record["spent_usd"]["judge"] > 0


def test_errored_arm_output_never_reaches_the_judge(tmp_path, fake_adapter):
    s = _judge_suite(tmp_path, n=1)
    p = _prices(tmp_path)
    fake_adapter.set_reply(
        INC_ID, "Q0?",
        providers.Reply(output=None, input_tokens=None, output_tokens=None, latency_ms=1, error="HTTP 500: boom", status_code=500),
    )
    _register_judge_replies(fake_adapter, s, "claude-judge")

    record = runner.run(s, INC_ID, CAND_ID, p, cap_usd=1.0, state_dir=tmp_path / "state", suite_path="x", name="r4")

    item = record["items"][0]
    assert item["judge"]["incumbent"]["skipped"] == "skipped: arm error"
    assert item["judge"]["incumbent"]["cost_usd"] == 0.0
    judge_calls = [c for c in fake_adapter.calls if c[0] == "claude-judge"]
    assert len(judge_calls) == 1  # only the candidate's clean output was judged


def test_report_shows_judged_and_skipped_counts_and_verdict_incomplete_via_truncation(tmp_path, fake_adapter):
    from pinbench import report as report_mod
    from pinbench import verdict as verdict_mod

    s = _judge_suite(tmp_path, n=2)
    p = _prices(tmp_path)
    fake_adapter.set_reply(
        INC_ID, "Q0?",
        providers.Reply(output="cut off", input_tokens=5, output_tokens=16, latency_ms=1, error=None, stop_reason="max_tokens"),
    )
    _register_judge_replies(fake_adapter, s, "claude-judge")

    record = runner.run(s, INC_ID, CAND_ID, p, cap_usd=1.0, state_dir=tmp_path / "state", suite_path="x", name="r5")
    result = verdict_mod.decide(record, s)

    # Verdict goes INCOMPLETE via the truncation check, not judge_errors.
    assert result.verdict == "INCOMPLETE"
    truncation_check = [c for c in result.checks if c.name == "truncation"][0]
    assert not truncation_check.passed
    judge_errors_checks = [c for c in result.checks if c.name == "judge_errors"]
    assert judge_errors_checks and judge_errors_checks[0].passed

    text = report_mod.render_text(record, s, result)
    assert "3 judged, 1 skipped (truncated/error)" in text
    md = report_mod.render_markdown(record, s, result)
    assert "3 judged, 1 skipped (truncated/error)" in md


def test_judge_metrics_computed_only_over_judged_items_in_a_mixed_run(tmp_path, fake_adapter):
    # Mixed case: one item truncated on the incumbent (its judge call skipped, excluded),
    # one item clean on both arms (judged normally) -- judge_recall/judge_extra_mean must
    # reflect only the judged item, not treat the skipped one as a miss.
    s = _judge_suite(tmp_path, n=2)
    p = _prices(tmp_path)
    fake_adapter.set_reply(
        INC_ID, "Q0?",
        providers.Reply(output="cut off", input_tokens=5, output_tokens=16, latency_ms=1, error=None, stop_reason="max_tokens"),
    )
    _register_judge_replies(fake_adapter, s, "claude-judge")

    record = runner.run(s, INC_ID, CAND_ID, p, cap_usd=1.0, state_dir=tmp_path / "state", suite_path="x", name="r6")

    inc_recall = scoring_mod.judge_recall(record, "incumbent")
    # q00's incumbent judge call was skipped (excluded entirely): only q01 contributes,
    # 1 rubric point, 1 hit (the fake judge replies hit every declared rubric point).
    assert inc_recall.points == 1
    assert inc_recall.hits == 1

    cand_recall = scoring_mod.judge_recall(record, "candidate")
    # The candidate was judged on both items: 2 rubric points, 2 hits.
    assert cand_recall.points == 2
    assert cand_recall.hits == 2
