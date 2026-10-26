import json

from pinbench import prices, providers, runner, suite


def _suite(tmp_path, n=3, max_out=16, name="demo"):
    items = "\n".join(
        f'  - id: q{i:02d}\n    input: "Q{i}?"\n    expect: "A{i}"' for i in range(n)
    )
    path = tmp_path / f"{name}.yaml"
    path.write_text(
        f"""\
version: 1
name: {name}
task: "Answer briefly."
max_output_tokens: {max_out}
policy:
  min_items: 1
items:
{items}
"""
    )
    return path


def _prices(tmp_path):
    path = tmp_path / "prices.yaml"
    path.write_text(
        """\
version: 1
checked: 2026-10-26
models:
  claude-sonnet-4-5: {input: 3.0, output: 15.0, source: "https://x"}
  gpt-4o-mini: {input: 0.15, output: 0.6, source: "https://y"}
"""
    )
    return prices.load(path)


def test_run_happy_path_writes_run_json(tmp_path, fake_adapter):
    suite_path = _suite(tmp_path, n=3)
    s = suite.load(suite_path)
    p = _prices(tmp_path)
    state_dir = tmp_path / "state"

    record = runner.run(
        s, "claude-sonnet-4-5", "gpt-4o-mini", p, cap_usd=1.0, state_dir=state_dir, suite_path=suite_path, name="r1"
    )

    assert record["run_name"] == "r1"
    assert record["items_completed"] == 3
    assert record["items_total"] == 3
    assert record["stopped_early"] is False
    assert fake_adapter.call_count == 6  # 3 items x 2 arms

    on_disk = json.loads((state_dir / "runs" / "r1" / "run.json").read_text())
    assert on_disk["run_name"] == "r1"
    assert on_disk["suite"] == str(suite_path)
    assert len(on_disk["items"]) == 3
    for item in on_disk["items"]:
        assert item["incumbent"]["error"] is None
        assert item["candidate"]["error"] is None


def test_run_refuses_on_unpriced_model(tmp_path, fake_adapter):
    suite_path = _suite(tmp_path, n=2)
    s = suite.load(suite_path)
    p = _prices(tmp_path)

    try:
        runner.run(s, "claude-sonnet-4-5", "ghost-model", p, cap_usd=1.0, state_dir=tmp_path / "state", suite_path=suite_path)
        assert False, "expected PlanRefused"
    except runner.PlanRefused:
        pass
    assert fake_adapter.call_count == 0


def test_run_refuses_on_unroutable_model(tmp_path, fake_adapter):
    suite_path = _suite(tmp_path, n=2)
    s = suite.load(suite_path)
    p = _prices(tmp_path)
    p.rows["weird-9000"] = prices.PriceRow(model="weird-9000", input=1.0, output=1.0, source="x")

    try:
        runner.run(s, "claude-sonnet-4-5", "weird-9000", p, cap_usd=1.0, state_dir=tmp_path / "state", suite_path=suite_path)
        assert False, "expected PlanRefused"
    except runner.PlanRefused:
        pass
    assert fake_adapter.call_count == 0


def test_run_refuses_when_estimate_exceeds_cap(tmp_path, fake_adapter):
    suite_path = _suite(tmp_path, n=3)
    s = suite.load(suite_path)
    p = _prices(tmp_path)

    try:
        runner.run(s, "claude-sonnet-4-5", "gpt-4o-mini", p, cap_usd=0.000001, state_dir=tmp_path / "state", suite_path=suite_path)
        assert False, "expected RunCapExceeded"
    except runner.RunCapExceeded as exc:
        assert exc.estimate.total_usd > 0.000001
    assert fake_adapter.call_count == 0


def test_run_stops_early_when_actual_usage_blows_past_estimate(tmp_path, fake_adapter):
    suite_path = _suite(tmp_path, n=5, max_out=16)
    s = suite.load(suite_path)
    p = _prices(tmp_path)

    # Make every actual reply report 10x the output tokens the estimate assumed.
    fake_adapter.default_output_tokens = 160

    # cap sized so the (correct) pre-run estimate fits, but actual spend overruns it fast.
    plan = runner.plan(s, "claude-sonnet-4-5", "gpt-4o-mini", p, cap_usd=None, state_dir=tmp_path / "state")
    cap = plan.estimate.total_usd * 1.05

    record = runner.run(
        s, "claude-sonnet-4-5", "gpt-4o-mini", p, cap_usd=cap, state_dir=tmp_path / "state", suite_path=suite_path, name="r2"
    )

    assert record["stopped_early"] is True
    assert record["items_completed"] < record["items_total"]
    assert record["spent_usd"]["total"] <= cap + 0.01  # allow one call's actual cost of headroom
    # partial run.json present
    on_disk = json.loads((tmp_path / "state" / "runs" / "r2" / "run.json").read_text())
    assert on_disk["stopped_early"] is True


def test_run_refuses_without_api_key(tmp_path, monkeypatch, fake_adapter):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    suite_path = _suite(tmp_path, n=2)
    s = suite.load(suite_path)
    p = _prices(tmp_path)

    try:
        runner.run(s, "claude-sonnet-4-5", "gpt-4o-mini", p, cap_usd=1.0, state_dir=tmp_path / "state", suite_path=suite_path)
        assert False, "expected PlanRefused"
    except runner.PlanRefused:
        pass
    assert fake_adapter.call_count == 0


def test_run_charges_estimate_when_usage_missing(tmp_path, fake_adapter):
    suite_path = _suite(tmp_path, n=1)
    s = suite.load(suite_path)
    p = _prices(tmp_path)
    fake_adapter.set_reply(
        "claude-sonnet-4-5",
        "Q0?",
        providers.Reply(output="A0", input_tokens=None, output_tokens=None, latency_ms=10, error=None),
    )

    record = runner.run(
        s, "claude-sonnet-4-5", "gpt-4o-mini", p, cap_usd=1.0, state_dir=tmp_path / "state", suite_path=suite_path, name="r3"
    )
    item = record["items"][0]
    assert item["incumbent"]["cost_usd"] > 0
