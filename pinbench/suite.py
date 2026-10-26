"""Load and validate a frozen suite of inputs (suite.yaml or suite.json).

Refuses to do: load a suite with duplicate or malformed item ids, or a suite where some
items have `expect` and others don't while scoring.labels != none. `params` is an optional
top-level dict of extra sampling params (e.g. temperature) sent to both arms; it defaults
to empty, so a suite that says nothing about sampling sends nothing extra. For
`scoring.labels: judge`, refuses a suite with no `judge:` block (after any `judge_defaults`
fallback is applied), or any item with no `rubric_points` -- a judge suite with nothing to
check for is not a suite, it is a prompt. `judge_defaults`/`policy_defaults` are optional
fallback dicts (from `config.resolve_judge_defaults`/`resolve_policy_defaults`); omitting
them keeps this module's own built-in defaults, so every existing caller of `load(path)` is
unaffected -- only a caller that explicitly wants pinbench.yaml's fallbacks passes them.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import yaml

from . import config as config_mod

_ID_RE = re.compile(r"^[A-Za-z0-9_.-]+$")

_SCORING_LABELS = ("exact", "contains", "regex", "none", "judge")
_VOTES_MODES = ("required", "optional", "none")


class SuiteError(ValueError):
    pass


@dataclass
class RubricPoint:
    id: str
    description: str


@dataclass
class Item:
    id: str
    input: str
    expect: Optional[str] = None
    rubric_points: list = field(default_factory=list)
    decoys: list = field(default_factory=list)


@dataclass
class JudgeConfig:
    model: str
    rubric: str
    # The per-call output ceiling for the judge's own strict-JSON reply. The JSON itself is
    # tiny (a few ids, an int, a short note), but some judge models cannot turn thinking off
    # (e.g. Claude Opus 5.5 -- see examples/prices.yaml) and thinking tokens count against
    # this same ceiling, so the default leaves room rather than matching the JSON's own size.
    max_output_tokens: int = 1024


@dataclass
class Policy:
    min_items: int = 10
    label_tolerance_points: float = 0.0
    cost_tolerance: float = 0.25
    require_votes_when_unlabeled: bool = True
    # How many truncated outputs (stop/finish reason hit the output ceiling) are tolerated
    # per arm before the verdict refuses to call the run complete. Default 0: any truncation
    # means the suite's max_output_tokens was too low for at least one item, and a report
    # built on cut-off answers is not evidence either way.
    max_truncated: int = 0
    # Judge-scoring tolerances (ignored unless scoring.labels == "judge"). recall_tolerance_points
    # mirrors label_tolerance_points: how many recall percentage points the candidate may lag
    # the incumbent and still be non-inferior. extra_tolerance is in mean-extra-findings-per-item,
    # not points: a judge counting spurious findings is a precision proxy, not a percentage.
    recall_tolerance_points: float = 0.0
    extra_tolerance: float = 0.5


@dataclass
class Scoring:
    labels: str = "exact"
    normalize: list = field(default_factory=lambda: ["strip", "casefold", "strip_punct"])
    votes: str = "optional"


@dataclass
class Suite:
    version: int
    name: str
    task: str
    max_output_tokens: int
    scoring: Scoring
    policy: Policy
    items: list
    params: dict = field(default_factory=dict)
    judge: Optional[JudgeConfig] = None
    # A per-suite cost cap override. None means the suite states no opinion; the caller's
    # precedence (cli.resolve_cap) falls through to pinbench.yaml, then the built-in None.
    cap_usd: Optional[float] = None


def _parse_rubric_points(raw_points, item_id) -> list:
    seen = set()
    points = []
    for raw in raw_points or []:
        point_id = raw.get("id")
        description = raw.get("description")
        if not point_id or not description:
            raise SuiteError(f"item {item_id}: rubric_points entry needs 'id' and 'description'")
        if point_id in seen:
            raise SuiteError(f"item {item_id}: duplicate rubric point id: {point_id}")
        seen.add(point_id)
        points.append(RubricPoint(id=str(point_id), description=str(description)))
    return points


def _parse_items(raw_items, judge_mode: bool) -> list:
    seen = set()
    items = []
    for raw in raw_items:
        item_id = raw.get("id")
        if not item_id or not _ID_RE.match(str(item_id)):
            raise SuiteError(f"bad item id: {item_id!r}")
        if item_id in seen:
            raise SuiteError(f"duplicate item id: {item_id}")
        seen.add(item_id)
        if not raw.get("input"):
            raise SuiteError(f"item {item_id}: empty input")
        rubric_points = _parse_rubric_points(raw.get("rubric_points"), item_id)
        if judge_mode and not rubric_points:
            raise SuiteError(f"item {item_id}: scoring.labels is 'judge' but item has no rubric_points")
        items.append(
            Item(
                id=item_id,
                input=raw["input"],
                expect=raw.get("expect"),
                rubric_points=rubric_points,
                decoys=list(raw.get("decoys") or []),
            )
        )
    return items


def _parse_judge(raw_judge, labels: str, defaults: Optional[dict] = None) -> Optional[JudgeConfig]:
    """`defaults` is an optional fallback {"model", "max_output_tokens", "rubric"} (from
    pinbench.yaml merged over the built-in table); the suite's own judge block wins per key.
    Omitting `defaults` (the direct-call case every pre-config test uses) means no fallback
    at all: a judge suite with no judge block still refuses, exactly as before.
    """
    if labels != "judge":
        return None
    raw_judge = raw_judge or {}
    base = defaults or {"model": None, "max_output_tokens": 1024, "rubric": None}
    model = raw_judge.get("model", base.get("model"))
    rubric = raw_judge.get("rubric", base.get("rubric"))
    if not model or not rubric:
        raise SuiteError("scoring.labels is 'judge' but suite has no judge.model / judge.rubric")
    max_output_tokens = raw_judge.get("max_output_tokens", base.get("max_output_tokens", 1024))
    return JudgeConfig(model=model, rubric=rubric, max_output_tokens=int(max_output_tokens))


def _parse_scoring(raw) -> Scoring:
    scoring_raw = raw.get("scoring", {}) or {}
    labels = scoring_raw.get("labels", "exact")
    if labels not in _SCORING_LABELS:
        raise SuiteError(f"scoring.labels must be one of {_SCORING_LABELS}, got {labels!r}")
    votes = scoring_raw.get("votes", "optional")
    if votes not in _VOTES_MODES:
        raise SuiteError(f"scoring.votes must be one of {_VOTES_MODES}, got {votes!r}")
    return Scoring(labels=labels, normalize=scoring_raw.get("normalize", ["strip", "casefold", "strip_punct"]), votes=votes)


def _parse_policy(raw, defaults: Optional[dict] = None) -> Policy:
    """`defaults` is an optional fallback dict (from pinbench.yaml merged over the built-in
    table); a suite's own `policy:` block wins per key. Omitting `defaults` keeps this
    module's own built-in literals -- identical to pre-config behavior."""
    base = defaults if defaults is not None else config_mod.BUILTIN_DEFAULTS["policy"]
    policy_raw = raw.get("policy", {}) or {}
    return Policy(
        min_items=policy_raw.get("min_items", base["min_items"]),
        label_tolerance_points=policy_raw.get("label_tolerance_points", base["label_tolerance_points"]),
        cost_tolerance=policy_raw.get("cost_tolerance", base["cost_tolerance"]),
        require_votes_when_unlabeled=policy_raw.get(
            "require_votes_when_unlabeled", base["require_votes_when_unlabeled"]
        ),
        max_truncated=policy_raw.get("max_truncated", base["max_truncated"]),
        recall_tolerance_points=policy_raw.get("recall_tolerance_points", base["recall_tolerance_points"]),
        extra_tolerance=policy_raw.get("extra_tolerance", base["extra_tolerance"]),
    )


