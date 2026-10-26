"""Load, validate, and edit pins.yaml, the model-pin registry.

Refuses to do: write a pin anywhere except through `set_model`, and refuses to load a
registry whose provider/id prefix do not agree or whose hold_back lacks a reason.
"""

from __future__ import annotations

import datetime as _dt
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import yaml

from . import providers


class RegistryError(ValueError):
    """Raised when pins.yaml fails validation."""


@dataclass
class Slot:
    name: str
    model: str
    provider: str
    owner: str = ""
    hold_back: Optional[str] = None
    hold_back_reason: Optional[str] = None
    sunset: Optional[str] = None


@dataclass
class Registry:
    version: int
    slots: dict = field(default_factory=dict)
    path: Optional[Path] = None

    def slot(self, name: str) -> Slot:
        if name not in self.slots:
            raise RegistryError(f"no such slot: {name}")
        return self.slots[name]


def _validate_slot(name: str, raw: dict) -> Slot:
    if "model" not in raw or not raw["model"]:
        raise RegistryError(f"slot {name}: missing model")
    if "provider" not in raw or not raw["provider"]:
        raise RegistryError(f"slot {name}: missing provider")
    model = raw["model"]
    provider = raw["provider"]
    if provider not in providers.ADAPTERS:
        raise RegistryError(f"slot {name}: no adapter for provider '{provider}'")
    try:
        expected = providers.provider_for_id(model)
    except providers.NoAdapterError as exc:
        raise RegistryError(f"slot {name}: {exc}") from exc
    if expected != provider:
        raise RegistryError(
            f"slot {name}: id '{model}' looks like {expected}, but provider is '{provider}'"
        )
    hold_back = raw.get("hold_back")
    hold_back_reason = raw.get("hold_back_reason")
    if hold_back and not hold_back_reason:
        raise RegistryError(f"slot {name}: hold_back set without hold_back_reason")
    sunset = raw.get("sunset")
    if sunset is not None:
        try:
            _dt.date.fromisoformat(str(sunset))
        except ValueError as exc:
            raise RegistryError(f"slot {name}: sunset is not an ISO date: {sunset}") from exc
        sunset = str(sunset)
    return Slot(
        name=name,
        model=model,
        provider=provider,
        owner=raw.get("owner", ""),
        hold_back=hold_back,
        hold_back_reason=hold_back_reason,
        sunset=sunset,
    )


def load(path) -> Registry:
    path = Path(path)
    raw = yaml.safe_load(path.read_text()) or {}
    if "slots" not in raw:
        raise RegistryError("pins.yaml: missing 'slots'")
    slots = {name: _validate_slot(name, body) for name, body in raw["slots"].items()}
    return Registry(version=raw.get("version", 1), slots=slots, path=path)


def set_model(path, slot_name: str, new_id: str) -> None:
    """Edit the `model:` line inside `slot_name`'s block, preserving everything else.

    Refuses to do: touch any line outside that one slot's `model:` key, verified by
    re-parsing the file afterward and asserting the only YAML-level difference is that key.
    """
    path = Path(path)
    before = load(path)
    if slot_name not in before.slots:
        raise RegistryError(f"no such slot: {slot_name}")

    lines = path.read_text().splitlines(keepends=True)
    slot_header = re.compile(rf"^  {re.escape(slot_name)}:\s*$")
    model_line = re.compile(r"^(\s+model:\s*)(\S+)(.*)$")

    in_slot = False
    edited = False
    out = []
    for line in lines:
        if slot_header.match(line.rstrip("\n")):
            in_slot = True
            out.append(line)
            continue
        if in_slot and re.match(r"^  \S", line):
            # a new top-level slot started; stop looking
            in_slot = False
        if in_slot and not edited:
            m = model_line.match(line)
            if m:
                out.append(f"{m.group(1)}{new_id}{m.group(3)}\n")
                edited = True
                continue
        out.append(line)

    if not edited:
        raise RegistryError(f"could not find model: line for slot {slot_name}")

    path.write_text("".join(out))

    after = load(path)
    for name, slot in after.slots.items():
        before_slot = before.slots[name]
        if name == slot_name:
            if slot.model != new_id:
                raise RegistryError("set_model: edit did not take effect")
            # every other field on this slot must be unchanged
            if (
                slot.provider != before_slot.provider
                or slot.owner != before_slot.owner
                or slot.hold_back != before_slot.hold_back
                or slot.hold_back_reason != before_slot.hold_back_reason
                or slot.sunset != before_slot.sunset
            ):
                raise RegistryError("set_model: touched more than the model key")
        else:
            if slot != before_slot:
                raise RegistryError(f"set_model: slot {name} changed unexpectedly")
