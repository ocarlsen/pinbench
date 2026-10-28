# Limits

What pinbench does not do, and the model-specific behavior it leaves to the registry. Part of [pinbench](../README.md).

- Two providers: Anthropic and OpenAI. Adding a third is one file in `providers/`: create
  `pinbench/providers/<name>.py` with a `call(...)` and a `list_models(...)` function matching
  the shape in `providers/anthropic.py`, then register it in `providers/__init__.py` with one
  entry each in `ADAPTERS` and `ENV_KEYS` and one prefix line in `provider_for_id`. The
  adapters use plain `urllib` so the stubbing seam is obvious and the dependency list stays at
  one line; the official SDKs are a drop-in swap behind `providers/`.
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
