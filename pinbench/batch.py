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


# One place for the retry policy: attempts per call (the first try included) and the backoff
# base. The delay before retry k (k = 1, 2, ...) is RETRY_BACKOFF_BASE_S * 2 ** (k - 1), times
# a jitter factor in [0.5, 1.0]. Tests inject `sleep_fn` and `rand_fn`.
RETRY_ATTEMPTS = 5
RETRY_BACKOFF_BASE_S = 2.0
# A create reconciliation lists batches created since the attempt started, minus this slack
# for clock skew between this machine and the API.
RECONCILE_SKEW_S = 120


class JudgeBatchError(RuntimeError):
    """The judge batch could not be completed (retries ran out, or a create's outcome is
    unknown). run.json (arms and judge state) is already persisted; the message names the
    batch id when one is known."""


class BatchRetriesExhausted(RuntimeError):
    """`with_retry` gave up on a transient failure; `__cause__` is the last BatchError."""


def _backoff_s(attempt, rand_fn):
    return RETRY_BACKOFF_BASE_S * 2 ** (attempt - 1) * (0.5 + 0.5 * rand_fn())


def with_retry(fn, sleep_fn, rand_fn=None, attempts=None):
    """Call `fn()` up to `attempts` times, retrying only a transient BatchError (429, 5xx,
    529, timeouts, connection errors). Any other exception, including a non-transient 4xx
    BatchError, propagates at once. Raises BatchRetriesExhausted when the attempts run out."""
    import random

    from .providers import anthropic as anthropic_mod

    rand_fn = rand_fn or random.random
    attempts = attempts or RETRY_ATTEMPTS
    for attempt in range(1, attempts + 1):
        try:
            return fn()
        except anthropic_mod.BatchError as exc:
            if not exc.transient:
                raise
            if attempt == attempts:
                raise BatchRetriesExhausted(f"{exc} (gave up after {attempts} attempts)") from exc
            sleep_fn(_backoff_s(attempt, rand_fn))


class SubmitOutcomeUnknown(RuntimeError):
    """A create timed out or failed after the server may have accepted it, and the batch list
    could not settle whether it did. Retrying could submit (and bill) a second batch."""


def create_with_retry(requests, api_key, sleep_fn, rand_fn=None, now_fn=None, attempts=None):
    """Create a Message Batch, retrying transient failures without ever submitting twice.

    The Message Batches docs describe no idempotency key for create, so a blind retry after
    an unknown outcome could create a SECOND billed batch. What the docs do offer: "You can
    list all Message Batches in your Workspace using the list endpoint" and "Ensure each
    request in the batch has a unique `custom_id`". A listed batch carries `created_at` and
    `request_counts` but not its custom_ids, so the match is by creation time (since this
    attempt started, minus clock-skew slack) and total request count.

    - HTTP 429: the request was rejected before any batch existed; retry directly.
    - Any other transient failure (5xx, 529, timeout, dropped connection): the outcome is
      unknown, so list recent batches first. Exactly one match: that is our batch, adopt it.
      No match: nothing was created, retry. Two or more matches, or the list itself failing:
      raise SubmitOutcomeUnknown and do NOT retry (a refused retry costs a re-run; a wrong
      one costs a second batch).
    - Any other 4xx propagates unchanged.
    """
    import datetime as _dt
    import random

    from .providers import anthropic as anthropic_mod

    rand_fn = rand_fn or random.random
    now_fn = now_fn or (lambda: _dt.datetime.now(_dt.timezone.utc))
    attempts = attempts or RETRY_ATTEMPTS

    def parse(ts):
        return _dt.datetime.fromisoformat(str(ts).replace("Z", "+00:00"))

    for attempt in range(1, attempts + 1):
        since = now_fn() - _dt.timedelta(seconds=RECONCILE_SKEW_S)
        try:
            return anthropic_mod.create_batch(requests, api_key)
        except anthropic_mod.BatchError as exc:
            if not exc.transient:
                raise
            if exc.status != 429:
                try:
                    listed = anthropic_mod.list_batches(api_key)
                except anthropic_mod.BatchError as list_exc:
                    raise SubmitOutcomeUnknown(
                        f"create_batch failed ({exc}) and the batch list could not be read ({list_exc}); "
                        "not retrying, because the batch may exist"
                    ) from exc
                matches = [
                    b for b in listed
                    if parse(b.get("created_at") or "1970-01-01T00:00:00Z") >= since
                    and sum((b.get("request_counts") or {}).values()) == len(requests)
                ]
                if len(matches) == 1:
                    return matches[0]
                if len(matches) > 1:
                    raise SubmitOutcomeUnknown(
                        f"create_batch failed ({exc}) and {len(matches)} recent batches of {len(requests)} requests exist "
                        f"({', '.join(str(b.get('id')) for b in matches)}); not retrying, cannot tell which is ours"
                    ) from exc
            if attempt == attempts:
                raise BatchRetriesExhausted(f"{exc} (gave up after {attempts} attempts)") from exc
            sleep_fn(_backoff_s(attempt, rand_fn))


