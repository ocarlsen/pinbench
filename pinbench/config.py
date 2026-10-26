"""Load and resolve pinbench.yaml: an optional project config file.

Keys: `cap_usd` (a default cost cap), `judge` (a fallback judge block: model,
max_output_tokens, rubric), `paths` (pins/prices/suites/runs, relative to the config
file's own directory), `policy` (fallback policy values any suite can override).

Precedence, used everywhere a setting has one: CLI flag > suite > pinbench.yaml > built-in
default. This module only resolves config-vs-builtin; callers layer the CLI flag and the
suite's own value on top (a suite is loaded with this module's judge/policy fallbacks
already merged in, so `suite.judge`/`suite.policy` land on the final value directly).

Refuses to do: guess a type for a bad config value -- an unknown top-level or nested key,
or a value of the wrong type, raises naming key=value and the reason, never a silent
fallback. Refuses to accept `incumbent`/`candidate` defaults here: which models are being
compared is a choice made deliberately, per run, never defaulted by a file.
"""

from __future__ import annotations

import hashlib
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import yaml

CONFIG_FILENAME = "pinbench.yaml"

_TOP_KEYS = {"cap_usd", "judge", "paths", "policy"}
_FORBIDDEN_TOP_KEYS = {"incumbent", "candidate"}
_JUDGE_KEYS = {"model", "max_output_tokens", "rubric"}
_PATH_KEYS = {"pins", "prices", "runs"}
_POLICY_INT_KEYS = {"min_items", "max_truncated"}
_POLICY_FLOAT_KEYS = {"label_tolerance_points", "cost_tolerance", "recall_tolerance_points", "extra_tolerance"}
_POLICY_BOOL_KEYS = {"require_votes_when_unlabeled"}
_POLICY_KEYS = _POLICY_INT_KEYS | _POLICY_FLOAT_KEYS | _POLICY_BOOL_KEYS

# The one place pinbench's built-in defaults live. A model id here (the judge's fallback)
# used to be a literal inside the example suite; now a pinbench.yaml can override it, and a
# suite's own `judge:` block still wins over both.
BUILTIN_DEFAULTS = {
    "cap_usd": None,
    "judge": {"model": "claude-opus-5-5", "max_output_tokens": 4096, "rubric": None},
    "paths": {
        "pins": "examples/pins.yaml",
        "prices": "examples/prices.yaml",
        "runs": ".pinbench",
    },
    "policy": {
        "min_items": 10,
        "label_tolerance_points": 0.0,
        "cost_tolerance": 0.25,
        "require_votes_when_unlabeled": True,
        "max_truncated": 0,
        "recall_tolerance_points": 0.0,
        "extra_tolerance": 0.5,
    },
}


class ConfigError(ValueError):
    pass


@dataclass
class Config:
    path: Optional[Path] = None
    raw: dict = field(default_factory=dict)
    content_hash: Optional[str] = None


def find_config(start: Optional[Path] = None) -> Optional[Path]:
    """Walk up from `start` (default: cwd) looking for pinbench.yaml. None if never found."""
    current = Path(start if start is not None else Path.cwd()).resolve()
    while True:
        candidate = current / CONFIG_FILENAME
        if candidate.is_file():
            return candidate
        parent = current.parent
        if parent == current:
            return None
        current = parent


