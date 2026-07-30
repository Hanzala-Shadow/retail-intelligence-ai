"""Build a BLINDED scoring page: production reader vs region candidate.

Decides one question only: on the pages where the two readers actually disagree,
is the candidate better than what production does today?

Blinding matters here. Every earlier review in this project was labelled, and the
reviewer has spent the session improving the candidate, so a labelled comparison
invites confirmation of that investment. The two texts are therefore presented as
"Parser A" and "Parser B", with the side assigned per page from a fixed seed. The
mapping is written to a separate JSON that the scoring page never loads, so it
cannot leak into the page and can still be recovered at tally time.

Two groups, kept apart on purpose:

Group 1 -- both readers produce text and it differs. These carry the decision.
Group 2 -- production returns `ambiguous`, i.e. it refuses the page and emits
    nothing. Blinding is meaningless when one side is empty, so these are shown
    unblinded and tallied separately. Counting them with group 1 would flatter
    the candidate: "produces something" is a low bar to beat "produces nothing".
"""

from __future__ import annotations

import html
import json
import random
import sys
from pathlib import Path

import pdfplumber

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

RAW_ROOT = REPO_ROOT / "data" / "01_raw" / "sustainability"
COMPARISON = REPO_ROOT / "reports" / "parser_comparison_2026-07-30" / "parser_comparison.json"
OUT_DIR = REPO_ROOT / "reports" / "old_vs_new_review_2026-07-30"

SEED = 20260730
RENDER_DPI = 90


def collect() -> tuple[list[dict], list[dict]]:
    documents = json.loads(COMPARISON.read_text(encoding="utf-8"))
    differing: list[dict] = []
    refused: list[dict] = []
    for document in documents:
        for page in document["pages"]:
            old = page["variants"]["column_order"]
            new = page["variants"]["regions"]
            record = {
                "ticker": document["ticker"],
                "pdf_file": document["pdf_file"],
                "page": page["page"],
                "old_text": old["text"],
                "new_text": new["text"],
                "old_status": old["status"],
                "old_reason": old["reason"],
                "new_reason": new["reason"],
                "body_word_count": page["body_word_count"],
            }
            if old["status"] == "ambiguous":
                refused.append(record)
            elif old["status"] == "reconstructed" and old["text"].split() != new["text"].split():
                differing.append(record)
    return differing, refused


def render(record: dict, name: str) -> str | None:
    path = RAW_ROOT / record["ticker"] / record["pdf_file"]
    try:
        with pdfplumber.open(path) as pdf:
            pdf.pages[record["page"] - 1].to_image(resolution=RENDER_DPI).save(OUT_DIR / name)
    except Exception as error:  # a missing render must not block scoring
        print(f"  render failed for {record['ticker']} p{record['page']}: {error}")
        return None
    return name


STYLE = """
:root { color-scheme: light dark; }
* { box-sizing: border-box; }
body { font-family: system-ui, sans-serif; margin: 0; padding: 0 20px 64px; background: #f6f7f9; color: #202124; }
header { position: sticky; top: 0; z-index: 10; background: #ffffffee; backdrop-filter: blur(6px);
         border-bottom: 1px solid #d7dbe0; padding: 12px 0; margin-bottom: 18px; }
h1 { margin: 0 0 8px; font-size: 19px; }
h2 { font-size: 16px; margin: 30px 0 6px; }
.tally { display: flex; gap: 12px; flex-wrap: wrap; align-items: center; font-size: 14px; }
.pill { padding: 4px 10px; border-radius: 999px; border: 1px solid #d7dbe0; background: #fff; }
.intro { font-size: 14px; max-width: 90ch; }
article { background: #fff; border: 1px solid #d7dbe0; border-radius: 8px; padding: 14px; margin: 0 0 20px; }
article.scored { border-color: #15803d; }
.meta { font-size: 12px; color: #5f6368; margin: 0 0 8px; }
.grid { display: grid; grid-template-columns: minmax(240px, 0.9fr) minmax(300px, 1fr) minmax(300px, 1fr); gap: 14px; align-items: start; }
h3 { margin: 0 0 6px; font-size: 12px; text-transform: uppercase; letter-spacing: .04em; color: #5f6368; }
img { width: 100%; border: 1px solid #bbb; border-radius: 4px; }
pre { white-space: pre-wrap; overflow-wrap: anywhere; margin: 0; padding: 10px; background: #111827;
      color: #e5e7eb; border-radius: 4px; max-height: 440px; overflow-y: auto; font-size: 11.5px; line-height: 1.5; }
pre.empty { background: #7f1d1d; }
.score { margin-top: 12px; display: flex; gap: 8px; flex-wrap: wrap; align-items: center; }
.score label { cursor: pointer; padding: 8px 16px; border: 1px solid #d7dbe0; border-radius: 6px; background: #fff; font-size: 14px; user-select: none; }
.score label:has(input:checked) { font-weight: 600; background: #dbeafe; border-color: #1a73e8; }
.score input[type=text] { flex: 1; min-width: 200px; padding: 8px; border: 1px solid #d7dbe0; border-radius: 6px; }
button { padding: 8px 14px; border: 1px solid #d7dbe0; border-radius: 6px; background: #fff; cursor: pointer; font-size: 14px; }
button.primary { background: #1a73e8; color: #fff; border-color: #1a73e8; }
#out { width: 100%; min-height: 150px; font-family: ui-monospace, monospace; font-size: 12px; padding: 12px; border: 1px solid #d7dbe0; border-radius: 6px; }
@media (max-width: 1150px) { .grid { grid-template-columns: 1fr; } }
@media (prefers-color-scheme: dark) {
  body { background: #16181c; color: #e8eaed; }
  header { background: #1f2126ee; border-color: #33363b; }
  article { background: #1f2126; border-color: #33363b; }
  .pill, .score label, button, #out { background: #26282d; border-color: #44474d; color: #e8eaed; }
  .meta, h3 { color: #9aa0a6; }
}
"""

