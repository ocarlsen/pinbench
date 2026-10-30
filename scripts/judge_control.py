"""Negative control for a judge suite: send deliberately bad summaries through its judge.

Usage: python scripts/judge_control.py SUITE CONTROLS [--prices PRICES] [--out PATH]

Each control is graded exactly as an arm's output would be (judge.judge_output, same rubric,
same model, same ceiling). A control PASSES when the judge satisfies at most `max_hits` rubric
points and counts at least `min_extra` unsupported claims. Exits 1 if any control fails or the
judge errors, so a lenient judge is a red result, not a footnote. Needs the judge provider's
API key in the environment; prints the judge's spend.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import os
import sys
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from pinbench import judge, prices as prices_mod, providers, suite as suite_mod  # noqa: E402


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("suite")
    ap.add_argument("controls")
    ap.add_argument("--prices", default="examples/prices.yaml")
    ap.add_argument("--out", default=None)
    args = ap.parse_args(argv)

    s = suite_mod.load(args.suite)
    if s.scoring.labels != "judge":
        print(f"error: {args.suite} is not a judge suite", file=sys.stderr)
        return 2
    spec = yaml.safe_load(Path(args.controls).read_text())
    if spec.get("suite") != s.name:
        print(f"error: controls are for suite {spec.get('suite')!r}, not {s.name!r}", file=sys.stderr)
        return 2
    max_hits = spec["expect"]["max_hits"]
    min_extra = spec["expect"]["min_extra"]
    items = {i.id: i for i in s.items}

    judge_id = s.judge.model
    row = prices_mod.price_row(prices_mod.load(args.prices), judge_id)
    if row is None:
        print(f"error: no price row for judge {judge_id}", file=sys.stderr)
        return 2
    env_key = providers.ENV_KEYS[providers.provider_for_id(judge_id)]
    api_key = os.environ.get(env_key)
    if not api_key:
        print(f"error: set {env_key}", file=sys.stderr)
        return 2

    results, spent, failed = [], 0.0, 0
    for c in spec["controls"]:
        item = items[c["id"]]
        outcome, reply = judge.judge_output(judge_id, item, s, c["summary"], api_key, params=row.request_defaults)
        cost = ((reply.input_tokens or 0) * row.input + (reply.output_tokens or 0) * row.output) / 1e6
        spent += cost
        ok = outcome.error is None and len(outcome.hit) <= max_hits and outcome.extra >= min_extra
        failed += not ok
        results.append({"id": c["id"], "hit": outcome.hit, "extra": outcome.extra, "notes": outcome.notes,
                        "error": outcome.error, "pass": ok, "cost_usd": round(cost, 6)})
        status = "PASS" if ok else "FAIL"
        print(f"{c['id']}  {status}  hits {len(outcome.hit)}/{len(item.rubric_points)}  extra {outcome.extra}"
              + (f"  error: {outcome.error}" if outcome.error else ""))

    record = {"suite": s.name, "judge_model": judge_id, "judge_prompt_hash": judge.prompt_hash(s.judge.rubric),
              "controls": args.controls, "expect": spec["expect"],
              "ran_at": _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds"),
              "spent_usd": round(spent, 6), "results": results}
    out = Path(args.out or f".pinbench/controls/{s.name}-{_dt.datetime.now().strftime('%Y%m%dT%H%M%S')}.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(record, indent=2))
    print(f"{len(results) - failed}/{len(results)} controls passed; judge spend ${spent:.4f}; wrote {out}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
