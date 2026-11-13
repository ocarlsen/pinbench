"""Anthropic Message Batches control flow for `pinbench run --batch`.

Refuses to do: guess at a result the vendor's own JSONL line doesn't name (see
`providers.anthropic.reply_from_batch_result`); let a judge batch outrun the remaining cap
without recording why (`stopped_early`); lose a submitted batch's id if the process is
interrupted while waiting (`batch.json`, written before the first poll). Never calls a
non-Anthropic adapter -- `runner.plan`'s Anthropic-only check already refused a mixed run
before this module is ever imported.

Batch mode submits every item for both arms (and, afterward, the judge) in ONE request each,
so the per-item cap check the sync path does before every call cannot apply here: the cap
gates submission only (`runner.plan`'s pre-spend estimate, on `.at_batch()` discounted
rows). Everything from here on is bookkeeping: build requests, submit, poll, map results
back by `custom_id` (never by line position -- the vendor states results may be reordered),
charge the meter, and report whether the actual spend still fit after the fact.
"""

from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from typing import Optional


class MaxWaitExceeded(RuntimeError):
    """Raised when a batch has not reached processing_status == "ended" within max_wait_s.

    The batch is left running (never canceled): the vendor's own words, "most batches
    completing within 1 hour" / "Batches expire if processing does not complete within 24
    hours", mean a longer wait is usually just a wait, and canceling a batch that is about
    to finish would throw away paid-for work. `batch.json` is already on disk (written
    before the first poll), so the ids are not lost even though this exception carries them
    in its message too, for whoever reads stderr.
    """


def custom_id(index: int, arm: str) -> str:
    """`f"{index:04d}-{arm}"` for an arm request, or pass `arm="judge-incumbent"` /
    `"judge-candidate"` for a judge request (`f"{index:04d}-judge-{side}"`). The index, not
    the item id, is the key: item ids may contain `.`, which custom_id's
    `^[a-zA-Z0-9_-]{1,64}$` pattern forbids.
    """
    return f"{index:04d}-{arm}"


def _write_batch_json(run_dir: Path, state: dict) -> None:
    run_dir.mkdir(parents=True, exist_ok=True)
    target = run_dir / "batch.json"
    tmp = run_dir / "batch.json.tmp"
    tmp.write_text(json.dumps(state, indent=2))
    tmp.replace(target)


def build_arm_requests(items, suite, incumbent_id, candidate_id, inc_price, cand_price):
    """One batch request per item per arm. Returns (requests, inc_params, cand_params) --
    the merged params are returned too so the caller can record them in run.json exactly as
    the sync path's `_recorded_params` does."""
    from . import runner as runner_mod
    from .providers import anthropic as anthropic_mod

    inc_params = runner_mod._merged_params(inc_price, suite)
    cand_params = runner_mod._merged_params(cand_price, suite)
    requests = []
    for index, item in enumerate(items):
        for arm, model_id, params in (
            ("incumbent", incumbent_id, inc_params),
            ("candidate", candidate_id, cand_params),
        ):
            body = anthropic_mod.build_params(model_id, suite.task, item.input, suite.max_output_tokens, params)
            requests.append({"custom_id": custom_id(index, arm), "params": body})
    return requests, inc_params, cand_params


def build_judge_requests(pending, suite, judge_id, judge_price):
    """`pending` is a list of (index, item, arm, output_text) for every (item, arm) that
    survived `judge.skip_reason`. One batch request per entry."""
    from . import judge as judge_mod

    requests = []
    for index, item, arm, output_text in pending:
        body = judge_mod.judge_request_params(judge_id, item, suite, output_text, judge_price)
        requests.append({"custom_id": custom_id(index, f"judge-{arm}"), "params": body})
    return requests


