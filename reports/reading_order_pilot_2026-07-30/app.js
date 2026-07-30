
const STORAGE_KEY = "esg_reading_order_pilot_verdicts_v1";

function loadVerdicts() {
  try { return JSON.parse(localStorage.getItem(STORAGE_KEY)) || {}; }
  catch (e) { return {}; }
}
function saveVerdicts(v) {
  localStorage.setItem(STORAGE_KEY, JSON.stringify(v));
}
function getVerdict(pageId) {
  const v = loadVerdicts();
  return v[pageId] || null;
}
function setVerdict(pageId, data) {
  const v = loadVerdicts();
  v[pageId] = Object.assign({}, v[pageId], data, { reviewed_at_utc: new Date().toISOString() });
  saveVerdicts(v);
  return v[pageId];
}

function csvEscape(s) {
  if (s === null || s === undefined) s = "";
  s = String(s);
  if (/[",\n]/.test(s)) s = '"' + s.replace(/"/g, '""') + '"';
  return s;
}

function exportCsv(indexData) {
  const v = loadVerdicts();
  const rows = [["page_id","role","ticker","pdf_file","page","candidate_status","verdict","note","reviewed_at_utc"]];
  for (const entry of indexData) {
    const rec = v[entry.page_id];
    if (!rec || !rec.verdict) continue;
    rows.push([
      entry.page_id, entry.role, entry.ticker, entry.pdf_file, entry.page, entry.candidate_status,
      rec.verdict, rec.note || "", rec.reviewed_at_utc || ""
    ]);
  }
  const csv = rows.map(r => r.map(csvEscape).join(",")).join("\r\n");
  const blob = new Blob([csv], { type: "text/csv;charset=utf-8;" });
  const a = document.createElement("a");
  a.href = URL.createObjectURL(blob);
  a.download = "reading_order_pilot_verdicts.csv";
  document.body.appendChild(a);
  a.click();
  a.remove();
}

function reviewedCount(indexData) {
  const v = loadVerdicts();
  return indexData.filter(e => v[e.page_id] && v[e.page_id].verdict).length;
}
