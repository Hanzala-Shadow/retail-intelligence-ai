"""Build the standalone reading-order pilot review site from pilot_summary.json.

Read-only against the corpus; only writes inside
``reports/reading_order_pilot_2026-07-30/``. Deliberately a SEPARATE site from
``reports/chunk_quality_review_2026-07-29/`` (its own storage key, its own
export filename) so Aziz's existing 25 saved chunk-quality answers are never
touched. Reuses that site's visual language (style.css) as instructed by the
task brief's fallback: "create a separate local pilot HTML page using the
existing site's style and components."
"""

from __future__ import annotations

import html
import json
import shutil
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
OUT_DIR = REPO_ROOT / "reports" / "reading_order_pilot_2026-07-30"
SOURCE_STYLE = REPO_ROOT / "reports" / "chunk_quality_review_2026-07-29" / "style.css"

STORAGE_KEY = "esg_reading_order_pilot_verdicts_v1"


def slug(row: dict) -> str:
    stem = row["pdf_stem"].replace(" ", "_").replace("&", "and")
    return f"{row['ticker']}_{stem}_p{row['page']:03d}"


APP_JS = f"""
const STORAGE_KEY = "{STORAGE_KEY}";

function loadVerdicts() {{
  try {{ return JSON.parse(localStorage.getItem(STORAGE_KEY)) || {{}}; }}
  catch (e) {{ return {{}}; }}
}}
function saveVerdicts(v) {{
  localStorage.setItem(STORAGE_KEY, JSON.stringify(v));
}}
function getVerdict(pageId) {{
  const v = loadVerdicts();
  return v[pageId] || null;
}}
function setVerdict(pageId, data) {{
  const v = loadVerdicts();
  v[pageId] = Object.assign({{}}, v[pageId], data, {{ reviewed_at_utc: new Date().toISOString() }});
  saveVerdicts(v);
  return v[pageId];
}}

function csvEscape(s) {{
  if (s === null || s === undefined) s = "";
  s = String(s);
  if (/[",\\n]/.test(s)) s = '"' + s.replace(/"/g, '""') + '"';
  return s;
}}

function exportCsv(indexData) {{
  const v = loadVerdicts();
  const rows = [["page_id","role","ticker","pdf_file","page","candidate_status","verdict","note","reviewed_at_utc"]];
  for (const entry of indexData) {{
    const rec = v[entry.page_id];
    if (!rec || !rec.verdict) continue;
    rows.push([
      entry.page_id, entry.role, entry.ticker, entry.pdf_file, entry.page, entry.candidate_status,
      rec.verdict, rec.note || "", rec.reviewed_at_utc || ""
    ]);
  }}
  const csv = rows.map(r => r.map(csvEscape).join(",")).join("\\r\\n");
  const blob = new Blob([csv], {{ type: "text/csv;charset=utf-8;" }});
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = "reading_order_pilot_verdicts.csv";
  document.body.appendChild(a);
  a.click();
  a.remove();
}}

function reviewedCount(indexData) {{
  const v = loadVerdicts();
  return indexData.filter(e => v[e.page_id] && v[e.page_id].verdict).length;
}}
"""


def build_index_data(rows: list[dict]) -> list[dict]:
    return [
        {
            "page_id": slug(row),
            "role": row["role"],
            "ticker": row["ticker"],
            "pdf_file": row["pdf_file"],
            "page": row["page"],
            "candidate_status": row["candidate_status"],
        }
        for row in rows
    ]


