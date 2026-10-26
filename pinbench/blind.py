"""Build the blind A/B mapping and the self-contained vote.html page.

Refuses to do: put a model id or the words incumbent/candidate inside a vote card; refuses
to re-shuffle an existing mapping.json (idempotent on re-publish).
"""

from __future__ import annotations

import json
import secrets
from pathlib import Path

FOOTER_SENTENCE = "sides are shuffled per item; the key is in mapping.json, not on this page."


def build_mapping(run: dict) -> dict:
    rng = secrets.SystemRandom()
    mapping = {}
    for item in run["items"]:
        if item.get("candidate") is None:
            continue
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


def _card_html(item, mapping_entry) -> str:
    a_output = item[mapping_entry["A"]]["output"] or ""
    b_output = item[mapping_entry["B"]]["output"] or ""
    return f"""
    <div class="card" data-id="{item['id']}" tabindex="0">
      <div class="input"><strong>Input</strong><p>{_esc(item['input'])}</p></div>
      <div class="outputs">
        <div class="side" data-side="A"><strong>A</strong><p>{_esc(a_output)}</p></div>
        <div class="side" data-side="B"><strong>B</strong><p>{_esc(b_output)}</p></div>
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


def build_vote_html(run: dict, mapping: dict, run_name: str) -> str:
    cards = []
    for item in run["items"]:
        if item["id"] not in mapping:
            continue
        cards.append(_card_html(item, mapping[item["id"]]))
    cards_html = "\n".join(cards)
    n = len(mapping)

    return f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>pinbench vote: {_esc(run_name)}</title>
<style>
  :root {{ color-scheme: light dark; }}
  body {{ font-family: -apple-system, system-ui, sans-serif; max-width: 760px; margin: 0 auto;
         padding: 16px; background: #fafafa; color: #1a1a1a; }}
  @media (prefers-color-scheme: dark) {{
    body {{ background: #15161a; color: #e6e6e6; }}
    .card {{ background: #1f2127; border-color: #333; }}
    .side {{ background: #26282f; }}
  }}
  h1 {{ font-size: 1.1rem; }}
  #progress {{ margin: 8px 0 16px; font-size: 0.9rem; opacity: 0.8; }}
  .card {{ border: 1px solid #ddd; border-radius: 8px; padding: 12px; margin-bottom: 14px;
          background: #fff; }}
  .card:focus {{ outline: 2px solid #4a90d9; }}
  .card.voted {{ opacity: 0.6; }}
  .outputs {{ display: flex; gap: 10px; flex-wrap: wrap; }}
  .side {{ flex: 1 1 260px; background: #f3f3f3; border-radius: 6px; padding: 8px; }}
  .controls {{ display: flex; gap: 8px; margin-top: 8px; align-items: center; flex-wrap: wrap; }}
  .vote-btn {{ padding: 6px 14px; border-radius: 6px; border: 1px solid #999; cursor: pointer;
              background: transparent; color: inherit; }}
  .vote-btn.selected {{ background: #4a90d9; color: #fff; border-color: #4a90d9; }}
  .note {{ flex: 1 1 160px; padding: 4px 6px; }}
  #actions {{ margin: 16px 0; display: flex; gap: 8px; }}
  button.action {{ padding: 8px 14px; border-radius: 6px; border: 1px solid #999;
                  background: transparent; color: inherit; cursor: pointer; }}
  footer {{ margin-top: 24px; font-size: 0.8rem; opacity: 0.7; }}
</style>
</head>
<body>
<h1>pinbench vote: {_esc(run_name)}</h1>
<div id="progress">0 of {n} voted</div>
<div id="cards">
{cards_html}
</div>
<div id="actions">
  <button class="action" id="export">Export votes.json</button>
  <button class="action" id="copy">Copy</button>
</div>
<footer>{FOOTER_SENTENCE}</footer>
<script>
(function() {{
  var RUN_KEY = "pinbench-votes-" + {json.dumps(run_name)};
  var cards = Array.prototype.slice.call(document.querySelectorAll('.card'));
  var votes = {{}};
  try {{ votes = JSON.parse(localStorage.getItem(RUN_KEY) || '{{}}'); }} catch (e) {{ votes = {{}}; }}

  function save() {{
    try {{ localStorage.setItem(RUN_KEY, JSON.stringify(votes)); }} catch (e) {{}}
  }}

  function renderCard(card) {{
    var id = card.getAttribute('data-id');
    var rec = votes[id];
    card.classList.toggle('voted', !!rec);
    card.querySelectorAll('.vote-btn').forEach(function(btn) {{
      btn.classList.toggle('selected', rec && rec.vote === btn.getAttribute('data-vote'));
    }});
    var noteInput = card.querySelector('.note');
    if (rec && rec.note) noteInput.value = rec.note;
  }}

  function updateProgress() {{
    var n = Object.keys(votes).length;
    document.getElementById('progress').textContent = n + ' of ' + cards.length + ' voted';
  }}

  function castVote(card, vote) {{
    var id = card.getAttribute('data-id');
    var noteInput = card.querySelector('.note');
    votes[id] = {{ vote: vote, note: noteInput ? noteInput.value : '' }};
    save();
    renderCard(card);
    updateProgress();
  }}

  cards.forEach(function(card) {{
    renderCard(card);
    card.querySelectorAll('.vote-btn').forEach(function(btn) {{
      btn.addEventListener('click', function() {{ castVote(card, btn.getAttribute('data-vote')); }});
    }});
    var noteInput = card.querySelector('.note');
    if (noteInput) {{
      noteInput.addEventListener('change', function() {{
        var id = card.getAttribute('data-id');
        if (votes[id]) {{ votes[id].note = noteInput.value; save(); }}
      }});
    }}
  }});
  updateProgress();

  var focusIndex = 0;
  function focusCard(i) {{
    if (i < 0 || i >= cards.length) return;
    focusIndex = i;
    cards[i].focus();
  }}
  if (cards.length) cards[0].setAttribute('tabindex', '0');

  document.addEventListener('keydown', function(e) {{
    var active = document.activeElement;
    var idx = cards.indexOf(active);
    if (idx === -1) idx = focusIndex;
    if (e.key === 'j') {{ focusCard(idx + 1); e.preventDefault(); }}
    else if (e.key === 'k') {{ focusCard(idx - 1); e.preventDefault(); }}
    else if (e.key === 'a') {{ castVote(cards[idx], 'A'); e.preventDefault(); }}
    else if (e.key === 'b') {{ castVote(cards[idx], 'B'); e.preventDefault(); }}
    else if (e.key === 't') {{ castVote(cards[idx], 'tie'); e.preventDefault(); }}
  }});

  function exportJson() {{ return JSON.stringify(votes, null, 2); }}

  document.getElementById('export').addEventListener('click', function() {{
    var blob = new Blob([exportJson()], {{ type: 'application/json' }});
    var url = URL.createObjectURL(blob);
    var a = document.createElement('a');
    a.href = url;
    a.download = 'votes.json';
    document.body.appendChild(a);
    a.click();
    a.remove();
    URL.revokeObjectURL(url);
  }});

  document.getElementById('copy').addEventListener('click', function() {{
    var text = exportJson();
    if (navigator.clipboard && navigator.clipboard.writeText) {{
      navigator.clipboard.writeText(text);
    }}
  }});
}})();
</script>
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
