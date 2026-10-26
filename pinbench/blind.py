"""Build the blind A/B mapping and the self-contained vote.html page.

Refuses to do: put a model id or the words incumbent/candidate inside a vote card or the
page's instructions block; refuses to re-shuffle an existing mapping.json (idempotent on
re-publish); refuses to render a blank card for an item missing output on either side (see
`missing_output_item_arms`), or to render a truncated output as if it were a complete
answer (see `truncated_item_arms`) -- the CLI gates both refusals, this module renders the
marked-up content once each is waived. The page opens with a short, blind-safe
instructions block (how to vote, that sides are blind, how to export and report) above a
sticky actions bar so Export is never buried below every card.
"""

from __future__ import annotations

import json
import secrets
from pathlib import Path

from . import scoring as scoring_mod

FOOTER_SENTENCE = "sides are shuffled per item; the key is in mapping.json, not on this page."

INSTRUCTIONS_LINE_1 = "For each item, read the input, then pick the better output: A, B, or Tie (keys a / b / t; j / k to move)."
INSTRUCTIONS_LINE_2 = "You are not told which system produced which side."
INSTRUCTIONS_LINE_3_TEMPLATE = "When every item is voted, click Export to save votes.json, then run: pinbench report {run_name} --votes <path-to-votes.json>"


def missing_output_item_arms(run: dict) -> list:
    """[(item_id, arm_name), ...] for every item where that arm has no output: either the
    arm was never attempted (None, e.g. the meter stopped before it) or it was attempted
    and errored (output is None). Empty when every item has both outputs.
    """
    missing = []
    for item in run["items"]:
        for arm_name in ("incumbent", "candidate"):
            arm = item.get(arm_name)
            if arm is None or arm.get("output") is None:
                missing.append((item["id"], arm_name))
    return missing


def truncated_item_arms(run: dict) -> list:
    """[(item_id, arm_name), ...] for every item/arm whose stop/finish reason says the
    output was cut off by the output ceiling (see scoring.is_truncated). Empty when nothing
    was truncated.
    """
    truncated = []
    for item in run["items"]:
        for arm_name in ("incumbent", "candidate"):
            if scoring_mod.is_truncated(item.get(arm_name)):
                truncated.append((item["id"], arm_name))
    return truncated


def build_mapping(run: dict) -> dict:
    """Covers every item, including one missing output on a side -- the caller decides
    whether that is a refusal (default) or renders an error card (--allow-missing).
    """
    rng = secrets.SystemRandom()
    mapping = {}
    for item in run["items"]:
        sides = ["incumbent", "candidate"]
        rng.shuffle(sides)
        mapping[item["id"]] = {"A": sides[0], "B": sides[1]}
    return mapping


def load_or_build_mapping(run: dict, mapping_path) -> dict:
    mapping_path = Path(mapping_path)
    if mapping_path.exists():
        return json.loads(mapping_path.read_text())
    mapping = build_mapping(run)
    mapping_path.write_text(json.dumps(mapping, indent=2))
    return mapping


def _side_body_html(item, arm_name) -> tuple:
    """(html, is_error) for one side. A missing or errored arm renders its error text,
    marked as an error, never a blank card (the CLI already refused unless the caller
    passed --allow-missing). A truncated-but-present output renders with a visible
    "truncated" badge above it, never silently as if it were complete (the CLI already
    refused unless the caller passed --allow-truncated)."""
    arm = item.get(arm_name)
    if arm is None:
        return '<p class="error-text">[no output: not attempted]</p>', True
    output = arm.get("output")
    if output is None:
        text = arm.get("error") or "[no output]"
        return f'<p class="error-text">{_esc(text)}</p>', True
    body = f"<p>{_esc(output)}</p>"
    if scoring_mod.is_truncated(arm):
        body = '<p class="truncated-badge">truncated</p>' + body
    return body, False


