# pinbench report: review-live2

**claude-sonnet-5-5** (incumbent) vs **claude-haiku-5-5** (candidate)

Config: pinbench.yaml  (sha256:6a21eab5feaf0b5972196468c65644d4dbc0c9d7f33b1a71cd5dba64c6d84579)

Judge: `claude-opus-5-5` (prompt `sha256:92391065edf0966576ee0d467d982e87c54ed70f30766e96e0c1c56a85f480ad`)

- Items: 6/6 completed
- Spend: $0.1261 of $1.50 cap (estimate $1.2507)

## Checks

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

## Verdict: RECOMMEND

a candidate is recommended when it is non-inferior on judge recall (within 0.0 points) and no worse than 0.5 extra findings per item than the incumbent (judge errors make the run INCOMPLETE, never guessed), non-losing on blind votes (ties excluded), and costs no more than 125% of the incumbent per item, over at least 6 completed items with no errors and no more than 0 truncated output(s) per arm. 6 items can show a large regression, not a small one (1/n = 16.7 points); the downside of a wrong RECOMMEND is bounded by `pinbench bump` being one reverted commit.