JUDGE_PENDING_NOTE = "not judged: the judge batch has not completed"


def run_judge_batch(
    suite, items, result_items, judge_id, judge_price, reuse, meter, run_dir, run_name, persist,
    poll_interval_s=30, max_wait_s=86400, sleep_fn=None, print_fn=None, rand_fn=None, now_fn=None,
):
    """`pinbench run --batch-judge`: judge the finished arms' outputs as ONE Message Batch.

    The arms already ran synchronously and `result_items` (the `runner._item_record` dicts)
    holds their outputs. `judge_price` is the judge row already at the batch price
    (`.at_batch()`); requests come from `build_judge_requests`, so they are the same bodies
    a sync judge call sends, and skips come from `judge.skip_reason`, so a skipped arm is
    recorded and never sent. With `reuse`, only the candidate is judged and the incumbent's
    stored judge record is carried over.

    Order: (1) every pending slot gets an honest "not judged" placeholder error, (2) the cap
    is checked against the batch's whole estimate (a batch cannot be stopped midway), (3)
    `persist(judge_state, spent_judge, stopped_early)` writes run.json BEFORE the submit and
    again once the id is known, so once the arms are done a crash, a timeout or a failed
    batch never loses their paid outputs (a crash DURING the arm phase still loses it: the
    arms are written only after they finish), (4) submit, write batch.json, wait. A batch
    that outlasts `max_wait_s` is left running and its slots keep the placeholder error (the
    verdict is INCOMPLETE); nothing is guessed.

    Transient failures (429, 5xx, 529, timeouts, dropped connections) on create, poll and
    results are retried `RETRY_ATTEMPTS` times with exponential backoff (`with_retry`); a
    create whose outcome is unknown is reconciled first (`create_with_retry`). A batch whose
    outcome or spend is unknown (timed out, polling or results failed after submit, submit
    outcome unknown) records its judge spend as the reserved estimate, never 0, because it
    may still bill; cost.py charges an unknown outcome at least its estimate for the same
    reason. Retries running out raises `JudgeBatchError` after persisting.
    Returns (spent_judge, stopped_early); mutates `result_items`.
    """
    import os

    from . import cost as cost_mod
    from . import judge as judge_mod
    from . import providers as providers_mod
    from . import runner as runner_mod
    from .providers import anthropic as anthropic_mod

    if poll_interval_s < 1:
        raise ValueError(f"poll interval must be at least 1 second, got {poll_interval_s}")
    sleep_fn = sleep_fn or time.sleep
    print_fn = print_fn or print
    api_key = os.environ.get(providers_mod.ENV_KEYS["anthropic"])

    state = {"id": None, "price_multiplier": judge_price.batch_multiplier, "status": "pending"}
    only_arms = ("candidate",) if reuse is not None else None
    pending: list = []
    slots: dict = {}  # (index, arm) -> the judge record dict to fill
    for index, rec in enumerate(result_items):
        if rec["candidate"] is None:
            continue  # the arms stopped before this item's candidate: nothing to judge
        item = items[index]
        order = judge_mod.pick_call_order()
        if only_arms is not None:
            order = [arm for arm in order if arm in only_arms]
        jr = {"call_order": order}
        if reuse is not None:
            jr["incumbent"] = reuse.items[item.id][1]
        rec["judge"] = jr
        for arm in order:
            reply = runner_mod._reply_from_record(rec[arm])
            reason = judge_mod.skip_reason(reply)
            if reason is not None:
                jr[arm] = judge_mod._outcome_record(judge_mod.JudgeOutcome(hit=[], extra=0, notes="", skipped=reason), 0.0)
            else:
                outcome = judge_mod.JudgeOutcome(hit=[], extra=0, notes="", error=JUDGE_PENDING_NOTE)
                jr[arm] = judge_mod._outcome_record(outcome, 0.0)
                pending.append((index, item, arm, reply.output))
                slots[(index, arm)] = jr

    def _mark(note):
        for (_index, arm), jr in slots.items():
            if jr[arm]["error"] is not None:
                jr[arm]["error"] = note

    if not pending:
        state["status"] = "nothing_to_judge"
        persist(state, 0.0, False)
        return 0.0, False

    total_est = sum(cost_mod.estimate_judge_call_cost(suite, item, judge_price) for (_, item, _, _) in pending)
    state["reserved_usd"] = total_est
    if not meter.check(total_est):
        # arms spend + the whole judge batch estimate would pass the cap, and a batch cannot
        # be stopped midway, so none of it is sent.
        state["status"] = "not_submitted_cap"
        _mark("not judged: judge batch not submitted, its estimate would exceed the cap")
        persist(state, 0.0, True)
        return 0.0, True

    requests = build_judge_requests(pending, suite, judge_id, judge_price)
    custom_ids = [r["custom_id"] for r in requests]
    persist(state, 0.0, False)  # arms are on disk before anything is submitted

    def _unresolved(status, note, exc, batch_id=None):
        """A batch whose outcome or spend is unknown: keep the arms, mark the slots not
        judged, record the reserved estimate as the judge spend (it may still bill), raise."""
        state["status"] = status
        state["spend_basis"] = "reserved_estimate"
        _mark(note)
        persist(state, total_est, False)
        where = f"batch {batch_id} " if batch_id else ""
        raise JudgeBatchError(f"judge {where}{status}: {exc}") from exc

    try:
        batch_obj = create_with_retry(requests, api_key, sleep_fn, rand_fn, now_fn)
    except SubmitOutcomeUnknown as exc:
        _unresolved("submit_outcome_unknown", f"not judged: judge batch submission outcome unknown: {exc}", exc)
    except Exception as exc:  # noqa: BLE001 - record the failed submit, then re-raise
        # a definite failure: nothing was created, so nothing is spent
        state["status"] = "submit_failed"
        _mark(f"not judged: judge batch submission failed: {exc}")
        persist(state, 0.0, False)
        raise
    batch_id = batch_obj["id"]
    print_fn(f"submitted judge batch {batch_id} ({len(requests)} requests)")
    state.update({"id": batch_id, "status": "submitted"})
    _write_batch_json(Path(run_dir), {"run_name": run_name, "arms": None, "judge": {"id": batch_id}})
    _mark(f"not judged: judge batch {batch_id} has not completed")
    persist(state, 0.0, False)

    try:
        final, polls = wait_for_batch(
            lambda: with_retry(lambda: anthropic_mod.get_batch(batch_id, api_key), sleep_fn, rand_fn),
            batch_id, poll_interval_s, max_wait_s, sleep_fn, print_fn,
        )
    except MaxWaitExceeded as exc:
        print_fn("")
        print(f"warning: {exc}", file=sys.stderr)
        state["status"] = "timed_out"
        state["spend_basis"] = "reserved_estimate"
        _mark(f"not judged: judge batch {batch_id} still running after {max_wait_s}s")
        persist(state, total_est, False)
        return total_est, False
    except (BatchRetriesExhausted, anthropic_mod.BatchError) as exc:
        _unresolved("poll_failed", f"not judged: polling judge batch {batch_id} failed: {exc}", exc, batch_id)
    except KeyboardInterrupt:
        print(
            f"\nbatch {batch_id} is still running (results kept 29 days). "
            f"Cancel: curl -X POST https://api.anthropic.com/v1/messages/batches/{batch_id}/cancel "
            "-H \"x-api-key: $ANTHROPIC_API_KEY\" -H \"anthropic-version: 2023-06-01\". "
            "Re-running the same command re-submits (and pays again).",
            file=sys.stderr,
        )
        raise
    print_fn("")

    try:
        results = with_retry(
            lambda: collect_results(anthropic_mod, providers_mod, batch_id, api_key, custom_ids), sleep_fn, rand_fn,
        )
    except (BatchRetriesExhausted, anthropic_mod.BatchError) as exc:
        _unresolved("results_failed", f"not judged: fetching results of judge batch {batch_id} failed: {exc}", exc, batch_id)
    spent_judge = 0.0
    result_types = {}
    for index, item, arm, _output in pending:
        cid = custom_id(index, f"judge-{arm}")
        reply, rtype = results[cid]
        outcome = judge_mod.interpret_reply(reply, item, suite)
        est = cost_mod.estimate_judge_call_cost(suite, item, judge_price)
        cost_usd = meter.charge(cost_mod.actual_call_cost(reply, judge_price, est), est)
        spent_judge += cost_usd
        slots[(index, arm)][arm] = judge_mod._outcome_record(outcome, cost_usd)
        result_types[cid] = {"item": item.id, "arm": arm, "type": rtype}

    state.update({
        "status": "ended", **_batch_json_entry(final), "id": batch_id, "results": result_types,
        "poll": {"interval_s": poll_interval_s, "max_wait_s": max_wait_s, "polls": polls},
        "cap_exceeded_after_results": meter.spent_usd > meter.cap_usd,
    })
    if state["cap_exceeded_after_results"]:
        print(
            f"warning: actual batch spend ${meter.spent_usd:.4f} exceeded cap ${meter.cap_usd:.2f} after the judge "
            "batch came back; the estimate is a ceiling, nothing remained to stop",
            file=sys.stderr,
        )
    state["status"] = "ended"
    persist(state, spent_judge, False)
    return spent_judge, False
