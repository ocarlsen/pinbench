"""Run a suite through two model ids (incumbent, candidate) and write run.json.

Refuses to do: send a single call when the pre-run estimate exceeds the cap, or continue
past the cap mid-run; a call already in flight is never aborted, so a stop never loses a
paid output.
"""

from __future__ import annotations

import datetime as _dt
import json
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from . import cost as cost_mod
from . import providers
from . import suite as suite_mod


class PlanRefused(ValueError):
    """Unpriced id, unroutable id, or missing API key. No call was made."""


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


def _require_price_and_route(model_id, prices):
    if prices.rows.get(model_id) is None:
        raise PlanRefused(f"no price row for '{model_id}'")
    providers.route(model_id)  # raises NoAdapterError -> caller wraps as PlanRefused


def plan(suite, incumbent_id, candidate_id, prices, cap_usd=None, limit=None, state_dir=None) -> PlanResult:
    try:
        _require_price_and_route(incumbent_id, prices)
        _require_price_and_route(candidate_id, prices)
    except providers.NoAdapterError as exc:
        raise PlanRefused(str(exc)) from exc

    estimate = cost_mod.estimate_run(suite, incumbent_id, candidate_id, prices, limit=limit, state_dir=state_dir)
    within_cap = None if cap_usd is None else estimate.total_usd <= cap_usd
    return PlanResult(incumbent_id=incumbent_id, candidate_id=candidate_id, estimate=estimate, cap_usd=cap_usd, within_cap=within_cap)


def _check_api_keys(incumbent_id, candidate_id):
    for model_id in (incumbent_id, candidate_id):
        provider_name = providers.provider_for_id(model_id)
        env_key = providers.ENV_KEYS[provider_name]
        if not os.environ.get(env_key):
            raise PlanRefused(f"set {env_key}")


def _default_params(suite):
    return {"max_tokens": suite.max_output_tokens, "temperature": 0}


def _actual_cost(reply, price):
    if reply.input_tokens is None or reply.output_tokens is None:
        return None
    return reply.input_tokens * price.input / 1e6 + reply.output_tokens * price.output / 1e6


def _call_arm(model_id, suite, item, params, api_keys):
    provider_name = providers.provider_for_id(model_id)
    api_key = api_keys[provider_name]
    adapter = providers.route(model_id)
    return adapter.call(model_id, suite.task, item.input, suite.max_output_tokens, api_key, params=params)


def run(suite, incumbent_id, candidate_id, prices, cap_usd, limit=None, name=None, state_dir=".pinbench", suite_path=None):
    """Run both arms over the suite. Raises PlanRefused or RunCapExceeded before any call."""
    if cap_usd is None:
        raise PlanRefused("--cap is required")

    plan_result = plan(suite, incumbent_id, candidate_id, prices, cap_usd=cap_usd, limit=limit, state_dir=state_dir)
    if not plan_result.within_cap:
        raise RunCapExceeded(
            f"estimate ${plan_result.estimate.total_usd:.4f} exceeds cap ${cap_usd:.4f}",
            plan_result.estimate,
        )
    _check_api_keys(incumbent_id, candidate_id)

    inc_price = prices.rows[incumbent_id]
    cand_price = prices.rows[candidate_id]
    inc_estimates = {e.item_id: e.cost_usd for e in plan_result.estimate.incumbent.items}
    cand_estimates = {e.item_id: e.cost_usd for e in plan_result.estimate.candidate.items}

    api_keys = {
        "anthropic": os.environ.get("ANTHROPIC_API_KEY"),
        "openai": os.environ.get("OPENAI_API_KEY"),
    }

    params = _default_params(suite)
    meter = cost_mod.CostMeter(cap_usd=cap_usd)
    spent_incumbent = 0.0
    spent_candidate = 0.0

    items = suite.items[:limit] if limit else suite.items
    run_name = name or f"{suite.name}-{_dt.datetime.now(_dt.timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
    started_at = _dt.datetime.now(_dt.timezone.utc).isoformat().replace("+00:00", "Z")

    result_items = []
    stopped_early = False
    for item in items:
        if not meter.check(inc_estimates[item.id]):
            stopped_early = True
            break
        inc_reply = _call_arm(incumbent_id, suite, item, params, api_keys)
        inc_cost = meter.charge(_actual_cost(inc_reply, inc_price), inc_estimates[item.id])
        spent_incumbent += inc_cost

        if not meter.check(cand_estimates[item.id]):
            stopped_early = True
            result_items.append(_item_record(item, incumbent_id=inc_reply, incumbent_cost=inc_cost, candidate=None, candidate_cost=None))
            break
        cand_reply = _call_arm(candidate_id, suite, item, params, api_keys)
        cand_cost = meter.charge(_actual_cost(cand_reply, cand_price), cand_estimates[item.id])
        spent_candidate += cand_cost

        result_items.append(_item_record(item, incumbent_id=inc_reply, incumbent_cost=inc_cost, candidate=cand_reply, candidate_cost=cand_cost))

    finished_at = _dt.datetime.now(_dt.timezone.utc).isoformat().replace("+00:00", "Z")
    items_completed = sum(1 for r in result_items if r["candidate"] is not None)

    record = {
        "run_name": run_name,
        "suite": str(suite_path) if suite_path else suite.name,
        "suite_hash": suite_mod.freeze_hash(suite),
        "incumbent": incumbent_id,
        "candidate": candidate_id,
        "started_at": started_at,
        "finished_at": finished_at,
        "cap_usd": cap_usd,
        "estimate_usd": {
            "incumbent": plan_result.estimate.incumbent.cost_usd,
            "candidate": plan_result.estimate.candidate.cost_usd,
            "total": plan_result.estimate.total_usd,
        },
        "spent_usd": {
            "incumbent": spent_incumbent,
            "candidate": spent_candidate,
            "total": spent_incumbent + spent_candidate,
        },
        "stopped_early": stopped_early,
        "items_completed": items_completed,
        "items_total": len(items),
        "request_params": {"incumbent": params, "candidate": params},
        "items": result_items,
    }

    _write_run_json(state_dir, run_name, record)
    return record


def _item_record(item, incumbent_id, incumbent_cost, candidate, candidate_cost):
    inc = incumbent_id
    return {
        "id": item.id,
        "input": item.input,
        "expect": item.expect,
        "incumbent": {
            "output": inc.output,
            "input_tokens": inc.input_tokens,
            "output_tokens": inc.output_tokens,
            "cost_usd": incumbent_cost,
            "latency_ms": inc.latency_ms,
            "error": inc.error,
        },
        "candidate": None
        if candidate is None
        else {
            "output": candidate.output,
            "input_tokens": candidate.input_tokens,
            "output_tokens": candidate.output_tokens,
            "cost_usd": candidate_cost,
            "latency_ms": candidate.latency_ms,
            "error": candidate.error,
        },
    }


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
