"""`run --reuse-incumbent RUN`: the incumbent's stored outputs and judge records fill the
incumbent slot; every refusal fires before any call; cost is recomputed; provenance is
recorded and reported. Built on examples/suites/review.yaml and the fake provider.

Every model here returns DIFFERENT output text, tokens, judge hits/extra/notes and recorded
request params, so a reuse that picks the wrong source arm (or the slot instead of the model)
cannot pass by accident."""

import json
import socket
from pathlib import Path

import pytest

from pinbench import cli, judge, prices, providers, report, runner, suite

SUITES = Path(__file__).resolve().parent.parent / "examples" / "suites"
REVIEW = SUITES / "review.yaml"
CAPITALS = SUITES / "capitals.yaml"
JUDGE = "claude-opus-5-5"
INC = "claude-inc"
CAND = "claude-cand"
OTHER = "claude-other"

# model -> (input_tokens, output_tokens), and its request_defaults (recorded per arm)
TOKENS = {INC: (111, 13), CAND: (222, 29), OTHER: (333, 41)}
DEFAULTS = {INC: {"effort": "low"}, CAND: {"effort": "high"}, OTHER: {"effort": "mid"}}
RATES = {CAND: (0.5, 1.0), OTHER: (0.7, 1.4)}


@pytest.fixture(autouse=True)
def _forbid_real_sockets(monkeypatch):
    def _raise(*a, **k):
        raise AssertionError("a test tried to open a real socket")

    monkeypatch.setattr(socket, "socket", _raise)


def _prices(tmp_path, inc_rate=(1.0, 2.0), defaults=None, judge_multiplier=0.5, name="prices.yaml", inc_id=INC):
    defaults = {**DEFAULTS, **(defaults or {})}
    rows = {inc_id: inc_rate, **RATES}
    lines = []
    for model, (i, o) in rows.items():
        d = defaults.get(model if model != inc_id else INC)
        extra = f", request_defaults: {json.dumps(d)}" if d else ""
        lines.append(f'  {model}: {{input: {i}, output: {o}, source: "https://x"{extra}}}')
    mult = f", batch_multiplier: {judge_multiplier}" if judge_multiplier is not None else ""
    lines.append(f'  {JUDGE}: {{input: 2.0, output: 4.0{mult}, source: "https://z"}}')
    path = tmp_path / name
    path.write_text("version: 1\nchecked: 2026-10-26\nmodels:\n" + "\n".join(lines) + "\n")
    return prices.load(path)


def _suite_copy(tmp_path, *replacements, name="suite.yaml", base=REVIEW):
    text = base.read_text()
    for old, new in replacements:
        assert old in text, old
        text = text.replace(old, new, 1)
    path = tmp_path / name
    path.write_text(text)
    return suite.load(path), path


def _out(model, item):
    return f"{model}-out:{item.input}"


def _register(fake_adapter, s, models=(INC, CAND, OTHER)):
    """Distinct arm reply and distinct judge verdict per model."""
    for model in models:
        in_tok, out_tok = TOKENS[model]
        for item in s.items:
            fake_adapter.set_reply(model, item.input, providers.Reply(output=_out(model, item), input_tokens=in_tok, output_tokens=out_tok, latency_ms=17, error=None, stop_reason="end_turn"))
            if s.judge is None:
                continue
            _system, user = judge.build_messages(item, s, _out(model, item))
            hits = [item.rubric_points[0].id] if model == INC else [p.id for p in item.rubric_points]
            raw = json.dumps({"hit": hits, "extra": 1 if model == INC else 0, "notes": f"judge-{model}"})
            fake_adapter.set_reply(JUDGE, user, providers.Reply(output=raw, input_tokens=100, output_tokens=10, latency_ms=1, error=None))


def _source_run(tmp_path, fake_adapter, incumbent=INC, candidate=CAND, p=None, name="src", base=REVIEW):
    s, path = _suite_copy(tmp_path, base=base)
    _register(fake_adapter, s)
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


def _ready(tmp_path, fake_adapter, **kw):
    src = _source_run(tmp_path, fake_adapter, **kw)
    s, path = _suite_copy(tmp_path)
    _register(fake_adapter, s)
    return src, s, path


