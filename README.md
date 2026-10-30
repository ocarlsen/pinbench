# pinbench

Pin your models. Bench a candidate on your own inputs before you switch.

A model upgrade is a deploy, and pinbench treats it like one. Every model you depend on is a
pinned id in a registry file, never a floating "latest" alias.

## How it works

| step | command | what it does |
|---|---|---|
| 1 | `pinbench watch` | Diffs each vendor's catalog against the last snapshot and names a newer id in any slot you use. Calls no model. |
| 2 | `pinbench run` | Sends a frozen suite of your own inputs through incumbent and candidate. Refuses to start over your cost cap; stops if spend reaches it. |
| 3 | `pinbench vote` | Builds a blind A/B page. You judge outputs without knowing which model wrote which. |
| 4 | `pinbench report` | Unblinds the votes, scores against any labels, prints RECOMMEND / HOLD with the rule it applied. |
| 5 | `pinbench bump` | Changes the pin only on a RECOMMEND for that exact pair, committed on a branch as a reviewable diff. |

## The problem

Upgrading by feel works until a newer model is worse at your task and a user tells you first.
"Better in most benchmarks" is true and not the question. The question is whether it is
better at the one thing you built on it, and only your own inputs can answer that.

What this looked like in a small production pipeline:

- A newer flagship model, tried as the judge in one step, agreed with 99 hand-labeled answers
  less often than the older model it would have replaced: 83.8% against 88.9%. It also passed
  an answer with an invented product detail that the older model had caught. On a floating
  alias, that regression ships on the vendor's release day.
- The first candidate run through this process, at roughly half the price of the incumbent,
  won 5, lost 4, and tied 4 of 13 frozen blind votes. It was switched in, because the
  downside was one reverted commit.
- The next candidate lost 4 to 6 with 3 ties against the same incumbent and was kept out.

Neither decision took more than reading a printed report. The full story:
[the essay](https://www.ocarlsen.com/model-upgrades.html).

## Quickstart

```
pip install -e ".[dev]"
export ANTHROPIC_API_KEY=sk-ant-...
pinbench plan examples/suites/summaries.yaml --incumbent claude-sonnet-5-5 --candidate claude-haiku-5-5 --cap 0.25
pinbench run examples/suites/summaries.yaml --incumbent claude-sonnet-5-5 --candidate claude-haiku-5-5 --cap 0.25 --name demo
pinbench vote demo --open
pinbench report demo --votes ~/Downloads/votes.json
```

The summaries suite is 10 short passages; the live run below cost about two cents. To compare
across providers, also export `OPENAI_API_KEY` or `GEMINI_API_KEY` and pass that provider's
model id (some OpenAI models require a verified organization).

Your app reads its model id from the pins file, so merging a bump PR is what switches it:

```python
import pinbench

model = pinbench.pin("summarizer")  # reads ./pins.yaml; pass a path as the second argument
```

A third example suite, `examples/suites/review.yaml`, skips the vote page and scores with a
fixed judge model; see [Judge scoring](docs/judge-scoring.md).

## Sample report (live run)

A real run on 2026-10-26: should a summarization job move from `claude-sonnet-5-5` to the much
cheaper `claude-haiku-5-5`? Ten passages, both models, then ten blind votes by a human who did
not know which side was which.

```
# pinbench report: summaries-v4

**claude-sonnet-5-5** (incumbent) vs **claude-haiku-5-5** (candidate)

- Items: 10/10 completed
- Spend: $0.0209 of $0.25 cap (estimate $0.0412)

## Checks

| check | result | detail |
|---|---|---|
| completeness | PASS | n=10 >= 10 |
| errors | PASS | incumbent and candidate both had 0 errors |
| truncation | PASS | incumbent 0 truncated, candidate 0 truncated; allowed 0 per arm; PASS |
| votes_present | PASS | votes cover all 10 completed items |
| votes | PASS | candidate 1, incumbent 1, ties 8 of 10; PASS (non-losing) |
| cost | PASS | candidate $0.000093/item vs incumbent $0.002001/item (ratio 0.05, allowed 1.25); PASS |

## Verdict: RECOMMEND

a candidate is recommended when it is non-inferior on labels (within 0.0 points), non-losing on blind votes (ties excluded), and costs no more than 125% of the incumbent per item, over at least 10 completed items with no errors and no more than 0 truncated output(s) per arm. 10 items can show a large regression, not a small one (1/n = 10.0 points); the downside of a wrong RECOMMEND is bounded by `pinbench bump` being one reverted commit.
```

Eight ties and one win each, and the candidate costs about a twentieth as much per item, so
the verdict is RECOMMEND. The rule says what that is worth: ten items can catch a large
regression, not a small one, and the switch is one reverted commit away.

The blind vote page from the same run. The instructions at the top are all a voter sees; model
ids never appear.

![vote page screenshot](docs/vote-page.png)

## Documentation

- [Commands](docs/commands.md): every subcommand, flag, exit code, refusal and warning.
- [Configuration](docs/configuration.md): the optional `pinbench.yaml` and the precedence rule.
- [Suite format](docs/suite-format.md): the fields of a suite file and the three example suites.
- [Judge scoring](docs/judge-scoring.md): one fixed, blind judge model for open-ended tasks; the live judge run.
- [What the live runs caught](docs/live-runs.md): four failures that became shipped checks, and a HOLD for contrast.
- [Design decisions](docs/design.md): why pins, frozen inputs, a blind page, a hard cap, a printed rule, a bump on a branch.
- [Limits](docs/limits.md): what pinbench does not do; adding a provider.

## Development

```
pytest -q
```

Runs entirely offline; `PINBENCH_NO_NETWORK=1` is set by `conftest.py`, and
`test_no_network.py` proves the full CLI pipeline never opens a socket. Adding a provider is
described in [Limits](docs/limits.md).

## License

MIT. See `LICENSE`.
