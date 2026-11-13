# Commands

Every `pinbench` subcommand, its flags, exit codes, and what each command refuses or warns about. Part of [pinbench](../README.md).

| command | what it does |
|---|---|
| `pinbench pins` | print the registry as a table |
| `pinbench plan SUITE --incumbent ID --candidate ID [--cap USD] [--allow-self-judge] [--batch] [--batch-judge] [--reuse-incumbent RUN]` | estimate cost, no spend |
| `pinbench run SUITE --incumbent ID --candidate ID [--cap USD] [--allow-self-judge] [--timeout SECONDS] [--batch] [--batch-judge] [--reuse-incumbent RUN] [--poll-interval SECONDS] [--max-wait SECONDS] [--name NAME] [--resume NAME]` | run both arms (and the judge, if any), write run.json after every item; `--resume` continues a stopped or crashed run |
| `pinbench vote RUN_NAME [--open] [--allow-missing] [--allow-truncated]` | build the blind A/B page |
| `pinbench report RUN_NAME [--votes votes.json]` | unblind, score, print the verdict |
| `pinbench watch [--seed]` | diff the catalog of each provider `pins.yaml` uses against the last snapshot |
| `pinbench bump SLOT --to ID --run RUN_NAME` | edit a pin, gated on a RECOMMEND report |
| `pinbench config [--suite SUITE]` | print the effective settings and where each one came from |

Every command has `--help` and takes `--config PATH` (see [Configuration](configuration.md)).
`plan`/`run` take `--prices`, `pins`/`watch`/`bump` take `--pins`, and every command that
touches run state takes `--state`. `--cap` on `plan`/`run` can also come from the suite or
`pinbench.yaml`.

Other flags:

