# What the live runs caught

The four failures the first live runs surfaced, each now a shipped check, plus a HOLD for contrast. Part of [pinbench](../README.md).

## What the first live runs caught

The tool was built and unit-tested offline first. The early live runs failed four times, and
each failure became a check that now ships with it:

1. **A rejected sampling parameter.** The newer model refused `temperature`, every call on
   both arms failed, and the first version still printed "10/10 completed", exited 0, charged
   the run estimate, and built a vote page of blank cards. Now a failed call is a failure, an
   arm with zero successes exits nonzero, a request refused before generating costs $0, and
   `vote` refuses to render missing output. That shape is `test_live_run_failure_regression.py`.
2. **Thinking that ate the budget.** The incumbent reasons by default, so a tight ceiling went
   to invisible reasoning: 19 of 20 outputs hit `max_output_tokens` mid-sentence with no flag
   raised, and one came back empty yet counted as a success (thinking spent the entire 60-token
   ceiling before any visible text). Neither looked like an error to the caller. Now empty text
   is a failure, every output records its stop reason, and a truncated output makes the verdict
   INCOMPLETE.
3. **"Off" is spelled differently per model.** The two models take different settings to turn
   reasoning off. That is a fact about each model, so it lives in the model registry
   (`request_defaults` in `prices.yaml`), not in a hard-coded default.
4. **A ceiling too tight for the task.** The first live judge run asked for code reviews under
   a 300-token ceiling and all twelve were cut off. The truncation check refused to score them
   and the verdict was INCOMPLETE. The fix was in the suite: a bullet-list task and a
   1,200-token ceiling.

None of these would show up as an error in a dashboard. They are why a verdict can be
INCOMPLETE, and why a report states what it checked.

## A HOLD, for contrast

`examples/suites/capitals.yaml` has labeled answers instead of votes. This run was not a live API
call: it was produced offline by the same fake provider the tests use, with `claude-sonnet-4-5` as
incumbent and `gpt-4o-mini` as candidate, and two candidate answers scripted wrong (Sydney for
Australia, Istanbul for Turkey). The labels check fails at 83.3% against 100%, and the verdict is
HOLD even though the candidate is twenty times cheaper. A cheaper model that is wrong more often is a regression, and `bump` refuses the pair.
