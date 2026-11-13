# Changelog

## Unreleased

`run` survives a crash or an exhausted quota, and can be resumed. A sync run now writes `run.json`
after every item (atomic, `status: in_progress`), so a kill at hour 5 keeps every finished item;
before, it wrote once at the end. SIGTERM, SIGHUP and SIGINT let the call in flight return, persist
and exit 143, 129 or 130 (a second signal abandons the call). `run` refuses a `--name` whose
`run.json` exists (it silently overwrote it) unless `--resume NAME` is given; default names gain a
random suffix. An arm or sync judge call that is HTTP 429 or quota-exhausted is retried with bounded
backoff (billed tokens on failed attempts are charged), then the run stops with `stop_reason`
`quota_or_rate_limit`, prints its exact resume command and exits 6. `run --resume NAME` continues a
stopped or crashed run: it refuses unless suite, arms, request params, judge, reuse source and batch
flags match what the run recorded, keeps finished arms and reused-incumbent slots, re-runs errored
and never-attempted arms (truncated outputs are kept, not re-run), and counts spend so far against
`--cap`. A `--batch-judge` run resumes judge-only: a recorded batch id is polled and collected
(arms kept as recorded), never re-submitted; an unknown submit outcome is refused (check the
Anthropic console). A run is `complete` only when every arm succeeded or truncated; otherwise it is
`stopped` and `report` writes no `verdict.json` (an `in_progress` checkpoint exits 1). An abandoned
call is charged at its estimate, `run.lock` stops two processes sharing a run, and `run.json` is
fsynced. Exit codes 6 and 129/130/143 are new. The report header notes a resumed or in-progress run.

