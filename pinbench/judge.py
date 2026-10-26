"""Blind LLM-judge scoring: one fixed judge model grades one arm's output at a time.

Refuses to do: show the judge a model id, the word "incumbent"/"candidate", or both arms'
outputs in one call -- each judge call sees the rubric, the rubric_points ids and
descriptions, the item input, and exactly ONE output, presented identically regardless of
which arm produced it. Refuses to guess at a malformed judge reply: a reply that is not
strict JSON, or whose "hit" ids are not a subset of this item's rubric_points, is a judge
error for that item, counted, never silently dropped or partially parsed. Refuses to let
two reports with different judge ids or different rubric text be compared as if they were
the same measurement.
"""

from __future__ import annotations

import hashlib
import json
import secrets
from dataclasses import dataclass
from typing import Optional

from . import cost as cost_mod
from . import providers
from . import scoring as scoring_mod

# Fixed across every call: the only things that vary per call are the rubric (suite-level,
# folded into the prompt hash) and the single output being graded. Never mentions "incumbent"
# or "candidate", or any model name -- the judge is told only that it is grading "a response".
JUDGE_SYSTEM_PROMPT = (
    "You are a strict, blind grader. You will be given a task, an input, a rubric, a list "
    "of rubric points (each with an id and a description), and exactly one response to that "
    "input. You do not know and must not guess which system produced the response.\n\n"
    "For each rubric point, decide whether the response satisfies it. Also judge whether the "
    "response raises any additional finding beyond the rubric points -- something a careful "
    "reviewer would flag that is not covered by any rubric point id -- and count those.\n\n"
    "Reply with ONLY strict JSON, no prose before or after, in exactly this shape:\n"
    '{"hit": ["<rubric point id>", ...], "extra": <integer count of additional findings>, '
    '"notes": "<one short sentence>"}\n'
    "\"hit\" must be a subset of the given rubric point ids. If the response satisfies none, "
    'use "hit": [].'
)


class JudgeParseError(ValueError):
    pass


class JudgeMismatch(ValueError):
    """Two runs used a different judge model or a different rubric; not comparable."""


@dataclass
class JudgeOutcome:
    hit: list
    extra: int
    notes: str
    error: Optional[str] = None


def prompt_hash(rubric: str) -> str:
    """sha256 of the fixed system prompt + this suite's rubric text.

    Recorded in run.json (and the report header) so a later run against the same rubric
    with the same judge id is provably the same measurement, and a run with either changed
    is provably not -- see `assert_comparable`.
    """
    payload = JUDGE_SYSTEM_PROMPT + "\n" + rubric
    return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


def pick_call_order() -> list:
    """["incumbent", "candidate"] in a random order, recorded per item so the order in
    which the two blind calls happened is known even though the judge itself never sees
    which side is which."""
    sides = ["incumbent", "candidate"]
    secrets.SystemRandom().shuffle(sides)
    return sides


def _rubric_points_block(rubric_points) -> str:
    return "\n".join(f"- {p.id}: {p.description}" for p in rubric_points)


def build_messages(item, suite, output_text: str) -> tuple:
    """(system, user) for grading ONE output of this item. Contains no model id and no
    "incumbent"/"candidate" anywhere in the scaffold text pinbench writes."""
    system = JUDGE_SYSTEM_PROMPT + "\n\nRubric:\n" + suite.judge.rubric
    user = (
        f"Task given to the response:\n{suite.task}\n\n"
        f"Input:\n{item.input}\n\n"
        "Rubric points to check for (id: description):\n"
        f"{_rubric_points_block(item.rubric_points)}\n\n"
        f"Response to grade:\n{output_text}\n\n"
        "Return the strict JSON described in your instructions, nothing else."
    )
    return system, user


