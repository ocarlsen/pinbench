"""Run a suite through two model ids (incumbent, candidate) and write run.json.

Refuses to do: send a single call when the pre-run estimate exceeds the cap, or continue
past the cap mid-run; a call already in flight is never aborted, so a stop never loses a
paid output. Also refuses to let a suite's fixed judge model be the same id as either arm
(self-judging) unless the caller explicitly passes allow_self_judge=True.
"""

from __future__ import annotations

import datetime as _dt
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from . import cost as cost_mod
from . import judge as judge_mod
from . import providers
from . import suite as suite_mod


class PlanRefused(ValueError):
    """Unpriced id, unroutable id, missing API key, or a self-judging suite. No call was made."""


class RunCapExceeded(ValueError):
    """The pre-run estimate already exceeds the cap. No call was made."""

    def __init__(self, message, estimate):
        super().__init__(message)
        self.estimate = estimate


@dataclass
class PlanResult:
    incumbent_id: str
    candidate_id: str
    estimate: cost_mod.RunEstimate
    cap_usd: Optional[float]
    within_cap: Optional[bool]
    judge_id: Optional[str] = None


def _require_price_and_route(model_id, prices):
    if prices.rows.get(model_id) is None:
        raise PlanRefused(f"no price row for '{model_id}'")
    providers.route(model_id)  # raises NoAdapterError -> caller wraps as PlanRefused


def _check_self_judge(judge_id, incumbent_id, candidate_id, allow_self_judge):
    if judge_id is None or allow_self_judge:
        return
    if judge_id in (incumbent_id, candidate_id):
        raise PlanRefused(
            f"judge model '{judge_id}' is also an arm in this run; a model cannot judge itself. "
            "Pass --allow-self-judge to override."
        )


def plan(suite, incumbent_id, candidate_id, prices, cap_usd=None, limit=None, state_dir=None, allow_self_judge=False) -> PlanResult:
    judge_id = suite.judge.model if suite.judge else None
    try:
        _require_price_and_route(incumbent_id, prices)
        _require_price_and_route(candidate_id, prices)
        if judge_id is not None:
            _require_price_and_route(judge_id, prices)
    except providers.NoAdapterError as exc:
        raise PlanRefused(str(exc)) from exc
    _check_self_judge(judge_id, incumbent_id, candidate_id, allow_self_judge)

    estimate = cost_mod.estimate_run(suite, incumbent_id, candidate_id, prices, limit=limit, state_dir=state_dir, judge_id=judge_id)
    within_cap = None if cap_usd is None else estimate.total_usd <= cap_usd
    return PlanResult(incumbent_id=incumbent_id, candidate_id=candidate_id, estimate=estimate, cap_usd=cap_usd, within_cap=within_cap, judge_id=judge_id)


def _check_api_keys(*model_ids):
    for model_id in model_ids:
        if model_id is None:
            continue
        provider_name = providers.provider_for_id(model_id)
        env_key = providers.ENV_KEYS[provider_name]
        if not os.environ.get(env_key):
            raise PlanRefused(f"set {env_key}")


def _merged_params(price, suite):
    """One arm's request params: the model's own request_defaults, then the suite's params
    on top (suite wins on any key both set).

    `price.request_defaults` is per-model truth from prices.yaml -- e.g. how a given model
    turns extended thinking off, which is not the same syntax across model generations and
    pinbench never guesses it. A model with no request_defaults row contributes nothing, so
    a suite that sets nothing extra either sends nothing beyond the output ceiling. The
    output ceiling itself is passed to the adapter separately (each vendor names that key
    differently); it is recorded alongside this dict so run.json shows the full request
    shape actually sent.
    """
    merged = dict(price.request_defaults)
    merged.update(suite.params)
    return merged


def _recorded_params(suite, params):
    return {"max_output_tokens": suite.max_output_tokens, **params}


def _call_arm(model_id, suite, item, params, api_keys):
    provider_name = providers.provider_for_id(model_id)
    api_key = api_keys[provider_name]
    adapter = providers.route(model_id)
    return adapter.call(model_id, suite.task, item.input, suite.max_output_tokens, api_key, params=params)


@dataclass
class _ArmStats:
    attempted: int = 0
    succeeded: int = 0
    first_error: Optional[str] = None

    def record(self, reply):
        self.attempted += 1
        if reply.error is None:
            self.succeeded += 1
        elif self.first_error is None:
            self.first_error = reply.error


def _judge_ready_outputs(inc_reply, cand_reply) -> Optional[dict]:
    """{"incumbent": text, "candidate": text} when both arms produced real output, else None."""
    if inc_reply.output is None or cand_reply.output is None:
        return None
    return {"incumbent": inc_reply.output, "candidate": cand_reply.output}


