"""CLI-facing config wiring: resolve pinbench.yaml + CLI flags + a loaded suite into the
effective settings `cli.py`'s subcommands use, plus the `pinbench config` subcommand itself.

Refuses to do: decide a setting's value itself -- every resolution here calls into
`config.py`'s precedence resolvers (cli > suite > pinbench.yaml > built-in); this module
only wires those resolvers to argparse Namespaces and a loaded suite, and prints the result.
"""

from __future__ import annotations

import sys

from . import config as config_mod
from . import suite as suite_mod


def load_config(args) -> config_mod.Config:
    return config_mod.load_effective(getattr(args, "config", None))


def resolved_paths(args, config: config_mod.Config) -> dict:
    """{key: (value, source)} for pins/prices/runs, honoring whichever of those flags this
    command exposes (a flag the command does not have is simply absent from `args`)."""
    cli_overrides = {
        "pins": getattr(args, "pins", None),
        "prices": getattr(args, "prices", None),
        "runs": getattr(args, "state", None),
    }
    return config_mod.resolve_paths(cli_overrides, config)


def load_suite_with_config(suite_path, config: config_mod.Config):
    """Load a suite with pinbench.yaml's judge/policy fallbacks already merged in, plus the
    full effective-settings dict (for run.json / `pinbench config`)."""
    judge_defaults, judge_sources = config_mod.resolve_judge_defaults(config)
    policy_defaults, policy_sources = config_mod.resolve_policy_defaults(config)
    suite = suite_mod.load(suite_path, judge_defaults=judge_defaults, policy_defaults=policy_defaults)
    effective = {
        "config_path": str(config.path) if config.path else None,
        "config_hash": config.content_hash,
        "judge_defaults": {"value": judge_defaults, "source": judge_sources},
        "policy_defaults": {"value": policy_defaults, "source": policy_sources},
    }
    return suite, effective


def _judge_rows(suite, judge_defaults, judge_sources, override_keys):
    """[(key, value, source)] -- when `suite` is a judge-mode suite, its own resolved
    suite.judge wins per key (source "suite" if the suite file set that key itself, else
    whichever source judge_defaults already had); with no suite, just judge_defaults."""
    if suite is not None and suite.scoring.labels == "judge":
        return [
            (key, getattr(suite.judge, key), "suite" if key in override_keys else judge_sources[key])
            for key in sorted(judge_defaults)
        ]
    return [(key, judge_defaults[key], judge_sources[key]) for key in sorted(judge_defaults)]


def _policy_rows(suite, policy_defaults, policy_sources, override_keys):
    if suite is not None:
        return [
            (key, getattr(suite.policy, key), "suite" if key in override_keys else policy_sources[key])
            for key in sorted(policy_defaults)
        ]
    return [(key, policy_defaults[key], policy_sources[key]) for key in sorted(policy_defaults)]


def cmd_config(args):
    try:
        config = load_config(args)
    except config_mod.ConfigError as exc:
        print(f"refused: {exc}", file=sys.stderr)
        return 1

    judge_defaults, judge_sources = config_mod.resolve_judge_defaults(config)
    policy_defaults, policy_sources = config_mod.resolve_policy_defaults(config)
    overrides = {"judge": set(), "policy": set()}
    suite = None
    if args.suite:
        overrides = suite_mod.peek_overrides(args.suite)
        suite = suite_mod.load(args.suite, judge_defaults=judge_defaults, policy_defaults=policy_defaults)

    cap_usd, cap_source = config_mod.resolve_cap(None, suite.cap_usd if suite else None, config)

    print(f"config file: {config.path if config.path else '(none found; using built-in defaults)'}")
    if config.content_hash:
        print(f"config hash: {config.content_hash}")
    print(f"cap_usd: {cap_usd}  (source: {cap_source})")

    print("judge:")
    for key, value, source in _judge_rows(suite, judge_defaults, judge_sources, overrides["judge"]):
        print(f"  {key}: {value!r}  (source: {source})")

    print("paths:")
    for key, (value, source) in sorted(resolved_paths(args, config).items()):
        print(f"  {key}: {value}  (source: {source})")

    print("policy:")
    for key, value, source in _policy_rows(suite, policy_defaults, policy_sources, overrides["policy"]):
        print(f"  {key}: {value!r}  (source: {source})")

    return 0
