"""`run --reuse-incumbent RUN`: the incumbent's stored outputs and judge records fill the
incumbent slot; every refusal fires before any call; cost is recomputed; provenance is
recorded and reported. Built on examples/suites/review.yaml and the fake provider."""

import json
import socket
from pathlib import Path

import pytest

from pinbench import cli, judge, prices, providers, report, runner, suite

REVIEW = Path(__file__).resolve().parent.parent / "examples" / "suites" / "review.yaml"
JUDGE = "claude-opus-5-5"
INC = "claude-inc"
CAND = "claude-cand"
OTHER = "claude-other"


@pytest.fixture(autouse=True)
def _forbid_real_sockets(monkeypatch):
    def _raise(*a, **k):
        raise AssertionError("a test tried to open a real socket")

    monkeypatch.setattr(socket, "socket", _raise)


def _prices(tmp_path, inc_rate=(1.0, 2.0), inc_defaults=None, name="prices.yaml"):
    extra = f", request_defaults: {json.dumps(inc_defaults)}" if inc_defaults else ""
    path = tmp_path / name
    path.write_text(
        f"""\
version: 1
checked: 2026-10-26
models:
  {INC}: {{input: {inc_rate[0]}, output: {inc_rate[1]}, source: "https://x"{extra}}}
  {CAND}: {{input: 0.5, output: 1.0, source: "https://y"}}
  {OTHER}: {{input: 0.7, output: 1.4, source: "https://y"}}
  {JUDGE}: {{input: 2.0, output: 4.0, batch_multiplier: 0.5, source: "https://z"}}
"""
    )
    return prices.load(path)


def _suite_copy(tmp_path, *replacements, name="suite.yaml"):
    text = REVIEW.read_text()
    for old, new in replacements:
        assert old in text, old
        text = text.replace(old, new, 1)
    path = tmp_path / name
    path.write_text(text)
    return suite.load(path), path


def _register_judge_replies(fake_adapter, s):
    for item in s.items:
        _system, user = judge.build_messages(item, s, f"echo:{item.input}")
        raw = json.dumps({"hit": [p.id for p in item.rubric_points], "extra": 0, "notes": "ok"})
        fake_adapter.set_reply(JUDGE, user, providers.Reply(output=raw, input_tokens=100, output_tokens=10, latency_ms=1, error=None))


def _source_run(tmp_path, fake_adapter, incumbent=INC, candidate=CAND, p=None, name="src"):
    s, path = _suite_copy(tmp_path)
    _register_judge_replies(fake_adapter, s)
    runner.run(
        s, incumbent, candidate, p or _prices(tmp_path), cap_usd=50.0, state_dir=tmp_path / "state",
        suite_path=str(path), name=name,
    )
    fake_adapter.calls.clear()
    fake_adapter.call_count = 0
    return tmp_path / "state" / "runs" / name / "run.json"


def _reuse(tmp_path, s=None, path=None, p=None, incumbent=INC, candidate=OTHER, source="src", **kw):
    if s is None:
        s, path = _suite_copy(tmp_path)
    return runner.run(
        s, incumbent, candidate, p or _prices(tmp_path), cap_usd=50.0, state_dir=tmp_path / "state",
        suite_path=str(path), name="new", reuse_incumbent=source, **kw,
    )


def _edit_source(run_json, fn):
    data = json.loads(run_json.read_text())
    fn(data)
    run_json.write_text(json.dumps(data))


def _assert_refused(fake_adapter, exc_info, *words):
    assert fake_adapter.call_count == 0
    for w in words:
        assert w in str(exc_info.value)


def test_happy_path_reuses_incumbent_and_calls_only_the_candidate(tmp_path, fake_adapter):
    src = _source_run(tmp_path, fake_adapter)
    s, path = _suite_copy(tmp_path)
    _register_judge_replies(fake_adapter, s)
    source_data = json.loads(src.read_text())
    # A new price row for the reused model: cost must be recomputed from recorded tokens.
    new_prices = _prices(tmp_path, inc_rate=(3.0, 6.0), name="prices2.yaml")

    record = _reuse(tmp_path, s, path, p=new_prices)

    models_called = [m for m, _ in fake_adapter.calls]
    assert INC not in models_called                      # incumbent adapter never called
    assert models_called.count(OTHER) == 6              # candidate ran on every item
    assert models_called.count(JUDGE) == 6              # candidate's judge calls only
    assert fake_adapter.call_count == 12
    assert record["spent_usd"]["incumbent"] == 0
    assert record["estimate_usd"]["incumbent"] == 0
    assert record["spent_usd"]["total"] == pytest.approx(record["spent_usd"]["candidate"] + record["spent_usd"]["judge"])

    for item, src_item in zip(record["items"], source_data["items"]):
        arm, src_arm = item["incumbent"], src_item["incumbent"]
        assert arm["output"] == src_arm["output"]
        assert arm["latency_ms"] == src_arm["latency_ms"]
        assert arm["reused_cost_usd"] == src_arm["cost_usd"]
        assert arm["cost_usd"] == pytest.approx(arm["input_tokens"] * 3.0 / 1e6 + arm["output_tokens"] * 6.0 / 1e6)
        assert arm["cost_usd"] != src_arm["cost_usd"]
        assert item["judge"]["incumbent"]["hit"] == src_item["judge"]["incumbent"]["hit"]
        assert item["judge"]["incumbent"]["reused_cost_usd"] == src_item["judge"]["incumbent"]["cost_usd"]
        assert item["candidate"]["output"].startswith("echo:")

    prov = record["reused_incumbent"]
    assert prov["run"] == "src"
    assert prov["arm_in_source"] == "incumbent"
    assert prov["source_started_at"] == source_data["started_at"]
    assert prov["source_run_sha256"] == report.sha256_file(src)
    assert json.loads((tmp_path / "state" / "runs" / "new" / "run.json").read_text())["reused_incumbent"] == prov