def render_index_html(rows: list[dict]) -> str:
    index_data = build_index_data(rows)
    body_rows = []
    for row in rows:
        pid = slug(row)
        region_types = ", ".join(r["region_type"] for r in row["regions"])
        body_rows.append(
            "<tr>"
            f"<td>{html.escape(row['role'])}</td>"
            f"<td>{html.escape(row['ticker'])}</td>"
            f"<td>{html.escape(row['pdf_file'])}</td>"
            f"<td>{row['page']}</td>"
            f"<td>{html.escape(row['current_decision'])}</td>"
            f"<td>{html.escape(row['candidate_status'])}</td>"
            f"<td>{html.escape(region_types)}</td>"
            f"<td><span class='badge unreviewed' id='badge-{pid}'>unreviewed</span></td>"
            f"<td><a href='{pid}.html'>open &rarr;</a></td>"
            "</tr>"
        )

    return f"""<!doctype html>
<html><head><meta charset="utf-8">
<title>Reading-order pilot review — 2026-07-30</title>
<link rel="stylesheet" href="style.css">
</head><body>
<header class="topbar">
  <strong>Reading-order pilot review</strong>
  <span class="small">8 named failure pages + 4 held-out "good" control pages. Separate from, and does not touch, the chunk-quality review's 25 saved answers.</span>
  <span style="flex:1"></span>
  <span class="progress" id="progress"></span>
  <button onclick="exportCsv(INDEX_DATA)">Export CSV</button>
</header>
<main>
<table class="index-table">
<thead><tr><th>role</th><th>ticker</th><th>pdf</th><th>page</th><th>current decision</th><th>candidate status</th><th>region types</th><th>verdict</th><th></th></tr></thead>
<tbody>
{''.join(body_rows)}
</tbody>
</table>
</main>
<script src="index_data.js"></script>
<script src="app.js"></script>
<script>
function refreshAll() {{
  for (const entry of INDEX_DATA) {{
    const rec = getVerdict(entry.page_id);
    const el = document.getElementById("badge-" + entry.page_id);
    if (!el) continue;
    const v = rec && rec.verdict;
    el.textContent = v || "unreviewed";
    el.className = "badge " + (v || "unreviewed");
  }}
  document.getElementById("progress").textContent = reviewedCount(INDEX_DATA) + " / " + INDEX_DATA.length + " reviewed";
}}
refreshAll();
</script>
</body></html>
"""


def render_regions_table(regions: list[dict]) -> str:
    rows = "".join(
        "<tr>"
        f"<td>{r['top']}</td><td>{r['bottom']}</td>"
        f"<td>{html.escape(r['region_type'])}</td><td>{r['column_count']}</td>"
        f"<td>{html.escape(r['reason'])}</td>"
        "</tr>"
        for r in regions
    )
    return (
        "<table class='meta'><tr><th>top</th><th>bottom</th><th>region type</th>"
        f"<th>columns</th><th>reason</th></tr>{rows}</table>"
    )