SCRIPT = """
const KEY = 'old_vs_new_scores_v1';
const saved = JSON.parse(localStorage.getItem(KEY) || '{}');
document.querySelectorAll('input[type=radio]').forEach(i => {
  if (saved[i.name] && saved[i.name].score === i.value) i.checked = true;
  i.addEventListener('change', persist);
});
document.querySelectorAll('input[type=text]').forEach(i => {
  const id = i.dataset.note;
  if (saved[id] && saved[id].note) i.value = saved[id].note;
  i.addEventListener('input', persist);
});
function persist() {
  const data = {};
  document.querySelectorAll('article').forEach(a => {
    const id = a.dataset.id;
    const picked = a.querySelector('input[type=radio]:checked');
    const note = a.querySelector('input[type=text]').value.trim();
    a.classList.toggle('scored', !!picked);
    if (picked || note) data[id] = { score: picked ? picked.value : '', note };
  });
  localStorage.setItem(KEY, JSON.stringify(data));
  render(data);
}
function render(data) {
  let done = 0;
  ORDER.forEach(([id]) => { if (data[id] && data[id].score) done++; });
  document.getElementById('n-done').textContent = done + ' / ' + ORDER.length;
  const lines = ['# Old vs new reader - blinded scores', ''];
  ORDER.forEach(([id, label]) => {
    const v = data[id];
    if (!v || !v.score) return;
    lines.push(`${id} | ${label} | ${v.score}${v.note ? ' | ' + v.note : ''}`);
  });
  lines.push('', `Scored: ${done} of ${ORDER.length}`);
  document.getElementById('out').value = lines.join('\\n');
}
document.getElementById('copy').addEventListener('click', async () => {
  const out = document.getElementById('out');
  out.select();
  try { await navigator.clipboard.writeText(out.value); } catch (e) { document.execCommand('copy'); }
  const b = document.getElementById('copy'); b.textContent = 'Copied';
  setTimeout(() => { b.textContent = 'Copy scores'; }, 1200);
});
document.getElementById('clear').addEventListener('click', () => {
  if (!confirm('Clear all scores?')) return;
  localStorage.removeItem(KEY);
  document.querySelectorAll('input[type=radio]').forEach(i => { i.checked = false; });
  document.querySelectorAll('input[type=text]').forEach(i => { i.value = ''; });
  persist();
});
persist();
"""