def _execute_items(items, suite, incumbent_id, candidate_id, inc_price, cand_price, inc_estimates, cand_estimates, meter, api_keys, judge_ctx=None):
    """Run every item through both arms (then the judge, if judge_ctx is given) until the
    meter stops it. Never aborts a call in flight."""
    inc_params = _merged_params(inc_price, suite)
    cand_params = _merged_params(cand_price, suite)
    result_items = []
    spent_incumbent = 0.0
    spent_candidate = 0.0
    spent_judge = 0.0
    stopped_early = False
    inc_stats = _ArmStats()
    cand_stats = _ArmStats()

    for item in items:
        if not meter.check(inc_estimates[item.id]):
            stopped_early = True
            break
        inc_reply = _call_arm(incumbent_id, suite, item, inc_params, api_keys)
        inc_stats.record(inc_reply)
        inc_cost = meter.charge(cost_mod.actual_call_cost(inc_reply, inc_price), inc_estimates[item.id])
        spent_incumbent += inc_cost

        if not meter.check(cand_estimates[item.id]):
            stopped_early = True
            result_items.append(_item_record(item, inc_reply, inc_cost, candidate=None, candidate_cost=None))
            break
        cand_reply = _call_arm(candidate_id, suite, item, cand_params, api_keys)
        cand_stats.record(cand_reply)
        cand_cost = meter.charge(cost_mod.actual_call_cost(cand_reply, cand_price), cand_estimates[item.id])
        spent_candidate += cand_cost

        judge_record = None
        if judge_ctx is not None:
            outputs = _judge_ready_outputs(inc_reply, cand_reply)
            if outputs is not None:
                judge_record, judge_stopped = judge_mod.run_judge_stage(
                    meter, judge_ctx["id"], judge_ctx["price"], judge_ctx["api_key"], suite, item, outputs
                )
                spent_judge += sum(judge_record[arm]["cost_usd"] for arm in ("incumbent", "candidate") if arm in judge_record)
                if judge_stopped:
                    stopped_early = True

        result_items.append(_item_record(item, inc_reply, inc_cost, cand_reply, cand_cost, judge_record))
        if judge_ctx is not None and stopped_early:
            break

    arm_stats = {"incumbent": inc_stats, "candidate": cand_stats}
    return result_items, stopped_early, spent_incumbent, spent_candidate, spent_judge, inc_params, cand_params, arm_stats


def run(
    suite, incumbent_id, candidate_id, prices, cap_usd, limit=None, name=None, state_dir=".pinbench",
    suite_path=None, allow_self_judge=False, effective_config=None,
):
    """Run both arms over the suite. Raises PlanRefused or RunCapExceeded before any call.

    `effective_config` (optional) is the resolved config/CLI/suite/built-in settings dict
    the caller computed (see `config.py` and `cli.py`'s resolvers); it is recorded verbatim
    in run.json so `pinbench report` can reload this exact suite later (judge/policy
    fallbacks included) even if pinbench.yaml has since changed, and so the report header
    can name which config file, if any, this run used.
    """
    if cap_usd is None:
        raise PlanRefused("a cost cap is required: set --cap, the suite's cap_usd, or pinbench.yaml's cap_usd")

    plan_result = plan(suite, incumbent_id, candidate_id, prices, cap_usd=cap_usd, limit=limit, state_dir=state_dir, allow_self_judge=allow_self_judge)
    if not plan_result.within_cap:
        raise RunCapExceeded(
            f"estimate ${plan_result.estimate.total_usd:.4f} exceeds cap ${cap_usd:.4f}",
            plan_result.estimate,
        )
    _check_api_keys(incumbent_id, candidate_id, plan_result.judge_id)

    inc_price = prices.rows[incumbent_id]
    cand_price = prices.rows[candidate_id]
    inc_estimates = {e.item_id: e.cost_usd for e in plan_result.estimate.incumbent.items}
    cand_estimates = {e.item_id: e.cost_usd for e in plan_result.estimate.candidate.items}

    api_keys = {
        "anthropic": os.environ.get("ANTHROPIC_API_KEY"),
        "openai": os.environ.get("OPENAI_API_KEY"),
    }

    judge_ctx = None
    if plan_result.judge_id is not None:
        judge_provider = providers.provider_for_id(plan_result.judge_id)
        judge_ctx = {"id": plan_result.judge_id, "price": prices.rows[plan_result.judge_id], "api_key": api_keys[judge_provider]}

    meter = cost_mod.CostMeter(cap_usd=cap_usd)
    items = suite.items[:limit] if limit else suite.items
    run_name = name or f"{suite.name}-{_dt.datetime.now(_dt.timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
    started_at = _dt.datetime.now(_dt.timezone.utc).isoformat().replace("+00:00", "Z")

    result_items, stopped_early, spent_incumbent, spent_candidate, spent_judge, inc_params, cand_params, arm_stats = _execute_items(
        items, suite, incumbent_id, candidate_id, inc_price, cand_price, inc_estimates, cand_estimates, meter, api_keys, judge_ctx=judge_ctx
    )

    finished_at = _dt.datetime.now(_dt.timezone.utc).isoformat().replace("+00:00", "Z")
    record = _build_record(
        run_name, suite, suite_path, incumbent_id, candidate_id, started_at, finished_at, cap_usd,
        plan_result, spent_incumbent, spent_candidate, spent_judge, stopped_early, items, inc_params, cand_params, result_items, arm_stats,
    )
    record["effective_config"] = effective_config

    _write_run_json(state_dir, run_name, record)
    return record


