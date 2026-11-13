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
import secrets
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from . import cost as cost_mod
from . import judge as judge_mod
from . import prices as prices_mod
from . import providers
from . import resume as resume_mod
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
    batch_judge: bool = False


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


def _check_batch_judge(suite, judge_id, prices, batch):
    """--batch-judge: only the judge goes through the Message Batches API, so only the judge
    must be Anthropic and carry a batch_multiplier. Every refusal is before any spend."""
    if batch:
        raise PlanRefused("--batch-judge and --batch are mutually exclusive: --batch already batches the judge")
    if judge_id is None:
        raise PlanRefused("--batch-judge needs a suite with a judge; this suite has none")
    if providers.provider_for_id(judge_id) != "anthropic":
        raise PlanRefused(f"--batch-judge is Anthropic-only; judge '{judge_id}' cannot be batched")
    _batch_prices(prices, judge_id)  # raises PlanRefused when the judge row has no batch_multiplier


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
    run's (sync) price row; the as-recorded figure is kept as reused_cost_usd. An arm that
    errored, or that lacks either recorded token count, is NOT repriced: its recorded cost
    (which may include billed tokens, metered by cost.actual_call_cost at the source run's
    price) carries over unchanged, with no batch-multiplier rescale."""
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
    source is carried over unchanged. The as-recorded figure is kept as reused_cost_usd.

    A `--batch-judge` source (run.json `judge_batch`; its arms were sync-priced and it has no
    `batch` block) rescales its judge cost by `judge_batch.price_multiplier` the same way. It
    is never mistaken for a batch-priced source, whose ARMS were batch-priced."""
    rec = dict(judge_rec)
    rec["reused_cost_usd"] = judge_rec.get("cost_usd")
    judge_batched = bool(source.get("judge_batch"))
    if (_source_is_batch(source) or judge_batched) and judge_rec.get("cost_usd") is not None:
        if judge_batched:
            multiplier = source["judge_batch"].get("price_multiplier")
        else:
            multipliers = (source.get("batch") or {}).get("price_multiplier") or {}
            multiplier = multipliers.get(judge_price.model)
        if multiplier is None:
            multiplier = judge_price.batch_multiplier
        else:
            # same rule prices.load applies to batch_multiplier: a number, > 0 and <= 1
            if isinstance(multiplier, bool) or not isinstance(multiplier, (int, float)) or not (0 < multiplier <= 1):
                where = "judge_batch.price_multiplier" if judge_batched else f"batch.price_multiplier[{judge_price.model!r}]"
                _refuse_reuse(run_name, f"source records {where} = {multiplier!r}; it must be a number > 0 and <= 1")
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
    allow_self_judge=False, batch=False, reuse_incumbent=None, batch_judge=False,
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
    if batch_judge:
        _check_batch_judge(suite, judge_id, prices, batch)
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

    if batch:
        estimate_prices = _batch_prices(prices, incumbent_id, candidate_id, judge_id)
    elif batch_judge:
        estimate_prices = _batch_prices(prices, judge_id)  # only the judge row is discounted
    else:
        estimate_prices = prices
    estimate = cost_mod.estimate_run(suite, incumbent_id, candidate_id, estimate_prices, limit=limit, state_dir=state_dir, judge_id=judge_id, reuse_incumbent=reuse is not None)
    within_cap = None if cap_usd is None else estimate.total_usd <= cap_usd
    return PlanResult(
        incumbent_id=incumbent_id, candidate_id=candidate_id, estimate=estimate, cap_usd=cap_usd,
        within_cap=within_cap, judge_id=judge_id, batch=batch, reuse=reuse, batch_judge=batch_judge,
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


@dataclass
class _Progress:
    """Mutable state of the arm loop, shared with run() so that a BaseException (a crash, a
    second signal) leaves every finished item and every charged dollar reachable."""

    result_items: list = field(default_factory=list)
    spent_incumbent: float = 0.0
    spent_candidate: float = 0.0
    spent_judge: float = 0.0
    stopped_early: bool = False
    stop_reason: Optional[str] = None
    inc_stats: _ArmStats = field(default_factory=_ArmStats)
    cand_stats: _ArmStats = field(default_factory=_ArmStats)
    unexpected: Optional[BaseException] = None
    inc_params: Optional[dict] = None
    cand_params: Optional[dict] = None


def _arm_ok(arm_rec):
    """A stored arm that succeeded (truncated counts: a truncation is a result, not a failure)."""
    return arm_rec is not None and arm_rec.get("error") is None


def _judge_ok(judge_rec, arms):
    """A stored judge record with every expected arm present and without a judge error."""
    return bool(judge_rec) and all(arm in judge_rec and judge_rec[arm].get("error") is None for arm in arms)


def _charge_inflight(prog, meter, inflight, judge_before):
    """A call abandoned in flight (a second signal, a crash mid-call) has an unknown outcome:
    charge its estimate, as cost.py does for any unknown outcome, never $0. For a judge call the
    judge calls that finished earlier in the stage were charged to the meter but not yet added
    to the run's judge spend; add those too."""
    if inflight is None:
        return
    kind, est = inflight
    meter.charge(est, est)
    if kind == "incumbent":
        prog.spent_incumbent += est
    elif kind == "candidate":
        prog.spent_candidate += est
    else:
        prog.spent_judge += (meter.spent_usd - judge_before) if judge_before is not None else est


def _execute_items(items, suite, incumbent_id, candidate_id, inc_price, cand_price, inc_estimates, cand_estimates, meter, api_keys, judge_ctx=None, timeout_s=DEFAULT_TIMEOUT_S, reuse=None, judge_reserve_usd=0.0,
                   prior=None, progress=None, checkpoint=None, guard=None, sleep_fn=None):
    """Run every item through both arms (then the judge, if judge_ctx is given) until the
    meter stops it. Never aborts a call in flight.

    `reuse` (a ReuseSource) fills the incumbent slot from stored records: no incumbent call,
    no incumbent judge call, nothing charged to the meter for either.

    `judge_reserve_usd` (--batch-judge) is the judge batch's estimate: every arm call's meter
    check must leave that much room under the cap, so the arms stop early rather than spend
    the cap and strand the batch the run was meant to end with.

    `prior` ({item id: stored item record}, a resume) supplies finished work: an arm that
    succeeded is kept as stored and not called again; an arm that errored, or never ran, is
    called. A reused-incumbent slot is kept from the checkpoint as it is. `progress` carries
    the running state (and the prior spend); `checkpoint(progress)` runs after every finished
    item; `guard` (a SignalGuard) stops the loop before the next call once a signal arrives.
    A call that is still rate-limited after bounded backoff stops the run (stop_reason
    quota_or_rate_limit) instead of recording a 429 for every remaining item."""
    prior = prior or {}
    prog = progress if progress is not None else _Progress()
    inc_params = prog.inc_params = _merged_params(inc_price, suite)
    cand_params = prog.cand_params = _merged_params(cand_price, suite)
    judge_arms = ("candidate",) if reuse is not None else ("incumbent", "candidate")

    def signalled():
        return guard is not None and guard.requested is not None

    def stop(reason):
        prog.stopped_early = True
        prog.stop_reason = reason
        if reason == "signal" and guard is not None:
            prog.stop_reason = f"signal:{guard.name}"

    for item in items:
        prev = prior.get(item.id)
        prev_inc = prev["incumbent"] if prev else None
        prev_cand = prev["candidate"] if prev else None
        inc_reply = inc_cost = cand_reply = cand_cost = None
        inc_rec = cand_rec = None
        reused_judge = None
        fresh = False
        n_before = len(prog.result_items)
        inflight = None  # (kind, estimate_usd) of a call started and not yet charged

        def partial():
            if len(prog.result_items) > n_before:
                return  # this item is already recorded (a checkpoint failed after it was appended)
            if inc_reply is not None:
                prog.result_items.append(_item_record(item, inc_reply, inc_cost, cand_reply, cand_cost, incumbent_record=inc_rec, candidate_record=cand_rec))
            elif prev is not None:
                prog.result_items.append(prev)

        try:
            if reuse is not None:
                inc_rec = prev_inc if prev_inc is not None else reuse.items[item.id][0]
                reused_judge = ((prev.get("judge") or {}).get("incumbent") if prev else None) or reuse.items[item.id][1]
                inc_reply = _reply_from_record(inc_rec)
                inc_cost = inc_rec["cost_usd"]
                prog.inc_stats.record(inc_reply)
            elif _arm_ok(prev_inc):
                inc_rec = prev_inc
                inc_reply = _reply_from_record(inc_rec)
                inc_cost = inc_rec["cost_usd"]
                prog.inc_stats.record(inc_reply)
            else:
                if signalled():
                    partial()
                    stop("signal")
                    break
                if not meter.check(inc_estimates[item.id] + judge_reserve_usd):
                    partial()
                    stop("cap")
                    break
                est = inc_estimates[item.id]
                inflight = ("incumbent", est)
                inc_reply, retried = resume_mod.call_with_rate_limit_retry(
                    lambda: _call_arm(incumbent_id, suite, item, inc_params, api_keys, timeout_s), inc_price, est, sleep_fn, meter=meter, stop=signalled)
                fresh = True
                prog.inc_stats.record(inc_reply)
                inc_cost = retried + meter.charge(cost_mod.actual_call_cost(inc_reply, inc_price, est), est)
                inflight = None
                prog.spent_incumbent += inc_cost
                if resume_mod.is_rate_limited(inc_reply):
                    partial()
                    stop("signal" if signalled() else resume_mod.STOP_QUOTA)
                    break

            if _arm_ok(prev_cand):
                cand_rec = prev_cand
                cand_reply = _reply_from_record(cand_rec)
                cand_cost = cand_rec["cost_usd"]
                prog.cand_stats.record(cand_reply)
            else:
                if signalled():
                    partial()
                    stop("signal")
                    break
                if not meter.check(cand_estimates[item.id] + judge_reserve_usd):
                    partial()
                    stop("cap")
                    break
                est = cand_estimates[item.id]
                inflight = ("candidate", est)
                cand_reply, retried = resume_mod.call_with_rate_limit_retry(
                    lambda: _call_arm(candidate_id, suite, item, cand_params, api_keys, timeout_s), cand_price, est, sleep_fn, meter=meter, stop=signalled)
                fresh = True
                prog.cand_stats.record(cand_reply)
                cand_cost = retried + meter.charge(cost_mod.actual_call_cost(cand_reply, cand_price, est), est)
                inflight = None
                prog.spent_candidate += cand_cost
                if resume_mod.is_rate_limited(cand_reply):
                    partial()
                    stop("signal" if signalled() else resume_mod.STOP_QUOTA)
                    break

            # --batch-judge (no sync judge stage): the stored judge block, with its recorded
            # call_order, rides along so a judge-only resume keeps it.
            judge_record = prev.get("judge") if (prev and judge_ctx is None) else None
            if judge_ctx is not None:
                prev_judge = prev.get("judge") if prev else None
                if not fresh and _judge_ok(prev_judge, judge_arms):
                    judge_record = prev_judge
                else:
                    if signalled():
                        partial()
                        stop("signal")
                        break
                    # Both arms are handed to the judge stage regardless of whether either errored
                    # or was cut off -- judge.run_judge_stage (via judge.skip_reason) decides per arm
                    # whether to actually call the judge or just record "skipped", never the caller.
                    judge_before = meter.spent_usd
                    inflight = ("judge", cost_mod.estimate_judge_call_cost(suite, item, judge_ctx["price"]))
                    try:
                        judge_record, judge_stopped = judge_mod.run_judge_stage(
                            meter, judge_ctx["id"], judge_ctx["price"], judge_ctx["api_key"], suite, item,
                            {"incumbent": inc_reply, "candidate": cand_reply}, timeout_s=timeout_s,
                            only_arms=("candidate",) if reuse is not None else None, sleep_fn=sleep_fn, stop=signalled,
                        )
                        inflight = None
                    except resume_mod.RateLimitExhausted as exc:
                        inflight = None
                        judge_record = exc.record
                        if reuse is not None:
                            judge_record["incumbent"] = reused_judge
                        prog.spent_judge += sum(judge_record[a]["cost_usd"] for a in judge_arms if a in judge_record)
                        prog.result_items.append(_item_record(item, inc_reply, inc_cost, cand_reply, cand_cost, judge_record, incumbent_record=inc_rec, candidate_record=cand_rec))
                        stop("signal" if signalled() else resume_mod.STOP_QUOTA)
                        break
                    if reuse is not None:
                        judge_record["incumbent"] = reused_judge
                    prog.spent_judge += sum(judge_record[a]["cost_usd"] for a in judge_arms if a in judge_record)
                    if judge_stopped:
                        prog.stopped_early = True
                        prog.stop_reason = "cap"

            prog.result_items.append(_item_record(item, inc_reply, inc_cost, cand_reply, cand_cost, judge_record, incumbent_record=inc_rec, candidate_record=cand_rec))
            if judge_ctx is not None and prog.stopped_early:
                break
            if checkpoint is not None:
                # The item is already appended: if this write fails, `partial()` sees that and
                # does not append it a second time.
                checkpoint(prog)
        except Exception as exc:  # noqa: BLE001 - keep every paid output, then re-raise from run()
            # Anything the adapters did not turn into a Reply error (a bug, an odd exception)
            # must not lose outputs already paid for: keep this item's collected half, stop,
            # and let run() write run.json before re-raising.
            prog.unexpected = exc
            prog.stopped_early = True
            prog.stop_reason = f"error: {type(exc).__name__}: {exc}"
            _charge_inflight(prog, meter, inflight, judge_before if inflight and inflight[0] == "judge" else None)
            partial()
            break
        except BaseException:
            # A crash or an abandoning signal (KeyboardInterrupt, RunInterrupted): keep the
            # finished half of this item, mark the run stopped, and let it propagate.
            prog.stopped_early = True
            prog.stop_reason = "interrupted"
            _charge_inflight(prog, meter, inflight, judge_before if inflight and inflight[0] == "judge" else None)
            partial()
            raise

    arm_stats = {"incumbent": prog.inc_stats, "candidate": prog.cand_stats}
    return prog.result_items, prog.stopped_early, prog.spent_incumbent, prog.spent_candidate, prog.spent_judge, inc_params, cand_params, arm_stats, prog.unexpected


# --- resume ---------------------------------------------------------------------------------

# judge_batch.status values that mean "the batch exists and its id is recorded": a resume
# polls and fetches it and never creates a second one.
_JB_COLLECT = ("submitted", "timed_out", "poll_failed", "results_failed")
# A resume submits: nothing was sent (the cap refused it, or a definite failure before any batch existed).
_JB_SUBMIT = ("not_submitted_cap", "submit_failed", "not_submitted")
# A batch may exist with no id recorded. `pending` is this state too: run.json says "about to
# submit" and a kill during the create call leaves it there.
_JB_UNKNOWN = ("pending", "submit_outcome_unknown")
_JB_DONE = ("ended", "nothing_to_judge")
# The batch phase is over but the run is not complete: a resume can still do something.
_JB_STOPPED = ("not_submitted_cap", "submit_failed", "submit_outcome_unknown", "timed_out", "poll_failed", "results_failed")


def _stamp():
    return _dt.datetime.now(_dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _default_run_name(suite, state_dir):
    """suite-UTCstamp-random, re-drawn while the directory exists: two runs started in the
    same second never share a directory."""
    stamp = _stamp()
    while True:
        name = f"{suite.name}-{stamp}-{secrets.token_hex(3)}"
        if not (Path(state_dir) / "runs" / name).exists():
            return name


def _now():
    return _dt.datetime.now(_dt.timezone.utc).isoformat().replace("+00:00", "Z")


def _refuse_resume(name, why):
    raise PlanRefused(f"--resume {name}: {why}")


def _load_resumable(state_dir, name):
    path = Path(state_dir) / "runs" / name / "run.json"
    if not path.is_file():
        _refuse_resume(name, f"no {path}")
    try:
        prior = json.loads(path.read_text())
    except json.JSONDecodeError as exc:
        _refuse_resume(name, f"run.json is not valid JSON: {exc}")
    if "status" not in prior:
        _refuse_resume(name, "run.json predates resume support (it has no `status`); start a new run")
    if prior.get("batch"):
        _refuse_resume(name, "a --batch run cannot be resumed (its arms are one batch)")
    if prior["status"] == "complete":
        _refuse_resume(name, "the run is complete; there is nothing to resume")
    return prior


def _check_resumable(prior, name, suite, incumbent_id, candidate_id, inc_params, cand_params, plan_result, limit, batch_judge, hold_judge_on_errors=False):
    """Every comparability refusal, before any call. A resumed run must be the SAME measurement."""
    def differs(what, recorded, now):
        _refuse_resume(name, f"{what} differs: the run recorded {recorded!r}, this command has {now!r}")

    if prior.get("suite_hash") != suite_mod.freeze_hash(suite):
        _refuse_resume(name, "suite_hash differs (items, inputs, task, max_output_tokens or rubric points changed)")
    if prior.get("incumbent") != incumbent_id:
        differs("incumbent", prior.get("incumbent"), incumbent_id)
    if prior.get("candidate") != candidate_id:
        differs("candidate", prior.get("candidate"), candidate_id)
    now_params = json.loads(json.dumps({"incumbent": _recorded_params(suite, inc_params), "candidate": _recorded_params(suite, cand_params)}))
    for arm in ("incumbent", "candidate"):
        if (prior.get("request_params") or {}).get(arm) != now_params[arm]:
            differs(f"request params for the {arm}", (prior.get("request_params") or {}).get(arm), now_params[arm])
    if prior.get("judge_model") != plan_result.judge_id:
        differs("judge model", prior.get("judge_model"), plan_result.judge_id)
    now_hash = judge_mod.prompt_hash(suite.judge.rubric) if suite.judge else None
    if prior.get("judge_prompt_hash") != now_hash:
        differs("judge prompt hash (rubric changed)", prior.get("judge_prompt_hash"), now_hash)
    recorded_reuse = prior.get("reused_incumbent")
    if (recorded_reuse is None) != (plan_result.reuse is None):
        differs("--reuse-incumbent", recorded_reuse and recorded_reuse.get("run"), plan_result.reuse and plan_result.reuse.run_name)
    if recorded_reuse is not None:
        if recorded_reuse.get("run") != plan_result.reuse.run_name:
            differs("--reuse-incumbent source", recorded_reuse.get("run"), plan_result.reuse.run_name)
        if recorded_reuse.get("source_run_sha256") != plan_result.reuse.sha256:
            differs("--reuse-incumbent source sha256 (the source run.json changed)", recorded_reuse.get("source_run_sha256"), plan_result.reuse.sha256)
    modes = prior.get("run_modes") or {}
    if bool(modes.get("batch_judge")) != bool(batch_judge):
        differs("--batch-judge", bool(modes.get("batch_judge")), bool(batch_judge))
    # The hold only decides WHEN the judge batch is sent, not what is measured, so dropping it on
    # a resume is allowed (a run whose item errors every time could never be judged otherwise).
    # Adding it is refused: a run that already submitted, or might have, must not be held late.
    if bool(hold_judge_on_errors) and not bool(modes.get("hold_judge_on_errors")):
        differs("--hold-judge-on-errors", bool(modes.get("hold_judge_on_errors")), bool(hold_judge_on_errors))
    if modes.get("limit") != limit:
        differs("--limit", modes.get("limit"), limit)


def _judge_resume_decision(prior):
    """For a --batch-judge resume: the recorded batch id to collect, or None to submit. Raises
    PlanRefused. A recorded id is always collected and never re-created (even with errored arms
    remaining: those arms were skipped when the batch was built, so its custom_ids still match).
    A submit whose outcome is unknown (no id recorded) is refused: a batch may exist and bill,
    and recovering from that is out of scope; check the Anthropic console."""
    name = prior["run_name"]
    jb = prior.get("judge_batch")
    if jb is None:
        return None
    status, bid = jb.get("status"), jb.get("id")
    if status in _JB_DONE:
        _refuse_resume(name, "the judge batch already ended; nothing to collect")
    if status in _JB_COLLECT and bid:
        return bid
    if status in _JB_SUBMIT:
        return None
    if status in _JB_UNKNOWN:
        _refuse_resume(
            name,
            f"the judge batch submission outcome is unknown (status {status!r}, no batch id recorded): a batch may exist and bill. "
            "Check the Anthropic console (Batches) first; a resume will not submit a second batch over it",
        )
    _refuse_resume(name, f"unrecognised judge_batch status {status!r}")


def run(*args, **kwargs):
    """See `_run`. The wrapper owns the run-directory lock for the whole call."""
    import contextlib

    with contextlib.ExitStack() as stack:
        return _run(*args, _stack=stack, **kwargs)


def _refuse_lock(why):
    raise PlanRefused(why)


def _run(
    suite, incumbent_id, candidate_id, prices, cap_usd, limit=None, name=None, state_dir=".pinbench",
    suite_path=None, allow_self_judge=False, effective_config=None,
    batch=False, reuse_incumbent=None, poll_interval_s=30, max_wait_s=86400, timeout_s=DEFAULT_TIMEOUT_S,
    batch_judge=False, resume=None, sleep_fn=None, hold_judge_on_errors=False, _stack=None,
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

    `batch_judge=True` runs the arms synchronously as usual, writes them to run.json, then
    sends every judge call as ONE Message Batch at the judge's batch price -- see
    `batch.run_judge_batch`. Incompatible with `batch`; the judge alone must be Anthropic.

    A sync run checkpoints run.json after every item (`status: in_progress`), so a crash
    keeps every finished item. It refuses a name that already has a run.json unless
    `resume=NAME` continues it (see `_check_resumable`, `_judge_resume_decision`). A call
    still rate-limited after bounded backoff stops the run with stop_reason
    quota_or_rate_limit; SIGTERM, SIGHUP and SIGINT stop it after the call in flight returns.
    """
    if cap_usd is None:
        raise PlanRefused("a cost cap is required: set --cap, the suite's cap_usd, or pinbench.yaml's cap_usd")

    if not isinstance(timeout_s, (int, float)) or isinstance(timeout_s, bool) or not math.isfinite(timeout_s) or timeout_s <= 0:
        raise PlanRefused(f"--timeout must be a positive number of seconds, got {timeout_s!r}")
    if (batch or batch_judge) and poll_interval_s < 1:
        raise PlanRefused(f"poll interval must be at least 1 second, got {poll_interval_s}")
    if hold_judge_on_errors and not batch_judge:
        raise PlanRefused("--hold-judge-on-errors applies only with --batch-judge")
    if resume and batch:
        raise PlanRefused("--resume does not apply to a --batch run (its arms are one batch)")
    if resume and name and name != resume:
        raise PlanRefused(f"--resume {resume} and --name {name} disagree; the resumed run keeps its name")

    prior = None
    if resume:
        run_name = resume
        prior = _load_resumable(state_dir, resume)
    else:
        run_name = name or _default_run_name(suite, state_dir)
        if (Path(state_dir) / "runs" / run_name / "run.json").exists():
            raise PlanRefused(f"run '{run_name}' already exists; pass --resume {run_name} to continue it, or choose another --name")

    plan_result = plan(
        suite, incumbent_id, candidate_id, prices, cap_usd=cap_usd, limit=limit, state_dir=state_dir,
        allow_self_judge=allow_self_judge, batch=batch, reuse_incumbent=reuse_incumbent, batch_judge=batch_judge,
    )
    if prior is None and not plan_result.within_cap:
        raise RunCapExceeded(
            f"estimate ${plan_result.estimate.total_usd:.4f} exceeds cap ${cap_usd:.4f}",
            plan_result.estimate,
        )
    _check_api_keys(None if reuse_incumbent is not None else incumbent_id, candidate_id, plan_result.judge_id)

    if batch:
        from . import batch as batch_mod  # local import: batch.py imports this module at load time

        record = batch_mod.run_batch(
            suite, incumbent_id, candidate_id, prices, plan_result, cap_usd, limit, run_name, state_dir, suite_path,
            poll_interval_s=poll_interval_s, max_wait_s=max_wait_s,
        )
        record["effective_config"] = effective_config
        _write_run_json(state_dir, record["run_name"], record)
        return record

    _stack.enter_context(resume_mod.RunLock(Path(state_dir) / "runs" / run_name, _refuse_lock))

    inc_price = prices.rows[incumbent_id]
    cand_price = prices.rows[candidate_id]
    inc_estimates = {e.item_id: e.cost_usd for e in plan_result.estimate.incumbent.items}
    cand_estimates = {e.item_id: e.cost_usd for e in plan_result.estimate.candidate.items}

    api_keys = {provider: os.environ.get(env_key) for provider, env_key in providers.ENV_KEYS.items()}

    judge_ctx = None
    if plan_result.judge_id is not None:
        judge_provider = providers.provider_for_id(plan_result.judge_id)
        judge_ctx = {"id": plan_result.judge_id, "price": prices.rows[plan_result.judge_id], "api_key": api_keys[judge_provider]}

    items = suite.items[:limit] if limit is not None else suite.items
    inc_params = _merged_params(inc_price, suite)
    cand_params = _merged_params(cand_price, suite)
    started_at = _now()
    prog = _Progress()
    prior_items = {}
    carried = {"jb": None, "spent": 0.0}
    attach_id = None
    resumes = None
    meter = cost_mod.CostMeter(cap_usd=cap_usd)

    if prior is not None:
        _check_resumable(prior, resume, suite, incumbent_id, candidate_id, inc_params, cand_params, plan_result, limit, batch_judge, hold_judge_on_errors)
        started_at = prior.get("started_at") or started_at
        prior_items = {rec["id"]: rec for rec in prior.get("items", [])}
        spent = prior.get("spent_usd") or {}
        prog.spent_incumbent = float(spent.get("incumbent") or 0.0)
        prog.spent_candidate = float(spent.get("candidate") or 0.0)
        judge_arms = ("candidate",) if plan_result.reuse is not None else ("incumbent", "candidate")
        arms_todo = [
            it for it in items
            if it.id not in prior_items
            or not _arm_ok(prior_items[it.id]["incumbent"]) and plan_result.reuse is None
            or not _arm_ok(prior_items[it.id]["candidate"])
        ]
        if batch_judge:
            attach_id = _judge_resume_decision(prior)
            prog.spent_judge = 0.0
            if prior.get("judge_batch") is not None:
                # Until run_judge_batch writes its own state, every write keeps the recorded
                # batch and its spend: a kill after the claim write must not forget the id
                # (the next resume would submit a second batch).
                carried["jb"] = prior["judge_batch"]
                carried["spent"] = float(spent.get("judge") or 0.0)
        else:
            prog.spent_judge = float(spent.get("judge") or 0.0)
        meter.spent_usd = prog.spent_incumbent + prog.spent_candidate + prog.spent_judge
        # Remaining estimate: only the calls this resume would make, against the cap that
        # also covers everything already spent (billed tokens on failed calls included).
        if attach_id:
            # Collecting a recorded batch: the arms are kept exactly as recorded (errored ones
            # included, they were skipped when the batch was built) and nothing is re-run.
            unreached = [it.id for it in items if it.id not in prior_items or prior_items[it.id].get("candidate") is None]
            if unreached:
                _refuse_resume(resume, f"a judge batch is recorded but items were never attempted ({unreached[:3]}); cannot collect it")
            for it in items:
                rec = prior_items[it.id]
                prog.result_items.append(rec)
                prog.inc_stats.record(_reply_from_record(rec["incumbent"]))
                prog.cand_stats.record(_reply_from_record(rec["candidate"]))
            arms_todo = []
        remaining = 0.0
        judge_total = plan_result.estimate.judge.cost_usd if plan_result.estimate.judge else 0.0
        judge_todo = 0
        for it in arms_todo:
            rec = prior_items.get(it.id)
            if plan_result.reuse is None and not (rec and _arm_ok(rec["incumbent"])):
                remaining += inc_estimates[it.id]
            if not (rec and _arm_ok(rec["candidate"])):
                remaining += cand_estimates[it.id]
        if batch_judge:
            remaining += 0.0 if attach_id else judge_total
        elif judge_ctx is not None:
            judge_todo = sum(
                1 for it in items
                if it in arms_todo or not _judge_ok((prior_items.get(it.id) or {}).get("judge"), judge_arms)
            )
            remaining += judge_total * judge_todo / max(len(items), 1)
        if meter.spent_usd + remaining > cap_usd:
            raise RunCapExceeded(
                f"already spent ${meter.spent_usd:.4f} plus ${remaining:.4f} for what remains exceeds cap ${cap_usd:.4f}",
                plan_result.estimate,
            )
        prev_resumes = prior.get("resumes") or {"count": 0, "history": []}
        resumed_at = _now()
        entry = {
            "resumed_at": resumed_at, "items_recorded_before": len(prior.get("items", [])),
            "spent_usd_before": spent.get("total"), "prior_status": prior["status"], "prior_stop_reason": prior.get("stop_reason"),
        }
        if (prior.get("run_modes") or {}).get("hold_judge_on_errors") and not hold_judge_on_errors:
            # run_modes.hold_judge_on_errors is recorded false from here on; this keeps the trail.
            entry["hold_released_at"] = resumed_at
        resumes = {"count": prev_resumes["count"] + 1, "history": prev_resumes["history"] + [entry]}

    def _all_items():
        """Finished items, then (on a resume) the stored records of the items not reached yet,
        so a checkpoint or a stop never drops work an earlier run already paid for."""
        tail = [prior_items[it.id] for it in items[len(prog.result_items):] if it.id in prior_items]
        return prog.result_items + tail

    def _write(judge_spent, status, judge_batch=None, stop_reason=None, stop_signal=None):
        if judge_batch is None and carried["jb"] is not None:
            judge_batch, judge_spent = carried["jb"], carried["spent"]
        rec = _build_record(
            run_name, suite, suite_path, incumbent_id, candidate_id, started_at, _now(), cap_usd,
            plan_result, prog.spent_incumbent, prog.spent_candidate, judge_spent, prog.stopped_early, items,
            inc_params, cand_params, _all_items(), {"incumbent": prog.inc_stats, "candidate": prog.cand_stats},
        )
        rec["effective_config"] = effective_config
        rec["timeout_s"] = timeout_s
        rec["status"] = status
        rec["checkpoint_at"] = rec["finished_at"]
        if status == "in_progress":
            rec["finished_at"] = None
        rec["items_recorded"] = len(prog.result_items)
        rec["run_modes"] = {"batch": False, "batch_judge": bool(batch_judge), "hold_judge_on_errors": bool(hold_judge_on_errors), "limit": limit}
        if stop_reason:
            rec["stop_reason"] = stop_reason
        if stop_signal:
            rec["stop_signal"] = stop_signal
        if resumes is not None:
            rec["resumes"] = resumes
        if plan_result.reuse is not None:
            reuse = plan_result.reuse
            rec["reused_incumbent"] = {
                "run": reuse.run_name, "arm_in_source": reuse.arm_in_source,
                "source_run_sha256": reuse.sha256, "source_started_at": reuse.started_at,
            }
        if judge_batch is not None:
            # Its own key, NOT `batch`: `_source_is_batch` reads `batch` to mean the ARMS
            # were batch-priced, which they were not.
            rec["judge_batch"] = judge_batch
        _write_run_json(state_dir, run_name, rec)
        return rec

    def _checkpoint(_prog):
        _write(_prog.spent_judge, "in_progress")

    _write(prog.spent_judge, "in_progress")  # claims the name before the first call

    guard = resume_mod.SignalGuard(graceful=True)
    try:
        with guard:
            if not attach_id:
                _execute_items(
                    items, suite, incumbent_id, candidate_id, inc_price, cand_price, inc_estimates, cand_estimates, meter, api_keys,
                    judge_ctx=None if batch_judge else judge_ctx, timeout_s=timeout_s, reuse=plan_result.reuse,
                    judge_reserve_usd=plan_result.estimate.judge.cost_usd if batch_judge and plan_result.estimate.judge else 0.0,
                    prior=prior_items, progress=prog, checkpoint=_checkpoint, guard=guard, sleep_fn=sleep_fn,
                )
    except BaseException as exc:
        # An abandoning signal or a crash that reached here: every finished item is already
        # in result_items; persist them as a stopped run and let the exception continue.
        _write(prog.spent_judge, "stopped", stop_reason=prog.stop_reason or f"interrupted: {type(exc).__name__}",
               stop_signal=exc.args[0] if isinstance(exc, resume_mod.RunInterrupted) else None)
        raise

    stop_reason = prog.stop_reason
    if guard.requested is not None and (stop_reason is None or stop_reason == "cap"):
        # A signal that arrived during the LAST call: nothing was left to stop, but the run
        # still must not go on to submit a judge batch or call itself complete.
        prog.stopped_early = True
        stop_reason = f"signal:{guard.name}"
    stop_signal = guard.name if guard.requested is not None else None
    hard_stop = stop_reason is not None and (stop_reason == resume_mod.STOP_QUOTA or stop_reason.startswith(("signal:", "error:")))

    judge_arms = ("candidate",) if plan_result.reuse is not None else ("incumbent", "candidate")

    def _arms_all_ok():
        """Every item attempted on both arms, each succeeded or truncated. A reused incumbent's
        carried errors are the source's, deliberately kept, and do not count here."""
        if len(prog.result_items) != len(items):
            return False
        return all(
            r.get("candidate") is not None and _arm_ok(r["candidate"]) and (plan_result.reuse is not None or _arm_ok(r["incumbent"]))
            for r in prog.result_items
        )

    if batch_judge and hold_judge_on_errors and not hard_stop and not attach_id:
        # Opt-in hold: the batch is paid and all-or-nothing, so submit it only over a finished
        # arm phase in which every arm succeeded or was truncated.
        why = "early_stop" if prog.stopped_early else (None if _arms_all_ok() else "arm_errors")
        if why is not None:
            return _write(
                prog.spent_judge, "stopped", {"status": "not_submitted", "reason": why}, stop_reason="judge_batch_not_submitted",
            )

    if batch_judge and not hard_stop:
        from . import batch as batch_mod

        # Arms go to run.json inside run_judge_batch's first persist, before any submit.
        final = {}

        def _persist(judge_state, judge_spent, stopped):
            carried["jb"] = None  # run_judge_batch now owns the judge state
            st = judge_state.get("status")
            if st in _JB_STOPPED or stopped or prog.stopped_early:
                status = "stopped"
            elif st in _JB_DONE:
                status = "complete" if _arms_all_ok() else "stopped"
            else:
                status = "in_progress"
            if status == "stopped" and st in _JB_DONE:
                # The batch ended: it judged what it judged and a resume refuses to submit
                # another, so this stop is final, not resumable.
                reason = "cap_judged" if (stopped or prog.stopped_early) else "arm_errors_judged"
            elif stopped or prog.stopped_early:
                reason = stop_reason
            else:
                reason = f"judge_batch:{st}" if status == "stopped" else None
            if prog.stopped_early or stopped:
                prog.stopped_early = True
            final["record"] = _write(judge_spent, status, judge_state, stop_reason=reason)

        with resume_mod.SignalGuard(graceful=False):
            batch_mod.run_judge_batch(
                suite, items, prog.result_items, plan_result.judge_id, prices.rows[plan_result.judge_id].at_batch(), plan_result.reuse,
                meter, Path(state_dir) / "runs" / run_name, run_name, _persist,
                poll_interval_s=poll_interval_s, max_wait_s=max_wait_s, attach_id=attach_id, sleep_fn=sleep_fn,
            )
        return final["record"]

    # A run is complete only when every item has both arms succeeded (or truncated) and the
    # judge stage is done; anything else is `stopped`, resumable, and never a verdict input.
    # "Done" means a judge record exists for each judged arm. A judge error is a result the
    # report counts (it makes the verdict INCOMPLETE), not an unfinished stage.
    judge_done = judge_ctx is None or all(all(arm in (r.get("judge") or {}) for arm in judge_arms) for r in prog.result_items)
    if prog.stopped_early:
        status = "stopped"
    elif not _arms_all_ok():
        status, stop_reason = "stopped", stop_reason or "arm_errors"
    elif not judge_done:
        status, stop_reason = "stopped", stop_reason or "judge_errors"
    else:
        status = "complete"
    record = _write(prog.spent_judge, status, stop_reason=stop_reason, stop_signal=stop_signal)
    if prog.unexpected is not None:
        raise prog.unexpected
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


def _item_record(item, incumbent, incumbent_cost, candidate, candidate_cost, judge_record=None, incumbent_record=None, candidate_record=None):
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
        "candidate": candidate_record if candidate_record is not None else (None if candidate is None else _arm_record(candidate, candidate_cost)),
    }
    if judge_record is not None:
        record["judge"] = judge_record
    return record


def _write_run_json(state_dir, run_name, record):
    run_dir = Path(state_dir) / "runs" / run_name
    run_dir.mkdir(parents=True, exist_ok=True)
    target = run_dir / "run.json"
    tmp = run_dir / "run.json.tmp"
    with open(tmp, "w") as f:
        f.write(json.dumps(record, indent=2))
        f.flush()
        os.fsync(f.fileno())
    tmp.replace(target)
    try:
        dir_fd = os.open(run_dir, os.O_RDONLY)
        try:
            os.fsync(dir_fd)
        finally:
            os.close(dir_fd)
    except OSError:
        pass  # a filesystem that cannot fsync a directory: the file itself is already synced


def load_run(state_dir, run_name) -> dict:
    path = Path(state_dir) / "runs" / run_name / "run.json"
    return json.loads(path.read_text())
