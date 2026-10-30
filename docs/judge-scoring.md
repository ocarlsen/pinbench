# Judge scoring

How `scoring.labels: judge` grades an open-ended task with one fixed, blind judge model, and what the live judge run showed. Part of [pinbench](../README.md).

Exact/contains/regex matching needs a reference answer, and a blind vote needs a human. Neither
fits an open-ended task like "review this diff for defects," where the right answer is "did it
find the planted problems." For that, a suite can declare `scoring.labels: judge`:

- a fixed `judge` model and a free-text `rubric` at the suite level
- per item, a list of `rubric_points` (each an `id` and a `description` of one thing a correct
  response should catch)
- per item, an optional list of `decoys`: plausible-looking issues that are not the planted
  one. Documentation only; never sent to the judge.

To try it on the example suite:

```
pinbench plan examples/suites/review.yaml --incumbent claude-sonnet-5-5 --candidate claude-haiku-5-5 --cap 1.50
pinbench run examples/suites/review.yaml --incumbent claude-sonnet-5-5 --candidate claude-haiku-5-5 --cap 1.50 --name review-demo
pinbench report review-demo
```

`plan` prints a third `judge` line next to `incumbent`/`candidate`; the judge's calls count
against the same `--cap`. The cap is higher here because the estimate assumes every judge call
spends its whole `judge.max_output_tokens` ceiling (4096 in this suite: the judge model always
thinks, and thinking tokens count against that ceiling). Actual spend is usually a fraction of
that.

How the judge is run:

- **One fixed judge.** A floating judge would hide a regression in the judge inside what looks
  like a regression in the candidate. `judge.model` is one pinned id. `run.json` and the report
  header record it with a hash of the rubric (`judge_prompt_hash`), so two reports are the same
  measurement only when both match. The library check `judge.assert_comparable` encodes that
  rule; no command compares two runs yet, so for now you compare the two header lines by eye.
- **Blind.** Each call sees the rubric, the rubric point ids and descriptions, the item input,
  and exactly one response, presented identically whichever arm produced it. It never sees the
  other arm's output, a model id, or the words "incumbent" or "candidate". Which arm is graded
  first is randomized per item with `secrets.SystemRandom` and recorded as `call_order` in
  `run.json`.
- **Strict JSON or a judge error.** The judge answers `{"hit": [ids], "extra": int,
  "notes": str}`: rubric point ids satisfied, a count of other real findings beyond the rubric,
  a one-line note. Invalid JSON, or a `hit` naming an id the item never declared, is a judge
  error for that item: counted, never guessed at, same bucket as an arm HTTP error.
- **Skipped, not graded.** An arm's reply that errored, is empty, or was cut off by the output
  ceiling never reaches the judge. The slot is recorded as `"skipped": "<reason>"` at zero
  cost. For recall it scores like a judge error (the item's points stay in that arm's
  denominator with zero hits), it is left out of the extra mean, and it never counts toward
  `judge_errors`. The points stay in on purpose: with `policy.max_truncated` above zero,
  dropping the item would let the arm that truncated on the hardest item post its recall over
  the easy ones. `pinbench report` prints "judge calls: N judged, M skipped (truncated/error)".
- **Two numbers per arm.** **Recall** is hits over total rubric points. **Extra** is the mean
  "extra" count, a precision proxy: hitting every point by flagging everything in sight is not
  better.
- **Metered like an arm.** Judge calls are estimated and metered against the same `--cap`
  (`pinbench plan` prints a third `judge` line). A judge id equal to either arm's id is refused
  unless you pass `--allow-self-judge`.

Checks a judge suite adds to the verdict:

| check | effect |
|---|---|
| `judge_errors` | INCOMPLETE if nonzero, same as an arm error |
| `judge_recall` | HOLD unless the candidate is within `policy.recall_tolerance_points` (default 0.0) of the incumbent |
| `judge_extra` | HOLD unless the candidate's mean extra-findings count is no more than `policy.extra_tolerance` (default 0.5 per item) worse than the incumbent's |

Cost waiver: a judge strict win (candidate recall strictly better beyond
`recall_tolerance_points`, with `judge_extra` also passing) waives the cost tolerance on its own
only when `scoring.votes` is `none`, the only signal such a suite has. A judge suite that also
collects votes needs both signals to agree: the judge strict win AND a candidate strict win on
the vote tally, the same pairing a labeled suite uses (label strict win plus vote strict win).
Treat a RECOMMEND that rests on it as "worth the money" by the judge's lights, not by yours.

Practical limits:

