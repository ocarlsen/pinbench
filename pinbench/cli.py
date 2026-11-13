"""The pinbench command line: argparse subcommands, each dispatching to one module.

Refuses to do: let any subcommand touch the network or write a pin outside the documented
exit codes (0 ok, 1 error, 2 refusal before spend, 3 cap exceeded, 4 a `run` arm had zero
successes, 5 a `--batch-judge` batch timed out, 6 a `run` stopped on a quota or rate limit and
can be resumed, 129/130/143 a `run` stopped by SIGHUP/SIGINT/SIGTERM and can be resumed). A `run` with SOME but not all items completed on both arms still exits 0 (a
printed warning, not a refusal); only a wholly-failed arm is treated as a hard failure. A
`run` arm with any truncated output (the model hit max_output_tokens) also still exits 0,
with a loud warning naming the count -- `report` is where truncation becomes a hard
INCOMPLETE, and `vote` refuses to show a truncated output unless --allow-truncated.
"""

from __future__ import annotations

import argparse
import shlex
import sys
import webbrowser
from pathlib import Path

from . import blind as blind_mod
from . import bump as bump_mod
from . import cli_config
from . import config as config_mod
from . import prices as prices_mod
from . import registry as registry_mod
from . import report as report_mod
from . import resume as resume_mod
from . import runner as runner_mod
from . import scoring as scoring_mod
from . import watch as watch_mod

DEFAULT_PINS = config_mod.BUILTIN_DEFAULTS["paths"]["pins"]
DEFAULT_PRICES = config_mod.BUILTIN_DEFAULTS["paths"]["prices"]
DEFAULT_STATE = config_mod.BUILTIN_DEFAULTS["paths"]["runs"]
ERROR_TRUNCATE_CHARS = 200


def _run_dir(state_dir, run_name):
    return Path(state_dir) / "runs" / run_name


def cmd_pins(args):
    try:
        config = cli_config.load_config(args)
    except config_mod.ConfigError as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 1
    pins_path = cli_config.resolved_paths(args, config)["pins"][0]
    reg = registry_mod.load(pins_path)
    print(f"{'slot':<14} {'model':<24} {'provider':<10} {'hold_back':<20} sunset")
    for name, slot in reg.slots.items():
        print(f"{name:<14} {slot.model:<24} {slot.provider:<10} {slot.hold_back or '-':<20} {slot.sunset or '-'}")
    return 0


def cmd_plan(args):
    try:
        config = cli_config.load_config(args)
    except config_mod.ConfigError as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 1
    suite, _effective = cli_config.load_suite_with_config(args.suite, config)
    paths = cli_config.resolved_paths(args, config)
    prices = prices_mod.load(paths["prices"][0])
    cap_usd, _cap_source = config_mod.resolve_cap(args.cap, suite.cap_usd, config)
    try:
        result = runner_mod.plan(
            suite, args.incumbent, args.candidate, prices, cap_usd=cap_usd, limit=args.limit,
            state_dir=paths["runs"][0], allow_self_judge=args.allow_self_judge, batch=args.batch, reuse_incumbent=args.reuse_incumbent, batch_judge=args.batch_judge,
        )
    except runner_mod.PlanRefused as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2

    for label, arm in (("incumbent", result.estimate.incumbent), ("candidate", result.estimate.candidate)):
        price = prices.rows[arm.model_id]
        disp_price = price.at_batch() if args.batch else price
        line = (
            f"{label:<9} {arm.model_id:<24} {len(arm.items):>3} items  est ${arm.cost_usd:.4f}  "
            f"(${disp_price.input:.2f}/${disp_price.output:.2f} per Mtok)"
        )
        if label == "incumbent" and result.reuse is not None:
            line += f"  [reused from {result.reuse.run_name}: no calls, $0]"
        if args.batch:
            line += f"  [batch]  batch_multiplier {price.batch_multiplier} (prices.yaml)"
        print(line)
    if result.estimate.judge is not None:
        judge_price = prices.rows[result.judge_id]
        judge_batched = args.batch or args.batch_judge
        judge_disp_price = judge_price.at_batch() if judge_batched else judge_price
        judge_calls = len(result.estimate.judge.items)
        line = (
            f"{'judge':<9} {result.judge_id:<24} {judge_calls:>3} calls  est ${result.estimate.judge.cost_usd:.4f}  "
            f"(${judge_disp_price.input:.2f}/${judge_disp_price.output:.2f} per Mtok)"
        )
        if judge_batched:
            line += f"  [batch]  batch_multiplier {judge_price.batch_multiplier} (prices.yaml)"
        print(line)
    total = result.estimate.total_usd
    if cap_usd is None:
        print(f"total est ${total:.4f}  (no cap given)")
        return 0
    if result.within_cap:
        print(f"total est ${total:.4f} within cap ${cap_usd:.2f}: run would proceed")
        return 0
    print(f"total est ${total:.4f} exceeds cap ${cap_usd:.2f}: run would refuse", file=sys.stderr)
    return 3


