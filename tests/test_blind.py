import json

from pinbench import blind


def _run(n=3):
    items = []
    for i in range(n):
        items.append(
            {
                "id": f"q{i:02d}",
                "input": f"Question {i}?",
                "expect": f"A{i}",
                "incumbent": {"output": f"inc-answer-{i}", "error": None},
                "candidate": {"output": f"cand-answer-{i}", "error": None},
            }
        )
    return {"run_name": "r1", "items": items}


def test_mapping_stable_across_two_calls(tmp_path):
    run = _run()
    mapping_path = tmp_path / "mapping.json"
    m1 = blind.load_or_build_mapping(run, mapping_path)
    raw1 = mapping_path.read_bytes()
    m2 = blind.load_or_build_mapping(run, mapping_path)
    raw2 = mapping_path.read_bytes()
    assert raw1 == raw2
    assert m1 == m2


def test_mapping_covers_every_item_with_a_candidate(tmp_path):
    run = _run(5)
    mapping = blind.build_mapping(run)
    assert set(mapping.keys()) == {item["id"] for item in run["items"]}
    for entry in mapping.values():
        assert sorted([entry["A"], entry["B"]]) == ["candidate", "incumbent"]


def test_vote_html_contains_no_model_ids_or_side_words(tmp_path):
    run = _run(3)
    mapping = blind.build_mapping(run)
    html = blind.build_vote_html(run, mapping, "r1")

    for banned in ("claude-sonnet-4-5", "gpt-4o-mini"):
        assert banned not in html

    # "incumbent"/"candidate" may appear ONLY in the footer sentence
    without_footer = html.replace(blind.FOOTER_SENTENCE, "")
    assert "incumbent" not in without_footer
    assert "candidate" not in without_footer
    assert blind.FOOTER_SENTENCE in html


def test_vote_html_has_both_outputs_and_controls():
    run = _run(1)
    mapping = blind.build_mapping(run)
    html = blind.build_vote_html(run, mapping, "r1")
    assert "Question 0?" in html
    assert "inc-answer-0" in html
    assert "cand-answer-0" in html
    assert 'data-vote="A"' in html
    assert 'data-vote="B"' in html
    assert 'data-vote="tie"' in html


def test_publish_writes_both_files(tmp_path):
    run = _run(2)
    run_dir = tmp_path / "runs" / "r1"
    mapping_path, html_path = blind.publish(run, run_dir, "r1")
    assert mapping_path.exists()
    assert html_path.exists()
    mapping = json.loads(mapping_path.read_text())
    assert set(mapping.keys()) == {"q00", "q01"}


def _run_with_missing_output():
    """Both arms attempted on every item, but every arm errored -- the exact shape of the
    live run that first surfaced this bug: HTTP errors on both sides, output null."""
    return {
        "run_name": "r1",
        "items": [
            {
                "id": "q00",
                "input": "Question 0?",
                "expect": "A0",
                "incumbent": {"output": None, "error": "HTTP 400: temperature is deprecated"},
                "candidate": {"output": None, "error": "HTTP 404: org must be verified"},
            }
        ],
    }


def test_missing_output_item_arms_finds_both_sides():
    run = _run_with_missing_output()
    missing = blind.missing_output_item_arms(run)
    assert set(missing) == {("q00", "incumbent"), ("q00", "candidate")}


def test_missing_output_item_arms_empty_when_complete():
    run = _run(2)
    assert blind.missing_output_item_arms(run) == []


def test_card_html_renders_error_text_not_blank_for_missing_output():
    run = _run_with_missing_output()
    mapping = blind.build_mapping(run)
    html = blind.build_vote_html(run, mapping, "r1")
    assert "HTTP 400: temperature is deprecated" in html
    assert "HTTP 404: org must be verified" in html
    assert 'class="side error"' in html


def test_card_html_handles_arm_never_attempted():
    """candidate is None entirely (the meter stopped before calling it), not just errored."""
    run = {
        "run_name": "r1",
        "items": [
            {
                "id": "q00",
                "input": "Question 0?",
                "expect": "A0",
                "incumbent": {"output": "inc-answer-0", "error": None},
                "candidate": None,
            }
        ],
    }
    mapping = blind.build_mapping(run)
    html = blind.build_vote_html(run, mapping, "r1")
    assert "not attempted" in html
    assert 'class="side error"' in html