- The judge's `max_output_tokens` caps its whole reply. On a judge model that always thinks
  (Claude Opus 5.5, for one: `thinking` cannot be turned off, and thinking tokens are billed as
  output and count against `max_tokens`), a tight ceiling gives a cut-off or empty reply, which
  is a judge error, which makes the run INCOMPLETE. Leave room.
- The judge sees the arm's raw output; pinbench strips nothing. A model that names itself ("As
  Claude, ...") unblinds that one call. The example suites' tasks do not invite that.
- A judge is a model, not ground truth, and can agree with itself more readily than with a
  careful human. Calibrate it against a small set of human labels before trusting it for a real
  bump decision. This tool does not build that step for you; see
  [the essay](https://www.ocarlsen.com/model-upgrades.html) on checking any judge, human or
  model, against ground truth before it decides anything.

## Live judge run

`examples/suites/review.yaml` on 2026-10-26: six short code and config snippets, each with one
planted defect, reviewed by `claude-sonnet-5-5` and by `claude-haiku-5-5`, then graded blind by
`claude-opus-5-5`, one review at a time.

| check | result | detail |
|---|---|---|
| completeness | PASS | n=6 >= 6 |
| errors | PASS | incumbent and candidate both had 0 errors |
| judge_errors | PASS | 0 judge errors |
| truncation | PASS | incumbent 0 truncated, candidate 0 truncated; allowed 0 per arm; PASS |
| votes_present | PASS | votes not required for this suite |
| judge_recall | PASS | candidate recall 100.0% (6/6) vs incumbent 100.0% (6/6); tolerance 0.0 points; PASS |
| judge_extra | PASS | candidate extra 0.50/item vs incumbent 1.17/item; tolerance 0.5/item; PASS |
| cost | PASS | candidate $0.000114/item vs incumbent $0.002883/item (ratio 0.04, allowed 1.25); PASS |

Verdict: **RECOMMEND**. Both models caught all six planted defects. The cheaper model raised
fewer findings beyond the planted ones (0.50 per item against 1.17). An "extra" finding is not
necessarily wrong, only outside the rubric, which is why it is a tolerance and not a hard
failure. The whole run, judge included, cost $0.13 against a ceiling-based estimate of $1.25:
the estimate assumes every call uses its full output ceiling, so it errs high on purpose.

## Live key-facts run: Sonnet 5.5 vs Gemini 4 Argon

`examples/suites/summaries-facts.yaml` on 2026-10-29, frozen at commit 304c190 before the full run
(judge prompt hash `sha256:8d94aab8...`), judged by `gpt-6.1-sol`, from neither arm's vendor. Run twice.

| check | run 1 | run 2 |
|---|---|---|
| judge_recall | candidate 30/30 vs incumbent 30/30 | candidate 30/30 vs incumbent 29/30 |
| judge_extra | candidate 0.20/item vs incumbent 0.00; FAIL | candidate 0.20/item vs incumbent 0.00; FAIL |
| cost | ratio 0.69; PASS | ratio 0.71; PASS |

Verdict: **HOLD** on both runs. Argon stated every key fact at about 70% of Sonnet's cost, but
wrote two claims per run that the passage does not support. One, on both runs, is item s04: the
passage itself is inconsistent (the halves add up to a six-point win, the text says twelve), and
Argon reported six points, on run 2 with a note saying so. True in the world, unsupported by the
source; a summarizer should not silently correct its input. The passage is left as is so this
run stays reproducible. The others were small embellishments ("sixth annual", "most lengthy
discussion of the year"). Argon's introductory price doubles on 2026-12-01, which would also fail
the cost check (about 1.4x Sonnet per item).

How far to trust the judge:

- **Rubric calibrated once, before the full run.** A 2-item probe showed the first points were too
  specific (which fields bulk edit changes) and failed accurate summaries. All 30 points were
  rewritten to core facts, for both arms; on the probe the rewrite helped the candidate more.
- **Negative control** (`scripts/judge_control.py`): 9 of 10 deliberately bad summaries scored at
  most one key fact and at least one unsupported claim. The tenth scored 0 of 3 facts but no
  extras: every error in it was a wrong version of a key fact, which this judge counts as a miss.
  A wrong claim outside the key facts counts as an extra. That control was mis-built (it had no
  claim outside the key facts) and is kept as it ran.
- **Hand check:** a person scored the three items behind the HOLD blind (s04, s07, s10, six
  summaries). All 18 key-fact calls matched the judge. On extras the judge flagged two small
  overreaches the person missed on a first read; on re-reading the passage, both were unsupported.
- **Run to run:** 3 of 20 judge records differed between the runs. Each run regenerates the
  summaries too, so that number mixes arm and judge variation; the verdict did not change.
