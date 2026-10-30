"""scripts/judge_control.py: a lenient judge is a red result (exit 1), a strict one green."""

import importlib.util
import json
from pathlib import Path

from pinbench import providers

ROOT = Path(__file__).resolve().parent.parent
_spec = importlib.util.spec_from_file_location("judge_control", ROOT / "scripts" / "judge_control.py")
judge_control = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(judge_control)

SUITE = ROOT / "examples" / "suites" / "summaries-facts.yaml"
CONTROLS = ROOT / "examples" / "controls" / "summaries-facts-controls.yaml"
PRICES = ROOT / "examples" / "prices.yaml"


class _Judge:
    def __init__(self, hits, extra):
        self.hits, self.extra = hits, extra

    def call(self, model_id, system, user_input, max_tokens, api_key, params=None, timeout=60):
        # Echo back the first `hits` rubric ids named in the prompt.
        ids = [line.split(":")[0].strip("- ").strip() for line in user_input.splitlines() if line.startswith("- ")]
        body = json.dumps({"hit": ids[: self.hits], "extra": self.extra, "notes": "n"})
        return providers.Reply(output=body, input_tokens=100, output_tokens=20, latency_ms=1, stop_reason="stop")


def _run(monkeypatch, tmp_path, fake):
    monkeypatch.setenv("OPENAI_API_KEY", "k")
    monkeypatch.setattr(providers, "route", lambda model_id: fake)
    out = tmp_path / "c.json"
    rc = judge_control.main([str(SUITE), str(CONTROLS), "--prices", str(PRICES), "--out", str(out)])
    return rc, json.loads(out.read_text())


def test_strict_judge_passes_every_control(monkeypatch, tmp_path):
    rc, rec = _run(monkeypatch, tmp_path, _Judge(hits=1, extra=1))
    assert rc == 0
    assert len(rec["results"]) == 10 and all(r["pass"] for r in rec["results"])


def test_lenient_judge_is_red(monkeypatch, tmp_path):
    rc, rec = _run(monkeypatch, tmp_path, _Judge(hits=3, extra=0))
    assert rc == 1
    assert not any(r["pass"] for r in rec["results"])


def test_controls_cover_every_suite_item():
    import yaml
    from pinbench import suite

    s = suite.load(SUITE)
    spec = yaml.safe_load(CONTROLS.read_text())
    assert [c["id"] for c in spec["controls"]] == [i.id for i in s.items]