def parse_response(raw_text: Optional[str], valid_ids) -> JudgeOutcome:
    """Strict parse of the judge's reply. Any violation raises JudgeParseError; callers
    turn that into a per-item judge error, never a guessed score."""
    if not raw_text:
        raise JudgeParseError("empty judge reply")
    try:
        data = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        raise JudgeParseError(f"not valid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise JudgeParseError(f"judge reply is not a JSON object: {raw_text[:200]!r}")

    hit = data.get("hit")
    if not isinstance(hit, list) or not all(isinstance(h, str) for h in hit):
        raise JudgeParseError(f"'hit' must be a list of strings, got {hit!r}")
    valid_ids = set(valid_ids)
    unknown = [h for h in hit if h not in valid_ids]
    if unknown:
        raise JudgeParseError(f"'hit' names unknown rubric point id(s): {unknown}")

    extra = data.get("extra")
    if isinstance(extra, bool) or not isinstance(extra, int) or extra < 0:
        raise JudgeParseError(f"'extra' must be a non-negative integer, got {extra!r}")

    notes = data.get("notes")
    if not isinstance(notes, str):
        raise JudgeParseError(f"'notes' must be a string, got {notes!r}")

    return JudgeOutcome(hit=sorted(set(hit)), extra=extra, notes=notes)


def judge_output(judge_id, item, suite, output_text, api_key, params=None):
    """Call the fixed judge model on exactly one output. Returns (JudgeOutcome, Reply).

    A transport/HTTP error on the call itself is a judge error (reply.error), never a
    malformed-JSON error -- the two are kept distinct in JudgeOutcome.error's text so a
    report can tell "the judge broke" from "the judge answered badly".
    """
    system, user = build_messages(item, suite, output_text)
    adapter = providers.route(judge_id)
    reply = adapter.call(judge_id, system, user, suite.judge.max_output_tokens, api_key, params=params)
    if reply.error is not None:
        return JudgeOutcome(hit=[], extra=0, notes="", error=reply.error), reply
    if reply.stop_reason in scoring_mod.TRUNCATION_STOP_REASONS:
        # A cut-off reply is never parsed, even if the fragment happens to be valid JSON:
        # the judge did not finish, so whatever it wrote is not its answer.
        err = f"judge output truncated at max_output_tokens={suite.judge.max_output_tokens} (stop_reason={reply.stop_reason!r})"
        return JudgeOutcome(hit=[], extra=0, notes="", error=err), reply
    valid_ids = [p.id for p in item.rubric_points]
    try:
        outcome = parse_response(reply.output, valid_ids)
    except JudgeParseError as exc:
        return JudgeOutcome(hit=[], extra=0, notes="", error=f"malformed judge JSON: {exc}"), reply
    return outcome, reply


def _outcome_record(outcome: JudgeOutcome, cost_usd: Optional[float]) -> dict:
    return {"hit": outcome.hit, "extra": outcome.extra, "notes": outcome.notes, "error": outcome.error, "cost_usd": cost_usd}


def run_judge_stage(meter, judge_id, judge_price, api_key, suite, item, outputs: dict) -> tuple:
    """Judge both arms' outputs for one item, blind and in a random call order.

    `outputs` is {"incumbent": text, "candidate": text}; both must be present (the caller
    only invokes this once both arms produced real output). Returns (record, stopped_early):
    `record` has "call_order" plus whichever of "incumbent"/"candidate" were judged before
    the cap stopped it -- a partial record is still returned, never discarded, same as a
    partial run.
    """
    order = pick_call_order()
    record = {"call_order": order}
    for arm in order:
        est = cost_mod.estimate_judge_call_cost(suite, item, judge_price)
        if not meter.check(est):
            return record, True
        # The judge model's own request_defaults (prices.yaml) travel with it, same as an
        # arm's do: a judge that needs a model-specific thinking/effort shape gets it.
        outcome, reply = judge_output(judge_id, item, suite, outputs[arm], api_key, params=judge_price.request_defaults)
        cost_usd = meter.charge(cost_mod.actual_call_cost(reply, judge_price), est)
        record[arm] = _outcome_record(outcome, cost_usd)
    return record, False


def assert_comparable(run_a: dict, run_b: dict) -> None:
    """Refuse to treat two runs as the same measurement unless they used the same judge
    model AND the same rubric (judge_prompt_hash covers the rubric text). Different judge
    ids or different rubrics are different instruments; their recall/extra numbers are not
    on the same scale and must never be diffed against each other.
    """
    a_model, b_model = run_a.get("judge_model"), run_b.get("judge_model")
    a_hash, b_hash = run_a.get("judge_prompt_hash"), run_b.get("judge_prompt_hash")
    if a_model is None or b_model is None:
        raise JudgeMismatch("one or both runs have no judge_model recorded; not judge-scored runs")
    if a_model != b_model:
        raise JudgeMismatch(f"different judge models: {a_model!r} vs {b_model!r}")
    if a_hash != b_hash:
        raise JudgeMismatch(f"different judge prompt hash (rubric changed): {a_hash!r} vs {b_hash!r}")
