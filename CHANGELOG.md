# Changelog

## Unreleased

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
