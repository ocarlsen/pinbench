import pytest

from pinbench import prices


def _write(tmp_path, text):
    path = tmp_path / "prices.yaml"
    path.write_text(text)
    return path


def test_load_and_row(tmp_path):
    path = _write(
        tmp_path,
        """\
version: 1
checked: 2026-10-26
models:
  model-a: {input: 1.0, output: 2.0, source: "https://x"}
  model-b: {input: 0.5, output: 1.0, source: "https://y"}
""",
    )
    p = prices.load(path)
    row = prices.price_row(p, "model-a")
    assert row.input == 1.0
    assert row.output == 2.0
    assert prices.price_row(p, "missing") is None


def test_price_ratio_uses_larger_axis(tmp_path):
    path = _write(
        tmp_path,
        """\
version: 1
checked: 2026-10-26
models:
  incumbent: {input: 2.0, output: 10.0, source: "https://x"}
  candidate: {input: 1.0, output: 8.0, source: "https://y"}
""",
    )
    p = prices.load(path)
    # input ratio 0.5, output ratio 0.8 -> larger is 0.8
    ratio = prices.price_ratio(p, "candidate", "incumbent")
    assert abs(ratio - 0.8) < 1e-9


def test_price_ratio_missing_row_is_none(tmp_path):
    path = _write(
        tmp_path,
        """\
version: 1
checked: 2026-10-26
models:
  incumbent: {input: 2.0, output: 10.0, source: "https://x"}
""",
    )
    p = prices.load(path)
    assert prices.price_ratio(p, "candidate", "incumbent") is None


def test_request_defaults_parsed_when_present(tmp_path):
    path = _write(
        tmp_path,
        """\
version: 1
checked: 2026-10-26
models:
  model-a:
    input: 1.0
    output: 2.0
    source: "https://x"
    request_defaults:
      thinking: {type: between_tools}
  model-b: {input: 0.5, output: 1.0, source: "https://y"}
""",
    )
    p = prices.load(path)
    assert prices.price_row(p, "model-a").request_defaults == {"thinking": {"type": "between_tools"}}
    # No row-level opinion -> empty dict, never None (callers merge it unconditionally).
    assert prices.price_row(p, "model-b").request_defaults == {}


def test_request_defaults_must_be_a_mapping(tmp_path):
    path = _write(
        tmp_path,
        """\
version: 1
checked: 2026-10-26
models:
  model-a:
    input: 1.0
    output: 2.0
    source: "https://x"
    request_defaults: "thinking: disabled"
""",
    )
    with pytest.raises(prices.PricesError, match="request_defaults must be a mapping"):
        prices.load(path)