def test_report_states_reuse_beside_verdict_and_uses_recomputed_cost(tmp_path, fake_adapter):
    _source_run(tmp_path, fake_adapter)
    s, path = _suite_copy(tmp_path)
    _register_judge_replies(fake_adapter, s)
    record = _reuse(tmp_path, s, path, p=_prices(tmp_path, inc_rate=(3.0, 6.0), name="prices2.yaml"))

    result = report.build_verdict(record, s, None, None)
    text = report.render(record, s, result, fmt="text")
    lines = text.splitlines()
    verdict_at = next(i for i, l in enumerate(lines) if l.startswith("Verdict:"))
    reuse_at = next(i for i, l in enumerate(lines) if "reused from run src" in l)
    assert 0 < verdict_at - reuse_at <= 2
    assert "different sessions" in lines[reuse_at]
    assert "recomputed" in lines[reuse_at] and "not what was paid" in lines[reuse_at]

    md = report.render(record, s, result, fmt="md")
    md_lines = md.splitlines()
    assert any(l.startswith("## Verdict") for l in md_lines)
    heading = next(i for i, l in enumerate(md_lines) if l.startswith("## Verdict"))
    assert "reused from run src" in "\n".join(md_lines[heading : heading + 4])
    assert json.loads(report.render(record, s, result, fmt="json"))["reused_incumbent"]["run"] == "src"

    # The cost check reads the recomputed figure (3.0/6.0 rates), not the recorded one.
    cost_check = next(c for c in result.checks if c.name == "cost")
    inc_per_item = sum(i["incumbent"]["cost_usd"] for i in record["items"]) / len(record["items"])
    assert f"incumbent ${inc_per_item:.6f}/item" in cost_check.detail


def test_non_reuse_run_has_no_reuse_line(tmp_path, fake_adapter):
    src = _source_run(tmp_path, fake_adapter)
    data = json.loads(src.read_text())
    s, _ = _suite_copy(tmp_path)
    assert "reused_incumbent" not in data
    assert report._reuse_line(data) is None


def test_source_arm_lookup_when_model_was_the_sources_candidate(tmp_path, fake_adapter):
    src = _source_run(tmp_path, fake_adapter, incumbent=OTHER, candidate=INC)
    s, path = _suite_copy(tmp_path)
    _register_judge_replies(fake_adapter, s)
    record = _reuse(tmp_path, s, path, incumbent=INC, candidate=CAND)

    assert record["reused_incumbent"]["arm_in_source"] == "candidate"
    source_data = json.loads(src.read_text())
    for item, src_item in zip(record["items"], source_data["items"]):
        assert item["incumbent"]["output"] == src_item["candidate"]["output"]
        assert item["judge"]["incumbent"]["hit"] == src_item["judge"]["candidate"]["hit"]
    assert INC not in [m for m, _ in fake_adapter.calls]


def test_batch_priced_source_is_recomputed_at_sync_prices(tmp_path, fake_adapter):
    src = _source_run(tmp_path, fake_adapter)
    source_data = json.loads(src.read_text())

    def make_batch(data):
        data["batch"] = {"arms": {"id": "msgbatch_x"}, "judge": None}
        for item in data["items"]:
            item["incumbent"]["cost_usd"] *= 0.5
            item["incumbent"]["latency_ms"] = None
            item["judge"]["incumbent"]["cost_usd"] *= 0.5

    _edit_source(src, make_batch)
    s, path = _suite_copy(tmp_path)
    _register_judge_replies(fake_adapter, s)
    record = _reuse(tmp_path, s, path)

    for item, orig in zip(record["items"], source_data["items"]):
        assert item["incumbent"]["cost_usd"] == pytest.approx(orig["incumbent"]["cost_usd"])
        assert item["incumbent"]["reused_cost_usd"] == pytest.approx(orig["incumbent"]["cost_usd"] * 0.5)
        assert item["incumbent"]["latency_ms"] is None   # never presented as live
        assert item["judge"]["incumbent"]["cost_usd"] == pytest.approx(orig["judge"]["incumbent"]["cost_usd"])