def test_happy_path_reuses_incumbent_and_calls_only_the_candidate(tmp_path, fake_adapter):
    src, s, path = _ready(tmp_path, fake_adapter)
    source_data = json.loads(src.read_text())
    new_prices = _prices(tmp_path, inc_rate=(3.0, 6.0), name="prices2.yaml")

    record = _reuse(tmp_path, s, path, p=new_prices)

    models_called = [m for m, _ in fake_adapter.calls]
    assert INC not in models_called                      # incumbent adapter never called
    assert models_called.count(OTHER) == 6               # candidate ran on every item
    assert models_called.count(JUDGE) == 6               # candidate's judge calls only
    assert fake_adapter.call_count == 12
    assert record["spent_usd"]["incumbent"] == 0
    assert record["estimate_usd"]["incumbent"] == 0

    cand_cost = 6 * (333 * 0.7 / 1e6 + 41 * 1.4 / 1e6)
    judge_cost = 6 * (100 * 2.0 / 1e6 + 10 * 4.0 / 1e6)      # the candidate's six calls, not twelve
    assert record["spent_usd"]["candidate"] == pytest.approx(cand_cost)
    assert record["spent_usd"]["judge"] == pytest.approx(judge_cost)
    assert record["spent_usd"]["total"] == pytest.approx(cand_cost + judge_cost)

    for item, src_item, s_item in zip(record["items"], source_data["items"], s.items):
        arm = item["incumbent"]
        assert arm["output"] == _out(INC, s_item)            # the INCUMBENT model's outputs, not the candidate's
        assert (arm["input_tokens"], arm["output_tokens"]) == TOKENS[INC]
        assert arm["latency_ms"] == src_item["incumbent"]["latency_ms"] == 17
        assert arm["reused_cost_usd"] == src_item["incumbent"]["cost_usd"]
        assert arm["cost_usd"] == pytest.approx(111 * 3.0 / 1e6 + 13 * 6.0 / 1e6)
        j = item["judge"]["incumbent"]
        assert j["hit"] == [s_item.rubric_points[0].id] and j["extra"] == 1 and j["notes"] == f"judge-{INC}"
        assert j["reused_cost_usd"] == src_item["judge"]["incumbent"]["cost_usd"]
        assert item["candidate"]["output"] == _out(OTHER, s_item)
        assert item["judge"]["candidate"]["notes"] == f"judge-{OTHER}"
        assert item["judge"]["call_order"] == ["candidate"]

    prov = record["reused_incumbent"]
    assert prov == {
        "run": "src", "arm_in_source": "incumbent", "source_run_sha256": report.sha256_file(src),
        "source_started_at": source_data["started_at"],
    }
    assert json.loads((tmp_path / "state" / "runs" / "new" / "run.json").read_text())["reused_incumbent"] == prov


def test_source_arm_lookup_when_model_was_the_sources_candidate(tmp_path, fake_adapter):
    src, s, path = _ready(tmp_path, fake_adapter, incumbent=OTHER, candidate=INC)
    source_data = json.loads(src.read_text())
    record = _reuse(tmp_path, s, path, incumbent=INC, candidate=CAND)

    assert record["reused_incumbent"]["arm_in_source"] == "candidate"
    for item, src_item, s_item in zip(record["items"], source_data["items"], s.items):
        assert item["incumbent"]["output"] == _out(INC, s_item) == src_item["candidate"]["output"]
        assert item["incumbent"]["output"] != src_item["incumbent"]["output"]
        assert (item["incumbent"]["input_tokens"], item["incumbent"]["output_tokens"]) == TOKENS[INC]
        assert item["judge"]["incumbent"]["notes"] == f"judge-{INC}"
        assert item["judge"]["incumbent"]["hit"] == src_item["judge"]["candidate"]["hit"]
    assert INC not in [m for m, _ in fake_adapter.calls]


