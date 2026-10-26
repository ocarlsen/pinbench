from pathlib import Path

import pytest

from pinbench import config


def _write(path, text):
    path.write_text(text)
    return path


def test_find_config_walks_up(tmp_path):
    (tmp_path / "pinbench.yaml").write_text("cap_usd: 1.0\n")
    nested = tmp_path / "a" / "b"
    nested.mkdir(parents=True)
    found = config.find_config(nested)
    assert found == tmp_path / "pinbench.yaml"


def test_find_config_none_when_absent(tmp_path):
    nested = tmp_path / "a" / "b"
    nested.mkdir(parents=True)
    assert config.find_config(nested) is None


def test_find_config_nearest_wins(tmp_path):
    (tmp_path / "pinbench.yaml").write_text("cap_usd: 1.0\n")
    nested = tmp_path / "a"
    nested.mkdir()
    (nested / "pinbench.yaml").write_text("cap_usd: 2.0\n")
    found = config.find_config(nested)
    assert found == nested / "pinbench.yaml"


def test_load_effective_with_explicit_path_refuses_if_missing(tmp_path):
    with pytest.raises(config.ConfigError, match="not found"):
        config.load_effective(explicit_path=tmp_path / "nope.yaml")


def test_load_effective_no_config_found_is_fine(tmp_path):
    nested = tmp_path / "a"
    nested.mkdir()
    cfg = config.load_effective(start=nested)
    assert cfg.path is None
    assert cfg.raw == {}


def test_load_unknown_top_level_key_refused(tmp_path):
    path = _write(tmp_path / "pinbench.yaml", "bogus_key: 1\n")
    with pytest.raises(config.ConfigError, match="bogus_key"):
        config.load(path)


def test_load_forbidden_incumbent_key_refused(tmp_path):
    path = _write(tmp_path / "pinbench.yaml", "incumbent: claude-sonnet-5-5\n")
    with pytest.raises(config.ConfigError, match="incumbent"):
        config.load(path)


def test_load_forbidden_candidate_key_refused(tmp_path):
    path = _write(tmp_path / "pinbench.yaml", "candidate: claude-haiku-5-5\n")
    with pytest.raises(config.ConfigError, match="candidate"):
        config.load(path)


def test_load_cap_usd_bad_type_refused(tmp_path):
    path = _write(tmp_path / "pinbench.yaml", "cap_usd: not-a-number\n")
    with pytest.raises(config.ConfigError, match="cap_usd"):
        config.load(path)


def test_load_cap_usd_bool_refused(tmp_path):
    # bool is an int subclass in Python; must be refused explicitly, not silently accepted.
    path = _write(tmp_path / "pinbench.yaml", "cap_usd: true\n")
    with pytest.raises(config.ConfigError, match="cap_usd"):
        config.load(path)


def test_load_cap_usd_must_be_positive(tmp_path):
    for text in ("cap_usd: 0\n", "cap_usd: -1\n"):
        path = _write(tmp_path / "pinbench.yaml", text)
        with pytest.raises(config.ConfigError, match="cap_usd=.*greater than zero"):
            config.load(path)


def test_load_malformed_yaml_refused(tmp_path):
    path = _write(tmp_path / "pinbench.yaml", "cap_usd: [unclosed\n")
    with pytest.raises(config.ConfigError, match="not valid YAML"):
        config.load(path)


def test_load_judge_max_output_tokens_must_be_positive(tmp_path):
    path = _write(tmp_path / "pinbench.yaml", "judge:\n  max_output_tokens: 0\n")
    with pytest.raises(config.ConfigError, match="max_output_tokens=0.*at least 1"):
        config.load(path)


def test_load_policy_negative_or_zero_min_items_refused(tmp_path):
    for text, key in (
        ("policy:\n  min_items: 0\n", "min_items=0"),
        ("policy:\n  max_truncated: -1\n", "max_truncated=-1"),
        ("policy:\n  cost_tolerance: -0.5\n", "cost_tolerance=-0.5"),
    ):
        path = _write(tmp_path / "pinbench.yaml", text)
        with pytest.raises(config.ConfigError, match=key):
            config.load(path)


def test_load_judge_unknown_key_refused(tmp_path):
    path = _write(tmp_path / "pinbench.yaml", "judge:\n  bogus: 1\n")
    with pytest.raises(config.ConfigError, match="bogus"):
        config.load(path)


def test_load_judge_max_output_tokens_bad_type_refused(tmp_path):
    path = _write(tmp_path / "pinbench.yaml", "judge:\n  max_output_tokens: \"a lot\"\n")
    with pytest.raises(config.ConfigError, match="max_output_tokens"):
        config.load(path)


def test_load_paths_unknown_key_refused(tmp_path):
    path = _write(tmp_path / "pinbench.yaml", "paths:\n  bogus: x\n")
    with pytest.raises(config.ConfigError, match="bogus"):
        config.load(path)