def test_batch_marker_file_also_marks_a_batch_source(tmp_path, fake_adapter):
    src = _source_run(tmp_path, fake_adapter)
    (src.parent / "batch.json").write_text("{}")
    source_data = json.loads(src.read_text())
    s, path = _suite_copy(tmp_path)
    _register_judge_replies(fake_adapter, s)
    record = _reuse(tmp_path, s, path)
    first = record["items"][0]["judge"]["incumbent"]
    assert first["cost_usd"] == pytest.approx(source_data["items"][0]["judge"]["incumbent"]["cost_usd"] * 2)


def test_judge_errors_and_skips_carry_over_as_recorded(tmp_path, fake_adapter):
    src = _source_run(tmp_path, fake_adapter)

    def mutate(data):
        data["items"][0]["judge"]["incumbent"].update({"hit": [], "error": "malformed judge JSON: x"})
        data["items"][1]["judge"]["incumbent"].update({"hit": [], "skipped": "skipped: arm output truncated", "cost_usd": 0.0})

    _edit_source(src, mutate)
    s, path = _suite_copy(tmp_path)
    _register_judge_replies(fake_adapter, s)
    record = _reuse(tmp_path, s, path)
    assert record["items"][0]["judge"]["incumbent"]["error"] == "malformed judge JSON: x"
    assert record["items"][1]["judge"]["incumbent"]["skipped"] == "skipped: arm output truncated"
    assert record["items"][1]["judge"]["incumbent"]["cost_usd"] == 0.0


def test_incumbent_api_key_not_required(tmp_path, fake_adapter, monkeypatch):
    # Both arms share a provider here, so assert on the mechanism: _check_api_keys skips None.
    runner._check_api_keys(None, None)


# ---- refusals: every one fires with zero calls ----

def test_refuse_missing_run(tmp_path, fake_adapter):
    _source_run(tmp_path, fake_adapter)
    with pytest.raises(runner.PlanRefused) as exc:
        _reuse(tmp_path, source="nope")
    _assert_refused(fake_adapter, exc, "nope")


def test_refuse_neither_arm_is_the_incumbent(tmp_path, fake_adapter):
    _source_run(tmp_path, fake_adapter, incumbent=OTHER, candidate=CAND)
    with pytest.raises(runner.PlanRefused) as exc:
        _reuse(tmp_path, incumbent=INC, candidate=OTHER)
    _assert_refused(fake_adapter, exc, "neither arm")


def test_refuse_both_arms_are_the_incumbent(tmp_path, fake_adapter):
    _source_run(tmp_path, fake_adapter, incumbent=INC, candidate=INC)
    with pytest.raises(runner.PlanRefused) as exc:
        _reuse(tmp_path)
    _assert_refused(fake_adapter, exc, "ambiguous")


@pytest.mark.parametrize("bad", ["../src", "a/b", ".."])
def test_refuse_run_name_that_is_a_path(tmp_path, fake_adapter, bad):
    _source_run(tmp_path, fake_adapter)
    with pytest.raises(runner.PlanRefused):
        _reuse(tmp_path, source=bad)
    assert fake_adapter.call_count == 0


def test_refuse_suite_hash_differs(tmp_path, fake_adapter):
    _source_run(tmp_path, fake_adapter)
    s, path = _suite_copy(tmp_path, ("max_output_tokens: 1200", "max_output_tokens: 1100"), name="s2.yaml")
    with pytest.raises(runner.PlanRefused) as exc:
        _reuse(tmp_path, s, path)
    _assert_refused(fake_adapter, exc, "suite_hash")


def test_refuse_limit(tmp_path, fake_adapter):
    _source_run(tmp_path, fake_adapter)
    with pytest.raises(runner.PlanRefused) as exc:
        _reuse(tmp_path, limit=3)
    _assert_refused(fake_adapter, exc, "--limit")


def test_refuse_judge_model_differs(tmp_path, fake_adapter):
    _source_run(tmp_path, fake_adapter)
    s, path = _suite_copy(tmp_path, (f"model: {JUDGE}", f"model: {OTHER}"), name="s2.yaml")
    with pytest.raises(runner.PlanRefused) as exc:
        _reuse(tmp_path, s, path, candidate=CAND)
    _assert_refused(fake_adapter, exc, "judge differs", "different judge models")


