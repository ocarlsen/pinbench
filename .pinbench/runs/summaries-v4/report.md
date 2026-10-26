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