def test_load_paths_bad_type_refused(tmp_path):
    path = _write(tmp_path / "pinbench.yaml", "paths:\n  pins: 5\n")
    with pytest.raises(config.ConfigError, match="pins"):
        config.load(path)


def test_load_policy_unknown_key_refused(tmp_path):
    path = _write(tmp_path / "pinbench.yaml", "policy:\n  bogus: 1\n")
    with pytest.raises(config.ConfigError, match="bogus"):
        config.load(path)


def test_load_policy_bad_type_refused(tmp_path):
    path = _write(tmp_path / "pinbench.yaml", "policy:\n  min_items: \"ten\"\n")
    with pytest.raises(config.ConfigError, match="min_items"):
        config.load(path)


def test_load_policy_bool_key_requires_bool(tmp_path):
    path = _write(tmp_path / "pinbench.yaml", "policy:\n  require_votes_when_unlabeled: 1\n")
    with pytest.raises(config.ConfigError, match="require_votes_when_unlabeled"):
        config.load(path)


def test_load_computes_content_hash(tmp_path):
    path = _write(tmp_path / "pinbench.yaml", "cap_usd: 1.0\n")
    cfg = config.load(path)
    assert cfg.content_hash.startswith("sha256:")
    cfg2 = config.load(path)
    assert cfg.content_hash == cfg2.content_hash


def test_resolve_cap_precedence_cli_wins(tmp_path):
    path = _write(tmp_path / "pinbench.yaml", "cap_usd: 1.0\n")
    cfg = config.load(path)
    value, source = config.resolve_cap(5.0, 2.0, cfg)
    assert (value, source) == (5.0, "cli")


def test_resolve_cap_precedence_suite_over_config(tmp_path):
    path = _write(tmp_path / "pinbench.yaml", "cap_usd: 1.0\n")
    cfg = config.load(path)
    value, source = config.resolve_cap(None, 2.0, cfg)
    assert (value, source) == (2.0, "suite")


def test_resolve_cap_precedence_config_over_builtin(tmp_path):
    path = _write(tmp_path / "pinbench.yaml", "cap_usd: 1.0\n")
    cfg = config.load(path)
    value, source = config.resolve_cap(None, None, cfg)
    assert (value, source) == (1.0, "config")


def test_resolve_cap_precedence_builtin_default_is_none():
    cfg = config.Config(path=None)
    value, source = config.resolve_cap(None, None, cfg)
    assert (value, source) == (None, "default")


def test_resolve_judge_defaults_config_overrides_builtin(tmp_path):
    path = _write(tmp_path / "pinbench.yaml", "judge:\n  model: my-judge\n")
    cfg = config.load(path)
    merged, source = config.resolve_judge_defaults(cfg)
    assert merged["model"] == "my-judge"
    assert source["model"] == "config"
    assert merged["max_output_tokens"] == config.BUILTIN_DEFAULTS["judge"]["max_output_tokens"]
    assert source["max_output_tokens"] == "default"


def test_resolve_judge_defaults_builtin_only_when_no_config():
    cfg = config.Config(path=None)
    merged, source = config.resolve_judge_defaults(cfg)
    assert merged == config.BUILTIN_DEFAULTS["judge"]
    assert set(source.values()) == {"default"}


def test_resolve_policy_defaults_config_overrides_builtin(tmp_path):
    path = _write(tmp_path / "pinbench.yaml", "policy:\n  min_items: 3\n")
    cfg = config.load(path)
    merged, source = config.resolve_policy_defaults(cfg)
    assert merged["min_items"] == 3
    assert source["min_items"] == "config"
    assert merged["cost_tolerance"] == config.BUILTIN_DEFAULTS["policy"]["cost_tolerance"]
    assert source["cost_tolerance"] == "default"


def test_resolve_paths_cli_over_config_over_builtin(tmp_path):
    path = _write(tmp_path / "pinbench.yaml", "paths:\n  prices: my-prices.yaml\n")
    cfg = config.load(path)
    resolved = config.resolve_paths({"prices": "/explicit/prices.yaml"}, cfg)
    assert resolved["prices"] == ("/explicit/prices.yaml", "cli")
    # pins has no config entry and no CLI override: built-in default.
    assert resolved["pins"] == (config.BUILTIN_DEFAULTS["paths"]["pins"], "default")


def test_resolve_paths_config_value_relative_to_config_file(tmp_path):
    subdir = tmp_path / "project"
    subdir.mkdir()
    path = _write(subdir / "pinbench.yaml", "paths:\n  prices: my-prices.yaml\n")
    cfg = config.load(path)
    resolved = config.resolve_paths({}, cfg)
    value, source = resolved["prices"]
    assert source == "config"
    assert Path(value).resolve() == (subdir / "my-prices.yaml").resolve()
    # Expressed relative to the cwd, so the value never carries an absolute local path.
    assert not Path(value).is_absolute()