def _card_html(item, mapping_entry) -> str:
    a_html, a_error = _side_body_html(item, mapping_entry["A"])
    b_html, b_error = _side_body_html(item, mapping_entry["B"])
    a_class = "side error" if a_error else "side"
    b_class = "side error" if b_error else "side"
    return f"""
    <div class="card" data-id="{item['id']}" tabindex="0">
      <div class="input"><strong>Input</strong><p>{_esc(item['input'])}</p></div>
      <div class="outputs">
        <div class="{a_class}" data-side="A"><strong>A</strong>{a_html}</div>
        <div class="{b_class}" data-side="B"><strong>B</strong>{b_html}</div>
      </div>
      <div class="controls">
        <button class="vote-btn" data-vote="A">A</button>
        <button class="vote-btn" data-vote="B">B</button>
        <button class="vote-btn" data-vote="tie">Tie</button>
        <input class="note" type="text" placeholder="note (optional)">
      </div>
    </div>"""


def _esc(text: str) -> str:
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


_VOTE_PAGE_CSS = """\
  :root { color-scheme: light dark; }
  body { font-family: system-ui, sans-serif; max-width: 760px; margin: 0 auto;
         padding: 16px; background: #fafafa; color: #1a1a1a; }
  @media (prefers-color-scheme: dark) {
    body { background: #15161a; color: #e6e6e6; }
    .card { background: #1f2127; border-color: #333; }
    .side { background: #26282f; }
    .side.error { border-color: #e66; }
    .error-text { color: #f88; }
    .truncated-badge { color: #e0a654; }
    #actions { background: #15161a; }
  }
  h1 { font-size: 1.1rem; }
  #instructions { font-size: 0.85rem; opacity: 0.85; margin: 4px 0 12px; }
  #instructions p { margin: 2px 0; }
  #progress { font-size: 0.9rem; opacity: 0.8; margin-left: auto; }
  .card { border: 1px solid #ddd; border-radius: 8px; padding: 12px; margin-bottom: 14px;
          background: #fff; }
  .card:focus { outline: 2px solid #4a90d9; }
  .card.voted { opacity: 0.6; }
  .outputs { display: flex; gap: 10px; flex-wrap: wrap; }
  .side { flex: 1 1 260px; background: #f3f3f3; border-radius: 6px; padding: 8px; }
  .side.error { border: 1px dashed #c33; }
  .error-text { color: #a33; font-style: italic; }
  .truncated-badge { color: #a60; font-size: 0.75rem; font-weight: 600; margin: 0 0 4px;
                      text-transform: uppercase; letter-spacing: 0.03em; }
  .controls { display: flex; gap: 8px; margin-top: 8px; align-items: center; flex-wrap: wrap; }
  .vote-btn { padding: 6px 14px; border-radius: 6px; border: 1px solid #999; cursor: pointer;
              background: transparent; color: inherit; }
  .vote-btn.selected { background: #4a90d9; color: #fff; border-color: #4a90d9; }
  .note { flex: 1 1 160px; padding: 4px 6px; }
  #actions { margin: 0 0 16px; padding: 10px 0; display: flex; gap: 8px; align-items: center;
             position: sticky; top: 0; background: #fafafa; z-index: 1; }
  button.action { padding: 8px 14px; border-radius: 6px; border: 1px solid #999;
                  background: transparent; color: inherit; cursor: pointer; }
  footer { margin-top: 24px; font-size: 0.8rem; opacity: 0.7; }
"""

