import pytest

from pinbench import registry


PINS_OK = """\
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
    hold_back: gpt-4o
    hold_back_reason: "lost 4-6"
    sunset: 2027-01-31
"""


def test_load_valid(tmp_path):
    path = tmp_path / "pins.yaml"
    path.write_text(PINS_OK)
    reg = registry.load(path)
    assert reg.slots["summarizer"].model == "claude-sonnet-4-5"
    assert reg.slots["classifier"].hold_back == "gpt-4o"
    assert reg.slots["classifier"].sunset == "2027-01-31"


def test_provider_prefix_mismatch(tmp_path):
    path = tmp_path / "pins.yaml"
    path.write_text(
        """\
version: 1
slots:
  s:
    model: claude-sonnet-4-5
    provider: openai
    owner: x
"""
    )
    with pytest.raises(registry.RegistryError):
        registry.load(path)


def test_unroutable_id(tmp_path):
    path = tmp_path / "pins.yaml"
    path.write_text(
        """\
version: 1
slots:
  s:
    model: mystery-model-9000
    provider: openai
    owner: x
"""
    )
    with pytest.raises(registry.RegistryError):
        registry.load(path)


def test_hold_back_without_reason(tmp_path):
    path = tmp_path / "pins.yaml"
    path.write_text(
        """\
version: 1
slots:
  s:
    model: gpt-4o-mini
    provider: openai
    owner: x
    hold_back: gpt-4o
"""
    )
    with pytest.raises(registry.RegistryError):
        registry.load(path)


def test_bad_sunset(tmp_path):
    path = tmp_path / "pins.yaml"
    path.write_text(
        """\
version: 1
slots:
  s:
    model: gpt-4o-mini
    provider: openai
    owner: x
    sunset: not-a-date
"""
    )
    with pytest.raises(registry.RegistryError):
        registry.load(path)


def test_set_model_edits_only_that_key(tmp_path):
    path = tmp_path / "pins.yaml"
    path.write_text(PINS_OK)

    registry.set_model(path, "classifier", "gpt-5-mini")

    reg = registry.load(path)
    assert reg.slots["classifier"].model == "gpt-5-mini"
    # untouched fields on the edited slot
    assert reg.slots["classifier"].hold_back == "gpt-4o"
    assert reg.slots["classifier"].hold_back_reason == "lost 4-6"
    assert reg.slots["classifier"].sunset == "2027-01-31"
    # untouched sibling slot
    assert reg.slots["summarizer"].model == "claude-sonnet-4-5"

    # comments preserved
    text = path.read_text()
    assert "# One line per slot" not in text  # fixture has no header comment; sanity check format
    assert "gpt-5-mini" in text


def test_set_model_unknown_slot(tmp_path):
    path = tmp_path / "pins.yaml"
    path.write_text(PINS_OK)
    with pytest.raises(registry.RegistryError):
        registry.set_model(path, "nope", "gpt-5-mini")


def test_set_model_preserves_comments(tmp_path):
    path = tmp_path / "pins.yaml"
    path.write_text(
        """\
# One line per slot. A slot is a named role in your system, not a model.
version: 1
slots:
  summarizer:
    model: claude-sonnet-4-5          # exact vendor id; never an alias
    provider: anthropic
    owner: owen
"""
    )
    registry.set_model(path, "summarizer", "claude-sonnet-5")
    text = path.read_text()
    assert "# One line per slot" in text
    assert "# exact vendor id; never an alias" in text
    assert "claude-sonnet-5" in text


def test_set_model_cross_provider_updates_provider(tmp_path):
    path = tmp_path / "pins.yaml"
    path.write_text(PINS_OK.replace("    provider: anthropic\n", "    provider: anthropic   # validated\n"))

    registry.set_model(path, "summarizer", "gpt-5-mini")

    reg = registry.load(path)
    assert reg.slots["summarizer"].model == "gpt-5-mini"
    assert reg.slots["summarizer"].provider == "openai"
    assert reg.slots["summarizer"].owner == "owen"
    assert reg.slots["classifier"].provider == "openai"
    assert "provider: openai   # validated" in path.read_text()


def test_set_model_same_provider_leaves_provider_line(tmp_path):
    path = tmp_path / "pins.yaml"
    path.write_text(PINS_OK)
    registry.set_model(path, "classifier", "gpt-5-mini")
    assert path.read_text() == PINS_OK.replace("model: gpt-4o-mini", "model: gpt-5-mini")


def test_set_model_unknown_id_prefix_refused(tmp_path):
    path = tmp_path / "pins.yaml"
    path.write_text(PINS_OK)
    with pytest.raises(registry.RegistryError):
        registry.set_model(path, "summarizer", "mystery-model-1")
    assert path.read_text() == PINS_OK
