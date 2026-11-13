# Limits

What pinbench does not do, and the model-specific behavior it leaves to the registry. Part of [pinbench](../README.md).

- Three providers: Anthropic, OpenAI and Google Gemini. Adding another is one file in `providers/`: create
  `pinbench/providers/<name>.py` with a `call(...)` and a `list_models(...)` function matching
  the shape in `providers/anthropic.py`, then register it in `providers/__init__.py` with one
  entry each in `ADAPTERS` and `ENV_KEYS` and one prefix line in `provider_for_id`. The
  adapters use plain `urllib` so the stubbing seam is obvious and the dependency list stays at
  one line; the official SDKs are a drop-in swap behind `providers/`.
- Gemini continuation: a non-streaming Gemini hop is capped (32,768 tokens on gemini-4-carbon
  and gemini-4-argon), so the adapter follows `status: requires_continuation` by resending the
  prompt with the `continuation_token`, up to `ceil(max_output_tokens / 32768) + 1` hops or until
  the summed thinking+output reaches `max_output_tokens`; either bound records the output as
  truncated. Usage is summed per hop, which assumes each hop's `usage` is its own (the docs are
  ambiguous), and cached input tokens are billed as ordinary input, so Gemini input cost is an
  upper bound. An error after a paid hop keeps its summed tokens on the reply, and the cost
  meter never charges less than them: an HTTP 4xx costs those tokens, and any other error
  (a 5xx, a timeout, a dropped connection) costs the higher of those tokens and the estimate. Known limit: a continuation hop that times out may itself have been billed, so the higher-of rule can still under-state the cost of a multi-hop failure.
- Label matching is exact, contains, or regex, or a fixed LLM judge scoring recall against a
  rubric. There is no fourth mode, and a judge suite does not also support `expect`-based
  labels on the same items.
- n is whatever you froze. The report states what that n can and cannot detect; a 12-item
  suite cannot show a regression smaller than roughly 8 percentage points.
- Prices are a file you maintain, not a live lookup. An unpriced model refuses to run rather
  than guess.
- The watch comparator is a heuristic over id strings (family and version tokens), not a
  vendor feed. It will miss a rename that does not follow the pattern.
- Both arms get the same `task` and `max_output_tokens`, but each request can differ beyond
  that. A model's own `request_defaults` row in `prices.yaml` (e.g. how it turns extended
  thinking off) is sent first; the suite's optional `params` go on top and win on any key both
  set. pinbench never injects a sampling default of its own: newer models increasingly reject
  a fixed temperature, and it never guaranteed determinism anyway. Whatever was sent is
  recorded per arm in `run.json` under `request_params`. A candidate that rejects a param the
  suite (or its own registry row) set records an error, which makes the verdict INCOMPLETE,
  not a silent retry.
- Current Claude models run extended thinking by default, and thinking tokens count against
  `max_output_tokens`. A low ceiling can spend the whole budget on an invisible thinking block
  and return empty text (see [What the live runs caught](live-runs.md)). The off switch is not one syntax
  across generations (one accepted `thinking: {"type": "disabled"}`; the next 400s on that and
  wants `{"type": "between_tools"}`, observed 2026-10-26), so the adapter carries no such
  default. It is a per-model `request_defaults` row in `prices.yaml`, merged under the suite's
  `params`; a model with no row gets no such key at all.
- No confidence intervals, inter-rater agreement, or rotating reviewers; no hosted voting; no
  chat or email notifications, scheduler, or daemon (`watch` is a one-shot you cron); no
  automatic PR creation or rollback tooling; no reasoning-effort normalization across models.
- `--batch` (Anthropic's Message Batches API) is Anthropic-only: a non-Anthropic id among
  incumbent/candidate/judge refuses the whole run rather than batching some arms and calling
  others. It cannot be stopped request by request once submitted -- the whole batch is one
  request -- so the cost cap gates submission only; an over-run discovered after results come
  back is recorded (`run.json`'s `batch.cap_exceeded_after_results`) and warned about loudly,
  never prevented after the fact. `latency_ms` is always `null` on a batch item: there is no
  meaningful per-call timing to record. Most batches finish in under an hour; a batch can take up to 24 hours before it
  `expired`s, so the default `--max-wait` is 86400 seconds (riding to that expiry rather than
  failing fast and leaving a half-finished wait to re-fetch by hand). Results are kept 29 days.
  There is no `--resume-batch`: if `--max-wait` or Ctrl-C ends `run --batch` early, the batch
  itself is left running (never canceled) and its ids are on disk in `batch.json`, but
  re-running the same command re-submits the work from scratch (and pays again).
