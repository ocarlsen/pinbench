import json
import math

import pytest

from pinbench import cost, prices, suite


def _suite(tmp_path, n=2, max_out=64):
    items = "\n".join(
        f'  - id: q{i:02d}\n    input: "What is {i}?"\n    expect: "{i}"' for i in range(n)
    )
    path = tmp_path / "suite.yaml"
    path.write_text(
        f"""\
version: 1
name: demo
task: "Answer briefly."
max_output_tokens: {max_out}
items:
{items}
"""
    )
    return suite.load(path)


def _prices(tmp_path, incumbent_rate=(1.0, 2.0), candidate_rate=(0.5, 1.0)):
    path = tmp_path / "prices.yaml"
    path.write_text(
        f"""\
version: 1
checked: 2026-10-26
models:
  incumbent: {{input: {incumbent_rate[0]}, output: {incumbent_rate[1]}, source: "https://x"}}
  candidate: {{input: {candidate_rate[0]}, output: {candidate_rate[1]}, source: "https://y"}}
"""
    )
    return prices.load(path)


def test_char_estimate_matches_formula(tmp_path):
    s = _suite(tmp_path, n=1, max_out=64)
    p = _prices(tmp_path)
    price = p.rows["incumbent"]
    arm = cost.estimate_arm(s, "incumbent", price, limit=None)
    item = arm.items[0]
    expected_in = math.ceil((len(s.task) + len(s.items[0].input)) / cost.CHARS_PER_TOKEN) + cost.INPUT_TOKEN_PAD
    assert item.input_tokens == expected_in
    assert item.output_tokens == 64
    raw_cost = expected_in * price.input / 1e6 + 64 * price.output / 1e6
    assert abs(arm.cost_usd - raw_cost * cost.ESTIMATE_HEADROOM) < 1e-12


def test_estimate_run_totals_both_arms(tmp_path):
    s = _suite(tmp_path, n=3)
    p = _prices(tmp_path)
    est = cost.estimate_run(s, "incumbent", "candidate", p)
    assert est.total_usd == est.incumbent.cost_usd + est.candidate.cost_usd
    assert len(est.incumbent.items) == 3
    assert len(est.candidate.items) == 3


def test_estimate_run_respects_limit(tmp_path):
    s = _suite(tmp_path, n=5)
    p = _prices(tmp_path)
    est = cost.estimate_run(s, "incumbent", "candidate", p, limit=2)
    assert len(est.incumbent.items) == 2


def test_unpriced_model_raises(tmp_path):
    s = _suite(tmp_path, n=1)
    with pytest.raises(ValueError):
        cost.estimate_arm(s, "ghost-model", None, limit=None)


def test_prior_measured_tokens_override_char_estimate(tmp_path):
    s = _suite(tmp_path, n=1)
    p = _prices(tmp_path)
    suite_hash = suite.freeze_hash(s)

    state_dir = tmp_path / "state"
    run_dir = state_dir / "runs" / "prior-run"
    run_dir.mkdir(parents=True)
    (run_dir / "run.json").write_text(
        json.dumps(
            {
                "suite_hash": suite_hash,
                "incumbent": "incumbent",
                "candidate": "candidate",
                "items": [
                    {
                        "id": "q00",
                        "incumbent": {"input_tokens": 999},
                        "candidate": {"input_tokens": 50},
                    }
                ],
            }
        )
    )

    arm = cost.estimate_arm(s, "incumbent", p.rows["incumbent"], state_dir=state_dir, suite_hash=suite_hash)
    assert arm.items[0].measured is True
    assert arm.items[0].input_tokens == 999
    assert arm.measured_count == 1


def test_cost_meter_check_and_charge():
    meter = cost.CostMeter(cap_usd=1.0)
    assert meter.check(0.5) is True
    meter.charge(0.5, 0.5)
    assert meter.spent_usd == 0.5
    assert meter.check(0.4) is True
    assert meter.check(0.6) is False
    assert meter.stopped_early is True


def test_cost_meter_charges_estimate_when_actual_missing():
    meter = cost.CostMeter(cap_usd=1.0)
    cost_charged = meter.charge(None, 0.3)
    assert cost_charged == 0.3
    assert meter.spent_usd == 0.3