def render_page_html(row: dict, prev_id: str, next_id: str, index: int, total: int) -> str:
    pid = slug(row)
    status_class = "good" if row["candidate_status"] == "candidate_ready" else "usable_with_defects"
    safety_note = (
        "OK: source and candidate word multisets match exactly."
        if row["external_token_check_passed"]
        else (
            "NOTE: candidate word count differs from source "
            f"({row['source_word_count']} source vs {row['candidate_word_count']} candidate tokens). "
            "This page used the verified-table-extraction hint, which defers to the existing, "
            "already-shipped table markdown path -- that path reformats cells (added separators, "
            "backfilled blanks) and does not preserve an exact word multiset. This is pre-existing "
            "table-extraction behaviour, not new word fabrication by the region-splitting logic."
        )
    )

    return f"""<!doctype html>
<html><head><meta charset="utf-8">
<title>{html.escape(row['pdf_file'])} p{row['page']} — reading-order pilot</title>
<link rel="stylesheet" href="style.css">
</head><body>
<header class="topbar">
  <a href="index.html">&larr; Index</a>
  <a href="{prev_id}.html">&larr; Prev</a>
  <span>{index} / {total}</span>
  <a href="{next_id}.html">Next &rarr;</a>
  <span id="verdict-badge" class="badge unreviewed">unreviewed</span>
  <span style="flex:1"></span>
  <button onclick="exportCsv(INDEX_DATA)">Export CSV</button>
</header>
<main>
<div class="columns">
  <div class="pane">
    <h2>Page {row['page']} of {html.escape(row['pdf_stem'])} ({html.escape(row['role'])})</h2>
    <div class="page-imgs">
      <img src="{row['image']}" alt="page {row['page']}" loading="lazy" onclick="this.classList.toggle('zoomed')">
    </div>
    <p class="small">Current audit decision: <code>{html.escape(row['current_decision'])}</code> &middot;
    Legacy whole-page reconstructor: <code>{html.escape(row['legacy_status'])}</code> (columns={row['legacy_columns']}) &middot;
    Candidate status: <code>{html.escape(row['candidate_status'])}</code> (<code>{html.escape(row['candidate_reason'])}</code>)</p>
    <h2>Detected regions</h2>
    {render_regions_table(row['regions'])}
    <p class="small">{safety_note}</p>
  </div>
  <div class="pane">
    <h2>Current parsed text (production, unchanged)</h2>
    <pre class="chunk-text">{html.escape(row['current_text'])}</pre>
    <h2>Candidate parsed text (region-aware, NOT written back)</h2>
    <pre class="chunk-text">{html.escape(row['candidate_text'])}</pre>
  </div>
</div>

<div class="pane verdict-form" style="margin-top:1rem">
  <h2>Verdict: is the candidate reading order better, the same, or worse than current?</h2>
  <fieldset>
    <legend>Compared to current parsed text</legend>
    <label><input type="radio" name="verdict" value="better"> Better</label><br>
    <label><input type="radio" name="verdict" value="same"> Same</label><br>
    <label><input type="radio" name="verdict" value="worse"> Worse</label>
  </fieldset>
  <fieldset>
    <legend>Note (optional)</legend>
    <textarea class="note" placeholder="free-text note"></textarea>
  </fieldset>
  <div class="toolbar">
    <button onclick="saveThisVerdict()">Save verdict</button>
    <span class="small">Saved automatically to this browser's localStorage on every change. Separate storage key from the chunk-quality review -- its 25 answers are untouched.</span>
  </div>
</div>
</main>
<script src="index_data.js"></script>
<script src="app.js"></script>
<script>
const PAGE_ID = "{pid}";
const PREV_HREF = "{prev_id}.html";
const NEXT_HREF = "{next_id}.html";

function refreshBadge() {{
  const rec = getVerdict(PAGE_ID);
  const el = document.getElementById("verdict-badge");
  const v = rec && rec.verdict;
  el.textContent = v || "unreviewed";
  el.className = "badge " + (v || "unreviewed");
}}

function currentFormState() {{
  const verdictEl = document.querySelector('input[name="verdict"]:checked');
  const note = document.querySelector('textarea.note').value;
  return {{ verdict: verdictEl ? verdictEl.value : null, note }};
}}

function saveThisVerdict() {{
  setVerdict(PAGE_ID, currentFormState());
  refreshBadge();
}}

function restoreForm() {{
  const rec = getVerdict(PAGE_ID);
  if (!rec) return;
  if (rec.verdict) {{
    const el = document.querySelector('input[name="verdict"][value="' + rec.verdict + '"]');
    if (el) el.checked = true;
  }}
  document.querySelector('textarea.note').value = rec.note || "";
}}

document.addEventListener("change", (e) => {{
  if (e.target.closest(".verdict-form")) saveThisVerdict();
}});

document.addEventListener("keydown", (e) => {{
  if (e.target.tagName === "TEXTAREA") return;
  if (e.key === "ArrowRight") window.location.href = NEXT_HREF;
  if (e.key === "ArrowLeft") window.location.href = PREV_HREF;
}});

restoreForm();
refreshBadge();
</script>
</body></html>
"""


def build_site() -> None:
    with open(OUT_DIR / "pilot_summary.json", encoding="utf-8") as handle:
        rows = json.load(handle)

    shutil.copyfile(SOURCE_STYLE, OUT_DIR / "style.css")
    (OUT_DIR / "app.js").write_text(APP_JS, encoding="utf-8")

    index_data = build_index_data(rows)
    (OUT_DIR / "index_data.js").write_text(
        "window.__INDEX_DATA__ = " + json.dumps(index_data) + ";\nconst INDEX_DATA = window.__INDEX_DATA__;\n",
        encoding="utf-8",
    )

    (OUT_DIR / "index.html").write_text(render_index_html(rows), encoding="utf-8")

    ids = [slug(row) for row in rows]
    total = len(rows)
    for i, row in enumerate(rows):
        prev_id = ids[(i - 1) % total]
        next_id = ids[(i + 1) % total]
        html_content = render_page_html(row, prev_id, next_id, i + 1, total)
        (OUT_DIR / f"{ids[i]}.html").write_text(html_content, encoding="utf-8")

    print(f"Built site with {total} pages under {OUT_DIR}")
    print(f"Open: {OUT_DIR / 'index.html'}")


if __name__ == "__main__":
    build_site()
