"""Run a suite through two model ids (incumbent, candidate) and write run.json.

Refuses to do: send a single call when the pre-run estimate exceeds the cap, or continue
past the cap mid-run; a call already in flight is never aborted, so a stop never loses a
paid output. Also refuses to let a suite's fixed judge model be the same id as either arm
(self-judging) unless the caller explicitly passes allow_self_judge=True.
"""

from __future__ import annotations

import math

import datetime as _dt
import hashlib
import json
import os
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from . import cost as cost_mod
from . import judge as judge_mod
from . import prices as prices_mod
from . import providers
from . import scoring as scoring_mod
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
    batch: bool = False
    reuse: Optional["ReuseSource"] = None


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


def _check_batch_provider(incumbent_id, candidate_id, judge_id):
    """--batch is Anthropic-only: a mixed run (some arms batched, some not) is never half-
    batched, so ANY non-Anthropic id among incumbent/candidate/judge refuses the whole run
    before anything is sent."""
    for label, model_id in (("incumbent", incumbent_id), ("candidate", candidate_id), ("judge", judge_id)):
        if model_id is None:
            continue
        if providers.provider_for_id(model_id) != "anthropic":
            raise PlanRefused(
                f"--batch is Anthropic-only; '{model_id}' ({label}) cannot be batched; "
                "a mixed run is never half-batched"
            )


def _batch_prices(prices, *model_ids) -> "prices_mod.Prices":
    """A copy of `prices` with each of `model_ids` (None entries skipped) replaced by its
    `.at_batch()` discounted row. A row with no batch_multiplier turns PricesError into
    PlanRefused -- same refusal class as every other pre-spend check."""
    rows = dict(prices.rows)
    for model_id in model_ids:
        if model_id is None:
            continue
        try:
            # From the ORIGINAL row: a model named twice (self-judge, or incumbent ==
            # candidate) must be discounted once, not compounded.
            rows[model_id] = prices.rows[model_id].at_batch()
        except prices_mod.PricesError as exc:
            raise PlanRefused(str(exc)) from exc
    return prices_mod.Prices(version=prices.version, checked=prices.checked, rows=rows)


@dataclass
class ReuseSource:
    """A validated `--reuse-incumbent` source: the stored arm outputs and judge records,
    already repriced for this run. Built only by `_load_reuse_source`."""

    run_name: str
    arm_in_source: str
    sha256: str
    started_at: Optional[str]
    carried_errors: list = field(default_factory=list)  # (item_id, what) for each arm/judge error carried over
    truncated_items: list = field(default_factory=list)  # item ids whose reused arm output was cut off (scoring.is_truncated)
    max_truncated: int = 0  # the suite's policy.max_truncated, which the truncation check compares against
    items: dict = field(default_factory=dict)  # item_id -> (arm_record, judge_record or None), both ready to copy into this run


def _refuse_reuse(run_name, why):
    raise PlanRefused(f"--reuse-incumbent {run_name}: {why}")


def _source_is_batch(source):
    """Batch only when the source's own run.json says so; a stray batch.json beside a sync
    run.json does not count."""
    return bool(source.get("batch"))


def _reprice_arm(arm_rec, price):
    """Copy of a source arm record with cost_usd recomputed from its recorded tokens at this
    run's (sync) price row; the as-recorded figure is kept as reused_cost_usd. An arm with an
    error or no recorded tokens has nothing to recompute from, so its recorded cost carries
    over unchanged."""
    rec = dict(arm_rec)
    rec["reused_cost_usd"] = arm_rec.get("cost_usd")
    in_tok, out_tok = arm_rec.get("input_tokens"), arm_rec.get("output_tokens")
    if arm_rec.get("error") is None and in_tok is not None and out_tok is not None:
        rec["cost_usd"] = in_tok * price.input / 1e6 + out_tok * price.output / 1e6
    return rec


