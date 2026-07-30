"""Build context-prefixed ESG embedding text (esg_embed_ctx_v1).

The verified 100-chunk handoff package
(`esg_chunk_handoff_100_20260728/`) ships `embedding_text` as a structured
header block, a blank line, then the citation text verbatim:

    Company: CARTER'S INC
    Ticker: CRI
    Document: Form 10-K
    Fiscal year: FY2025
    SEC section: Item 5
    Subsection: Share Repurchase Program
    Content type: narrative

    <chunk_text>

All 100 reference rows carry those seven keys in that order, and on all 100
`embedding_text` minus the header equals `chunk_text` byte-for-byte. This
script reproduces that shape for the ESG corpus.

Two labels are deliberately renamed. The contract states SEC-specific fields
must be replaced with ESG equivalents rather than copied, so `Fiscal year`
becomes `Reporting year` and `SEC section` becomes `ESG topic`.

The body is the raw chunk text, not the `esg_embed_norm_v1` normalized text.
The normalization collapses repeated spaces, which would break the
`embedding_text - header == chunk_text` invariant the reference package holds
exactly. The v1 outputs are left untouched under `data/05_embedding/esg/`.

The reference package also carries an optional `Continuation context` key on
18/100 rows. It is deliberately NOT reproduced here. `esg_chunker.py` splits
with a 50-token overlap, so the previous chunk's tail is already inside the
next chunk: measured over 151 same-section chunk pairs, the previous chunk's
full 200-character tail appeared within the first 400 characters of the next
chunk on 151 of 151 (100%). Adding the key would duplicate that text and
double-weight it in the embedding. Revisit only if the overlap goes to zero.

`Table context` is also not reproduced: it requires extracted table column
headers, which the ESG pipeline does not yet produce.

Outputs are additive: nothing existing is overwritten.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import os
import re
import statistics
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
import _bootstrap  # noqa: F401  (import path: config, pipeline src, common)
import config  # noqa: E402

CHUNKS_INDEX = config.ESG_CHUNKS_INDEX_ENRICHED_CSV
SECTIONS_INDEX = config.ESG_SECTIONS_INDEX_CSV
OUT_TEXT_ROOT = config.ESG_EMBEDDING_CTX_DIR
OUT_INDEX = config.ESG_CHUNK_EMBEDDING_CONTEXT_CSV
OUT_SUMMARY = config.ESG_EMBEDDING_CONTEXT_SUMMARY_MD

EMBEDDING_CONTEXT_VERSION = "esg_embed_ctx_v1"
CONTENT_TYPE_RULE_VERSION = "esg_content_type_v1"

# Content-type thresholds, calibrated on the full live corpus (16,341 chunks).
# Medians by existing quality tier:
#   layout_sensitive  short=0.562  digit=0.036   (n=3,526)
#   narrative         short=0.222  digit=0.012   (n=12,665)
# `table` therefore requires sitting above the narrative band on both axes at
# once, which puts the cutoffs between the two distributions rather than
# inside either.
TABLE_SHORT_LINE_RATIO = 0.50
TABLE_DIGIT_RATIO = 0.030
LIST_BULLET_RATIO = 0.30
SHORT_LINE_MAX_WORDS = 4

BULLET_RE = re.compile(r"^([•●▪◦\-–—\*]|\(?\d{1,2}[.)])\s+")

DOCUMENT_LABELS = {
    "sustainability": "Sustainability Report",
    "annual_report_with_esg": "Annual Report with ESG Disclosure",
    "program_impact_report": "Program Impact Report",
}

TOPIC_LABELS = {
    "ceo_letter": "CEO Letter",
    "about_this_report": "About This Report",
    "environmental": "Environmental",
    "climate": "Climate",
    "energy": "Energy",
    "emissions": "Emissions",
    "waste": "Waste",
    "water": "Water",
    "social": "Social",
    "human_capital": "Human Capital",
    "diversity_equity_inclusion": "Diversity, Equity and Inclusion",
    "supply_chain_ethics": "Supply Chain and Ethics",
    "community": "Community",
    "governance": "Governance",
    "ethics_compliance": "Ethics and Compliance",
    "data_summary": "Data Summary",
    "appendix": "Appendix",
    "other": "Other",
    "full_document": "Full Document",
}

INDEX_FIELDS = [
    "chunk_id",
    "ticker",
    "canonical_ticker",
    "company_name",
    "report_year",
    "section_code",
    "section_title",
    "content_type",
    "content_type_reason",
    "chunk_text_sha256",
    "embedding_text_ctx_file",
    "embedding_text_ctx_sha256",
    "embedding_context_version",
    "content_type_rule_version",
]


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None


def topic_label(section_code: str) -> str:
    if section_code in TOPIC_LABELS:
        return TOPIC_LABELS[section_code]
    return section_code.replace("_", " ").strip().title()


def document_label(row: dict) -> str:
    source_type = (row.get("source_type") or "").strip()
    doc_type = (row.get("doc_type") or "").strip()
    for key in (source_type, doc_type):
        if key in DOCUMENT_LABELS:
            return DOCUMENT_LABELS[key]
    return (source_type or doc_type or "Sustainability Report").replace("_", " ").title()


def reporting_year(row: dict) -> str:
    year = (row.get("report_year") or "").strip()
    if not year:
        return "unknown"
    if (row.get("report_year_status") or "").strip() == "multi_year_range":
        span = (row.get("report_year_span") or "").strip()
        if span and span != year:
            return f"{year} (report covers {span})"
    return year


def classify_content(text: str) -> tuple[str, str]:
    """Return (content_type, reason). Conservative: defaults to narrative."""
    lines = [line.strip() for line in text.split("\n") if line.strip()]
    if not lines:
        return "narrative", "empty"

    short_ratio = sum(1 for l in lines if len(l.split()) <= SHORT_LINE_MAX_WORDS) / len(lines)
    digit_ratio = sum(c.isdigit() for c in text) / max(len(text), 1)
    bullet_ratio = sum(1 for l in lines if BULLET_RE.match(l)) / len(lines)

    if bullet_ratio >= LIST_BULLET_RATIO:
        return "list", f"bullet_ratio={bullet_ratio:.2f}"
    if short_ratio >= TABLE_SHORT_LINE_RATIO and digit_ratio >= TABLE_DIGIT_RATIO:
        return "table", f"short_lines={short_ratio:.2f};digits={digit_ratio:.3f}"
    return "narrative", f"short_lines={short_ratio:.2f};digits={digit_ratio:.3f}"


def build_header(row: dict, section_title: str, content_type: str) -> str:
    ticker = (row.get("canonical_ticker") or row.get("ticker") or "").strip()
    return "\n".join(
        [
            f"Company: {(row.get('company_name') or '').strip() or 'unknown'}",
            f"Ticker: {ticker or 'unknown'}",
            f"Document: {document_label(row)}",
            f"Reporting year: {reporting_year(row)}",
            f"ESG topic: {topic_label((row.get('section_code') or '').strip())}",
            f"Subsection: {section_title or 'unknown'}",
            f"Content type: {content_type}",
        ]
    )


def load_section_titles() -> dict[tuple[str, str, str], str]:
    titles: dict[tuple[str, str, str], str] = {}
    with SECTIONS_INDEX.open(encoding="utf-8", errors="replace", newline="") as fh:
        for row in csv.DictReader(fh):
            key = (row["ticker"], row["pdf_stem"], row["section_instance_id"])
            titles[key] = (row.get("section_title") or "").strip()
    return titles


def safe_name(chunk_id: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]", "_", chunk_id)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=0, help="process only the first N chunks")
    parser.add_argument("--dry-run", action="store_true", help="do not write any files")
    args = parser.parse_args(argv)

    if not CHUNKS_INDEX.exists():
        print(f"missing input: {CHUNKS_INDEX}", file=sys.stderr)
        return 2

    titles = load_section_titles()

    with CHUNKS_INDEX.open(encoding="utf-8", errors="replace", newline="") as fh:
        rows = list(csv.DictReader(fh))
    if args.limit:
        rows = rows[: args.limit]

    # Group by section instance so continuation context can look backwards.
    rows.sort(key=lambda r: (r["ticker"], r["pdf_stem"], r["section_instance_id"], int(r["chunk_index"] or 0)))

    out_rows: list[dict] = []
    type_counts: Counter = Counter()
    missing_text = 0
    missing_title = 0
    invariant_failures: list[str] = []
    header_lengths: list[int] = []

    prev_key: tuple | None = None
    prev_type: str = ""

    for row in rows:
        chunk_id = row["chunk_id"]
        key = (row["ticker"], row["pdf_stem"], row["section_instance_id"])

        chunk_text = read_text(ROOT / row["chunk_file"]) if not os.path.isabs(row["chunk_file"]) else read_text(Path(row["chunk_file"]))
        if chunk_text is None:
            chunk_text = read_text(Path(row["chunk_file"]))
        if chunk_text is None:
            missing_text += 1
            prev_key, prev_type = key, ""
            continue

        section_title = titles.get(key, "")
        if not section_title:
            missing_title += 1

        content_type, reason = classify_content(chunk_text)
        if content_type == "table" and prev_key == key and prev_type in ("table", "table_continuation"):
            content_type = "table_continuation"

        header = build_header(row, section_title, content_type)
        embedding_text = f"{header}\n\n{chunk_text}"
        header_lengths.append(len(header))

        # The reference package holds this exactly on all 100 rows.
        if embedding_text.partition("\n\n")[2] != chunk_text:
            invariant_failures.append(chunk_id)

        # Recorded in embedding_text_ctx_file, so it stays repo-relative.
        rel_path = (
            config.as_repo_relative(OUT_TEXT_ROOT)
            / row["ticker"]
            / f"{safe_name(chunk_id)}.txt"
        )
        if not args.dry_run:
            abs_path = ROOT / rel_path
            abs_path.parent.mkdir(parents=True, exist_ok=True)
            abs_path.write_text(embedding_text, encoding="utf-8")

        out_rows.append(
            {
                "chunk_id": chunk_id,
                "ticker": row["ticker"],
                "canonical_ticker": row.get("canonical_ticker", ""),
                "company_name": row.get("company_name", ""),
                "report_year": row.get("report_year", ""),
                "section_code": row.get("section_code", ""),
                "section_title": section_title,
                "content_type": content_type,
                "content_type_reason": reason,
                "chunk_text_sha256": sha256_text(chunk_text),
                "embedding_text_ctx_file": rel_path.as_posix(),
                "embedding_text_ctx_sha256": sha256_text(embedding_text),
                "embedding_context_version": EMBEDDING_CONTEXT_VERSION,
                "content_type_rule_version": CONTENT_TYPE_RULE_VERSION,
            }
        )
        type_counts[content_type] += 1
        prev_key, prev_type = key, content_type

    if not args.dry_run:
        OUT_INDEX.parent.mkdir(parents=True, exist_ok=True)
        with OUT_INDEX.open("w", encoding="utf-8", newline="") as fh:
            writer = csv.DictWriter(fh, fieldnames=INDEX_FIELDS)
            writer.writeheader()
            writer.writerows(out_rows)

    total = len(out_rows)
    dup_ctx = total - len({r["embedding_text_ctx_sha256"] for r in out_rows})
    dup_chunk = total - len({r["chunk_text_sha256"] for r in out_rows})

    lines = [
        "# ESG embedding context summary",
        "",
        f"- Version: `{EMBEDDING_CONTEXT_VERSION}` (content type `{CONTENT_TYPE_RULE_VERSION}`)",
        f"- Chunks written: {total}",
        f"- Chunk text unreadable: {missing_text}",
        f"- Section title empty: {missing_title}",
        f"- Header length median: {int(statistics.median(header_lengths)) if header_lengths else 0} chars",
        f"- `embedding_text - header == chunk_text` failures: {len(invariant_failures)}",
        f"- Duplicate embedding_text_ctx_sha256: {dup_ctx}",
        f"- Duplicate chunk_text_sha256: {dup_chunk}",
        "",
        "## Content type",
        "",
    ]
    for name, count in type_counts.most_common():
        lines.append(f"- {name}: {count} ({count / max(total,1):.2%})")
    if invariant_failures:
        lines += ["", "## Invariant failures", ""] + [f"- {c}" for c in invariant_failures[:20]]
    summary = "\n".join(lines) + "\n"

    if not args.dry_run:
        OUT_SUMMARY.parent.mkdir(parents=True, exist_ok=True)
        OUT_SUMMARY.write_text(summary, encoding="utf-8")

    print(summary)
    return 1 if invariant_failures or missing_text else 0


if __name__ == "__main__":
    raise SystemExit(main())
