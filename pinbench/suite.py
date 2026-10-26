"""Load and validate a frozen suite of inputs (suite.yaml or suite.json).

Refuses to do: load a suite with duplicate or malformed item ids, or a suite where some
items have `expect` and others don't while scoring.labels != none.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import yaml

_ID_RE = re.compile(r"^[A-Za-z0-9_.-]+$")

_SCORING_LABELS = ("exact", "contains", "regex", "none")
_VOTES_MODES = ("required", "optional", "none")


class SuiteError(ValueError):
    pass


@dataclass
class Item:
    id: str
    input: str
    expect: Optional[str] = None


@dataclass
class Policy:
    min_items: int = 10
    label_tolerance_points: float = 0.0
    cost_tolerance: float = 0.25
    require_votes_when_unlabeled: bool = True


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


def _parse_items(raw_items) -> list:
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
        items.append(Item(id=item_id, input=raw["input"], expect=raw.get("expect")))
    return items


def load(path) -> Suite:
    path = Path(path)
    text = path.read_text()
    if path.suffix == ".json":
        raw = json.loads(text)
    else:
        raw = yaml.safe_load(text)
    if not raw:
        raise SuiteError(f"{path}: empty suite")

    scoring_raw = raw.get("scoring", {}) or {}
    labels = scoring_raw.get("labels", "exact")
    if labels not in _SCORING_LABELS:
        raise SuiteError(f"scoring.labels must be one of {_SCORING_LABELS}, got {labels!r}")
    votes = scoring_raw.get("votes", "optional")
    if votes not in _VOTES_MODES:
        raise SuiteError(f"scoring.votes must be one of {_VOTES_MODES}, got {votes!r}")
    scoring = Scoring(
        labels=labels,
        normalize=scoring_raw.get("normalize", ["strip", "casefold", "strip_punct"]),
        votes=votes,
    )

    policy_raw = raw.get("policy", {}) or {}
    policy = Policy(
        min_items=policy_raw.get("min_items", 10),
        label_tolerance_points=policy_raw.get("label_tolerance_points", 0.0),
        cost_tolerance=policy_raw.get("cost_tolerance", 0.25),
        require_votes_when_unlabeled=policy_raw.get("require_votes_when_unlabeled", True),
    )

    items = _parse_items(raw.get("items", []))
    if not items:
        raise SuiteError(f"{path}: no items")

    if labels != "none":
        missing = [i.id for i in items if i.expect is None]
        if missing:
            raise SuiteError(
                "mixed suite: scoring.labels != none but these items have no 'expect': "
                + ", ".join(missing)
            )

    if "name" not in raw or not raw["name"]:
        raise SuiteError(f"{path}: missing 'name'")
    if "task" not in raw or not raw["task"]:
        raise SuiteError(f"{path}: missing 'task'")
    if "max_output_tokens" not in raw:
        raise SuiteError(f"{path}: missing 'max_output_tokens'")

    return Suite(
        version=raw.get("version", 1),
        name=raw["name"],
        task=raw["task"],
        max_output_tokens=int(raw["max_output_tokens"]),
        scoring=scoring,
        policy=policy,
        items=items,
    )


def freeze_hash(suite: Suite) -> str:
    """sha256 of the canonical JSON of items + task + max_output_tokens."""
    payload = {
        "task": suite.task,
        "max_output_tokens": suite.max_output_tokens,
        "items": [
            {"id": i.id, "input": i.input, "expect": i.expect} for i in suite.items
        ],
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(canonical.encode("utf-8")).hexdigest()