def cmd_run(args):
    try:
        config = cli_config.load_config(args)
    except config_mod.ConfigError as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 1
    suite, effective = cli_config.load_suite_with_config(args.suite, config)
    paths = cli_config.resolved_paths(args, config)
    prices = prices_mod.load(paths["prices"][0])
    cap_usd, cap_source = config_mod.resolve_cap(args.cap, suite.cap_usd, config)
    effective["cap_usd"] = {"value": cap_usd, "source": cap_source}
    effective["paths"] = {k: {"value": v, "source": s} for k, (v, s) in paths.items()}
    try:
        record = runner_mod.run(
            suite,
            args.incumbent,
            args.candidate,
            prices,
            cap_usd=cap_usd,
            limit=args.limit,
            name=args.name,
            state_dir=paths["runs"][0],
            suite_path=args.suite,
            allow_self_judge=args.allow_self_judge,
            effective_config=effective,
            batch=args.batch,
            reuse_incumbent=args.reuse_incumbent,
            batch_judge=args.batch_judge,
            poll_interval_s=args.poll_interval,
            max_wait_s=args.max_wait,
            timeout_s=args.timeout_s,
            resume=args.resume,
            resubmit_judge=args.resubmit_judge,
            judge_batch_id=args.judge_batch_id,
        )
    except runner_mod.RunCapExceeded as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 3
    except runner_mod.PlanRefused as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 2

    print(f"run {record['run_name']}: spent ${record['spent_usd']['total']:.4f} of cap ${record['cap_usd']:.2f}")
    print(f"items completed: {record['items_completed']}/{record['items_total']}" + ("  STOPPED EARLY" if record["stopped_early"] else ""))

    zero_success_arms, truncated_arms = _print_arm_lines(record)
    stop_reason = record.get("stop_reason") or ""
    if record.get("status") == "stopped" and (stop_reason == resume_mod.STOP_QUOTA or stop_reason.startswith("signal:")):
        why = "the provider kept returning 429 / quota exhausted after retries" if stop_reason == resume_mod.STOP_QUOTA else f"stopped by {record.get('stop_signal')}"
        print(f"stopped: {why}. Every finished item is saved. Resume with:\n  {_resume_command(args, record['run_name'])}", file=sys.stderr)
        return resume_mod.EXIT_QUOTA_STOP if stop_reason == resume_mod.STOP_QUOTA else resume_mod.exit_code_for_signal(record["stop_signal"])
    if zero_success_arms:
        print(f"error: zero successes on: {', '.join(zero_success_arms)}", file=sys.stderr)
        return 4
    for arm, truncated in truncated_arms:
        print(
            f"warning: {arm} had {truncated}/{record['items_total']} truncated output(s) "
            "(stop/finish reason hit the output ceiling); raise the suite's max_output_tokens "
            "or the report will be INCOMPLETE",
            file=sys.stderr,
        )
    if record["items_completed"] < record["items_total"]:
        print("warning: not every item completed on both arms", file=sys.stderr)
    # One loud line per arm where every judge call was skipped: otherwise a judge
    # recall/extra built on zero actual calls for that arm looks like a clean measurement.
    if record.get("judge_model"):
        for arm in scoring_mod.judge_arms_fully_skipped(record):
            n = scoring_mod.judge_skipped_count(record, arm)
            print(f"warning: judge was never called for {arm} (all {n} item(s) skipped: truncated/error)", file=sys.stderr)
    judge_batch = record.get("judge_batch") or {}
    if judge_batch.get("status") == "timed_out":
        print(
            f"error: judge batch {judge_batch.get('id')} did not finish within --max-wait and is still running; "
            f"no item was judged, and its cost is recorded as the reserved estimate ${record['spent_usd'].get('judge', 0.0):.4f}",
            file=sys.stderr,
        )
        return 5
    return 0