| flag | on | what it does |
|---|---|---|
| `--limit N` | `plan`, `run` | use only the first N items of the suite, for both the estimate and the run; `items_total` in `run.json` is then N |
| `--batch` | `plan`, `run` | send every Anthropic arm (and the judge, if any) as one Message Batch at the batch discount instead of one call per item. Anthropic-only: any non-Anthropic id among incumbent/candidate/judge refuses the whole run before anything is sent. `plan --batch` prints the discounted rate per arm/judge with a `batch_multiplier` line from `prices.yaml` |
| `--batch-judge` | `plan`, `run` | run the arms synchronously as usual (so a Gemini or OpenAI arm works), then send every judge call as ONE Anthropic Message Batch at the judge row's `batch_multiplier`. Refuses (exit 2, before any call) with `--batch`, when the suite has no judge, when the judge is not Anthropic, or when the judge's price row has no `batch_multiplier`. `plan` estimates the judge at the batch price and the arms at the sync price. The judge requests are built by the same code a sync judge call uses, so the bodies are identical; an arm output the judge skips (errored, empty, truncated) is recorded as skipped and never sent. The cap is checked against arms spend plus the whole judge batch estimate before the batch is submitted (a batch cannot be stopped midway); if that would pass the cap the batch is not sent, the run is marked `stopped_early`, and the judge records say so. After the results arrive the actual token cost is charged at the batch price. The arms are written to `run.json` before the batch is submitted, and the judge batch id is written to `batch.json`. `run.json` gets `judge_batch` (`id`, `price_multiplier`, `status`, request counts); the top-level `batch` key is not set, because it means the arms were batch-priced. The arm phase leaves room for the judge batch: every arm call's cap check includes the judge batch's estimate, so the arms stop early (`stopped_early`) rather than spend the cap and strand the judging. Calls to the batch API (create, poll, results) are retried up to 5 attempts with exponential backoff (2s base, 0.5 to 1.0 jitter) on 429, 5xx, 529, timeouts and connection errors, never on another 4xx. Only a 429 on create is retried, because it means no batch was created. The Message Batches docs describe no idempotency key, and a batch cannot be matched to this run from the batch list (a sibling run has the same custom_ids and request count), so any other transient create failure (5xx, 529, timeout, dropped connection) is an unknown outcome that is never re-sent and never adopted: `judge_batch.status` is `submit_outcome_unknown`, the judge spend is the reserved estimate, `batch.json` records it, and `run` exits 1 telling you a batch may exist (check the Anthropic console, Batches). `--resume NAME` refuses such a run (see `--resume`): it never submits a second batch over one that may exist. A malformed results line or an unknown result type is not retried: `results_failed` at the reserved estimate. When retries run out `run.json` and `batch.json` are persisted, the batch id is printed, and `run` exits 1. If the batch is still running after `--max-wait`, it is left running, the judge records carry a "not judged" error, the verdict is INCOMPLETE, `run` exits 5, and `spent_usd.judge` is the reserved estimate (`judge_batch.spend_basis` `reserved_estimate`) because the batch may still bill; the same estimate is recorded when polling or fetching results fails after submit. Works with `--reuse-incumbent`: only the candidate's judge calls are batched and the incumbent's judge records are carried over. The report header reads `judge: batched (id ...)`. Uses `--poll-interval` and `--max-wait` |
| `--hold-judge-on-errors` | `run` | with `--batch-judge` (refused without it): submit the judge batch only if the arm phase finished with every arm succeeded or truncated; otherwise stop resumable (`stop_reason` `judge_batch_not_submitted`, exit 6) and print the resume command, which says that if an item keeps failing you can resume without `--hold-judge-on-errors` to judge the rest. The hold only decides when the judge batch is sent, not what is measured, so a resume may drop it: errored and never-attempted arms are re-run once, then the judge batch is submitted for the arms that did not error (errored arms are recorded as skipped), and the run ends `arm_errors_judged` (final, not resumable). The drop is recorded (`run_modes.hold_judge_on_errors` false, `resumes.history[].hold_released_at`) and the report says the judge batch was released with N candidate arm error(s) still present. A resume cannot add the hold to a run recorded without it (refused, exit 2) |
| `--reuse-incumbent RUN` | `plan`, `run` | copy the incumbent's per-item outputs and judge records from `runs/RUN/run.json` instead of calling it again. The arm whose model id is `--incumbent` is taken from RUN, whichever slot it held there; the candidate and its judge calls run as usual. `spent_usd.incumbent` is 0 and `plan` estimates the incumbent and its judge calls at $0. Refuses (exit 2, before any call) when: RUN is missing or neither arm is the incumbent id (or both are); `suite_hash` differs; the judge model or judge prompt hash differs; RUN's recorded request params for that arm differ from what this run would send; RUN stopped early, covers fewer items than the suite, or lacks a record for the arm; the arm to reuse is the incumbent slot of a reuse run (reuse the original instead; a reuse run's own candidate can be reused); a batch source records a `batch.price_multiplier` for the judge, or a `--batch-judge` source records a `judge_batch.price_multiplier`, that is not a number > 0 and <= 1; `--limit` or `--batch` is also given. For the cost check and the report the reused arm's per-item cost is recomputed from its recorded tokens at this run's prices; an arm with an error or no recorded tokens keeps its recorded cost. Judge records carry no tokens, so the incumbent's judge cost is carried over unchanged from a sync source and rescaled (divided by the batch multiplier the source recorded for the judge, else the price row's `batch_multiplier`, else refused) from a batch source, which is a run.json with a `batch` block, or from a `--batch-judge` source, which is a run.json with a `judge_batch` block (its arms were sync-priced and are not rescaled). The recorded figure is kept per item as `reused_cost_usd`. Latency is the source's, unchanged. Judge errors and skips carry over as recorded; nothing is re-judged. If the reused arm carries arm or judge errors, or more truncated outputs than the suite's `policy.max_truncated` (the same truncation test the verdict uses), `plan` and `run` print a warning before spending (count, first item id) that the verdict will be INCOMPLETE because of them, and the report notes it beside the verdict. The report counts only the judge calls made in this run and states the reused judge records separately. `run.json` gets `reused_incumbent` (`run`, `arm_in_source`, `source_run_sha256`, `source_started_at`), and the report states the reuse beside the verdict. The hash identifies the source file; `run.json` is local, so it is not tamper evidence |
| `--name NAME` | `run` | the run directory, `runs/NAME/`. `run` refuses (exit 2, before any call) when `runs/NAME/run.json` already exists, unless `--resume NAME` is given; it used to overwrite silently. Without `--name` the default is `SUITE-UTCSTAMP-RANDOM` (6 hex digits, re-drawn while the directory exists), so two runs started in the same second never share a directory |
| `--resume NAME` | `run` | continue the stopped or crashed sync run NAME (`status` `in_progress` or `stopped`). Give the same suite, `--incumbent`, `--candidate`, and the same `--reuse-incumbent`, `--batch-judge` and `--limit` flags; `--cap`, `--timeout` and the prices file may change. Refuses (exit 2, before any call) unless these all match what the run recorded: `suite_hash`, incumbent, candidate, the request params per arm (the merged `request_defaults` plus suite params), judge model, judge prompt hash, the `--reuse-incumbent` source and its sha256, and the batch-judge and limit flags. `--hold-judge-on-errors` may be DROPPED on a resume (it only decides when the judge batch is sent, not what is measured) but not ADDED: a run recorded without the hold may already have submitted. Also refuses a complete run, a run another live process holds (`runs/NAME/run.lock` holds a pid; a dead pid is taken over with a warning), a `--batch` run, a `run.json` written before this feature (no `status`), and a `--name` that differs from NAME. Rules: an arm that succeeded is kept as recorded and never called again, and an item whose arms and judge records are all fine is skipped; an arm with an error, or never attempted, is run (the item's judge calls are redone). A truncated output is a result, not a failure, so it is kept as recorded and not re-run; raise `max_output_tokens` and start a new run for that. A reused-incumbent slot is kept from the checkpoint as it is. Spend so far, billed tokens on failed calls included, counts against `--cap`: the cap covers the whole run, and the resume is refused (exit 3) when spend so far plus the estimate of what remains exceeds it. `run.json` records `resumes` (`count`, and per resume its time, items recorded before, spend before and prior stop reason, plus `hold_released_at` when the resume dropped `--hold-judge-on-errors`, in which case `run_modes.hold_judge_on_errors` is now false); the report header says the run was resumed. A `--batch-judge` run resumes judge-only when a judge batch id is recorded (`judge_batch.status` `submitted`, `timed_out`, `poll_failed` or `results_failed`): the arms are kept exactly as recorded, errored ones included (they were skipped when the batch was built, so its custom_ids still match), and that batch is polled and fetched (existing retries), never re-created. `not_submitted_cap` or `submit_failed` submits (after re-running any errored arms). `submit_outcome_unknown`, or `pending` (killed during the create call), is refused with a note to check the Anthropic console, because a batch may exist and bill; recovering from that state is not supported. A batch that already ended is refused (nothing to collect) |
| `--timeout SECONDS` | `run` | seconds to wait for each arm and judge call before giving it up as an error; default 60. Recorded in run.json as `timeout_s`. Not used with `--batch`, which makes no per-call request |
| `--poll-interval SECONDS` | `run` (`--batch` or `--batch-judge`) | seconds between batch status polls; default 30 |
| `--max-wait SECONDS` | `run` (`--batch` or `--batch-judge`) | seconds to wait for a batch to reach `ended` before giving up; default 86400 (24h, the vendor's own expiry). Giving up never cancels the batch -- it is left running, and the same command re-run re-submits (and pays again) |
| `--format text\|md\|json` | `report` | the format printed to stdout (default `text`); the run's `report.md` is always written in Markdown regardless |
| `--no-git` | `bump` | edit the pins file and stop: no branch, no commit |
| `--force REASON` | `bump` | skip the RECOMMEND gate; the reason is written into the commit message as `(forced: REASON, run RUN_NAME)` |

`report` also writes `verdict.json` beside `report.md`: the verdict plus a SHA-256 of every
input it came from (`run.json`, the suite file, and the votes file and `mapping.json` when votes
were given).

`bump` never reads the verdict from `report.md`'s text. It refuses unless `verdict.json` exists,
every recorded hash still matches, and re-deriving the verdict from those inputs gives the same
RECOMMEND. A hand-edited `report.md` or `verdict.json` is refused; rerun `pinbench report`.

Without `--no-git`, `bump` creates branch `bump/SLOT-ID` in the pins file's repository before it
touches the pins file, so an existing branch is a refusal with nothing edited. When `--to` is
another provider's id, the slot's `provider:` line is switched with it. It then commits
the pins file and the run's `report.md` there, and prints "next step: open a PR from this
branch". If the edit or commit fails, the pins file and the original branch are restored and the
new branch is deleted.

Exit codes:

| code | meaning |
|---|---|
| 0 | ok |
| 1 | error (including `vote` refusing a run with missing output) |
| 2 | refusal before any spend |
| 3 | the cost cap was exceeded |
| 4 | a `run` where one arm got zero successes |
| 5 | `run --batch-judge`: the judge batch did not finish within `--max-wait` (left running; judge spend recorded as the reserved estimate) |
| 6 | `run` stopped and is resumable: an arm or sync judge call was still HTTP 429 or quota-exhausted (Gemini RESOURCE_EXHAUSTED, OpenAI insufficient_quota) after bounded backoff (or a `--batch-judge` run was stopped before its judge batch was submitted). Every finished item is saved, `stopped_early` is true with `stop_reason` `quota_or_rate_limit`, and the exact `--resume` command is printed |
| 129, 130, 143 | `run` stopped by SIGHUP, SIGINT or SIGTERM (128 plus the signal number); also `run --batch` interrupted (Ctrl-C) while waiting on a batch, which leaves the batch running |

Crash safety:

- A sync `run` writes `run.json` atomically (temp file, then replace) after every finished item,
  both arms and the sync judge stage, with `status: "in_progress"`, `items_recorded`,
  `items_completed` and `checkpoint_at`; `finished_at` is null until the end. The end sets
  `status` to `complete`, or to `stopped` with a `stop_reason` (a run is `complete` only when every item has both arms succeeded or truncated and the judge stage ran; a run that ends with an arm error is `stopped`, `arm_errors`, and `--resume` re-runs those arms). `pinbench report` marks the completeness check failed for any status but `complete`, exits 1 for an `in_progress` checkpoint, and never writes `verdict.json` for a run that is not `complete`. The status is (`cap`, `quota_or_rate_limit`,
  `signal:SIGTERM`, `interrupted`, `error: ...`, `judge_batch:STATUS`). The file and its directory are fsynced around the replace. A process killed outright
  (SIGKILL, power loss, reboot) leaves the last checkpoint, which holds whole items only; the call in flight at that moment is not recorded, so spend can be under-counted by up to one item.
- 429 and quota errors on an arm or sync judge call are retried, up to 5 attempts with delays of
  15, 30, 60 and 120 seconds (`pinbench/resume.py`: `RATE_LIMIT_ATTEMPTS`,
  `RATE_LIMIT_BACKOFF_BASE_S`, `RATE_LIMIT_BACKOFF_MAX_S`). Tokens billed on a failed attempt are
  charged as the cost rules always charge them. Once retries run out the run stops instead of
  recording a 429 for every remaining item.
- SIGTERM, SIGHUP and SIGINT: the first signal lets the call in flight RETURN (its tokens are paid,
  so it is kept, not abandoned), then the run persists, prints the resume command and exits 143,
  129 or 130. The first signal prints `finishing the call in flight, then saving; press again to abandon it (charged at its estimate)`. A second signal abandons the call in flight and persists at once; the abandoned call has an unknown outcome and is charged at its estimate, never $0. A 429 retry loop stops retrying once a signal is pending or the cap has no room for another call. A signal that arrives during the last call still stops the run (no judge batch is submitted). While `--batch-judge`
  waits on its judge batch, the first signal exits at once (the batch id is already on disk).
  The Hydrogen and Carbon runs can run side by side under different `--name`s.

Refusals and warnings:

- `run` prints a per-arm `succeeded/attempted` line and the first error (truncated) when an arm
  had any. Some-but-not-all items completed is a warning at exit 0; a wholly-failed arm is
  exit 4.
- `run` records each arm's stop/finish reason verbatim (`stop_reason` in `run.json`, from
  Anthropic's `stop_reason`, OpenAI's `finish_reason` or Gemini's interaction `status`) and prints a per-arm truncated count
  plus a loud warning when any output hit the ceiling. Not a refusal: whether a few truncated
  items sink the verdict is `report`'s call, gated by `policy.max_truncated`.
- `vote` refuses (exit 1) when any item is missing output on either arm, naming the item/arm
  pairs; `--allow-missing` renders those as explicit error cards. It also refuses when any
  output was truncated; `--allow-truncated` renders those cards with a visible "truncated"
  badge.
- `report` folds any per-arm error, or more than `policy.max_truncated` (default 0) truncated
  outputs on either arm, into an INCOMPLETE verdict. A 4xx error before the model generated
  anything costs $0 in `run.json`, never the pre-run estimate.
- With `--batch` the cap is enforced before submission only: a Message Batch is one request
  for every item, so there is no per-call point left to stop mid-run. If actual spend still
  ends up over the cap once results come back (the pre-run estimate is a ceiling, not every
  real call), `run.json`'s `batch.cap_exceeded_after_results` is set and `run` prints a loud
  warning, but the exit code is unchanged -- the money is already spent, and nothing remains
  to stop. A model with no `batch_multiplier` in `prices.yaml` refuses the same way an unpriced
  model does (exit 2), and any non-Anthropic id among incumbent/candidate/judge refuses the
  whole run (exit 2) before anything is sent -- `--batch` is Anthropic-only, never half-batched.
