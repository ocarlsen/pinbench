"""The pinbench command line: argparse subcommands, each dispatching to one module.

Refuses to do: let any subcommand touch the network or write a pin outside the documented
exit codes (0 ok, 1 error, 2 refusal before spend, 3 cap exceeded).
"""

from __future__ import annotations

import argparse
import sys
import webbrowser
from pathlib import Path

from . import blind as blind_mod
from . import bump as bump_mod
from . import prices as prices_mod
from . import registry as registry_mod
from . import report as report_mod
from . import runner as runner_mod
from . import suite as suite_mod
from . import watch as watch_mod

DEFAULT_PINS = "examples/pins.yaml"
DEFAULT_PRICES = "examples/prices.yaml"
DEFAULT_STATE = ".pinbench"


def _run_dir(state_dir, run_name):
    return Path(state_dir) / "runs" / run_name


def cmd_pins(args):
    reg = registry_mod.load(args.pins)
    print(f"{'slot':<14} {'model':<24} {'provider':<10} {'hold_back':<20} sunset")
    for name, slot in reg.slots.items():
        print(f"{name:<14} {slot.model:<24} {slot.provider:<10} {slot.hold_back or '-':<20} {slot.sunset or '-'}")
    return 0


def cmd_plan(args):
    suite = suite_mod.load(args.suite)
    prices = prices_mod.load(args.prices)
    try:
        result = runner_mod.plan(suite, args.incumbent, args.candidate, prices, cap_usd=args.cap, limit=args.limit, state_dir=args.state)
    except runner_mod.PlanRefused as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2

    for label, arm in (("incumbent", result.estimate.incumbent), ("candidate", result.estimate.candidate)):
        price = prices.rows[arm.model_id]
        print(
            f"{label:<9} {arm.model_id:<24} {len(arm.items):>3} items  est ${arm.cost_usd:.4f}  "
            f"(${price.input:.2f}/${price.output:.2f} per Mtok)"
        )
    total = result.estimate.total_usd
    if args.cap is None:
        print(f"total est ${total:.4f}  (no --cap given)")
        return 0
    if result.within_cap:
        print(f"total est ${total:.4f} within cap ${args.cap:.2f}: run would proceed")
        return 0
    print(f"total est ${total:.4f} exceeds cap ${args.cap:.2f}: run would refuse", file=sys.stderr)
    return 3


def cmd_run(args):
    suite = suite_mod.load(args.suite)
    prices = prices_mod.load(args.prices)
    try:
        record = runner_mod.run(
            suite,
            args.incumbent,
            args.candidate,
            prices,
            cap_usd=args.cap,
            limit=args.limit,
            name=args.name,
            state_dir=args.state,
            suite_path=args.suite,
        )
    except runner_mod.RunCapExceeded as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 3
    except runner_mod.PlanRefused as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2

    print(f"run {record['run_name']}: spent ${record['spent_usd']['total']:.4f} of cap ${record['cap_usd']:.2f}")
    print(f"items completed: {record['items_completed']}/{record['items_total']}" + ("  STOPPED EARLY" if record["stopped_early"] else ""))
    return 0


def cmd_vote(args):
    run = runner_mod.load_run(args.state, args.run_name)
    run_dir = _run_dir(args.state, args.run_name)
    mapping_path, html_path = blind_mod.publish(run, run_dir, args.run_name)
    print(str(html_path))
    if args.open:
        webbrowser.open(html_path.resolve().as_uri())
    return 0


def cmd_report(args):
    run = runner_mod.load_run(args.state, args.run_name)
    suite = suite_mod.load(run["suite"])

    votes = None
    mapping = None
    if args.votes:
        try:
            votes = report_mod.load_votes(args.votes)
        except Exception as exc:  # noqa: BLE001
            print(f"refused: could not load votes: {exc}", file=sys.stderr)
            return 1
        mapping_path = _run_dir(args.state, args.run_name) / "mapping.json"
        if not mapping_path.exists():
            print("refused: no mapping.json for this run; run `pinbench vote` first", file=sys.stderr)
            return 1
        mapping = report_mod.load_mapping(mapping_path)

    try:
        result = report_mod.build_verdict(run, suite, votes, mapping)
    except ValueError as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 1

    text = report_mod.render(run, suite, result, fmt=args.format)
    print(text, end="")

    report_path = _run_dir(args.state, args.run_name) / "report.md"
    report_path.write_text(report_mod.render(run, suite, result, fmt="md"))
    return 0


