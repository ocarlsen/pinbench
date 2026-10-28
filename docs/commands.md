# Commands

Every `pinbench` subcommand, its flags, exit codes, and what each command refuses or warns about. Part of [pinbench](../README.md).

| command | what it does |
|---|---|
| `pinbench pins` | print the registry as a table |
| `pinbench plan SUITE --incumbent ID --candidate ID [--cap USD] [--allow-self-judge]` | estimate cost, no spend |
| `pinbench run SUITE --incumbent ID --candidate ID [--cap USD] [--allow-self-judge]` | run both arms (and the judge, if any), write run.json |
| `pinbench vote RUN_NAME [--open] [--allow-missing] [--allow-truncated]` | build the blind A/B page |
| `pinbench report RUN_NAME [--votes votes.json]` | unblind, score, print the verdict |
| `pinbench watch [--seed]` | diff each provider's catalog against the last snapshot |
| `pinbench bump SLOT --to ID --run RUN_NAME` | edit a pin, gated on a RECOMMEND report |
| `pinbench config [--suite SUITE]` | print the effective settings and where each one came from |

Every command has `--help` and takes `--config PATH` (see [Configuration](configuration.md)).
`plan`/`run` take `--prices`, `pins`/`watch`/`bump` take `--pins`, and every command that
touches run state takes `--state`. `--cap` on `plan`/`run` can also come from the suite or
`pinbench.yaml`.

Other flags:

| flag | on | what it does |
|---|---|---|
| `--limit N` | `plan`, `run` | use only the first N items of the suite, for both the estimate and the run; `items_total` in `run.json` is then N |
| `--format text\|md\|json` | `report` | the format printed to stdout (default `text`); the run's `report.md` is always written in Markdown regardless |
| `--no-git` | `bump` | edit the pins file and stop: no branch, no commit |
| `--force REASON` | `bump` | skip the RECOMMEND gate; the reason is written into the commit message as `(forced: REASON, run RUN_NAME)` |

`report` also writes `verdict.json` beside `report.md`: the verdict plus a SHA-256 of every
input it came from (`run.json`, the suite file, and the votes file and `mapping.json` when votes
were given).

`bump` never reads the verdict from `report.md`'s text. It refuses unless `verdict.json` exists,
every recorded hash still matches, and re-deriving the verdict from those inputs gives the same
RECOMMEND. A hand-edited `report.md` or `verdict.json` is refused; rerun `pinbench report`.

Without `--no-git`, `bump` creates branch `bump/SLOT-ID` in the pins file's repository before it
touches the pins file, so an existing branch is a refusal with nothing edited. When `--to` is
another provider's id, the slot's `provider:` line is switched with it. It then commits
the pins file and the run's `report.md` there, and prints "next step: open a PR from this
branch". If the edit or commit fails, the pins file and the original branch are restored and the
new branch is deleted.

Exit codes:

| code | meaning |
|---|---|
| 0 | ok |
| 1 | error (including `vote` refusing a run with missing output) |
| 2 | refusal before any spend |
| 3 | the cost cap was exceeded |
| 4 | a `run` where one arm got zero successes |

Refusals and warnings:

- `run` prints a per-arm `succeeded/attempted` line and the first error (truncated) when an arm
  had any. Some-but-not-all items completed is a warning at exit 0; a wholly-failed arm is
  exit 4.
- `run` records each arm's stop/finish reason verbatim (`stop_reason` in `run.json`, from
  Anthropic's `stop_reason` or OpenAI's `finish_reason`) and prints a per-arm truncated count
  plus a loud warning when any output hit the ceiling. Not a refusal: whether a few truncated
  items sink the verdict is `report`'s call, gated by `policy.max_truncated`.
- `vote` refuses (exit 1) when any item is missing output on either arm, naming the item/arm
  pairs; `--allow-missing` renders those as explicit error cards. It also refuses when any
  output was truncated; `--allow-truncated` renders those cards with a visible "truncated"
  badge.
- `report` folds any per-arm error, or more than `policy.max_truncated` (default 0) truncated
  outputs on either arm, into an INCOMPLETE verdict. A 4xx error before the model generated
  anything costs $0 in `run.json`, never the pre-run estimate.