def main() -> None:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    differing, refused = collect()
    print(f"blinded group: {len(differing)} pages; production-refuses group: {len(refused)} pages")

    rng = random.Random(SEED)
    key_map: list[dict] = []
    order: list[list[str]] = []
    parts = [
        "<!doctype html>",
        '<html lang="en"><head><meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width, initial-scale=1">',
        "<title>Reader comparison - blinded</title>",
        f"<style>{STYLE}</style></head><body>",
        "<header><h1>Which reader reads this page better?</h1>",
        '<div class="tally">',
        '<span class="pill">Scored <b id="n-done">0</b></span>',
        '<button id="copy" class="primary">Copy scores</button>',
        '<button id="clear">Clear</button>',
        "</div></header>",
        '<p class="intro"><strong>The two texts are blinded.</strong> Which parser is A and '
        "which is B changes from page to page, and this page does not know the answer. Judge only "
        "which text reads closer to how a person reads the page: sentences whole, labels next to "
        "their values, blocks not interleaved. If neither is clearly better, choose "
        "<em>No difference</em> &mdash; that is a real answer, not a cop-out.</p>",
        f"<h2>Group 1 &mdash; both readers produce text ({len(differing)} pages)</h2>",
        '<p class="intro">These carry the decision.</p>',
    ]

    for index, record in enumerate(differing, 1):
        identifier = f"G{index}"
        label = f"{record['ticker']} p{record['page']}"
        image = render(record, f"g{index:02d}_{record['ticker']}_{record['page']}.png")
        swap = rng.random() < 0.5
        left_text, right_text = (
            (record["new_text"], record["old_text"]) if swap else (record["old_text"], record["new_text"])
        )
        key_map.append({
            "id": identifier, "page": label, "A": "regions" if swap else "column_order",
            "B": "column_order" if swap else "regions",
        })
        order.append([identifier, label])
        parts += [
            f'<article data-id="{identifier}"><h3 style="font-size:14px;text-transform:none;color:inherit">'
            f"{identifier}. {html.escape(label)}</h3>",
            f'<p class="meta">{record["body_word_count"]} body words</p>',
            '<div class="grid">',
            f'<section><h3>Source page</h3><img src="{image}" alt="page"></section>' if image
            else "<section><h3>Source page</h3><p>render unavailable</p></section>",
            f"<section><h3>Parser A</h3><pre>{html.escape(left_text) or '(nothing)'}</pre></section>",
            f"<section><h3>Parser B</h3><pre>{html.escape(right_text) or '(nothing)'}</pre></section>",
            "</div>",
            '<div class="score">',
            f'<label><input type="radio" name="{identifier}" value="A better">A better</label>',
            f'<label><input type="radio" name="{identifier}" value="No difference">No difference</label>',
            f'<label><input type="radio" name="{identifier}" value="B better">B better</label>',
            f'<input type="text" data-note="{identifier}" placeholder="note (optional)">',
            "</div></article>",
        ]

    parts.append(f"<h2>Group 2 &mdash; production refuses these pages ({len(refused)})</h2>")
    parts.append('<p class="intro">Production returns <code>ambiguous</code> and emits nothing, so '
                 "these are shown unblinded and tallied separately. Judge whether the candidate's "
                 "text is genuinely usable &mdash; not merely present.</p>")
    for index, record in enumerate(refused, 1):
        identifier = f"R{index}"
        label = f"{record['ticker']} p{record['page']}"
        image = render(record, f"r{index:02d}_{record['ticker']}_{record['page']}.png")
        order.append([identifier, label])
        parts += [
            f'<article data-id="{identifier}"><h3 style="font-size:14px;text-transform:none;color:inherit">'
            f"{identifier}. {html.escape(label)} &mdash; production refuses ({html.escape(record['old_reason'])})</h3>",
            '<div class="grid">',
            f'<section><h3>Source page</h3><img src="{image}" alt="page"></section>' if image
            else "<section><h3>Source page</h3><p>render unavailable</p></section>",
            "<section><h3>Production</h3><pre class='empty'>(refuses: emits nothing)</pre></section>",
            f"<section><h3>Candidate</h3><pre>{html.escape(record['new_text']) or '(nothing)'}</pre></section>",
            "</div>",
            '<div class="score">',
            f'<label><input type="radio" name="{identifier}" value="Candidate usable">Candidate usable</label>',
            f'<label><input type="radio" name="{identifier}" value="Candidate not usable">Candidate not usable</label>',
            f'<input type="text" data-note="{identifier}" placeholder="note (optional)">',
            "</div></article>",
        ]

    parts += [
        "<h2>Results</h2>",
        '<textarea id="out" readonly></textarea>',
        f"<script>const ORDER = {json.dumps(order)};{SCRIPT}</script>",
        "</body></html>",
    ]

    (OUT_DIR / "review.html").write_text("\n".join(parts), encoding="utf-8")
    # Written separately and never referenced by review.html, so the page cannot leak it.
    (OUT_DIR / "BLIND_KEY_do_not_open_until_scored.json").write_text(
        json.dumps(key_map, indent=2), encoding="utf-8"
    )
    print(f"wrote {OUT_DIR / 'review.html'}")
    print(f"wrote {OUT_DIR / 'BLIND_KEY_do_not_open_until_scored.json'}")


if __name__ == "__main__":
    main()