def _reprice_judge(judge_rec, source, judge_price, run_name):
    """Copy of the source's judge record for the reused arm. Judge records carry only
    cost_usd, no tokens, so this is a RESCALE, not a recomputation: a batch-priced source
    (its run.json has a `batch` block) has its judge cost divided by the multiplier the
    source recorded for the judge model (batch.price_multiplier[judge model]). If the source
    says batch but records no multiplier for the judge, fall back to the batch_multiplier on
    today's price row (the best available figure), and refuse if that is absent too. A sync
    source is carried over unchanged. The as-recorded figure is kept as reused_cost_usd."""
    rec = dict(judge_rec)
    rec["reused_cost_usd"] = judge_rec.get("cost_usd")
    if _source_is_batch(source) and judge_rec.get("cost_usd") is not None:
        multipliers = (source.get("batch") or {}).get("price_multiplier") or {}
        multiplier = multipliers.get(judge_price.model)
        if multiplier is None:
            multiplier = judge_price.batch_multiplier
        else:
            # same rule prices.load applies to batch_multiplier: a number, > 0 and <= 1
            if isinstance(multiplier, bool) or not isinstance(multiplier, (int, float)) or not (0 < multiplier <= 1):
                _refuse_reuse(run_name, f"source records batch.price_multiplier[{judge_price.model!r}] = {multiplier!r}; it must be a number > 0 and <= 1")
        if not multiplier:
            _refuse_reuse(run_name, f"source was batch-priced, records no multiplier for '{judge_price.model}', and prices.yaml has no batch_multiplier for it; cannot rescale its judge cost")
        rec["cost_usd"] = judge_rec["cost_usd"] / multiplier
    return rec


def _load_reuse_source(run_name, suite, incumbent_id, prices, state_dir, limit, batch) -> ReuseSource:
    """Every refusal for `--reuse-incumbent`, before any spend (and before any call)."""
    # 6. batch
    if batch:
        _refuse_reuse(run_name, "not supported with --batch yet")
    # 2 (part). partial reuse
    if limit is not None:
        _refuse_reuse(run_name, "cannot be combined with --limit; no partial reuse")
    # 1. missing run
    if not run_name or Path(run_name).name != run_name or run_name in (".", ".."):
        _refuse_reuse(run_name, "not a run name")
    source_dir = Path(state_dir or ".pinbench") / "runs" / run_name
    source_path = source_dir / "run.json"
    if not source_path.is_file():
        _refuse_reuse(run_name, f"no {source_path}")
    raw = source_path.read_bytes()
    try:
        source = json.loads(raw)
    except json.JSONDecodeError as exc:
        _refuse_reuse(run_name, f"run.json is not valid JSON: {exc}")
    # 1. which arm is M
    arms = [a for a in ("incumbent", "candidate") if source.get(a) == incumbent_id]
    if not arms:
        _refuse_reuse(run_name, f"neither arm is '{incumbent_id}' (incumbent '{source.get('incumbent')}', candidate '{source.get('candidate')}')")
    if len(arms) == 2:
        _refuse_reuse(run_name, f"both arms are '{incumbent_id}'; ambiguous which to reuse")
    arm = arms[0]
    # chained reuse: a reuse run's incumbent slot is itself copied, so reuse the original. Its
    # candidate was measured live in that run (outputs and judge records), so that is allowed.
    if arm == "incumbent" and source.get("reused_incumbent"):
        _refuse_reuse(run_name, f"that run is itself a reuse run and '{incumbent_id}' is its reused incumbent (copied from '{source['reused_incumbent'].get('run')}'); reuse the original run instead")
    # 2. suite
    if source.get("suite_hash") != suite_mod.freeze_hash(suite):
        _refuse_reuse(run_name, "suite_hash differs (items, inputs, task, max_output_tokens or rubric points changed)")
    # 3. judge
    if suite.judge is None:
        if source.get("judge_model") is not None:
            _refuse_reuse(run_name, f"source was judged by '{source.get('judge_model')}' but this suite has no judge")
    else:
        this_run = {"judge_model": suite.judge.model, "judge_prompt_hash": judge_mod.prompt_hash(suite.judge.rubric)}
        try:
            judge_mod.assert_comparable(source, this_run)
        except judge_mod.JudgeMismatch as exc:
            _refuse_reuse(run_name, f"judge differs: {exc}")
    # 4. request params
    price = prices.rows[incumbent_id]
    this_params = _recorded_params(suite, _merged_params(price, suite))
    recorded = (source.get("request_params") or {}).get(arm)
    if recorded is None or recorded != json.loads(json.dumps(this_params)):
        _refuse_reuse(run_name, f"request params for '{incumbent_id}' differ: source sent {recorded!r}, this run would send {this_params!r}")
    # 5. complete
    source_items = source.get("items") or []
    if source.get("stopped_early"):
        _refuse_reuse(run_name, "the source run stopped early")
    if len(source_items) != len(suite.items) or source.get("items_total") != len(suite.items):
        _refuse_reuse(run_name, f"the source covers {len(source_items)} of {len(suite.items)} suite items")
    judge_price = prices.rows[suite.judge.model] if suite.judge else None
    items = {}
    carried = []
    truncated = []
    for expected, rec in zip(suite.items, source_items):
        arm_rec = rec.get("id") == expected.id and rec.get(arm)
        judge_rec = (rec.get("judge") or {}).get(arm) if suite.judge else None
        if not arm_rec or (suite.judge and not judge_rec):
            _refuse_reuse(run_name, f"the source is incomplete at item '{expected.id}' (no {'judge record or ' if suite.judge else ''}arm record)")
        if arm_rec.get("error"):
            carried.append((expected.id, "arm error"))
        if judge_rec and judge_rec.get("error"):
            carried.append((expected.id, "judge error"))
        if scoring_mod.is_truncated(arm_rec):
            truncated.append(expected.id)
        items[expected.id] = (
            _reprice_arm(arm_rec, price),
            _reprice_judge(judge_rec, source, judge_price, run_name) if suite.judge else None,
        )
    return ReuseSource(
        run_name=run_name, arm_in_source=arm, sha256="sha256:" + hashlib.sha256(raw).hexdigest(),
        started_at=source.get("started_at"), items=items, carried_errors=carried,
        truncated_items=truncated, max_truncated=suite.policy.max_truncated,
    )


