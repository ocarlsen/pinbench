"""Diff the model list of each provider pins.yaml uses against the last snapshot: newer pins, vanished pins, new ids.

Refuses to do: call a model, or advance a provider's snapshot when that provider was
unreachable (an unreachable vendor is reported, never silently skipped).
"""

from __future__ import annotations

import json
import os
import re
from pathlib import Path

from . import providers
from . import registry as registry_mod

_DATE_SUFFIX_RE = re.compile(r"-(\d{4}-\d{2}-\d{2}|\d{8})$")
_NUMERIC_RE = re.compile(r"^\d+(\.\d+)*$")


def strip_date_suffix(model_id: str) -> str:
    return _DATE_SUFFIX_RE.sub("", model_id)


def family_and_version(model_id: str):
    stripped = strip_date_suffix(model_id)
    family = []
    version = []
    for token in stripped.split("-"):
        if _NUMERIC_RE.match(token):
            version.append(tuple(int(p) for p in token.split(".")))
        else:
            family.append(token)
    return tuple(family), tuple(version)


def candidate_for_pin(pin_model: str, ids, hold_back=None):
    """The newest id with the same family and a strictly greater version, excluding hold_back."""
    pin_family, pin_version = family_and_version(pin_model)
    held = strip_date_suffix(hold_back) if hold_back else None
    best = None
    best_version = None
    for model_id in ids:
        if held and strip_date_suffix(model_id) == held:
            continue
        family, version = family_and_version(model_id)
        if family != pin_family:
            continue
        if version <= pin_version:
            continue
        if best is None or version > best_version:
            best = model_id
            best_version = version
    return best


def load_snapshot(state_dir) -> dict:
    path = Path(state_dir) / "catalog_snapshot.json"
    if not path.exists():
        return {}
    return json.loads(path.read_text())


def save_snapshot(state_dir, snapshot: dict) -> None:
    path = Path(state_dir) / "catalog_snapshot.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(snapshot, indent=2, sort_keys=True))


def run_watch(pins_path, state_dir, seed: bool = False, env=None):
    env = env or os.environ
    reg = registry_mod.load(pins_path)
    snapshot = load_snapshot(state_dir)
    lines = []
    any_unreachable = False
    # Only the providers pins.yaml actually uses: a provider you pin nothing on needs no key,
    # and its absence is not an outage.
    for provider_name in sorted({slot.provider for slot in reg.slots.values()}):
        adapter = providers.adapter_for(provider_name)
        api_key = env.get(providers.ENV_KEYS[provider_name])
        try:
            if not api_key:
                raise ValueError(f"{providers.ENV_KEYS[provider_name]} is not set")
            ids = adapter.list_models(api_key)
        except Exception as exc:  # noqa: BLE001 - any fetch failure marks this provider unreachable
            lines.append(f"{provider_name} UNREACHABLE: {exc}")
            any_unreachable = True
            continue

        prior = snapshot.get(provider_name)
        if prior is None or seed:
            lines.append(f"seeded {len(ids)} ids for {provider_name}")
            snapshot[provider_name] = ids
            continue

        lines.extend(_diff_provider(provider_name, prior, ids, reg))
        snapshot[provider_name] = ids

    save_snapshot(state_dir, snapshot)
    return lines, (1 if any_unreachable else 0)


def _diff_provider(provider_name, prior_ids, ids, reg):
    lines = []
    id_set = set(ids)
    for slot in reg.slots.values():
        if slot.provider != provider_name:
            continue
        candidate = candidate_for_pin(slot.model, ids, hold_back=slot.hold_back)
        if candidate:
            lines.append(f"NEWER IN SLOT {slot.name}: {slot.model} -> {candidate} ({provider_name})")
        if slot.model not in id_set:
            lines.append(f"VANISHED {slot.name}: {slot.model} no longer listed by {provider_name}")

    new_ids = sorted(set(ids) - set(prior_ids))
    if new_ids:
        lines.append(f"NEW IDS {provider_name}: " + ", ".join(new_ids))
    return lines
