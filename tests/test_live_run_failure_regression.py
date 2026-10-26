"""Hermetic regression test for the first LIVE run (.pinbench/runs/summaries-live/run.json):
every call failed (incumbent HTTP 400 on `temperature`, candidate HTTP 404 on an
unverified org), yet the CLI used to print "items completed: 10/10" and exit 0, charge the
run estimate instead of $0, and `vote` built a page with blank A/B cards. This test
reproduces that exact failure shape against a fake provider (no network, no API keys) and
asserts every one of those defects is now fixed.
"""

from __future__ import annotations

from pinbench import cli, prices as prices_mod, providers, runner as runner_mod, suite as suite_mod

INC_ERROR = (
    'HTTP 400: {"type":"error","error":{"type":"invalid_request_error",'
    '"message":"`temperature` is deprecated for this model."},"request_id":"req_1"}'
)
CAND_ERROR = (
    'HTTP 404: {\n    "error": {\n        "message": "Your organization must be verified '
    'to use the model `gpt-5-mini`. Please go to: https://platform.openai.com/settings'
)


def _write_suite(tmp_path, n=10):
    items = "\n".join(f'  - id: s{i:02d}\n    input: "Summarize item {i} in one sentence."' for i in range(n))
    path = tmp_path / "summaries.yaml"
    path.write_text(
        f"""\
version: 1
name: summaries
task: "Summarize in one sentence under 25 words."
max_output_tokens: 60
scoring:
  labels: none
  votes: required
policy:
  min_items: 10
items:
{items}
"""
    )
    return path


def _write_prices(tmp_path):
    path = tmp_path / "prices.yaml"
    path.write_text(
        """\
version: 1
checked: 2026-10-26
models:
  claude-sonnet-5-5: {input: 2.0, output: 10.0, source: "https://x"}
  gpt-5-mini: {input: 0.25, output: 2.0, source: "https://y"}
"""
    )
    return path


def _install_always_erroring_call(fake_adapter):
    """Both arms error on every call: incumbent with an HTTP 400, candidate with an HTTP
    404, exactly the shape of the live run that surfaced this bug."""

    def fake_call(model_id, system, user_input, max_tokens, api_key, params=None, timeout=60):
        fake_adapter.call_count += 1
        fake_adapter.calls.append((model_id, user_input))
        if not api_key:
            raise ValueError("missing api key")
        if model_id == "claude-sonnet-5-5":
            return providers.Reply(
                output=None, input_tokens=None, output_tokens=None, latency_ms=300,
                error=INC_ERROR, status_code=400,
            )
        return providers.Reply(
            output=None, input_tokens=None, output_tokens=None, latency_ms=500,
            error=CAND_ERROR, status_code=404,
        )

    fake_adapter.call = fake_call


def test_live_run_failure_reproduced_and_fixed(tmp_path, fake_adapter, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    suite_path = _write_suite(tmp_path, n=10)
    prices_path = _write_prices(tmp_path)
    state_dir = tmp_path / ".pinbench"
    _install_always_erroring_call(fake_adapter)

    rc = cli.main(
        [
            "run", str(suite_path),
            "--incumbent", "claude-sonnet-5-5",
            "--candidate", "gpt-5-mini",
            "--cap", "0.25",
            "--prices", str(prices_path),
            "--state", str(state_dir),
            "--name", "summaries-live-repro",
        ]
    )
    out = capsys.readouterr()

    # Defect 2 (fail loudly): a run where every call failed is a hard failure, not exit 0.
    assert rc == 4, out.out + out.err
    assert "incumbent: 0/10 succeeded" in out.out
    assert "candidate: 0/10 succeeded" in out.out
    assert "temperature" in out.out  # first incumbent error, truncated, printed
    assert "zero successes" in out.err

    record = runner_mod.load_run(state_dir, "summaries-live-repro")

    # Defect 3 (cost accounting): a 4xx before generating costs $0, never the estimate.
    assert record["spent_usd"]["total"] == 0.0
    assert record["spent_usd"]["incumbent"] == 0.0
    assert record["spent_usd"]["candidate"] == 0.0
    assert record["estimate_usd"]["total"] > 0.0  # the estimate itself is still > 0

    # Defect 2 continued: nothing "completed" when both arms errored on every item.
    assert record["items_completed"] == 0
    assert record["items_succeeded"]["incumbent"] == 0
    assert record["items_succeeded"]["candidate"] == 0

    # Defect 1 (sampling params): nothing was explicitly set, so nothing extra was sent.
    assert record["request_params"]["incumbent"] == {"max_output_tokens": 60}
    assert record["request_params"]["candidate"] == {"max_output_tokens": 60}

    # Defect 4: `vote` refuses instead of building a page with blank A/B cards.
    capsys.readouterr()
    rc = cli.main(["vote", "summaries-live-repro", "--state", str(state_dir)])
    out = capsys.readouterr()
    assert rc != 0
    assert "refused" in out.err
    assert not (state_dir / "runs" / "summaries-live-repro" / "vote.html").exists()

    # --allow-missing still never renders a blank card: every output is error text.
    rc = cli.main(["vote", "summaries-live-repro", "--state", str(state_dir), "--allow-missing"])
    assert rc == 0
    vote_html = (state_dir / "runs" / "summaries-live-repro" / "vote.html").read_text()
    assert "temperature" in vote_html
    assert 'class="side error"' in vote_html

    # Defect 5 (verdict): INCOMPLETE, not a silent RECOMMEND/HOLD on zero real data.
    capsys.readouterr()
    rc = cli.main(["report", "summaries-live-repro", "--state", str(state_dir)])
    out = capsys.readouterr()
    assert rc == 0
    assert "Verdict: INCOMPLETE" in out.out


def test_plan_still_estimates_nonzero_before_any_call(tmp_path, fake_adapter, monkeypatch):
    """Sanity companion: `plan` on this exact pair/suite is unaffected (no calls made)."""
    monkeypatch.chdir(tmp_path)
    suite_path = _write_suite(tmp_path, n=10)
    prices_path = _write_prices(tmp_path)
    s = suite_mod.load(suite_path)
    p = prices_mod.load(prices_path)
    from pinbench import runner as runner_mod

    plan = runner_mod.plan(s, "claude-sonnet-5-5", "gpt-5-mini", p, cap_usd=0.25)
    assert plan.estimate.total_usd > 0.0
    assert plan.within_cap is True
    assert fake_adapter.call_count == 0
