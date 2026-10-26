"""Hermetic regression test for the second LIVE run (.pinbench/runs/summaries-haiku/run.json):
19 of 20 outputs came back with output_tokens == max_output_tokens == 60, truncated
mid-sentence, with no flag raised anywhere in `run`, `vote`, or `report`; one item
(incumbent, s04) came back an empty string at output_tokens=60 and was still counted as
succeeded. This test reproduces the all-truncated shape against a fake provider (no
network, no API keys) and asserts the fixes: `run` warns per arm, `report` is INCOMPLETE on
the truncation check, and `vote` refuses to show a truncated output unless --allow-truncated.
The empty-output half of that run is covered at the adapter level in test_providers.py
(test_anthropic_empty_text_with_thinking_block_is_a_failure), since it is a parsing defect,
not a runner/report one.
"""

from __future__ import annotations

from pinbench import cli, providers


def _write_suite(tmp_path, n=10, max_truncated=None):
    items = "\n".join(f'  - id: s{i:02d}\n    input: "Summarize item {i} in one sentence."' for i in range(n))
    policy_extra = f"\n  max_truncated: {max_truncated}" if max_truncated is not None else ""
    path = tmp_path / "summaries.yaml"
    path.write_text(
        f"""\
version: 1
name: summaries
task: "Summarize in 2-3 sentences."
max_output_tokens: 60
scoring:
  labels: none
  votes: required
policy:
  min_items: 10{policy_extra}
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
  claude-haiku-5-5: {input: 0.1, output: 0.5, source: "https://y"}
"""
    )
    return path


def _install_all_truncated_call(fake_adapter):
    """Both arms succeed on every item, but every output hit max_output_tokens -- the exact
    shape of the second live run's finding A (19/20 truncated)."""

    def fake_call(model_id, system, user_input, max_tokens, api_key, params=None, timeout=60):
        fake_adapter.call_count += 1
        fake_adapter.calls.append((model_id, user_input))
        if not api_key:
            raise ValueError("missing api key")
        return providers.Reply(
            output=f"a cut-off summary of: {user_input}"[:max_tokens],
            input_tokens=300,
            output_tokens=max_tokens,
            latency_ms=900,
            error=None,
            stop_reason="max_tokens",
        )

    fake_adapter.call = fake_call


def test_all_truncated_run_warns_report_incomplete_vote_refuses(tmp_path, fake_adapter, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    suite_path = _write_suite(tmp_path, n=10)
    prices_path = _write_prices(tmp_path)
    state_dir = tmp_path / ".pinbench"
    _install_all_truncated_call(fake_adapter)

    rc = cli.main(
        [
            "run", str(suite_path),
            "--incumbent", "claude-sonnet-5-5",
            "--candidate", "claude-haiku-5-5",
            "--cap", "0.25",
            "--prices", str(prices_path),
            "--state", str(state_dir),
            "--name", "summaries-haiku-repro",
        ]
    )
    out = capsys.readouterr()

    # `run` still exits 0 -- a truncated-but-present output is a successful call -- but
    # prints a per-arm truncated count and a loud warning naming the cause.
    assert rc == 0, out.out + out.err
    assert "incumbent: 10/10 succeeded, 10 truncated" in out.out
    assert "candidate: 10/10 succeeded, 10 truncated" in out.out
    assert "incumbent had 10/10 truncated output(s)" in out.err
    assert "candidate had 10/10 truncated output(s)" in out.err

    from pinbench import runner as runner_mod

    record = runner_mod.load_run(state_dir, "summaries-haiku-repro")
    assert record["items_completed"] == 10  # truncated-but-present still "completed"
    for item in record["items"]:
        assert item["incumbent"]["stop_reason"] == "max_tokens"
        assert item["candidate"]["stop_reason"] == "max_tokens"

    # `report`: the truncation check fails both arms and the verdict is INCOMPLETE, same
    # bucket as an arm error -- never a silent RECOMMEND/HOLD on truncated answers.
    capsys.readouterr()
    rc = cli.main(["report", "summaries-haiku-repro", "--state", str(state_dir)])
    out = capsys.readouterr()
    assert rc == 0
    assert "Verdict: INCOMPLETE" in out.out
    assert "[FAIL] truncation: incumbent 10 truncated, candidate 10 truncated" in out.out

    # `vote` refuses by default (truncated outputs are not a real comparison) ...
    capsys.readouterr()
    rc = cli.main(["vote", "summaries-haiku-repro", "--state", str(state_dir)])
    out = capsys.readouterr()
    assert rc != 0
    assert "refused" in out.err
    assert "truncated" in out.err
    assert not (state_dir / "runs" / "summaries-haiku-repro" / "vote.html").exists()

    # ... and renders every card marked truncated, never silently, with --allow-truncated.
    rc = cli.main(["vote", "summaries-haiku-repro", "--state", str(state_dir), "--allow-truncated"])
    assert rc == 0
    vote_html = (state_dir / "runs" / "summaries-haiku-repro" / "vote.html").read_text()
    assert 'class="truncated-badge"' in vote_html
    assert vote_html.count('class="truncated-badge"') == 20  # both arms, all 10 items


def test_policy_max_truncated_knob_tolerates_some_truncation(tmp_path, fake_adapter, monkeypatch, capsys):
    """A suite that explicitly tolerates truncated items (policy.max_truncated) is not
    forced into INCOMPLETE by this check -- the knob is the suite author's call, not a
    hard-coded zero."""
    monkeypatch.chdir(tmp_path)
    suite_path = _write_suite(tmp_path, n=10, max_truncated=10)
    prices_path = _write_prices(tmp_path)
    state_dir = tmp_path / ".pinbench"
    _install_all_truncated_call(fake_adapter)

    cli.main(
        [
            "run", str(suite_path),
            "--incumbent", "claude-sonnet-5-5",
            "--candidate", "claude-haiku-5-5",
            "--cap", "0.25",
            "--prices", str(prices_path),
            "--state", str(state_dir),
            "--name", "summaries-tolerant",
        ]
    )
    capsys.readouterr()
    # votes required by the suite but not supplied -> INCOMPLETE on votes_present, not on
    # truncation -- proves the truncation check itself passed under the raised knob.
    rc = cli.main(["report", "summaries-tolerant", "--state", str(state_dir)])
    out = capsys.readouterr()
    assert rc == 0
    assert "[PASS] truncation:" in out.out
    assert "Verdict: INCOMPLETE" in out.out
    assert "votes_present" in out.out