`run --batch-judge` / `plan --batch-judge`: run the arms synchronously, then send every judge call
as one Anthropic Message Batch at the judge row's `batch_multiplier`. A judged run with a
non-Anthropic arm (Gemini) cannot use `--batch`, which is all-or-nothing and Anthropic-only, so its
Anthropic judge paid full price; this halves just the judge. Refuses before any spend with
`--batch`, a suite with no judge, a non-Anthropic judge, or a judge row with no `batch_multiplier`.
Judge request bodies come from the same builder as a sync judge call, and arms the judge skips
(errored, empty, truncated) are recorded as skipped and not sent. The cap is checked against arms
spend plus the whole batch estimate before submitting; actual token cost is charged at the batch
price after the results arrive. The arms are written to `run.json` before the batch is submitted,
the id goes to `batch.json`, and a batch that outlasts `--max-wait` is left running with the judge
records marked "not judged" (the verdict is INCOMPLETE). `run.json` records `judge_batch`, not
`batch`, and `--reuse-incumbent` of such a run rescales the judge cost by
`judge_batch.price_multiplier` while treating its arm cost as sync-priced. Works with
`--reuse-incumbent` (only the candidate's judge calls are batched). The report header notes
`judge: batched (id ...)`.
Batch API calls (create, poll, results) retry transient failures (429, 5xx, 529, timeouts,
connection errors) with bounded exponential backoff, never other 4xx or a malformed result. A
create is retried only after a 429 (no batch was created); any other transient create failure is
an unknown outcome, never re-sent or adopted, recorded as `submit_outcome_unknown` at the
reserved estimate with exit 1 (check the Anthropic console, then `run --resume`). A judge batch that times out now exits 5 and records its
spend as the reserved estimate, not 0. The arm phase reserves the judge batch's estimate under
the cap, so the arms stop early instead of stranding the judging.

The cost meter no longer under-charges an errored reply that carries billed tokens (issue #20,
follow-up to the Gemini continuation change). `cost.actual_call_cost` now takes the call's
estimate and, for an errored reply with both token counts, charges the tokens at price for an
HTTP 4xx or a reply with a stop reason (a complete response, such as an empty-text truncation,
whose usage is exact), and the higher of tokens and the estimate for any other error (5xx,
timeout, dropped connection), where the last hop's outcome is unknown. A reply missing
either count is unchanged: a 4xx is $0 and any other error is the estimate. A Gemini
continuation reply with no continuation_token now keeps an unreported output count as None
instead of 0.

Gemini adapter follows `status: requires_continuation`. A non-streaming hop stops at 32,768
tokens on gemini-4-carbon and gemini-4-argon (docs: aistudio.google.com/docs/long-decode), and a
live run at thinking_level medium came back after 32,765 thought tokens with no text, which was
recorded as an empty-output error. `call()` now resends the same request plus the response's
`continuation_token` until the status changes, joins visible text, and sums input, visible and
thought tokens across hops (every hop is billed). The loop stops at the summed
`max_output_tokens` or after `ceil(max_output_tokens / 32768) + 1` hops and records stop reason
`incomplete`, which scoring already counts as truncation. The per-call `timeout` applies per hop.
A hop that fails after earlier hops succeeded returns the error with the summed tokens. A
single-hop call is unchanged.

`run --reuse-incumbent RUN` / `plan --reuse-incumbent RUN` (issue #17): reuse a fixed incumbent's
stored outputs and judge records from an earlier judged run instead of paying for them again.
The candidate and its judge calls run as usual; `spent_usd.incumbent` is 0 and `plan` estimates
the incumbent side at $0. Refuses before any call unless RUN holds the incumbent id (in either
arm, not both), with the same suite hash, judge model and judge prompt hash (the first caller
of `judge.assert_comparable`), the same recorded request params, and a complete (not stopped
early, every item) record; `--limit` and `--batch` refuse with it. The reused arm's cost is
recomputed from recorded tokens at this run's prices (an errored or tokenless arm keeps its
recorded cost; a batch source's incumbent judge cost, which has no tokens, is rescaled by the
source's recorded batch multiplier) for the cost check and report (recorded figure kept as
`reused_cost_usd`), `run.json` records `reused_incumbent`, and the report says beside the
verdict that the two arms were judged in different sessions and counts only the judge calls
made this run. A source that is itself a reuse run refuses only when the arm being reused is its reused
incumbent slot; its freshly measured candidate can be reused. A batch source's recorded
judge multiplier must be a number > 0 and <= 1, as in `prices.load`, or the run refuses. Carried-over arm or judge errors,
and a reused arm with more truncated outputs than `policy.max_truncated`, make the verdict
INCOMPLETE; `plan` and `run` warn about them before spending. Judge errors and skips
carry over as recorded; no re-judging.

`run --timeout SECONDS` (issue #8): the per-call HTTP timeout for arms and the judge, which was
fixed at 60 seconds in every provider adapter with no way to change it. A long input on a model
that always thinks can take minutes per call, and a timeout lands as an error. Default stays
60; the value used is recorded in run.json as `timeout_s`; a non-positive value is refused
before any call. `--batch` runs are unaffected.

`run --batch` / `plan --batch`: send every Anthropic arm (and the judge, if any) as one
Anthropic Message Batch at the batch discount instead of one call per item
(`pinbench/batch.py`, `providers/anthropic.py`'s `create_batch`/`get_batch`/`fetch_results`/
`cancel_batch`). Anthropic-only -- a non-Anthropic id among incumbent/candidate/judge refuses
the whole run before anything is sent, never a half-batched mix. The discount is a new
`batch_multiplier` row in `prices.yaml` (0.5 on the three Claude rows here, "a 50 percent
discount on both input and output tokens":
https://platform.claude.com/docs/en/about-claude/pricing); a row with no `batch_multiplier`
refuses to run under `--batch` rather than guess a price. Results are mapped back to items by
`custom_id`, never by line position -- the vendor states results may come back out of order
(https://platform.claude.com/docs/en/build-with-claude/batch-processing). An errored/canceled/
expired result is an arm error costing $0, same as a 4xx on the sync path. The cost cap gates
submission only (one batch is one request; there is no per-call point left to stop mid-run);
an over-run discovered after results come back is recorded
(`run.json`'s `batch.cap_exceeded_after_results`) and warned about loudly, never prevented
after the fact since the money is already spent. `latency_ms` is `null` on every batch item
(no per-call timing exists). Default
`--max-wait` is 86400 seconds, riding to the vendor's own 24-hour expiry rather than failing
fast on a batch that is usually about to finish anyway ("most batches completing within 1
hour"). No `--resume-batch`: a batch left running past `--max-wait` or Ctrl-C keeps its ids in
the run's `batch.json`, but re-running the same command re-submits (and pays again).

`scripts/judge_control.py` and `examples/controls/summaries-facts-controls.yaml`: a negative
control for a judge suite. One deliberately bad summary per item (one fact kept, one wrong, one
left out, one invented claim) goes through the suite's judge exactly as an arm's output would;
each must score at most `max_hits` points and at least `min_extra` unsupported claims, and any
lenient grade or judge error exits 1. Offline tests prove a lenient judge goes red.

`examples/suites/summaries-facts.yaml`: the summaries suite's passages and task, scored by a
fixed judge against three key facts per item, with unsupported claims counted as extras. For
outputs too close for a useful A/B vote. Judge `gpt-6.1-sol` (price row added), from neither
vendor of a Claude-vs-Gemini comparison. Strict tolerances (0.0 recall, 0.0 extras),
frozen before the full run; the header records the one rubric calibration after a 2-item probe.

Google Gemini provider (`pinbench/providers/gemini.py`, `GEMINI_API_KEY`, ids starting
`gemini-`), on the Interactions API (`POST /v1beta/interactions`, key in the
`x-goog-api-key` header, `store: false`). Billed output is visible plus thought tokens
(`usage.total_output_tokens + usage.total_thought_tokens`), since Google bills thinking at the
output rate. The interaction `status` is recorded lower-cased as the stop reason, and
`incomplete` joins `scoring.TRUNCATION_STOP_REASONS`; empty text is a failure, as on the other
adapters. Inside the adapter, a `generation_config` mapping in `params` merges into the
request's generation_config, so `thinking_level` never drops the output ceiling. Between a
model's `request_defaults` and a suite's `params` the merge stays per top-level key: a suite
that sets its own `generation_config` replaces the model's, `thinking_level` included. `examples/prices.yaml` gains `gemini-4-argon` at its introductory $2/$10 rate
(through 2026-11-30; the row says what it becomes on Dec 1) with `thinking_level: low`, the
lowest level, since Argon cannot turn thinking off.

`run` builds its per-provider key map from `providers.ENV_KEYS`. It had two providers
hard-coded, so a Gemini arm would pass the plan, bill the incumbent's first call, then crash
with `KeyError: 'gemini'`.

`watch` now checks only the providers `pins.yaml` uses. It used to iterate every adapter, so
adding a provider made `watch` exit 1 ("UNREACHABLE: ... is not set") for anyone who pins
nothing there.

Judge stage skips a bad arm output instead of grading it: an arm's reply that errored, is
empty, or was cut off by the output ceiling (`scoring.TRUNCATION_STOP_REASONS`) is never
sent to the judge (`judge.skip_reason`, `judge.run_judge_stage`). A live run had been
spending a full judge call (about $0.10) grading a truncated output the truncation check
then made the run INCOMPLETE over anyway. The skipped slot is recorded as
`"skipped": "<reason>"` with `cost_usd` 0.0. `scoring.judge_recall` scores it like a judge
error (points kept in the denominator, zero hits), `judge_extra_mean` leaves it out, and
`judge_error_count` never counts it, so a truncation tolerated by `policy.max_truncated`
cannot shrink one arm's denominator and lift its recall. `report` prints a new "judge calls: N judged, M skipped
(truncated/error)" line, and `run` prints a loud warning when every item on one arm was
skipped (`scoring.judge_arms_fully_skipped`). The pre-run cost estimate is unchanged (it is
already a ceiling); only actual judge spend drops.

Project configuration: an optional `pinbench.yaml` at the project root (found by walking up
from the current directory, or `--config PATH`) sets defaults for `cap_usd`, a `judge`
fallback block (model/max_output_tokens/rubric), `paths` (pins/prices/suites/runs, resolved
relative to the config file's own directory), and a `policy` fallback block. Precedence is
CLI flag > suite > `pinbench.yaml` > built-in default, applied the same way by one set of
resolvers (`config.py`) everywhere a setting is used; `incumbent`/`candidate` are refused as
config keys (a model comparison is chosen per run, never defaulted); a malformed file, a cap
at or below zero, or a negative policy value refuses the same way. Every `run` records its
full effective settings and provenance in `run.json`, including which config file (if any)
and its content hash, with the config file and any config-resolved path written relative to
the current directory so no absolute local path lands in `run.json` or the `report.md` that
`bump` commits; `pinbench report`'s header names the config file and hash when one was
used, and reloads the suite using the run's own recorded judge/policy fallback so a later
report is unaffected by `pinbench.yaml` changing or disappearing. New `pinbench config
[--suite SUITE]` command prints the effective settings and each one's source. The judge's
built-in default model (previously only a literal inside the example suite) now lives in
`config.BUILTIN_DEFAULTS`, overridable by `pinbench.yaml`, with the suite's own `judge:`
block still winning. New example `pinbench.yaml` at the repo root.

Judge strict-win cost waiver correction: a judge recall strict win now waives the cost
tolerance on its own only when the suite's `scoring.votes` is `none` (its only independent
signal); a suite that also collects votes needs both the judge strict win and a candidate
vote strict win to agree, matching `verdict._hold_checks`'s long-standing docstring.

Blind LLM-judge scoring (`scoring.labels: judge`): a fixed, pinned judge model grades one
arm's output at a time against a suite-level rubric and per-item rubric points, blind (no
model ids, no side labels, randomized call order recorded per item), returning strict JSON
(`hit`/`extra`/`notes`). New checks `judge_errors` (INCOMPLETE on any), `judge_recall`, and
`judge_extra` (HOLD-causing, tolerances in `policy`). Judge calls are estimated and metered
against the same `--cap` as the arms, and a suite whose judge id equals either arm's id is
refused unless `--allow-self-judge` is passed. `run.json` and the report record the judge's
model id and a hash of its rubric (`judge_prompt_hash`); `judge.assert_comparable` refuses to
treat two runs as the same measurement when either differs. New example suite
`examples/suites/review.yaml` (6 synthetic code/config snippets with planted defects).

## 0.1.0

Initial release: registry (`pins.yaml`), suite runner against Anthropic and OpenAI, a cost cap
checked before and during a run, a blind A/B voting page, a printed decision rule
(RECOMMEND / HOLD / INCOMPLETE), and a gated `bump` command that edits a pin on a branch.