def test_incumbent_api_key_not_required(tmp_path, fake_adapter, monkeypatch):
    gpt = "gpt-inc"
    p = _prices(tmp_path, inc_id=gpt, defaults={})
    p.rows[gpt].request_defaults = {}
    s, path = _suite_copy(tmp_path)
    for item in s.items:
        fake_adapter.set_reply(gpt, item.input, providers.Reply(output=f"{gpt}-out:{item.input}", input_tokens=5, output_tokens=6, latency_ms=3, error=None, stop_reason="stop"))
        for model in (gpt, OTHER):
            _s, user = judge.build_messages(item, s, f"{model}-out:{item.input}")
            fake_adapter.set_reply(JUDGE, user, providers.Reply(output=json.dumps({"hit": [], "extra": 0, "notes": "n"}), input_tokens=1, output_tokens=1, latency_ms=1, error=None))
        fake_adapter.set_reply(OTHER, item.input, providers.Reply(output=f"{OTHER}-out:{item.input}", input_tokens=5, output_tokens=6, latency_ms=3, error=None, stop_reason="stop"))
    runner.run(s, gpt, CAND, p, cap_usd=50.0, state_dir=tmp_path / "state", suite_path=str(path), name="src")
    monkeypatch.delenv("OPENAI_API_KEY")
    record = runner.run(s, gpt, OTHER, p, cap_usd=50.0, state_dir=tmp_path / "state", suite_path=str(path), name="new", reuse_incumbent="src")
    assert record["reused_incumbent"]["arm_in_source"] == "incumbent"
    # ...and the plain run still needs the key
    with pytest.raises(runner.PlanRefused, match="OPENAI_API_KEY"):
        runner.run(s, gpt, OTHER, p, cap_usd=50.0, state_dir=tmp_path / "state", suite_path=str(path), name="plain")


def test_non_judge_suite_reuse(tmp_path, fake_adapter):
    s, path = _suite_copy(tmp_path, base=CAPITALS)
    p = _prices(tmp_path)
    runner.run(s, INC, CAND, p, cap_usd=5.0, state_dir=tmp_path / "state", suite_path=str(path), name="src")
    fake_adapter.calls.clear()
    record = runner.run(s, INC, OTHER, p, cap_usd=5.0, state_dir=tmp_path / "state", suite_path=str(path), name="new", reuse_incumbent="src")
    assert [m for m, _ in fake_adapter.calls] == [OTHER] * len(s.items)
    assert all("judge" not in i for i in record["items"])


def test_report_states_reuse_beside_verdict_and_uses_recomputed_cost(tmp_path, fake_adapter):
    _, s, path = _ready(tmp_path, fake_adapter)
    record = _reuse(tmp_path, s, path, p=_prices(tmp_path, inc_rate=(3.0, 6.0), name="prices2.yaml"))

    result = report.build_verdict(record, s, None, None)
    text = report.render(record, s, result, fmt="text")
    lines = text.splitlines()
    verdict_at = next(i for i, l in enumerate(lines) if l.startswith("Verdict:"))
    reuse_at = next(i for i, l in enumerate(lines) if "reused from run src" in l)
    assert 0 < verdict_at - reuse_at <= 2
    assert "different sessions" in lines[reuse_at]
    assert "recomputed" in lines[reuse_at] and "keeps its recorded cost" in lines[reuse_at] and "rescaled" in lines[reuse_at]
    assert "Carried-over" not in lines[reuse_at]

    # Judge calls: only the six made this run are "judged"; the six copied are stated apart.
    assert "judge calls: 6 judged, 0 skipped (truncated/error), all candidate; 6 incumbent judge records reused, not made this run" in text
    assert "12 judged" not in text

    md = report.render(record, s, result, fmt="md")
    md_lines = md.splitlines()
    heading = next(i for i, l in enumerate(md_lines) if l.startswith("## Verdict"))
    assert "reused from run src" in "\n".join(md_lines[heading : heading + 4])
    payload = json.loads(report.render(record, s, result, fmt="json"))
    assert payload["reused_incumbent"]["run"] == "src"
    assert payload["judge_calls_judged"] == 6

    # The cost check reads the recomputed figure (3.0/6.0 rates), not the recorded one.
    cost_check = next(c for c in result.checks if c.name == "cost")
    inc_per_item = 111 * 3.0 / 1e6 + 13 * 6.0 / 1e6
    assert f"incumbent ${inc_per_item:.6f}/item" in cost_check.detail


def test_non_reuse_run_has_no_reuse_line_and_counts_both_judge_arms(tmp_path, fake_adapter):
    src = _source_run(tmp_path, fake_adapter)
    data = json.loads(src.read_text())
    s, _ = _suite_copy(tmp_path)
    assert "reused_incumbent" not in data
    assert report._reuse_line(data) is None
    assert report._judge_calls_line(data) == "judge calls: 12 judged, 0 skipped (truncated/error)"