def _hash_text(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()


def display_path(path) -> str:
    """A path as it should appear in printed output and persisted artifacts (run.json, the
    report header): relative to the current directory, so a report.md that `bump` commits
    or a pasted `pinbench config` never carries a machine-specific absolute path. Falls
    back to the path as given only when no relative form exists (a different drive)."""
    try:
        return os.path.relpath(path)
    except ValueError:
        return str(path)


def _require_number(value, key):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(f"{key}={value!r}: must be a number")


def check_cap_usd(value, key="cap_usd") -> float:
    """The one rule for a cost cap wherever it is set (pinbench.yaml or a suite): a number
    greater than zero. Returns it as a float; raises ConfigError naming key=value."""
    _require_number(value, key)
    if value <= 0:
        raise ConfigError(f"{key}={value!r}: must be greater than zero")
    return float(value)


def _validate_judge(raw_judge):
    unknown = set(raw_judge) - _JUDGE_KEYS
    if unknown:
        raise ConfigError(f"judge has unknown key(s): {sorted(unknown)}")
    if "model" in raw_judge and not isinstance(raw_judge["model"], str):
        raise ConfigError(f"judge.model={raw_judge['model']!r}: must be a string")
    if "rubric" in raw_judge and not isinstance(raw_judge["rubric"], str):
        raise ConfigError(f"judge.rubric={raw_judge['rubric']!r}: must be a string")
    if "max_output_tokens" in raw_judge:
        v = raw_judge["max_output_tokens"]
        if isinstance(v, bool) or not isinstance(v, int):
            raise ConfigError(f"judge.max_output_tokens={v!r}: must be an integer")
        if v < 1:
            raise ConfigError(f"judge.max_output_tokens={v!r}: must be at least 1")


def _validate_paths(raw_paths):
    unknown = set(raw_paths) - _PATH_KEYS
    if unknown:
        raise ConfigError(f"paths has unknown key(s): {sorted(unknown)}")
    for k, v in raw_paths.items():
        if not isinstance(v, str) or not v:
            raise ConfigError(f"paths.{k}={v!r}: must be a non-empty string")


def _validate_policy(raw_policy):
    unknown = set(raw_policy) - _POLICY_KEYS
    if unknown:
        raise ConfigError(f"policy has unknown key(s): {sorted(unknown)}")
    for k, v in raw_policy.items():
        if k in _POLICY_BOOL_KEYS:
            if not isinstance(v, bool):
                raise ConfigError(f"policy.{k}={v!r}: must be true or false")
        elif k in _POLICY_INT_KEYS:
            if isinstance(v, bool) or not isinstance(v, int):
                raise ConfigError(f"policy.{k}={v!r}: must be an integer")
            if v < 0 or (k == "min_items" and v < 1):
                raise ConfigError(f"policy.{k}={v!r}: must be at least {1 if k == 'min_items' else 0}")
        else:
            if isinstance(v, bool) or not isinstance(v, (int, float)):
                raise ConfigError(f"policy.{k}={v!r}: must be a number")
            if v < 0:
                raise ConfigError(f"policy.{k}={v!r}: must not be negative")


def load(path) -> Config:
    """Parse and validate one pinbench.yaml. Raises ConfigError naming key=value and the
    reason on any unknown key or wrong type; never falls back silently."""
    path = Path(path)
    text = path.read_text()
    try:
        raw = yaml.safe_load(text) or {}
    except yaml.YAMLError as exc:
        raise ConfigError(f"{path}: not valid YAML: {exc}") from exc
    if not isinstance(raw, dict):
        raise ConfigError(f"{path}: must be a mapping at the top level")

    forbidden = _FORBIDDEN_TOP_KEYS & set(raw)
    if forbidden:
        raise ConfigError(
            f"{path}: {sorted(forbidden)} may not be set here -- which models are being "
            "compared is a choice made deliberately per run, never defaulted by a config file"
        )
    unknown = set(raw) - _TOP_KEYS
    if unknown:
        raise ConfigError(f"{path}: unknown top-level key(s): {sorted(unknown)}")

    if "cap_usd" in raw:
        check_cap_usd(raw["cap_usd"])
    if "judge" in raw:
        if not isinstance(raw["judge"], dict):
            raise ConfigError(f"{path}: judge must be a mapping")
        _validate_judge(raw["judge"])
    if "paths" in raw:
        if not isinstance(raw["paths"], dict):
            raise ConfigError(f"{path}: paths must be a mapping")
        _validate_paths(raw["paths"])
    if "policy" in raw:
        if not isinstance(raw["policy"], dict):
            raise ConfigError(f"{path}: policy must be a mapping")
        _validate_policy(raw["policy"])

    return Config(path=path, raw=raw, content_hash=_hash_text(text))


def load_effective(explicit_path=None, start=None) -> Config:
    """--config PATH if given (refuses if it does not exist); else the nearest pinbench.yaml
    walking up from `start` (default cwd); else Config(path=None) -- no config is fine, the
    caller falls through to built-in defaults."""
    if explicit_path is not None:
        p = Path(explicit_path)
        if not p.is_file():
            raise ConfigError(f"--config {p}: not found")
        return load(p)
    found = find_config(start)
    if found is None:
        return Config(path=None)
    return load(found)


def resolve_cap(cli_cap: Optional[float], suite_cap: Optional[float], config: Config):
    """(value, source) for cap_usd: cli > suite > config > built-in (None)."""
    if cli_cap is not None:
        return cli_cap, "cli"
    if suite_cap is not None:
        return suite_cap, "suite"
    config_cap = config.raw.get("cap_usd") if config else None
    if config_cap is not None:
        return float(config_cap), "config"
    return BUILTIN_DEFAULTS["cap_usd"], "default"


def resolve_paths(cli_overrides: dict, config: Config):
    """{key: (value, source)} for pins/prices/suites/runs. `cli_overrides` maps a subset of
    those keys to an explicit CLI value (or omits/None for "not passed"). A path that comes
    from pinbench.yaml is resolved relative to THAT FILE's own directory, so the same
    pinbench.yaml behaves the same regardless of which directory pinbench was run from,
    then expressed relative to the current directory (see `display_path`) so the value is
    usable from here AND safe to persist; a CLI-flag or built-in-default path is left
    exactly as given (relative to the caller's cwd, same as before this module existed)."""
    config_paths = (config.raw.get("paths") or {}) if config else {}
    result = {}
    for key in _PATH_KEYS:
        cli_value = cli_overrides.get(key)
        if cli_value is not None:
            result[key] = (cli_value, "cli")
        elif key in config_paths:
            result[key] = (_relative_to_config(config_paths[key], config), "config")
        else:
            result[key] = (BUILTIN_DEFAULTS["paths"][key], "default")
    return result


def _relative_to_config(value: str, config: Config) -> str:
    if config.path is None or Path(value).is_absolute():
        return value
    return display_path((config.path.parent / value).resolve())


def resolve_judge_defaults(config: Config):
    """Merged {model, max_output_tokens, rubric} + {key: source} -- config over built-in.
    This is the FALLBACK a suite's own judge block is layered onto; the suite always wins
    per key (see suite.load's `judge_defaults` argument)."""
    merged = dict(BUILTIN_DEFAULTS["judge"])
    source = {k: "default" for k in merged}
    config_judge = (config.raw.get("judge") or {}) if config else {}
    for k, v in config_judge.items():
        merged[k] = v
        source[k] = "config"
    return merged, source


def resolve_policy_defaults(config: Config):
    """Merged policy field dict + {key: source} -- config over built-in, the fallback a
    suite's own `policy:` block is layered onto."""
    merged = dict(BUILTIN_DEFAULTS["policy"])
    source = {k: "default" for k in merged}
    config_policy = (config.raw.get("policy") or {}) if config else {}
    for k, v in config_policy.items():
        merged[k] = v
        source[k] = "config"
    return merged, source