def plan(
    suite, incumbent_id, candidate_id, prices, cap_usd=None, limit=None, state_dir=None,
    allow_self_judge=False, batch=False, reuse_incumbent=None,
) -> PlanResult:
    judge_id = suite.judge.model if suite.judge else None
    try:
        _require_price_and_route(incumbent_id, prices)
        _require_price_and_route(candidate_id, prices)
        if judge_id is not None:
            _require_price_and_route(judge_id, prices)
    except providers.NoAdapterError as exc:
        raise PlanRefused(str(exc)) from exc
    if batch:
        _check_batch_provider(incumbent_id, candidate_id, judge_id)
    _check_self_judge(judge_id, incumbent_id, candidate_id, allow_self_judge)
    reuse = None
    if reuse_incumbent is not None:
        reuse = _load_reuse_source(reuse_incumbent, suite, incumbent_id, prices, state_dir, limit, batch)
        if reuse.carried_errors:
            # Warning, not a refusal (design pending): errors are carried over as recorded
            # and nothing is re-judged, so the report will be INCOMPLETE.
            print(
                f"warning: reused incumbent from '{reuse_incumbent}' carries {len(reuse.carried_errors)} arm/judge error(s), "
                f"first at item '{reuse.carried_errors[0][0]}' ({reuse.carried_errors[0][1]}); "
                "the verdict will be INCOMPLETE because of carried-over errors",
                file=sys.stderr,
            )
        if len(reuse.truncated_items) > reuse.max_truncated:
            print(
                f"warning: reused incumbent from '{reuse_incumbent}' has {len(reuse.truncated_items)} truncated output(s), "
                f"more than the suite's policy.max_truncated ({reuse.max_truncated}); first at item '{reuse.truncated_items[0]}'; "
                "the verdict will be INCOMPLETE because of the truncation check",
                file=sys.stderr,
            )

    estimate_prices = _batch_prices(prices, incumbent_id, candidate_id, judge_id) if batch else prices
    estimate = cost_mod.estimate_run(suite, incumbent_id, candidate_id, estimate_prices, limit=limit, state_dir=state_dir, judge_id=judge_id, reuse_incumbent=reuse is not None)
    within_cap = None if cap_usd is None else estimate.total_usd <= cap_usd
    return PlanResult(
        incumbent_id=incumbent_id, candidate_id=candidate_id, estimate=estimate, cap_usd=cap_usd,
        within_cap=within_cap, judge_id=judge_id, batch=batch, reuse=reuse,
    )


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