def _require_top_level_fields(raw, path) -> None:
    if "name" not in raw or not raw["name"]:
        raise SuiteError(f"{path}: missing 'name'")
    if "task" not in raw or not raw["task"]:
        raise SuiteError(f"{path}: missing 'task'")
    if "max_output_tokens" not in raw:
        raise SuiteError(f"{path}: missing 'max_output_tokens'")


def raw_load(path) -> dict:
    """Parse a suite file (YAML or JSON by extension) with no validation. Used by `load`
    itself, and by `config.py`'s provenance peek to see which top-level/judge/policy keys a
    suite set explicitly, without re-running full validation."""
    path = Path(path)
    text = path.read_text()
    raw = json.loads(text) if path.suffix == ".json" else yaml.safe_load(text)
    return raw or {}


def peek_overrides(path) -> dict:
    """{"cap_usd": bool, "judge": set-of-keys, "policy": set-of-keys} -- which of those a
    suite's own file sets explicitly, for a caller (e.g. `pinbench config`) that reports
    where each effective value came from."""
    raw = raw_load(path)
    return {
        "cap_usd": "cap_usd" in raw,
        "judge": set((raw.get("judge") or {}).keys()),
        "policy": set((raw.get("policy") or {}).keys()),
    }


def _parse_cap_usd(raw, path) -> Optional[float]:
    if "cap_usd" not in raw:
        return None
    try:
        return float(raw["cap_usd"])
    except (TypeError, ValueError) as exc:
        raise SuiteError(f"{path}: cap_usd={raw['cap_usd']!r} must be a number") from exc


def load(path, judge_defaults: Optional[dict] = None, policy_defaults: Optional[dict] = None) -> Suite:
    """`judge_defaults`/`policy_defaults` are optional config-resolved fallbacks (see the
    module docstring); omit them to get this module's own built-in defaults untouched."""
    path = Path(path)
    raw = raw_load(path)
    if not raw:
        raise SuiteError(f"{path}: empty suite")

    scoring = _parse_scoring(raw)
    policy = _parse_policy(raw, defaults=policy_defaults)
    cap_usd = _parse_cap_usd(raw, path)

    judge_mode = scoring.labels == "judge"
    items = _parse_items(raw.get("items", []), judge_mode)
    if not items:
        raise SuiteError(f"{path}: no items")

    if scoring.labels not in ("none", "judge"):
        missing = [i.id for i in items if i.expect is None]
        if missing:
            raise SuiteError(
                "mixed suite: scoring.labels != none but these items have no 'expect': "
                + ", ".join(missing)
            )

    judge = _parse_judge(raw.get("judge"), scoring.labels, defaults=judge_defaults)
    _require_top_level_fields(raw, path)

    return Suite(
        version=raw.get("version", 1),
        name=raw["name"],
        task=raw["task"],
        max_output_tokens=int(raw["max_output_tokens"]),
        scoring=scoring,
        policy=policy,
        items=items,
        params=dict(raw.get("params") or {}),
        judge=judge,
        cap_usd=cap_usd,
    )


def freeze_hash(suite: Suite) -> str:
    """sha256 of the canonical JSON of items + task + max_output_tokens.

    Rubric points and decoys are part of the frozen input for a judge suite: changing what
    a judge is asked to check for is changing the suite, same as changing an `expect`.
    """
    payload = {
        "task": suite.task,
        "max_output_tokens": suite.max_output_tokens,
        "items": [
            {
                "id": i.id,
                "input": i.input,
                "expect": i.expect,
                "rubric_points": [{"id": p.id, "description": p.description} for p in i.rubric_points],
                "decoys": list(i.decoys),
            }
            for i in suite.items
        ],
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()