def cmd_watch(args):
    lines, code = watch_mod.run_watch(args.pins, args.state, seed=args.seed)
    for line in lines:
        print(line)
    return code


def cmd_bump(args):
    try:
        result = bump_mod.bump(
            args.pins,
            args.slot,
            args.to,
            args.run_name,
            state_dir=args.state,
            no_git=args.no_git,
            force_reason=args.force,
        )
    except bump_mod.BumpRefused as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2

    print(result.message)
    if result.committed:
        print(f"branch: {result.branch}")
        print("next step: open a PR from this branch")
    return 0


def build_parser():
    parser = argparse.ArgumentParser(prog="pinbench", description="Pin your models. Bench a candidate before you switch.")
    sub = parser.add_subparsers(dest="command", required=True)

    p_pins = sub.add_parser("pins", help="print the registry as a table")
    p_pins.add_argument("--pins", default=DEFAULT_PINS)
    p_pins.set_defaults(func=cmd_pins)

    p_plan = sub.add_parser("plan", help="estimate cost without spending")
    p_plan.add_argument("suite")
    p_plan.add_argument("--incumbent", required=True)
    p_plan.add_argument("--candidate", required=True)
    p_plan.add_argument("--cap", type=float, default=None)
    p_plan.add_argument("--limit", type=int, default=None)
    p_plan.add_argument("--prices", default=DEFAULT_PRICES)
    p_plan.add_argument("--state", default=DEFAULT_STATE)
    p_plan.set_defaults(func=cmd_plan)

    p_run = sub.add_parser("run", help="run both arms over a suite")
    p_run.add_argument("suite")
    p_run.add_argument("--incumbent", required=True)
    p_run.add_argument("--candidate", required=True)
    p_run.add_argument("--cap", type=float, required=True)
    p_run.add_argument("--limit", type=int, default=None)
    p_run.add_argument("--name", default=None)
    p_run.add_argument("--prices", default=DEFAULT_PRICES)
    p_run.add_argument("--state", default=DEFAULT_STATE)
    p_run.set_defaults(func=cmd_run)

    p_vote = sub.add_parser("vote", help="build the blind A/B vote page")
    p_vote.add_argument("run_name")
    p_vote.add_argument("--state", default=DEFAULT_STATE)
    p_vote.add_argument("--open", action="store_true")
    p_vote.set_defaults(func=cmd_vote)

    p_report = sub.add_parser("report", help="unblind, score, and print the verdict")
    p_report.add_argument("run_name")
    p_report.add_argument("--votes", default=None)
    p_report.add_argument("--state", default=DEFAULT_STATE)
    p_report.add_argument("--format", choices=["text", "md", "json"], default="text")
    p_report.set_defaults(func=cmd_report)

    p_watch = sub.add_parser("watch", help="diff provider catalogs against the last snapshot")
    p_watch.add_argument("--pins", default=DEFAULT_PINS)
    p_watch.add_argument("--state", default=DEFAULT_STATE)
    p_watch.add_argument("--seed", action="store_true")
    p_watch.set_defaults(func=cmd_watch)

    p_bump = sub.add_parser("bump", help="edit a pin, gated on a RECOMMEND report")
    p_bump.add_argument("slot")
    p_bump.add_argument("--to", required=True)
    p_bump.add_argument("--run", dest="run_name", required=True)
    p_bump.add_argument("--pins", default=DEFAULT_PINS)
    p_bump.add_argument("--state", default=DEFAULT_STATE)
    p_bump.add_argument("--no-git", action="store_true")
    p_bump.add_argument("--force", default=None, metavar="REASON")
    p_bump.set_defaults(func=cmd_bump)

    return parser


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except Exception as exc:  # noqa: BLE001 - last-resort: report, never a bare traceback
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