def wait_for_batch(get_batch_fn, batch_id, poll_interval_s, max_wait_s, sleep_fn, print_fn):
    """Poll `get_batch_fn()` (a zero-arg callable so tests can script a status sequence)
    until `processing_status == "ended"`. Prints one progress line per poll. Raises
    MaxWaitExceeded, never cancels, past `max_wait_s` elapsed. Returns (batch_obj, polls).
    """
    if poll_interval_s < 1:
        # elapsed advances by the interval; a zero or negative one never reaches max_wait_s.
        raise ValueError(f"poll interval must be at least 1 second, got {poll_interval_s}")
    elapsed = 0
    polls = 0
    while True:
        batch_obj = get_batch_fn()
        polls += 1
        counts = batch_obj.get("request_counts") or {}
        print_fn(
            f"\r{elapsed}s processing {counts.get('processing', 0)} succeeded {counts.get('succeeded', 0)} "
            f"errored {counts.get('errored', 0)}"
        )
        if batch_obj.get("processing_status") == "ended":
            return batch_obj, polls
        if elapsed >= max_wait_s:
            raise MaxWaitExceeded(
                f"batch {batch_id} is still running after {max_wait_s}s (results kept 29 days). "
                f"Cancel: curl -X POST https://api.anthropic.com/v1/messages/batches/{batch_id}/cancel "
                "-H \"x-api-key: $ANTHROPIC_API_KEY\" -H \"anthropic-version: 2023-06-01\". "
                "Re-running the same command re-submits (and pays again)."
            )
        sleep_fn(poll_interval_s)
        elapsed += poll_interval_s


def collect_results(anthropic_mod, providers_mod, batch_id, api_key, expected_custom_ids):
    """Fetch and parse one batch's results, keyed by custom_id (never by line position --
    the vendor states results "may be given out of request order"). A custom_id in
    `expected_custom_ids` that is absent from the results file becomes an arm error "batch
    result missing", never silently dropped. Returns {custom_id: (Reply, result_type)};
    result_type is "missing" for that case.
    """
    lines = anthropic_mod.fetch_results(batch_id, api_key)
    by_id = {}
    for line in lines:
        cid = line.get("custom_id")
        if cid in by_id:
            raise ValueError(f"batch {batch_id}: duplicate custom_id {cid!r} in results; refusing to guess which is right")
        result = line.get("result") or {}
        by_id[cid] = (anthropic_mod.reply_from_batch_result(line), result.get("type"))

    out = {}
    for cid in expected_custom_ids:
        if cid in by_id:
            out[cid] = by_id[cid]
        else:
            out[cid] = (
                providers_mod.Reply(
                    output=None, input_tokens=None, output_tokens=None, latency_ms=None,
                    error="batch result missing", status_code=400,
                ),
                "missing",
            )
    return out


def _batch_json_entry(batch_obj: dict) -> dict:
    return {
        "id": batch_obj.get("id"),
        "created_at": batch_obj.get("created_at"),
        "ended_at": batch_obj.get("ended_at"),
        "processing_status": batch_obj.get("processing_status"),
        "request_counts": batch_obj.get("request_counts"),
    }


