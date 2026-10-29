# Changelog

## Unreleased

Google Gemini provider (`pinbench/providers/gemini.py`, `GEMINI_API_KEY`, ids starting
`gemini-`), on the Interactions API (`POST /v1beta/interactions`, key in the
`x-goog-api-key` header, `store: false`). Billed output is visible plus thought tokens
(`usage.total_output_tokens + usage.total_thought_tokens`), since Google bills thinking at the
output rate. The interaction `status` is recorded lower-cased as the stop reason, and
`incomplete` joins `scoring.TRUNCATION_STOP_REASONS`; empty text is a failure, as on the other
adapters. A `generation_config` mapping in `params` merges into the request's
generation_config instead of replacing it, so a model's `thinking_level` default keeps the
output ceiling. `examples/prices.yaml` gains `gemini-4-argon` at its introductory $2/$10 rate
(through 2026-11-30; the row says what it becomes on Dec 1) with `thinking_level: low`, the
lowest level, since Argon cannot turn thinking off.

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