def _make_batch(multiplier, recorded_multiplier=True):
    def fn(data):
        data["batch"] = {"arms": {"id": "msgbatch_x"}, "judge": None}
        if recorded_multiplier:
            data["batch"]["price_multiplier"] = {INC: 0.5, CAND: 0.5, JUDGE: multiplier}
        for item in data["items"]:
            item["incumbent"]["cost_usd"] *= 0.5
            item["incumbent"]["latency_ms"] = None
            item["judge"]["incumbent"]["cost_usd"] *= multiplier
            item["judge"]["candidate"]["cost_usd"] *= multiplier
    return fn


def test_batch_source_rescaled_by_the_sources_recorded_multiplier_not_todays_row(tmp_path, fake_adapter):
    src, s, path = _ready(tmp_path, fake_adapter)
    orig = json.loads(src.read_text())
    _edit_source(src, _make_batch(0.4))      # today's price row says 0.5; the source recorded 0.4
    record = _reuse(tmp_path, s, path)
    for item, o in zip(record["items"], orig["items"]):
        assert item["incumbent"]["cost_usd"] == pytest.approx(111 * 1.0 / 1e6 + 13 * 2.0 / 1e6)
        assert item["incumbent"]["reused_cost_usd"] == pytest.approx(o["incumbent"]["cost_usd"] * 0.5)
        assert item["incumbent"]["latency_ms"] is None       # never presented as live
        assert item["judge"]["incumbent"]["cost_usd"] == pytest.approx(o["judge"]["incumbent"]["cost_usd"])
        assert item["judge"]["incumbent"]["reused_cost_usd"] == pytest.approx(o["judge"]["incumbent"]["cost_usd"] * 0.4)


def test_batch_source_without_recorded_multiplier_falls_back_to_the_price_row(tmp_path, fake_adapter):
    src, s, path = _ready(tmp_path, fake_adapter)
    orig = json.loads(src.read_text())
    _edit_source(src, _make_batch(0.5, recorded_multiplier=False))
    record = _reuse(tmp_path, s, path)
    assert record["items"][0]["judge"]["incumbent"]["cost_usd"] == pytest.approx(orig["items"][0]["judge"]["incumbent"]["cost_usd"])


def test_refuse_batch_source_with_no_multiplier_anywhere(tmp_path, fake_adapter):
    src, s, path = _ready(tmp_path, fake_adapter)
    _edit_source(src, _make_batch(0.5, recorded_multiplier=False))
    with pytest.raises(runner.PlanRefused) as exc:
        _reuse(tmp_path, s, path, p=_prices(tmp_path, judge_multiplier=None, name="prices2.yaml"))
    _assert_refused(fake_adapter, exc, "batch-priced", "batch_multiplier")


def test_stray_batch_json_beside_a_sync_run_does_not_make_it_batch(tmp_path, fake_adapter):
    src, s, path = _ready(tmp_path, fake_adapter)
    orig = json.loads(src.read_text())
    (src.parent / "batch.json").write_text("{}")
    record = _reuse(tmp_path, s, path)
    assert record["items"][0]["judge"]["incumbent"]["cost_usd"] == orig["items"][0]["judge"]["incumbent"]["cost_usd"]


def test_judge_errors_and_skips_carry_over_as_recorded_and_warn(tmp_path, fake_adapter, capsys):
    src, s, path = _ready(tmp_path, fake_adapter)

    def mutate(data):
        data["items"][0]["judge"]["incumbent"].update({"hit": [], "error": "malformed judge JSON: x"})
        data["items"][1]["judge"]["incumbent"].update({"hit": [], "skipped": "skipped: arm output truncated", "cost_usd": 0.0})

    _edit_source(src, mutate)
    record = _reuse(tmp_path, s, path)
    err = capsys.readouterr().err
    assert "warning:" in err and "1 arm/judge error(s)" in err and "'r01'" in err and "INCOMPLETE" in err
    assert record["items"][0]["judge"]["incumbent"]["error"] == "malformed judge JSON: x"
    assert record["items"][1]["judge"]["incumbent"]["skipped"] == "skipped: arm output truncated"
    assert record["items"][1]["judge"]["incumbent"]["cost_usd"] == 0.0
    line = report._reuse_line(record)
    assert "Carried-over incumbent errors: 0 arm, 1 judge" in line and "INCOMPLETE" in line


def test_clean_source_prints_no_warning(tmp_path, fake_adapter, capsys):
    _, s, path = _ready(tmp_path, fake_adapter)
    _reuse(tmp_path, s, path)
    assert "warning" not in capsys.readouterr().err


