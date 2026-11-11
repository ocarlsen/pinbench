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
    # The Message Batches API discount for this model (Anthropic: "a 50 percent discount on
    # both input and output tokens" -- platform.claude.com/docs/en/about-claude/pricing,
    # "Batch processing"). None means this row has no batch price at all: `--batch` refuses
    # to run that model rather than guess a discount. When set, must be in (0, 1]; a value
    # of 1 would be a real but unusual "no discount" batch row, 0 or negative is never a
    # real price.
    batch_multiplier: Optional[float] = None

    def at_batch(self) -> "PriceRow":
        """A copy of this row with input/output multiplied by batch_multiplier.

        Raises PricesError when this row has no batch_multiplier -- never guesses a
        discount for a model that hasn't published one.
        """
        if self.batch_multiplier is None:
            raise PricesError(f"no batch price for '{self.model}': add batch_multiplier to its prices.yaml row")
        return PriceRow(
            model=self.model,
            input=self.input * self.batch_multiplier,
            output=self.output * self.batch_multiplier,
            source=self.source,
            request_defaults=self.request_defaults,
            batch_multiplier=self.batch_multiplier,
        )


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
        batch_multiplier = body.get("batch_multiplier")
        if batch_multiplier is not None:
            batch_multiplier = float(batch_multiplier)
            if not (0 < batch_multiplier <= 1):
                raise PricesError(f"prices: {model} batch_multiplier must be > 0 and <= 1, got {batch_multiplier}")
        rows[model] = PriceRow(
            model=model,
            input=float(body["input"]),
            output=float(body["output"]),
            source=body.get("source", ""),
            request_defaults=request_defaults,
            batch_multiplier=batch_multiplier,
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