def _build_record(run_name, suite, suite_path, incumbent_id, candidate_id, started_at, finished_at, cap_usd,
                   plan_result, spent_incumbent, spent_candidate, spent_judge, stopped_early, items, inc_params, cand_params, result_items, arm_stats):
    # An item "completed" only when BOTH arms returned real output (no error on either side);
    # an item where one arm errored is not completed, even though both arms were attempted.
    items_completed = sum(
        1
        for r in result_items
        if r["candidate"] is not None and r["incumbent"]["output"] is not None and r["candidate"]["output"] is not None
    )
    estimate_usd = {
        "incumbent": plan_result.estimate.incumbent.cost_usd,
        "candidate": plan_result.estimate.candidate.cost_usd,
        "total": plan_result.estimate.total_usd,
    }
    spent_usd = {"incumbent": spent_incumbent, "candidate": spent_candidate, "total": spent_incumbent + spent_candidate + spent_judge}
    if plan_result.judge_id is not None:
        estimate_usd["judge"] = plan_result.estimate.judge.cost_usd if plan_result.estimate.judge else 0.0
        spent_usd["judge"] = spent_judge
    return {
        "run_name": run_name,
        "suite": str(suite_path) if suite_path else suite.name,
        "suite_hash": suite_mod.freeze_hash(suite),
        "incumbent": incumbent_id,
        "candidate": candidate_id,
        "judge_model": plan_result.judge_id,
        "judge_prompt_hash": judge_mod.prompt_hash(suite.judge.rubric) if suite.judge else None,
        "started_at": started_at,
        "finished_at": finished_at,
        "cap_usd": cap_usd,
        "estimate_usd": estimate_usd,
        "spent_usd": spent_usd,
        "stopped_early": stopped_early,
        "items_completed": items_completed,
        "items_total": len(items),
        "items_attempted": {"incumbent": arm_stats["incumbent"].attempted, "candidate": arm_stats["candidate"].attempted},
        "items_succeeded": {"incumbent": arm_stats["incumbent"].succeeded, "candidate": arm_stats["candidate"].succeeded},
        "request_params": {"incumbent": _recorded_params(suite, inc_params), "candidate": _recorded_params(suite, cand_params)},
        "items": result_items,
    }


def _arm_record(reply, cost_usd):
    return {
        "output": reply.output,
        "input_tokens": reply.input_tokens,
        "output_tokens": reply.output_tokens,
        "cost_usd": cost_usd,
        "latency_ms": reply.latency_ms,
        "error": reply.error,
        "stop_reason": reply.stop_reason,
    }


def _item_record(item, incumbent, incumbent_cost, candidate, candidate_cost, judge_record=None):
    """`incumbent`/`candidate` are providers.Reply objects; candidate is None when the meter
    stopped first. `judge_record` (from judge.run_judge_stage) is None for a non-judge suite,
    or when the meter stopped before the judge stage ran for this item.
    """
    record = {
        "id": item.id,
        "input": item.input,
        "expect": item.expect,
        "rubric_points": [{"id": p.id, "description": p.description} for p in item.rubric_points],
        "incumbent": _arm_record(incumbent, incumbent_cost),
        "candidate": None if candidate is None else _arm_record(candidate, candidate_cost),
    }
    if judge_record is not None:
        record["judge"] = judge_record
    return record


def _write_run_json(state_dir, run_name, record):
    run_dir = Path(state_dir) / "runs" / run_name
    run_dir.mkdir(parents=True, exist_ok=True)
    target = run_dir / "run.json"
    tmp = run_dir / "run.json.tmp"
    tmp.write_text(json.dumps(record, indent=2))
    tmp.replace(target)


def load_run(state_dir, run_name) -> dict:
    path = Path(state_dir) / "runs" / run_name / "run.json"
    return json.loads(path.read_text())