def test_plan_also_warns_about_carried_errors(tmp_path, fake_adapter, capsys):
    src, s, path = _ready(tmp_path, fake_adapter)
    _edit_source(src, lambda d: d["items"][2]["incumbent"].update(error="boom", output=None))
    runner.plan(s, INC, OTHER, _prices(tmp_path), cap_usd=50.0, state_dir=tmp_path / "state", reuse_incumbent="src")
    err = capsys.readouterr().err
    assert "'r03'" in err and "INCOMPLETE" in err


def test_errored_arm_keeps_its_recorded_cost_even_with_tokens(tmp_path, fake_adapter):
    src, s, path = _ready(tmp_path, fake_adapter)

    def mutate(data):
        data["items"][0]["incumbent"].update(error="boom", output=None, cost_usd=0.5)           # tokens still present
        data["items"][1]["incumbent"].update(input_tokens=None, output_tokens=None, cost_usd=0.25)  # tokenless

    _edit_source(src, mutate)
    record = _reuse(tmp_path, s, path, p=_prices(tmp_path, inc_rate=(3.0, 6.0), name="prices2.yaml"))
    assert record["items"][0]["incumbent"]["cost_usd"] == 0.5
    assert record["items"][1]["incumbent"]["cost_usd"] == 0.25
    assert record["items"][2]["incumbent"]["cost_usd"] == pytest.approx(111 * 3.0 / 1e6 + 13 * 6.0 / 1e6)


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


def test_refuse_source_judged_but_suite_has_no_judge(tmp_path, fake_adapter):
    s, path = _suite_copy(tmp_path, base=CAPITALS)
    p = _prices(tmp_path)
    runner.run(s, INC, CAND, p, cap_usd=5.0, state_dir=tmp_path / "state", suite_path=str(path), name="src")
    fake_adapter.call_count = 0
    _edit_source(tmp_path / "state" / "runs" / "src" / "run.json", lambda d: d.update(judge_model=JUDGE))
    with pytest.raises(runner.PlanRefused) as exc:
        runner.run(s, INC, OTHER, p, cap_usd=5.0, state_dir=tmp_path / "state", suite_path=str(path), name="new", reuse_incumbent="src")
    _assert_refused(fake_adapter, exc, "no judge")


def test_refuse_request_params_differ(tmp_path, fake_adapter):
    _source_run(tmp_path, fake_adapter)
    different = _prices(tmp_path, defaults={INC: {"effort": "high"}}, name="prices2.yaml")
    with pytest.raises(runner.PlanRefused) as exc:
        _reuse(tmp_path, p=different)
    _assert_refused(fake_adapter, exc, "request params")


def test_refuse_params_compared_against_the_reused_arm_not_the_other(tmp_path, fake_adapter):
    # The source's candidate (CAND) ran at effort=high; reusing INC (effort=low) must compare to INC's arm.
    _source_run(tmp_path, fake_adapter)
    p = _prices(tmp_path, defaults={INC: {"effort": "high"}}, name="prices2.yaml")   # equals the OTHER arm's recorded params
    with pytest.raises(runner.PlanRefused) as exc:
        _reuse(tmp_path, p=p)
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


def test_refuse_source_items_total_smaller_than_suite(tmp_path, fake_adapter):
    src = _source_run(tmp_path, fake_adapter)
    _edit_source(src, lambda d: d.update(items_total=3))
    with pytest.raises(runner.PlanRefused) as exc:
        _reuse(tmp_path)
    _assert_refused(fake_adapter, exc, "suite items")


def test_refuse_source_item_ids_out_of_step_with_suite(tmp_path, fake_adapter):
    src = _source_run(tmp_path, fake_adapter)
    _edit_source(src, lambda d: d["items"][0].update(id="zzz"))
    with pytest.raises(runner.PlanRefused) as exc:
        _reuse(tmp_path)
    _assert_refused(fake_adapter, exc, "incomplete", "r01")


def test_refuse_source_missing_judge_record_for_the_arm(tmp_path, fake_adapter):
    src = _source_run(tmp_path, fake_adapter)
    _edit_source(src, lambda d: d["items"][4]["judge"].pop("incumbent"))
    with pytest.raises(runner.PlanRefused) as exc:
        _reuse(tmp_path)
    _assert_refused(fake_adapter, exc, "incomplete")


