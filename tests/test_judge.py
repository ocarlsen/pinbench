import json

import pytest

from pinbench import cost, judge, prices, providers, suite


class _FakeJudgeAdapter:
    """A stand-in for providers.<name>.call(), keyed by call index rather than by exact
    prompt text (the judge prompt is long and generated, unlike the short fixed item inputs
    conftest's FakeAdapter keys on)."""

    def __init__(self, replies):
        self.replies = list(replies)
        self.calls = []

    def call(self, model_id, system, user_input, max_tokens, api_key, params=None, timeout=60):
        self.calls.append({"model_id": model_id, "system": system, "user_input": user_input, "max_tokens": max_tokens})
        return self.replies.pop(0)


def _reply(output=None, error=None, status_code=None, input_tokens=10, output_tokens=10):
    return providers.Reply(
        output=output, input_tokens=input_tokens, output_tokens=output_tokens, latency_ms=5, error=error, status_code=status_code
    )


def _judge_suite():
    class _S:
        pass

    item = suite.Item(
        id="r01",
        input="some artifact",
        rubric_points=[
            suite.RubricPoint(id="bug-a", description="describes bug a"),
            suite.RubricPoint(id="bug-b", description="describes bug b"),
        ],
    )
    s = _S()
    s.task = "Review this for defects."
    s.max_output_tokens = 100
    s.judge = suite.JudgeConfig(model="judge-model", rubric="Grade strictly against the rubric points.")
    return s, item


def _price(input_rate=1.0, output_rate=2.0):
    return prices.PriceRow(model="judge-model", input=input_rate, output=output_rate, source="https://x")


def test_build_messages_never_names_a_model_or_side(monkeypatch):
    s, item = _judge_suite()
    system, user = judge.build_messages(item, s, "the arm's actual output text")
    blob = (system + "\n" + user).lower()
    for banned in ("incumbent", "candidate", "claude-", "gpt-", "openai", "anthropic"):
        assert banned not in blob, banned
    assert "bug-a" in user and "bug-b" in user
    assert "the arm's actual output text" in user


def test_pick_call_order_is_a_permutation_of_both_arms():
    seen = set()
    for _ in range(20):
        order = judge.pick_call_order()
        assert sorted(order) == ["candidate", "incumbent"]
        seen.add(tuple(order))
    # SystemRandom over 20 draws should show both orders at least once (not a hard guarantee,
    # but failing this would mean shuffle degenerated to a constant order).
    assert len(seen) == 2


def test_parse_response_accepts_valid_json():
    raw = json.dumps({"hit": ["bug-a"], "extra": 1, "notes": "found bug a"})
    outcome = judge.parse_response(raw, valid_ids=["bug-a", "bug-b"])
    assert outcome.hit == ["bug-a"]
    assert outcome.extra == 1
    assert outcome.notes == "found bug a"


def test_parse_response_rejects_not_json():
    with pytest.raises(judge.JudgeParseError):
        judge.parse_response("not json at all", valid_ids=["bug-a"])


def test_parse_response_rejects_unknown_hit_id():
    raw = json.dumps({"hit": ["not-a-real-point"], "extra": 0, "notes": ""})
    with pytest.raises(judge.JudgeParseError, match="unknown rubric point"):
        judge.parse_response(raw, valid_ids=["bug-a"])


def test_parse_response_rejects_bad_extra_type():
    raw = json.dumps({"hit": [], "extra": "two", "notes": ""})
    with pytest.raises(judge.JudgeParseError):
        judge.parse_response(raw, valid_ids=["bug-a"])


def test_parse_response_rejects_negative_extra():
    raw = json.dumps({"hit": [], "extra": -1, "notes": ""})
    with pytest.raises(judge.JudgeParseError):
        judge.parse_response(raw, valid_ids=["bug-a"])


def test_parse_response_rejects_bool_as_extra():
    # bool is a subclass of int in Python; a judge sending true/false for "extra" is malformed.
    raw = json.dumps({"hit": [], "extra": True, "notes": ""})
    with pytest.raises(judge.JudgeParseError):
        judge.parse_response(raw, valid_ids=["bug-a"])


def test_parse_response_rejects_missing_notes():
    raw = json.dumps({"hit": [], "extra": 0})
    with pytest.raises(judge.JudgeParseError):
        judge.parse_response(raw, valid_ids=["bug-a"])


def test_judge_output_call_error_is_a_judge_error(monkeypatch):
    s, item = _judge_suite()
    adapter = _FakeJudgeAdapter([_reply(error="HTTP 500: boom", status_code=500)])
    monkeypatch.setattr(providers, "route", lambda model_id: adapter)

    outcome, reply = judge.judge_output("judge-model", item, s, "output text", "key")
    assert outcome.error == "HTTP 500: boom"
    assert outcome.hit == []
    assert reply.error == "HTTP 500: boom"


