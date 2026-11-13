"""Gemini adapter: requires_continuation hops (text join, usage sums, bounds, mid-hop failure).

Bodies are hand-written to the Interactions API shape; no live multi-hop response is checked in.
"""

import json
import urllib.error

import pytest

from pinbench import cost, scoring
from pinbench.providers import gemini


class _Resp:
    def __init__(self, payload):
        self._body = json.dumps(payload).encode("utf-8")

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def _hop(text, status="requires_continuation", token="tok", inp=100, out=0, thoughts=0):
    steps = [{"type": "thought", "content": [{"type": "text", "text": "hidden"}]}]
    if text:
        steps.append({"type": "model_output", "content": [{"type": "text", "text": text}]})
    d = {"id": "i", "status": status, "steps": steps,
         "usage": {"total_input_tokens": inp, "total_output_tokens": out, "total_thought_tokens": thoughts}}
    if token:
        d["continuation_token"] = token
    return d


@pytest.fixture
def hops(monkeypatch):
    monkeypatch.delenv("PINBENCH_NO_NETWORK", raising=False)
    state = {"queue": [], "bodies": [], "timeouts": []}

    def fake(req, timeout=None):
        state["bodies"].append(json.loads(req.data.decode("utf-8")))
        state["timeouts"].append(timeout)
        item = state["queue"].pop(0) if len(state["queue"]) > 1 else state["queue"][0]
        if isinstance(item, Exception):
            raise item
        return _Resp(item)

    monkeypatch.setattr(gemini.urllib.request, "urlopen", fake)
    return state


def _call(max_tokens=64000, **kw):
    return gemini.call("gemini-4-carbon", "SYS", "INPUT", max_tokens, "k", params={"generation_config": {"thinking_level": "medium"}}, **kw)


def test_two_hops_join_text_and_sum_usage(hops):
    hops["queue"] = [
        _hop(None, token="t1", inp=100, out=0, thoughts=32765),
        _hop("Answer.", status="completed", token=None, inp=150, out=20, thoughts=5),
    ]
    r = _call(timeout=7)
    assert r.error is None and r.output == "Answer."
    assert r.stop_reason == "completed"
    assert r.input_tokens == 250
    assert r.output_tokens == 32765 + 20 + 5
    first, second = hops["bodies"]
    assert "continuation_token" not in first
    assert second["continuation_token"] == "t1"
    for k in ("model", "system_instruction", "input", "generation_config"):
        assert second[k] == first[k]
    assert second["store"] is False and "previous_interaction_id" not in second
    assert hops["timeouts"] == [7, 7]


def test_three_hops(hops):
    hops["queue"] = [
        _hop("a", token="t1", inp=10, out=1, thoughts=2),
        _hop("b", token="t2", inp=20, out=3, thoughts=4),
        _hop("c", status="completed", token=None, inp=30, out=5, thoughts=6),
    ]
    r = _call()
    assert r.output == "abc"
    assert (r.input_tokens, r.output_tokens) == (60, 21)
    assert [b.get("continuation_token") for b in hops["bodies"]] == [None, "t1", "t2"]


def test_hop_cap(hops):
    # ceil(40000/32768)+1 = 3 hops, then stop; tiny usage so the token bound is not what fires.
    hops["queue"] = [_hop("x", token="t", inp=1, out=1, thoughts=1)]
    r = _call(max_tokens=40000)
    assert len(hops["bodies"]) == 3
    assert r.output == "xxx" and r.stop_reason == "incomplete"
    assert scoring.is_truncated({"stop_reason": r.stop_reason})


def test_summed_output_bound(hops):
    hops["queue"] = [_hop("x", token="t", inp=1, out=0, thoughts=40000)]
    r = _call(max_tokens=64000)
    assert len(hops["bodies"]) == 2 and r.stop_reason == "incomplete"
    hops["bodies"].clear()
    r = _call(max_tokens=30000)
    assert len(hops["bodies"]) == 1 and r.stop_reason == "incomplete"
    assert r.output_tokens == 40000


def test_mid_hop_failure_keeps_usage(hops):
    hops["queue"] = [_hop("a", token="t1", inp=100, out=10, thoughts=900), urllib.error.URLError("boom")]
    r = _call()
    assert r.error and r.output is None
    assert r.input_tokens == 100 and r.output_tokens == 910


def test_mid_hop_http_error_keeps_usage(hops):
    err = urllib.error.HTTPError("u", 503, "x", {}, None)
    err.read = lambda: b"unavailable"
    hops["queue"] = [_hop("a", token="t1", inp=100, out=10, thoughts=900), err]
    r = _call()
    assert r.status_code == 503 and r.input_tokens == 100 and r.output_tokens == 910


def test_first_hop_failure_has_no_tokens(hops):
    hops["queue"] = [urllib.error.URLError("boom")]
    r = _call()
    assert r.error and r.input_tokens is None and r.output_tokens is None


def test_missing_continuation_token_is_an_error(hops):
    hops["queue"] = [_hop("a", token=None, inp=5, out=1, thoughts=1)]
    r = _call()
    assert r.error and "continuation_token" in r.error and r.output_tokens == 2


def test_single_hop_body_and_reply_unchanged(hops):
    hops["queue"] = [_hop("Hi.", status="completed", token=None, inp=120, out=40, thoughts=25)]
    r = _call()
    assert len(hops["bodies"]) == 1
    assert hops["bodies"][0] == {
        "model": "gemini-4-carbon", "system_instruction": "SYS", "input": "INPUT", "store": False,
        "generation_config": {"max_output_tokens": 64000, "thinking_level": "medium"},
    }
    assert (r.output, r.input_tokens, r.output_tokens, r.stop_reason, r.error) == ("Hi.", 120, 65, "completed", None)


def test_no_network(monkeypatch):
    monkeypatch.setenv("PINBENCH_NO_NETWORK", "1")
    with pytest.raises(gemini.NoNetwork):
        _call()