def test_refuse_chained_reuse(tmp_path, fake_adapter):
    _, s, path = _ready(tmp_path, fake_adapter)
    _reuse(tmp_path, s, path)                       # writes run "new", a reuse run
    fake_adapter.call_count = 0
    with pytest.raises(runner.PlanRefused) as exc:
        runner.run(s, INC, CAND, _prices(tmp_path), cap_usd=50.0, state_dir=tmp_path / "state", suite_path=str(path), name="third", reuse_incumbent="new")
    _assert_refused(fake_adapter, exc, "itself a reuse run", "reuse the original run")


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
    _, s, path = _ready(tmp_path, fake_adapter)
    _prices(tmp_path)
    monkeypatch.chdir(tmp_path)
    common = [str(path), "--incumbent", INC, "--candidate", OTHER, "--cap", "50", "--prices", "prices.yaml", "--state", "state"]

    assert cli.main(["plan", *common, "--reuse-incumbent", "src"]) == 0
    assert "reused from src" in capsys.readouterr().out
    assert cli.main(["run", *common, "--name", "viacli", "--reuse-incumbent", "src"]) == 0
    assert "reused from src" in capsys.readouterr().out
    assert INC not in [m for m, _ in fake_adapter.calls]

    assert cli.main(["run", *common, "--reuse-incumbent", "src", "--batch"]) == 2
    assert cli.main(["run", *common, "--reuse-incumbent", "gone"]) == 2


def test_reusing_a_reuse_runs_live_candidate_is_allowed(tmp_path, fake_adapter):
    _, s, path = _ready(tmp_path, fake_adapter)
    _reuse(tmp_path, s, path)                       # run "new": INC reused, OTHER measured live
    fake_adapter.calls.clear()
    fake_adapter.call_count = 0
    record = runner.run(s, OTHER, CAND, _prices(tmp_path), cap_usd=50.0, state_dir=tmp_path / "state", suite_path=str(path), name="third", reuse_incumbent="new")
    assert record["reused_incumbent"]["arm_in_source"] == "candidate"
    for item, s_item in zip(record["items"], s.items):
        assert item["incumbent"]["output"] == _out(OTHER, s_item)
    assert OTHER not in [m for m, _ in fake_adapter.calls]


def test_chained_refusal_names_the_reused_incumbent_slot(tmp_path, fake_adapter):
    _, s, path = _ready(tmp_path, fake_adapter)
    _reuse(tmp_path, s, path)
    fake_adapter.call_count = 0
    with pytest.raises(runner.PlanRefused) as exc:
        runner.plan(s, INC, CAND, _prices(tmp_path), cap_usd=50.0, state_dir=tmp_path / "state", reuse_incumbent="new")
    _assert_refused(fake_adapter, exc, "reused incumbent", "reuse the original run")


def test_truncated_reused_arm_warns_with_count_and_first_item(tmp_path, fake_adapter, capsys):
    src, s, path = _ready(tmp_path, fake_adapter)

    def mutate(data):
        for i in (1, 3):
            data["items"][i]["incumbent"]["stop_reason"] = "max_tokens"

    _edit_source(src, mutate)
    _reuse(tmp_path, s, path)
    err = capsys.readouterr().err
    assert "2 truncated output(s)" in err and "max_truncated" in err and "'r02'" in err and "INCOMPLETE" in err


def test_truncation_within_policy_allowance_prints_no_warning(tmp_path, fake_adapter, capsys):
    src, s, path = _ready(tmp_path, fake_adapter)
    s.policy.max_truncated = 1
    _edit_source(src, lambda d: d["items"][1]["incumbent"].update(stop_reason="max_tokens"))
    _reuse(tmp_path, s, path)
    assert "truncated" not in capsys.readouterr().err


@pytest.mark.parametrize("bad", [0, -0.5, 1.5, "0.5", True, float("nan")])
def test_refuse_invalid_recorded_batch_multiplier(tmp_path, fake_adapter, bad):
    src, s, path = _ready(tmp_path, fake_adapter)
    _edit_source(src, _make_batch(0.5))
    _edit_source(src, lambda d: d["batch"]["price_multiplier"].update({JUDGE: bad}))
    with pytest.raises(runner.PlanRefused) as exc:
        _reuse(tmp_path, s, path)
    _assert_refused(fake_adapter, exc, "price_multiplier", "> 0 and <= 1")