def _resume_command(args, run_name):
    """The exact command that continues this run: same suite and arms, same flags, --resume NAME."""
    parts = ["pinbench", "run", args.suite, "--incumbent", args.incumbent, "--candidate", args.candidate]
    for flag, value in (("--cap", args.cap), ("--limit", args.limit), ("--prices", args.prices), ("--state", args.state),
                        ("--reuse-incumbent", args.reuse_incumbent), ("--config", args.config)):
        if value is not None:
            parts += [flag, str(value)]
    if args.allow_self_judge:
        parts.append("--allow-self-judge")
    if args.batch_judge:
        parts.append("--batch-judge")
    if args.timeout_s != runner_mod.DEFAULT_TIMEOUT_S:
        parts += ["--timeout", str(args.timeout_s)]
    parts += ["--resume", run_name]
    return " ".join(shlex.quote(p) for p in parts)


def _print_arm_lines(record):
    """Print one summary line per arm; return (zero_success_arms, [(arm, truncated_count)])
    for cmd_run's exit-code and warning decisions."""
    zero_success_arms = []
    truncated_arms = []
    for arm in ("incumbent", "candidate"):
        attempted = record["items_attempted"][arm]
        succeeded = record["items_succeeded"][arm]
        truncated = scoring_mod.truncated_count(record, arm)
        line = f"{arm}: {succeeded}/{attempted} succeeded"
        if arm == "incumbent" and record.get("reused_incumbent"):
            line += f" (reused from {record['reused_incumbent']['run']}, no calls made)"
        if truncated:
            line += f", {truncated} truncated"
        if succeeded < attempted:
            first_err = scoring_mod.first_error(record, arm)
            if first_err:
                line += f"; first error: {first_err[:ERROR_TRUNCATE_CHARS]}"
        print(line)
        if attempted > 0 and succeeded == 0:
            zero_success_arms.append(arm)
        if truncated:
            truncated_arms.append((arm, truncated))
    return zero_success_arms, truncated_arms


def cmd_vote(args):
    try:
        config = cli_config.load_config(args)
    except config_mod.ConfigError as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 1
    args.state = cli_config.resolved_paths(args, config)["runs"][0]
    run = runner_mod.load_run(args.state, args.run_name)
    missing = blind_mod.missing_output_item_arms(run)
    if missing and not args.allow_missing:
        detail = ", ".join(f"{item_id}/{arm}" for item_id, arm in missing)
        print(
            f"refused: {len(missing)} item/arm pair(s) have no output ({detail}); "
            "pass --allow-missing to render them as error cards",
            file=sys.stderr,
        )
        return 1

    truncated = blind_mod.truncated_item_arms(run)
    if truncated and not args.allow_truncated:
        detail = ", ".join(f"{item_id}/{arm}" for item_id, arm in truncated)
        print(
            f"refused: {len(truncated)} item/arm pair(s) have a truncated output ({detail}); "
            "pass --allow-truncated to render them marked as truncated",
            file=sys.stderr,
        )
        return 1

    run_dir = _run_dir(args.state, args.run_name)
    mapping_path, html_path = blind_mod.publish(run, run_dir, args.run_name)
    print(str(html_path))
    if missing:
        print(f"warning: {len(missing)} item/arm pair(s) rendered as error cards (--allow-missing)", file=sys.stderr)
    if truncated:
        print(f"warning: {len(truncated)} item/arm pair(s) rendered marked truncated (--allow-truncated)", file=sys.stderr)
    if args.open:
        webbrowser.open(html_path.resolve().as_uri())
    return 0


