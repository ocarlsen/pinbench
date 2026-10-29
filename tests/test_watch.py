from pinbench import watch


def test_family_and_version_basic():
    assert watch.family_and_version("claude-sonnet-4-5") == (("claude", "sonnet"), ((4,), (5,)))


def test_family_and_version_strips_date_suffix_dashed():
    a = watch.family_and_version("gpt-4o-mini-2024-07-18")
    b = watch.family_and_version("gpt-4o-mini")
    assert a == b


def test_family_and_version_strips_date_suffix_compact():
    a = watch.family_and_version("claude-sonnet-4-5-20241022")
    b = watch.family_and_version("claude-sonnet-4-5")
    assert a == b


def test_dated_resnapshot_of_same_id_is_not_newer():
    candidate = watch.candidate_for_pin("claude-sonnet-4-5", ["claude-sonnet-4-5-20241022"])
    assert candidate is None


def test_codename_sibling_is_different_family():
    # gpt-4o-mini vs gpt-4o: "4o" is not purely numeric, so it's a family token -> different family tuples
    fam_mini, _ = watch.family_and_version("gpt-4o-mini")
    fam_plain, _ = watch.family_and_version("gpt-4o")
    assert fam_mini != fam_plain
    candidate = watch.candidate_for_pin("gpt-4o-mini", ["gpt-4o"])
    assert candidate is None


def test_hold_back_id_does_not_surface():
    ids = ["gpt-4o", "gpt-4.1"]
    # hold_back matches the only newer id in family -> no candidate surfaces
    candidate = watch.candidate_for_pin("gpt-4", ids, hold_back="gpt-4.1")
    assert candidate is None


def test_newest_of_several_newer_ids_is_named():
    ids = ["claude-sonnet-4-5", "claude-sonnet-4-6", "claude-sonnet-4-7"]
    candidate = watch.candidate_for_pin("claude-sonnet-4-5", ids)
    assert candidate == "claude-sonnet-4-7"


PINS_YAML = """\
version: 1
slots:
  summarizer:
    model: claude-sonnet-4-5
    provider: anthropic
    owner: owen
  classifier:
    model: gpt-4o-mini
    provider: openai
    owner: owen
"""


def test_first_run_seeds_and_says_so(tmp_path, fake_adapter):
    pins_path = tmp_path / "pins.yaml"
    pins_path.write_text(PINS_YAML)
    fake_adapter.models = ["claude-sonnet-4-5", "claude-opus-4-1"]

    lines, code = watch.run_watch(pins_path, tmp_path / "state")
    assert code == 0
    assert any("seeded 2 ids for anthropic" in l for l in lines)
    assert any("seeded 2 ids for openai" in l for l in lines)
    # No slot pins a Gemini model and GEMINI_API_KEY is unset: that provider is not checked.
    assert not any("gemini" in l for l in lines)

    snapshot = watch.load_snapshot(tmp_path / "state")
    assert snapshot["anthropic"] == ["claude-sonnet-4-5", "claude-opus-4-1"]


def test_second_run_diffs_newer_vanished_new(tmp_path, fake_adapter):
    pins_path = tmp_path / "pins.yaml"
    pins_path.write_text(PINS_YAML)
    state_dir = tmp_path / "state"

    fake_adapter.models = ["claude-sonnet-4-5", "gpt-4o-mini"]
    watch.run_watch(pins_path, state_dir)

    # claude-sonnet-4-5 vanished and gets replaced by a newer sibling; a brand new id shows up
    fake_adapter.models = ["claude-sonnet-4-6", "claude-haiku-4-5"]
    lines, code = watch.run_watch(pins_path, state_dir)

    assert code == 0
    assert any(l.startswith("NEWER IN SLOT summarizer: claude-sonnet-4-5 -> claude-sonnet-4-6") for l in lines)
    assert any(l.startswith("VANISHED summarizer: claude-sonnet-4-5") for l in lines)
    assert any(l.startswith("NEW IDS anthropic:") and "claude-haiku-4-5" in l for l in lines)


def test_nothing_changed_prints_nothing_for_that_provider(tmp_path, fake_adapter):
    pins_path = tmp_path / "pins.yaml"
    pins_path.write_text(PINS_YAML)
    state_dir = tmp_path / "state"

    fake_adapter.models = ["claude-sonnet-4-5"]
    watch.run_watch(pins_path, state_dir)
    lines, code = watch.run_watch(pins_path, state_dir)
    anthropic_lines = [l for l in lines if "anthropic" in l or "summarizer" in l]
    assert anthropic_lines == []


def test_unreachable_provider_reported_and_exit_1(tmp_path, fake_adapter, monkeypatch):
    pins_path = tmp_path / "pins.yaml"
    pins_path.write_text(PINS_YAML)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    fake_adapter.models = ["claude-sonnet-4-5"]

    lines, code = watch.run_watch(pins_path, tmp_path / "state")
    assert code == 1
    assert any("openai UNREACHABLE" in l for l in lines)
    # the unreachable provider's snapshot is not advanced; the reachable one's is
    snapshot = watch.load_snapshot(tmp_path / "state")
    assert "openai" not in snapshot
    assert snapshot["anthropic"] == ["claude-sonnet-4-5"]