def test_judge_output_malformed_json_is_a_judge_error(monkeypatch):
    s, item = _judge_suite()
    adapter = _FakeJudgeAdapter([_reply(output="not json")])
    monkeypatch.setattr(providers, "route", lambda model_id: adapter)

    outcome, reply = judge.judge_output("judge-model", item, s, "output text", "key")
    assert outcome.error is not None
    assert "malformed judge JSON" in outcome.error


def test_judge_output_valid_reply_parses(monkeypatch):
    s, item = _judge_suite()
    raw = json.dumps({"hit": ["bug-a", "bug-b"], "extra": 2, "notes": "both found, plus extras"})
    adapter = _FakeJudgeAdapter([_reply(output=raw)])
    monkeypatch.setattr(providers, "route", lambda model_id: adapter)

    outcome, reply = judge.judge_output("judge-model", item, s, "output text", "key")
    assert outcome.error is None
    assert outcome.hit == ["bug-a", "bug-b"]
    assert outcome.extra == 2


def test_run_judge_stage_judges_both_arms_and_charges_meter(monkeypatch):
    s, item = _judge_suite()
    raw_hit_a = json.dumps({"hit": ["bug-a"], "extra": 0, "notes": ""})
    raw_hit_both = json.dumps({"hit": ["bug-a", "bug-b"], "extra": 1, "notes": ""})
    adapter = _FakeJudgeAdapter([_reply(output=raw_hit_a), _reply(output=raw_hit_both)])
    monkeypatch.setattr(providers, "route", lambda model_id: adapter)

    meter = cost.CostMeter(cap_usd=1.0)
    price = _price()
    outputs = {"incumbent": "incumbent output text", "candidate": "candidate output text"}
    record, stopped_early = judge.run_judge_stage(meter, "judge-model", price, "key", s, item, outputs)

    assert stopped_early is False
    assert set(record["call_order"]) == {"incumbent", "candidate"}
    assert len(record["incumbent"]["hit"]) + len(record["candidate"]["hit"]) == 3  # one call hit 1, the other hit 2
    assert meter.spent_usd > 0
    assert len(adapter.calls) == 2


def test_run_judge_stage_stops_early_when_cap_hit(monkeypatch):
    s, item = _judge_suite()
    adapter = _FakeJudgeAdapter([_reply(output=json.dumps({"hit": [], "extra": 0, "notes": ""}))])
    monkeypatch.setattr(providers, "route", lambda model_id: adapter)

    price = _price(input_rate=1_000_000.0, output_rate=1_000_000.0)  # first call alone exceeds the cap
    meter = cost.CostMeter(cap_usd=0.0001)
    outputs = {"incumbent": "incumbent output text", "candidate": "candidate output text"}
    record, stopped_early = judge.run_judge_stage(meter, "judge-model", price, "key", s, item, outputs)

    assert stopped_early is True
    judged_arms = [arm for arm in ("incumbent", "candidate") if arm in record]
    assert len(judged_arms) == 0  # the very first call already exceeds the cap
    assert len(adapter.calls) == 0


def test_prompt_hash_stable_and_sensitive_to_rubric_text():
    h1 = judge.prompt_hash("rubric text A")
    h2 = judge.prompt_hash("rubric text A")
    h3 = judge.prompt_hash("rubric text B")
    assert h1 == h2
    assert h1 != h3
    assert h1.startswith("sha256:")


def test_assert_comparable_same_judge_and_hash_passes():
    run_a = {"judge_model": "judge-model", "judge_prompt_hash": "sha256:same"}
    run_b = {"judge_model": "judge-model", "judge_prompt_hash": "sha256:same"}
    judge.assert_comparable(run_a, run_b)  # no raise


def test_assert_comparable_different_judge_model_refuses():
    run_a = {"judge_model": "judge-model-a", "judge_prompt_hash": "sha256:same"}
    run_b = {"judge_model": "judge-model-b", "judge_prompt_hash": "sha256:same"}
    with pytest.raises(judge.JudgeMismatch, match="different judge models"):
        judge.assert_comparable(run_a, run_b)


def test_assert_comparable_different_prompt_hash_refuses():
    run_a = {"judge_model": "judge-model", "judge_prompt_hash": "sha256:aaa"}
    run_b = {"judge_model": "judge-model", "judge_prompt_hash": "sha256:bbb"}
    with pytest.raises(judge.JudgeMismatch, match="different judge prompt hash"):
        judge.assert_comparable(run_a, run_b)


def test_assert_comparable_non_judge_run_refuses():
    run_a = {"judge_model": None, "judge_prompt_hash": None}
    run_b = {"judge_model": "judge-model", "judge_prompt_hash": "sha256:bbb"}
    with pytest.raises(judge.JudgeMismatch):
        judge.assert_comparable(run_a, run_b)
