# pinbench

A model upgrade is a deploy. pinbench treats it like one. Every model you depend on is a
pinned id in a registry file, never a floating "latest" alias. When a vendor ships a newer
model in a slot you use, `pinbench watch` names it. `pinbench run` sends a frozen suite of
your own inputs through the incumbent and the candidate, refusing to start if the cost
estimate is over your cap and stopping if actual spend reaches it. `pinbench vote` builds a
blind A/B page where you judge outputs without knowing which model wrote which. `pinbench
report` unblinds the votes, scores against any reference labels, and prints a
RECOMMEND / HOLD verdict with the rule it applied stated in the output. `pinbench bump`
changes the pin only when a report for that exact pair says RECOMMEND, and commits the
change on a branch so the switch is a reviewable diff.

## The problem

Upgrading a model by feel works right up until a newer one is worse at your task and a
user tells you first. A vendor's release notes say "better in most benchmarks," which is
true and also not the question. The question is whether it is better at the one thing you
built on top of it, and the only way to answer that is to run it against your own inputs
before anyone else does.

I found this out running a small production pipeline. A newer flagship model, tried as the
judge in one step, agreed with 99 hand-labeled answers less often than the older model it
would have replaced: 83.8% against 88.9%. It also passed an answer that contained an
invented product detail, one the older model had caught. Nothing in the vendor's own
materials would have told me that. If that slot had pointed at a floating alias instead of
a pinned id, the regression would have shipped on the vendor's release day, not on a day I
chose.

The fix was not to stop upgrading, it was to make upgrading boring. The first real
candidate that went through this process, at roughly half the price of the incumbent, won
5, lost 4, and tied 4 of 13 frozen blind votes. I switched, because the downside of being
wrong was bounded: one reverted commit. The next candidate through the same process lost 4
to 6 against the same incumbent and was kept out. Neither decision took more than reading
a printed report. See the full story: [ESSAY_URL].

## Quickstart

```
pip install -e ".[dev]"
export ANTHROPIC_API_KEY=sk-ant-...
export OPENAI_API_KEY=sk-...
pinbench plan examples/suites/capitals.yaml --incumbent claude-sonnet-4-5 --candidate gpt-4o-mini --cap 0.10
pinbench run examples/suites/capitals.yaml --incumbent claude-sonnet-4-5 --candidate gpt-4o-mini --cap 0.10 --name demo
pinbench vote demo --open
pinbench report demo
```

The example suite is 12 short items; at current prices the whole run costs under $0.05.

## Sample report

This is `report.md` from a real run of `examples/suites/capitals.yaml`, incumbent
`claude-sonnet-4-5` against candidate `gpt-4o-mini`, pasted verbatim.

```
# pinbench report: capitals-demo

**claude-sonnet-4-5** (incumbent) vs **gpt-4o-mini** (candidate)

- Items: 12/12 completed
- Spend: $0.0026 of $0.10 cap (estimate $0.0148)

## Checks

| check | result | detail |
|---|---|---|
| completeness | PASS | n=12 >= 10 |
| votes_present | PASS | votes not required for this suite |
| candidate_errors | PASS | candidate had 0 errors |
| labels | FAIL | candidate 83.3% (10/12) vs incumbent 100.0% (12/12); tolerance 0.0 points; FAIL |
| cost | PASS | candidate $0.000010/item vs incumbent $0.000210/item (ratio 0.05, allowed 1.25); PASS |

## Verdict: HOLD

a candidate is recommended when it is non-inferior on labels (within 0.0 points),
non-losing on blind votes (ties excluded), and costs no more than 125% of the incumbent
per item, over at least 10 completed items with no errors. 10 items can show a large
regression, not a small one (1/n = 10.0 points); the downside of a wrong RECOMMEND is
bounded by `pinbench bump` being one reverted commit.
```

The candidate missed the two capitals most often confused with a bigger city in the same
country (Canberra, Ankara), which is exactly the kind of thing a glance at a leaderboard
would never show. The verdict is HOLD, not RECOMMEND: the labels check failed, so `bump`
refuses this pair until a report says otherwise.

`vote.html` from the same run:

![vote page screenshot](docs/vote-page.png)

TODO(Owen): replace this sample with a live run once you authorize the metered API spend:
`pinbench run examples/suites/capitals.yaml --incumbent claude-sonnet-4-5 --candidate
gpt-4o-mini --cap 0.10 --name capitals-live`, then swap the report text and screenshot
above for the real ones.