def test_refuse_judge_prompt_hash_differs(tmp_path, fake_adapter):
    _source_run(tmp_path, fake_adapter)
    s, path = _suite_copy(tmp_path, ("You are reviewing a short code diff", "You are reviewing a tiny code diff"), name="s2.yaml")
    with pytest.raises(runner.PlanRefused) as exc:
        _reuse(tmp_path, s, path)
    _assert_refused(fake_adapter, exc, "judge prompt hash")


def test_refuse_request_params_differ(tmp_path, fake_adapter):
    _source_run(tmp_path, fake_adapter)
    different = _prices(tmp_path, inc_defaults={"effort": "high"}, name="prices2.yaml")
    with pytest.raises(runner.PlanRefused) as exc:
        _reuse(tmp_path, p=different)
    _assert_refused(fake_adapter, exc, "request params")


def test_refuse_source_missing_recorded_params(tmp_path, fake_adapter):
    src = _source_run(tmp_path, fake_adapter)
    _edit_source(src, lambda d: d.pop("request_params"))
    with pytest.raises(runner.PlanRefused) as exc:
        _reuse(tmp_path)
    _assert_refused(fake_adapter, exc, "request params")


def test_refuse_source_stopped_early(tmp_path, fake_adapter):
    src = _source_run(tmp_path, fake_adapter)
    _edit_source(src, lambda d: d.update(stopped_early=True))
    with pytest.raises(runner.PlanRefused) as exc:
        _reuse(tmp_path)
    _assert_refused(fake_adapter, exc, "stopped early")


def test_refuse_source_covers_fewer_items(tmp_path, fake_adapter):
    src = _source_run(tmp_path, fake_adapter)
    _edit_source(src, lambda d: d.update(items=d["items"][:5]))
    with pytest.raises(runner.PlanRefused) as exc:
        _reuse(tmp_path)
    _assert_refused(fake_adapter, exc, "5 of 6")


def test_refuse_source_missing_judge_record_for_the_arm(tmp_path, fake_adapter):
    src = _source_run(tmp_path, fake_adapter)
    _edit_source(src, lambda d: d["items"][4]["judge"].pop("incumbent"))
    with pytest.raises(runner.PlanRefused) as exc:
        _reuse(tmp_path)
    _assert_refused(fake_adapter, exc, "incomplete")


def test_refuse_batch_with_reuse(tmp_path, fake_adapter):
    _source_run(tmp_path, fake_adapter)
    with pytest.raises(runner.PlanRefused) as exc:
        _reuse(tmp_path, batch=True)
    _assert_refused(fake_adapter, exc, "--batch", "not supported")


# ---- plan and CLI ----

def test_plan_estimates_incumbent_and_its_judge_calls_at_zero(tmp_path, fake_adapter):
    _source_run(tmp_path, fake_adapter)
    s, _ = _suite_copy(tmp_path)
    p = _prices(tmp_path)
    full = runner.plan(s, INC, OTHER, p, cap_usd=50.0, state_dir=tmp_path / "state")
    reused = runner.plan(s, INC, OTHER, p, cap_usd=50.0, state_dir=tmp_path / "state", reuse_incumbent="src")
    assert reused.estimate.incumbent.cost_usd == 0
    assert len(reused.estimate.judge.items) == 6
    assert all(e.item_id.endswith(":candidate") for e in reused.estimate.judge.items)
    assert reused.estimate.judge.cost_usd == pytest.approx(full.estimate.judge.cost_usd / 2)
    assert reused.estimate.candidate.cost_usd == full.estimate.candidate.cost_usd
    assert fake_adapter.call_count == 0


def test_plan_refuses_what_run_would_refuse(tmp_path, fake_adapter):
    s, _ = _suite_copy(tmp_path)
    with pytest.raises(runner.PlanRefused):
        runner.plan(s, INC, OTHER, _prices(tmp_path), cap_usd=50.0, state_dir=tmp_path / "state", reuse_incumbent="missing")


def test_cli_plan_and_run_accept_the_flag(tmp_path, fake_adapter, monkeypatch, capsys):
    _source_run(tmp_path, fake_adapter)
    s, path = _suite_copy(tmp_path)
    _register_judge_replies(fake_adapter, s)
    _prices(tmp_path)
    monkeypatch.chdir(tmp_path)
    common = [str(path), "--incumbent", INC, "--candidate", OTHER, "--cap", "50", "--prices", "prices.yaml", "--state", "state"]

    assert cli.main(["plan", *common, "--reuse-incumbent", "src"]) == 0
    assert "reused from src" in capsys.readouterr().out
    assert cli.main(["run", *common, "--name", "viacli", "--reuse-incumbent", "src"]) == 0
    out = capsys.readouterr().out
    assert "reused from src" in out
    assert INC not in [m for m, _ in fake_adapter.calls]

    assert cli.main(["run", *common, "--reuse-incumbent", "src", "--batch"]) == 2
    assert cli.main(["run", *common, "--reuse-incumbent", "gone"]) == 2
