# pinbench report: capitals-demo

**claude-sonnet-4-5** (incumbent) vs **gpt-4o-mini** (candidate)

- Items: 12/12 completed
- Spend: $0.0026 of $0.10 cap (estimate $0.0148)

## Checks

| check | result | detail |
|---|---|---|
| completeness | PASS | n=12 >= 10 |
| errors | PASS | incumbent and candidate both had 0 errors |
| votes_present | PASS | votes not required for this suite |
| labels | FAIL | candidate 83.3% (10/12) vs incumbent 100.0% (12/12); tolerance 0.0 points; FAIL |
| cost | PASS | candidate $0.000010/item vs incumbent $0.000210/item (ratio 0.05, allowed 1.25); PASS |

## Verdict: HOLD

a candidate is recommended when it is non-inferior on labels (within 0.0 points), non-losing on blind votes (ties excluded), and costs no more than 125% of the incumbent per item, over at least 10 completed items with no errors. 12 items can show a large regression, not a small one (1/n = 8.3 points); the downside of a wrong RECOMMEND is bounded by `pinbench bump` being one reverted commit.