## Design decisions

**Pins, not aliases.** A floating alias changes what you are running on a date the vendor
picked, not a date you picked. A pin changes only when `bump` changes it, and `bump`
changes it only on a RECOMMEND.

**Your own frozen inputs, not a leaderboard.** A public benchmark score tells you how a
model does on someone else's task. The only score that matters for a bump decision is how
it does on yours, so the suite is your own inputs, frozen by a hash, run every time
unchanged.

**Blind, and the key lives outside the page.** A judgment made while you know which output
is the familiar one is not a judgment, it is a habit. The vote page never shows a model id,
and the mapping that would unblind it lives in a separate file the page never reads.

**The cost cap is checked before and during.** A pre-run estimate can still be wrong, for
instance when a model's real output runs longer than its stated ceiling. The meter checks
again before every single call, so a cap is a hard stop, not a hope.

**The decision rule is printed, not implied.** A RECOMMEND or HOLD with no stated rule is
an opinion. Every report prints the exact rule it applied, with every number read from the
suite's policy, so the next person can check the math instead of trusting the verdict.

**A bump is a commit on a branch.** Changing a pin is a code change with a clear before and
after, which means it is also a code change you can revert with one command if it turns
out to be wrong.

**A watch that calls no model.** Checking whether a vendor shipped something newer should
never itself cost money or risk a call to an untested id. `watch` only lists models and
diffs id strings.

## Commands

| command | what it does |
|---|---|
| `pinbench pins` | print the registry as a table |
| `pinbench plan SUITE --incumbent ID --candidate ID [--cap USD]` | estimate cost, no spend |
| `pinbench run SUITE --incumbent ID --candidate ID --cap USD` | run both arms, write run.json |
| `pinbench vote RUN_NAME [--open]` | build the blind A/B page |
| `pinbench report RUN_NAME [--votes votes.json]` | unblind, score, print the verdict |
| `pinbench watch [--seed]` | diff each provider's catalog against the last snapshot |
| `pinbench bump SLOT --to ID --run RUN_NAME` | edit a pin, gated on a RECOMMEND report |

Every command has `--help`. Exit codes: 0 ok, 1 error, 2 refusal before any spend, 3 the
cost cap was exceeded.

## Suite format

A suite is a frozen set of inputs in `suite.yaml` (or `.json` with the same keys): a
`task` string used as the system prompt for both arms, a `max_output_tokens` ceiling, a
`scoring` block (`labels: exact | contains | regex | none`, a `normalize` list, and
whether `votes` are `required | optional | none`), a `policy` block with the decision
rule's knobs, and a list of `items`, each with an `id`, an `input`, and an optional
`expect`. Either every item has `expect` or none do.

Two example suites ship in `examples/suites/`: `capitals.yaml` (12 labeled items, world
capitals, exact match) and `summaries.yaml` (10 unlabeled items, each a short synthetic
paragraph, vote-only).

## Limits

- Two providers: Anthropic and OpenAI. Adding a third is one file in `providers/`.
- Label matching is exact, contains, or regex. There is no LLM-as-judge scoring mode; see
  the design note above on why frozen inputs come first.
- n is whatever you froze. The report states what that n can and cannot detect; a 12-item
  suite cannot show a regression smaller than roughly 8 percentage points.
- Prices are a file you maintain, not a live lookup. An unpriced model refuses to run
  rather than guess.
- The watch comparator is a heuristic over id strings (family and version tokens), not a
  vendor feed. It will miss a rename that does not follow the pattern.
- No confidence intervals, inter-rater agreement, or rotating reviewers; no hosted voting;
  no chat or email notifications, scheduler, or daemon (`watch` is a one-shot you cron); no
  automatic PR creation or rollback tooling; no thinking/reasoning-effort normalization
  across models.

## Development

```
pytest -q
```

Runs entirely offline; `PINBENCH_NO_NETWORK=1` is set by `conftest.py`, and
`test_no_network.py` proves the full CLI pipeline never opens a socket.

To add a provider: create `pinbench/providers/<name>.py` with a `call(...)` and a
`list_models(...)` function matching the shape in `providers/anthropic.py`, then add one
prefix line to `provider_for_id` in `providers/__init__.py`.

## License

MIT. See `LICENSE`.