# Per-call HTTP timeout for the sync path (arms and judge): judge.DEFAULT_TIMEOUT_S, the
# adapters' own default. `run --timeout` overrides it. Batch runs make no per-call request.
DEFAULT_TIMEOUT_S = judge_mod.DEFAULT_TIMEOUT_S


def _call_arm(model_id, suite, item, params, api_keys, timeout_s=DEFAULT_TIMEOUT_S):
    provider_name = providers.provider_for_id(model_id)
    api_key = api_keys[provider_name]
    adapter = providers.route(model_id)
    return adapter.call(model_id, suite.task, item.input, suite.max_output_tokens, api_key, params=params, timeout=timeout_s)


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


def _execute_items(items, suite, incumbent_id, candidate_id, inc_price, cand_price, inc_estimates, cand_estimates, meter, api_keys, judge_ctx=None, timeout_s=DEFAULT_TIMEOUT_S, reuse=None):
    """Run every item through both arms (then the judge, if judge_ctx is given) until the
    meter stops it. Never aborts a call in flight.

    `reuse` (a ReuseSource) fills the incumbent slot from stored records: no incumbent call,
    no incumbent judge call, nothing charged to the meter for either."""
    inc_params = _merged_params(inc_price, suite)
    cand_params = _merged_params(cand_price, suite)
    result_items = []
    spent_incumbent = 0.0
    spent_candidate = 0.0
    spent_judge = 0.0
    stopped_early = False
    inc_stats = _ArmStats()
    cand_stats = _ArmStats()

    unexpected = None
    for item in items:
        inc_reply = inc_cost = cand_reply = cand_cost = None
        reused_arm = reused_judge = None
        try:
            if reuse is not None:
                reused_arm, reused_judge = reuse.items[item.id]
                inc_reply = _reply_from_record(reused_arm)
                inc_cost = reused_arm["cost_usd"]
                inc_stats.record(inc_reply)
            else:
                if not meter.check(inc_estimates[item.id]):
                    stopped_early = True
                    break
                inc_reply = _call_arm(incumbent_id, suite, item, inc_params, api_keys, timeout_s)
                inc_stats.record(inc_reply)
                inc_cost = meter.charge(cost_mod.actual_call_cost(inc_reply, inc_price, inc_estimates[item.id]), inc_estimates[item.id])
                spent_incumbent += inc_cost

            if not meter.check(cand_estimates[item.id]):
                stopped_early = True
                result_items.append(_item_record(item, inc_reply, inc_cost, candidate=None, candidate_cost=None, incumbent_record=reused_arm))
                break
            cand_reply = _call_arm(candidate_id, suite, item, cand_params, api_keys, timeout_s)
            cand_stats.record(cand_reply)
            cand_cost = meter.charge(cost_mod.actual_call_cost(cand_reply, cand_price, cand_estimates[item.id]), cand_estimates[item.id])
            spent_candidate += cand_cost

            judge_record = None
            if judge_ctx is not None:
                # Both arms are handed to the judge stage regardless of whether either errored
                # or was cut off -- judge.run_judge_stage (via judge.skip_reason) decides per arm
                # whether to actually call the judge or just record "skipped", never the caller.
                judge_record, judge_stopped = judge_mod.run_judge_stage(
                    meter, judge_ctx["id"], judge_ctx["price"], judge_ctx["api_key"], suite, item,
                    {"incumbent": inc_reply, "candidate": cand_reply}, timeout_s=timeout_s,
                    only_arms=("candidate",) if reuse is not None else None,
                )
                if reuse is not None:
                    judge_record["incumbent"] = reused_judge
                    spent_judge += judge_record["candidate"]["cost_usd"] if "candidate" in judge_record else 0.0
                else:
                    spent_judge += sum(judge_record[arm]["cost_usd"] for arm in ("incumbent", "candidate") if arm in judge_record)
                if judge_stopped:
                    stopped_early = True

            result_items.append(_item_record(item, inc_reply, inc_cost, cand_reply, cand_cost, judge_record, incumbent_record=reused_arm))
            if judge_ctx is not None and stopped_early:
                break
        except Exception as exc:  # noqa: BLE001 - keep every paid output, then re-raise from run()
            # Anything the adapters did not turn into a Reply error (a bug, an odd exception)
            # must not lose outputs already paid for: keep this item's collected half, stop,
            # and let run() write run.json before re-raising.
            unexpected = exc
            stopped_early = True
            if inc_reply is not None and inc_cost is not None:
                result_items.append(_item_record(item, inc_reply, inc_cost, cand_reply, cand_cost, incumbent_record=reused_arm))
            break

    arm_stats = {"incumbent": inc_stats, "candidate": cand_stats}
    return result_items, stopped_early, spent_incumbent, spent_candidate, spent_judge, inc_params, cand_params, arm_stats, unexpected