_VOTE_PAGE_JS_TEMPLATE = """\
(function() {
  var RUN_KEY = "pinbench-votes-" + %(run_key_json)s;
  var cards = Array.prototype.slice.call(document.querySelectorAll('.card'));
  var votes = {};
  try { votes = JSON.parse(localStorage.getItem(RUN_KEY) || '{}'); } catch (e) { votes = {}; }

  function save() {
    try { localStorage.setItem(RUN_KEY, JSON.stringify(votes)); } catch (e) {}
  }

  function renderCard(card) {
    var id = card.getAttribute('data-id');
    var rec = votes[id];
    card.classList.toggle('voted', !!rec);
    card.querySelectorAll('.vote-btn').forEach(function(btn) {
      btn.classList.toggle('selected', rec && rec.vote === btn.getAttribute('data-vote'));
    });
    var noteInput = card.querySelector('.note');
    if (rec && rec.note) noteInput.value = rec.note;
  }

  function updateProgress() {
    var n = Object.keys(votes).length;
    document.getElementById('progress').textContent = n + ' of ' + cards.length + ' voted';
  }

  function castVote(card, vote) {
    var id = card.getAttribute('data-id');
    var noteInput = card.querySelector('.note');
    votes[id] = { vote: vote, note: noteInput ? noteInput.value : '' };
    save();
    renderCard(card);
    updateProgress();
  }

  cards.forEach(function(card) {
    renderCard(card);
    card.querySelectorAll('.vote-btn').forEach(function(btn) {
      btn.addEventListener('click', function() { castVote(card, btn.getAttribute('data-vote')); });
    });
    var noteInput = card.querySelector('.note');
    if (noteInput) {
      noteInput.addEventListener('change', function() {
        var id = card.getAttribute('data-id');
        if (votes[id]) { votes[id].note = noteInput.value; save(); }
      });
    }
  });
  updateProgress();

  var focusIndex = 0;
  function focusCard(i) {
    if (i < 0 || i >= cards.length) return;
    focusIndex = i;
    cards[i].focus();
  }
  if (cards.length) cards[0].setAttribute('tabindex', '0');

  cards.forEach(function(card, i) {
    card.addEventListener('focus', function() { focusIndex = i; });
  });

  document.addEventListener('keydown', function(e) {
    var active = document.activeElement;
    var tag = active && active.tagName;
    if (tag === 'INPUT' || tag === 'TEXTAREA') return;  // typing a note, not voting
    if (e.metaKey || e.ctrlKey || e.altKey) return;
    var idx = cards.indexOf(active);
    if (idx === -1) idx = focusIndex;
    if (e.key === 'j') { focusCard(idx + 1); e.preventDefault(); }
    else if (e.key === 'k') { focusCard(idx - 1); e.preventDefault(); }
    else if (e.key === 'a') { castVote(cards[idx], 'A'); e.preventDefault(); }
    else if (e.key === 'b') { castVote(cards[idx], 'B'); e.preventDefault(); }
    else if (e.key === 't') { castVote(cards[idx], 'tie'); e.preventDefault(); }
  });

  function exportJson() { return JSON.stringify(votes, null, 2); }

  document.getElementById('export').addEventListener('click', function() {
    var blob = new Blob([exportJson()], { type: 'application/json' });
    var url = URL.createObjectURL(blob);
    var a = document.createElement('a');
    a.href = url;
    a.download = 'votes.json';
    document.body.appendChild(a);
    a.click();
    a.remove();
    URL.revokeObjectURL(url);
  });

  document.getElementById('copy').addEventListener('click', function() {
    var text = exportJson();
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text);
    }
  });
})();
"""


def _vote_page_js(run_name: str) -> str:
    return _VOTE_PAGE_JS_TEMPLATE % {"run_key_json": json.dumps(run_name)}


def _cards_section(run: dict, mapping: dict) -> str:
    cards = [
        _card_html(item, mapping[item["id"]])
        for item in run["items"]
        if item["id"] in mapping
    ]
    return "\n".join(cards)


def build_vote_html(run: dict, mapping: dict, run_name: str) -> str:
    cards_html = _cards_section(run, mapping)
    n = len(mapping)
    title = _esc(run_name)
    report_cmd = _esc(INSTRUCTIONS_LINE_3_TEMPLATE.format(run_name=run_name))

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>pinbench vote: {title}</title>
<style>
{_VOTE_PAGE_CSS}</style>
</head>
<body>
<h1>pinbench vote: {title}</h1>
<div id="instructions">
  <p>{_esc(INSTRUCTIONS_LINE_1)}</p>
  <p>{_esc(INSTRUCTIONS_LINE_2)}</p>
  <p>{report_cmd}</p>
</div>
<div id="actions">
  <button class="action" id="export">Export votes.json</button>
  <button class="action" id="copy">Copy</button>
  <span id="progress">0 of {n} voted</span>
</div>
<div id="cards">
{cards_html}
</div>
<footer>{FOOTER_SENTENCE}</footer>
<script>
{_vote_page_js(run_name)}</script>
</body>
</html>
"""


def publish(run: dict, run_dir, run_name: str) -> tuple:
    """Write mapping.json (if absent) and vote.html under run_dir. Returns (mapping_path, html_path)."""
    run_dir = Path(run_dir)
    run_dir.mkdir(parents=True, exist_ok=True)
    mapping_path = run_dir / "mapping.json"
    html_path = run_dir / "vote.html"
    mapping = load_or_build_mapping(run, mapping_path)
    html_path.write_text(build_vote_html(run, mapping, run_name))
    return mapping_path, html_path