def run_batch(
    suite, incumbent_id, candidate_id, prices, plan_result, cap_usd, limit, name, state_dir, suite_path,
    poll_interval_s=30, max_wait_s=86400, sleep_fn=None, print_fn=None,
):
    """Submit both arms as one Message Batch, wait, collect, then (if the suite has a judge
    and the cap still has room) submit one judge batch the same way. Returns the same record
    shape `runner._build_record` returns, plus a `"batch"` key; the caller (`runner.run`)
    sets `effective_config` and writes run.json exactly as the sync path does.

    Raises whatever `providers.anthropic.create_batch`/`get_batch`/`fetch_results` raise
    (BatchError on a non-2xx response), or `MaxWaitExceeded` if a batch never reaches
    processing_status == "ended" within `max_wait_s`. In both cases nothing further is
    written here; `batch.json` (written right after the arm batch is created, before the
    first poll) is the only record on disk of what was submitted.
    """
    import datetime as _dt
    import os

    from . import cost as cost_mod
    from . import judge as judge_mod
    from . import providers as providers_mod
    from . import runner as runner_mod
    from .providers import anthropic as anthropic_mod

    if poll_interval_s < 1:
        # Refuse before any spend: the wait loop runs only after the batch is submitted.
        raise ValueError(f"poll interval must be at least 1 second, got {poll_interval_s}")

    sleep_fn = sleep_fn or time.sleep
    print_fn = print_fn or print

    api_key = os.environ.get(providers_mod.ENV_KEYS["anthropic"])

    inc_price = prices.rows[incumbent_id].at_batch()
    cand_price = prices.rows[candidate_id].at_batch()
    judge_id = plan_result.judge_id
    judge_price = prices.rows[judge_id].at_batch() if judge_id is not None else None

    items = suite.items[:limit] if limit is not None else suite.items
    run_name = name or f"{suite.name}-{_dt.datetime.now(_dt.timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
    started_at = _dt.datetime.now(_dt.timezone.utc).isoformat().replace("+00:00", "Z")
    run_dir = Path(state_dir) / "runs" / run_name

    inc_estimates = {e.item_id: e.cost_usd for e in plan_result.estimate.incumbent.items}
    cand_estimates = {e.item_id: e.cost_usd for e in plan_result.estimate.candidate.items}

    # --- 1. arms: build, submit, write batch.json BEFORE the first poll, wait, collect ---
    requests, inc_params, cand_params = build_arm_requests(items, suite, incumbent_id, candidate_id, inc_price, cand_price)
    arm_custom_ids = [r["custom_id"] for r in requests]

    arm_batch_obj = anthropic_mod.create_batch(requests, api_key)
    arm_batch_id = arm_batch_obj["id"]
    print_fn(f"submitted batch {arm_batch_id} ({len(requests)} requests)")

    batch_state = {"run_name": run_name, "arms": {"id": arm_batch_id}, "judge": None}
    _write_batch_json(run_dir, batch_state)

    try:
        arm_batch_final, arm_polls = wait_for_batch(
            lambda: anthropic_mod.get_batch(arm_batch_id, api_key), arm_batch_id, poll_interval_s, max_wait_s, sleep_fn, print_fn,
        )
    except KeyboardInterrupt:
        print(
            f"\nbatch {arm_batch_id} is still running (results kept 29 days). "
            f"Cancel: curl -X POST https://api.anthropic.com/v1/messages/batches/{arm_batch_id}/cancel "
            "-H \"x-api-key: $ANTHROPIC_API_KEY\" -H \"anthropic-version: 2023-06-01\". "
            "Re-running the same command re-submits (and pays again).",
            file=sys.stderr,
        )
        raise
    print_fn("")  # end the \r progress line

    meter = cost_mod.CostMeter(cap_usd=cap_usd)
    arm_results = collect_results(anthropic_mod, providers_mod, arm_batch_id, api_key, arm_custom_ids)

    inc_stats = runner_mod._ArmStats()
    cand_stats = runner_mod._ArmStats()
    arm_replies: dict = {}
    arm_costs: dict = {}
    batch_results_record: dict = {}
    spent_incumbent = 0.0
    spent_candidate = 0.0

    for index, item in enumerate(items):
        inc_cid, cand_cid = custom_id(index, "incumbent"), custom_id(index, "candidate")
        inc_reply, inc_type = arm_results[inc_cid]
        cand_reply, cand_type = arm_results[cand_cid]
        inc_stats.record(inc_reply)
        cand_stats.record(cand_reply)
        inc_cost = meter.charge(cost_mod.actual_call_cost(inc_reply, inc_price, inc_estimates[item.id]), inc_estimates[item.id])
        cand_cost = meter.charge(cost_mod.actual_call_cost(cand_reply, cand_price, cand_estimates[item.id]), cand_estimates[item.id])
        spent_incumbent += inc_cost
        spent_candidate += cand_cost
        arm_replies[index] = {"incumbent": inc_reply, "candidate": cand_reply}
        arm_costs[index] = {"incumbent": inc_cost, "candidate": cand_cost}
        batch_results_record[inc_cid] = {"item": item.id, "arm": "incumbent", "type": inc_type}
        batch_results_record[cand_cid] = {"item": item.id, "arm": "candidate", "type": cand_type}

    # --- 2. judge (if any): skip per arm exactly as judge.run_judge_stage would, check the
    # remaining cap on the SUM of pending judge estimates (one batch, not call-by-call), then
    # submit/wait/collect the same way ---
    judge_records: dict = {}
    judge_pending: list = []
    for index, item in enumerate(items):
        if judge_id is None:
            judge_records[index] = None
            continue
        order = judge_mod.pick_call_order()
        jr = {"call_order": order}
        judge_records[index] = jr
        for arm in order:
            reply = arm_replies[index][arm]
            reason = judge_mod.skip_reason(reply)
            if reason is not None:
                outcome = judge_mod.JudgeOutcome(hit=[], extra=0, notes="", error=None, skipped=reason)
                jr[arm] = judge_mod._outcome_record(outcome, 0.0)
            else:
                judge_pending.append((index, item, arm, reply.output))

    stopped_early = False
    spent_judge = 0.0
    judge_batch_final = None
    judge_polls = 0

    if judge_id is not None and judge_pending:
        total_est = sum(cost_mod.estimate_judge_call_cost(suite, item, judge_price) for (_, item, _, _) in judge_pending)
        if not meter.check(total_est):
            # Reachable only if arm actuals beat the ceiling estimate enough that the judge
            # no longer fits; mirrors the sync meter checking before the call. No judge batch
            # is sent, and `judge_records` keeps only whatever was set above (skips); every
            # pending arm's record entry is left unset, same partial-record behavior as the
            # sync path's `judge.run_judge_stage` stopping mid-item.
            stopped_early = True
        else:
            judge_requests = build_judge_requests(judge_pending, suite, judge_id, judge_price)
            judge_custom_ids = [r["custom_id"] for r in judge_requests]
            judge_batch_obj = anthropic_mod.create_batch(judge_requests, api_key)
            judge_batch_id = judge_batch_obj["id"]
            print_fn(f"submitted judge batch {judge_batch_id} ({len(judge_requests)} requests)")

            batch_state["judge"] = {"id": judge_batch_id}
            _write_batch_json(run_dir, batch_state)

            try:
                judge_batch_final, judge_polls = wait_for_batch(
                    lambda: anthropic_mod.get_batch(judge_batch_id, api_key), judge_batch_id, poll_interval_s, max_wait_s, sleep_fn, print_fn,
                )
            except KeyboardInterrupt:
                print(
                    f"\nbatch {judge_batch_id} is still running (results kept 29 days). "
                    f"Cancel: curl -X POST https://api.anthropic.com/v1/messages/batches/{judge_batch_id}/cancel "
                    "-H \"x-api-key: $ANTHROPIC_API_KEY\" -H \"anthropic-version: 2023-06-01\". "
                    "Re-running the same command re-submits (and pays again).",
                    file=sys.stderr,
                )
                raise
            print_fn("")

            judge_results = collect_results(anthropic_mod, providers_mod, judge_batch_id, api_key, judge_custom_ids)
            for index, item, arm, output_text in judge_pending:
                cid = custom_id(index, f"judge-{arm}")
                reply, rtype = judge_results[cid]
                outcome = judge_mod.interpret_reply(reply, item, suite)
                est = cost_mod.estimate_judge_call_cost(suite, item, judge_price)
                cost_usd = meter.charge(cost_mod.actual_call_cost(reply, judge_price, est), est)
                spent_judge += cost_usd
                judge_records[index][arm] = judge_mod._outcome_record(outcome, cost_usd)
                batch_results_record[cid] = {"item": item.id, "arm": arm, "type": rtype}

    # --- 3. assemble run.json ---
    result_items = []
    for index, item in enumerate(items):
        result_items.append(
            runner_mod._item_record(
                item,
                arm_replies[index]["incumbent"], arm_costs[index]["incumbent"],
                arm_replies[index]["candidate"], arm_costs[index]["candidate"],
                judge_records[index],
            )
        )

    finished_at = _dt.datetime.now(_dt.timezone.utc).isoformat().replace("+00:00", "Z")
    record = runner_mod._build_record(
        run_name, suite, suite_path, incumbent_id, candidate_id, started_at, finished_at, cap_usd,
        plan_result, spent_incumbent, spent_candidate, spent_judge, stopped_early, items, inc_params, cand_params,
        result_items, {"incumbent": inc_stats, "candidate": cand_stats},
    )

    price_multiplier = {incumbent_id: inc_price.batch_multiplier, candidate_id: cand_price.batch_multiplier}
    if judge_id is not None:
        price_multiplier[judge_id] = judge_price.batch_multiplier

    cap_exceeded_after_results = meter.spent_usd > cap_usd
    if cap_exceeded_after_results:
        print_fn(
            f"warning: actual batch spend ${meter.spent_usd:.4f} exceeded cap ${cap_usd:.2f} after results came "
            "back; the estimate is a ceiling, not everything fits it, and nothing remains to stop",
            file=sys.stderr,
        )

    record["batch"] = {
        "arms": _batch_json_entry(arm_batch_final),
        "judge": _batch_json_entry(judge_batch_final) if judge_batch_final is not None else None,
        "results": batch_results_record,
        "price_multiplier": price_multiplier,
        "poll": {"interval_s": poll_interval_s, "max_wait_s": max_wait_s, "polls": arm_polls + judge_polls},
        "cap_exceeded_after_results": cap_exceeded_after_results,
    }
    return record
