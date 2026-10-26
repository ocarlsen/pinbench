"""Load prices.yaml: USD-per-million-token rates, with a source and a checked date per row.

Refuses to do: invent a price for an id that is not in the file (price_row returns None;
callers must refuse to run rather than assume a price); accept a `request_defaults` that
is not a mapping (a model's own request shape, e.g. how it turns thinking off, is per-model
truth read from this file -- never a type the runner has to coerce or guess at).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import yaml


class PricesError(ValueError):
    pass


@dataclass
class PriceRow:
    model: str
    input: float
    output: float
    source: str = ""
    # Per-model request shape (e.g. {"thinking": {"type": "between_tools"}}) sent on every
    # call to this model, before the suite's own `params` are applied on top. Empty by
    # default: a model with no row-level opinion sends nothing extra.
    request_defaults: dict = field(default_factory=dict)


@dataclass
class Prices:
    version: int
    checked: str
    rows: dict


def load(path) -> Prices:
    path = Path(path)
    raw = yaml.safe_load(path.read_text()) or {}
    if "models" not in raw:
        raise PricesError(f"{path}: missing 'models'")
    rows = {}
    for model, body in raw["models"].items():
        if "input" not in body or "output" not in body:
            raise PricesError(f"prices: {model} missing input/output rate")
        request_defaults = body.get("request_defaults") or {}
        if not isinstance(request_defaults, dict):
            raise PricesError(f"prices: {model} request_defaults must be a mapping, got {type(request_defaults).__name__}")
        rows[model] = PriceRow(
            model=model,
            input=float(body["input"]),
            output=float(body["output"]),
            source=body.get("source", ""),
            request_defaults=request_defaults,
        )
    return Prices(version=raw.get("version", 1), checked=str(raw.get("checked", "")), rows=rows)


def price_row(prices: Prices, model_id: str) -> Optional[PriceRow]:
    return prices.rows.get(model_id)


def price_ratio(prices: Prices, candidate: str, incumbent: str) -> Optional[float]:
    """The larger of the input-rate ratio and output-rate ratio, candidate over incumbent.

    None when either row is missing. The larger of the two axes is used so a candidate
    cheap on input but expensive on output is never under-estimated.
    """
    cand = price_row(prices, candidate)
    inc = price_row(prices, incumbent)
    if cand is None or inc is None:
        return None
    if inc.input == 0 or inc.output == 0:
        return None
    input_ratio = cand.input / inc.input
    output_ratio = cand.output / inc.output
    return max(input_ratio, output_ratio)