def cmd_report(args):
    try:
        config = cli_config.load_config(args)
    except config_mod.ConfigError as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 1
    args.state = cli_config.resolved_paths(args, config)["runs"][0]
    run = runner_mod.load_run(args.state, args.run_name)
    suite = report_mod.load_suite_for_run(run)

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

    run_dir = _run_dir(args.state, args.run_name)
    (run_dir / "report.md").write_text(report_mod.render(run, suite, result, fmt="md"))
    report_mod.write_verdict_record(run_dir, run, result, votes_path=args.votes)
    return 0


def cmd_watch(args):
    try:
        config = cli_config.load_config(args)
    except config_mod.ConfigError as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 1
    paths = cli_config.resolved_paths(args, config)
    lines, code = watch_mod.run_watch(paths["pins"][0], paths["runs"][0], seed=args.seed)
    for line in lines:
        print(line)
    return code


def cmd_bump(args):
    try:
        config = cli_config.load_config(args)
    except config_mod.ConfigError as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 1
    paths = cli_config.resolved_paths(args, config)
    try:
        result = bump_mod.bump(
            paths["pins"][0],
            args.slot,
            args.to,
            args.run_name,
            state_dir=paths["runs"][0],
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


def _positive_int(text):
    try:
        value = int(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"{text!r} is not an integer") from None
    if value < 1:
        raise argparse.ArgumentTypeError(f"must be at least 1, got {value}")
    return value


def _add_config_arg(p):
    p.add_argument("--config", default=None, help="path to pinbench.yaml (default: walk up from cwd)")


def _add_arm_args(p):
    """--cap is never argparse-required: `run` still refuses with no cap, but the cap may
    come from the suite's own cap_usd or pinbench.yaml, not only --cap (see cli_config.load_config
    / config.resolve_cap and runner.run's own refusal when every source is empty)."""
    p.add_argument("suite")
    p.add_argument("--incumbent", required=True)
    p.add_argument("--candidate", required=True)
    p.add_argument("--cap", type=float, default=None)
    p.add_argument("--limit", type=_positive_int, default=None)
    p.add_argument("--prices", default=None)
    p.add_argument("--state", default=None)
    p.add_argument("--allow-self-judge", action="store_true", help="allow the suite's judge model to also be an arm")
    p.add_argument(
        "--batch", action="store_true",
        help="send every Anthropic arm (and judge, if any) as one Message Batch at the batch discount; Anthropic-only",
    )
    p.add_argument(
        "--batch-judge", action="store_true", dest="batch_judge",
        help="run the arms synchronously, then send every judge call as ONE Message Batch at the judge's batch price; the judge must be Anthropic with a batch_multiplier; not with --batch",
    )
    p.add_argument(
        "--reuse-incumbent", default=None, metavar="RUN",
        help="reuse the incumbent's stored outputs and judge records from run RUN instead of calling it again; refuses unless suite, judge and request params match (not with --limit or --batch)",
    )
    _add_config_arg(p)


def build_parser():
    parser = argparse.ArgumentParser(prog="pinbench", description="Pin your models. Bench a candidate before you switch.")
    sub = parser.add_subparsers(dest="command", required=True)

    p_pins = sub.add_parser("pins", help="print the registry as a table")
    p_pins.add_argument("--pins", default=None)
    _add_config_arg(p_pins)
    p_pins.set_defaults(func=cmd_pins)

    p_plan = sub.add_parser("plan", help="estimate cost without spending")
    _add_arm_args(p_plan)
    p_plan.set_defaults(func=cmd_plan)

    p_run = sub.add_parser("run", help="run both arms over a suite")
    _add_arm_args(p_run)
    p_run.add_argument("--name", default=None, help="run directory name (default: suite-UTCstamp-random); refused if runs/NAME/run.json exists unless --resume NAME")
    p_run.add_argument("--resume", default=None, metavar="NAME", help="continue the stopped or crashed run NAME: skips finished items, re-runs errored and never-attempted ones, counts spend so far against --cap; refuses unless suite, arms, request params, judge and reuse source match what the run recorded")
    p_run.add_argument("--resubmit-judge", action="store_true", dest="resubmit_judge", help="with --resume --batch-judge: submit a NEW judge batch although the recorded submit outcome is unknown (a batch may exist; check the Anthropic console first)")
    p_run.add_argument("--judge-batch-id", default=None, dest="judge_batch_id", metavar="ID", help="with --resume --batch-judge: attach a judge batch found in the Anthropic console instead of submitting one")
    p_run.add_argument("--timeout", type=_positive_int, default=runner_mod.DEFAULT_TIMEOUT_S, dest="timeout_s", help="seconds to wait for each arm and judge call before giving up (default 60; not used with --batch)")
    p_run.add_argument("--poll-interval", type=_positive_int, default=30, dest="poll_interval", help="seconds between batch status polls (--batch or --batch-judge)")
    p_run.add_argument("--max-wait", type=int, default=86400, dest="max_wait", help="seconds to wait for a batch before giving up, leaving it running (--batch or --batch-judge)")
    p_run.set_defaults(func=cmd_run)

    p_vote = sub.add_parser("vote", help="build the blind A/B vote page")
    p_vote.add_argument("run_name")
    p_vote.add_argument("--state", default=None)
    p_vote.add_argument("--open", action="store_true")
    p_vote.add_argument("--allow-missing", action="store_true", help="render a missing-output item as an error card instead of refusing")
    p_vote.add_argument("--allow-truncated", action="store_true", help="render a truncated-output item marked truncated instead of refusing")
    _add_config_arg(p_vote)
    p_vote.set_defaults(func=cmd_vote)

    p_report = sub.add_parser("report", help="unblind, score, and print the verdict")
    p_report.add_argument("run_name")
    p_report.add_argument("--votes", default=None)
    p_report.add_argument("--state", default=None)
    p_report.add_argument("--format", choices=["text", "md", "json"], default="text")
    _add_config_arg(p_report)
    p_report.set_defaults(func=cmd_report)

    p_watch = sub.add_parser("watch", help="diff provider catalogs against the last snapshot")
    p_watch.add_argument("--pins", default=None)
    p_watch.add_argument("--state", default=None)
    p_watch.add_argument("--seed", action="store_true")
    _add_config_arg(p_watch)
    p_watch.set_defaults(func=cmd_watch)

    p_bump = sub.add_parser("bump", help="edit a pin, gated on a RECOMMEND report")
    p_bump.add_argument("slot")
    p_bump.add_argument("--to", required=True)
    p_bump.add_argument("--run", dest="run_name", required=True)
    p_bump.add_argument("--pins", default=None)
    p_bump.add_argument("--state", default=None)
    p_bump.add_argument("--no-git", action="store_true")
    p_bump.add_argument("--force", default=None, metavar="REASON")
    _add_config_arg(p_bump)
    p_bump.set_defaults(func=cmd_bump)

    p_config = sub.add_parser("config", help="print the effective settings and where each came from")
    p_config.add_argument("--suite", default=None, help="also resolve this suite's cap_usd/judge/policy overrides")
    _add_config_arg(p_config)
    p_config.set_defaults(func=cli_config.cmd_config)

    return parser


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except resume_mod.RunInterrupted as exc:
        print(f"interrupted by {exc}: finished items are saved in run.json; continue with --resume", file=sys.stderr)
        return 128 + exc.signum
    except KeyboardInterrupt:
        # A batch left running during `run --batch` prints its own ids/cancel-instructions
        # (pinbench.batch.run_batch) before this unwinds; nothing further to say here.
        return 130
    except Exception as exc:  # noqa: BLE001 - last-resort: report, never a bare traceback
        print(f"error: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