def run(
    suite, incumbent_id, candidate_id, prices, cap_usd, limit=None, name=None, state_dir=".pinbench",
    suite_path=None, allow_self_judge=False, effective_config=None,
    batch=False, reuse_incumbent=None, poll_interval_s=30, max_wait_s=86400, timeout_s=DEFAULT_TIMEOUT_S,
):
    """Run both arms over the suite. Raises PlanRefused or RunCapExceeded before any call.

    `effective_config` (optional) is the resolved config/CLI/suite/built-in settings dict
    the caller computed (see `config.py` and `cli.py`'s resolvers); it is recorded verbatim
    in run.json so `pinbench report` can reload this exact suite later (judge/policy
    fallbacks included) even if pinbench.yaml has since changed, and so the report header
    can name which config file, if any, this run used.

    `batch=True` sends every Anthropic arm (and the judge, if any) as a Message Batch at the
    batch discount instead of one call per item -- see `batch.run_batch`. The pre-spend
    checks (cap, price/route, self-judge, plus the Anthropic-only check) are identical; only
    how the calls are made and metered differs.
    """
    if cap_usd is None:
        raise PlanRefused("a cost cap is required: set --cap, the suite's cap_usd, or pinbench.yaml's cap_usd")

    if not isinstance(timeout_s, (int, float)) or isinstance(timeout_s, bool) or not math.isfinite(timeout_s) or timeout_s <= 0:
        raise PlanRefused(f"--timeout must be a positive number of seconds, got {timeout_s!r}")
    if batch and poll_interval_s < 1:
        raise PlanRefused(f"poll interval must be at least 1 second, got {poll_interval_s}")

    plan_result = plan(
        suite, incumbent_id, candidate_id, prices, cap_usd=cap_usd, limit=limit, state_dir=state_dir,
        allow_self_judge=allow_self_judge, batch=batch, reuse_incumbent=reuse_incumbent,
    )
    if not plan_result.within_cap:
        raise RunCapExceeded(
            f"estimate ${plan_result.estimate.total_usd:.4f} exceeds cap ${cap_usd:.4f}",
            plan_result.estimate,
        )
    _check_api_keys(None if reuse_incumbent is not None else incumbent_id, candidate_id, plan_result.judge_id)

    if batch:
        from . import batch as batch_mod  # local import: batch.py imports this module at load time

        record = batch_mod.run_batch(
            suite, incumbent_id, candidate_id, prices, plan_result, cap_usd, limit, name, state_dir, suite_path,
            poll_interval_s=poll_interval_s, max_wait_s=max_wait_s,
        )
        record["effective_config"] = effective_config
        _write_run_json(state_dir, record["run_name"], record)
        return record

    inc_price = prices.rows[incumbent_id]
    cand_price = prices.rows[candidate_id]
    inc_estimates = {e.item_id: e.cost_usd for e in plan_result.estimate.incumbent.items}
    cand_estimates = {e.item_id: e.cost_usd for e in plan_result.estimate.candidate.items}

    api_keys = {provider: os.environ.get(env_key) for provider, env_key in providers.ENV_KEYS.items()}

    judge_ctx = None
    if plan_result.judge_id is not None:
        judge_provider = providers.provider_for_id(plan_result.judge_id)
        judge_ctx = {"id": plan_result.judge_id, "price": prices.rows[plan_result.judge_id], "api_key": api_keys[judge_provider]}

    meter = cost_mod.CostMeter(cap_usd=cap_usd)
    items = suite.items[:limit] if limit is not None else suite.items
    run_name = name or f"{suite.name}-{_dt.datetime.now(_dt.timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
    started_at = _dt.datetime.now(_dt.timezone.utc).isoformat().replace("+00:00", "Z")

    result_items, stopped_early, spent_incumbent, spent_candidate, spent_judge, inc_params, cand_params, arm_stats, unexpected = _execute_items(
        items, suite, incumbent_id, candidate_id, inc_price, cand_price, inc_estimates, cand_estimates, meter, api_keys, judge_ctx=judge_ctx,
        timeout_s=timeout_s, reuse=plan_result.reuse,
    )

    finished_at = _dt.datetime.now(_dt.timezone.utc).isoformat().replace("+00:00", "Z")
    record = _build_record(
        run_name, suite, suite_path, incumbent_id, candidate_id, started_at, finished_at, cap_usd,
        plan_result, spent_incumbent, spent_candidate, spent_judge, stopped_early, items, inc_params, cand_params, result_items, arm_stats,
    )
    record["effective_config"] = effective_config
    record["timeout_s"] = timeout_s
    if plan_result.reuse is not None:
        reuse = plan_result.reuse
        record["reused_incumbent"] = {
            "run": reuse.run_name, "arm_in_source": reuse.arm_in_source,
            "source_run_sha256": reuse.sha256, "source_started_at": reuse.started_at,
        }

    _write_run_json(state_dir, run_name, record)
    if unexpected is not None:
        raise unexpected
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


def _reply_from_record(arm_rec):
    """A providers.Reply rebuilt from a stored arm record, so a reused incumbent goes through
    the same stats and judge-skip rules as a live reply."""
    return providers.Reply(
        output=arm_rec.get("output"), input_tokens=arm_rec.get("input_tokens"), output_tokens=arm_rec.get("output_tokens"),
        latency_ms=arm_rec.get("latency_ms"), error=arm_rec.get("error"), stop_reason=arm_rec.get("stop_reason"),
    )


def _item_record(item, incumbent, incumbent_cost, candidate, candidate_cost, judge_record=None, incumbent_record=None):
    """`incumbent`/`candidate` are providers.Reply objects; candidate is None when the meter
    stopped first. `judge_record` (from judge.run_judge_stage) is None for a non-judge suite,
    or when the meter stopped before the judge stage ran for this item.
    """
    record = {
        "id": item.id,
        "input": item.input,
        "expect": item.expect,
        "rubric_points": [{"id": p.id, "description": p.description} for p in item.rubric_points],
        # incumbent_record: a reused arm record (already repriced), copied as stored.
        "incumbent": incumbent_record if incumbent_record is not None else _arm_record(incumbent, incumbent_cost),
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
