"""P1 chunk-metadata enrichment for the ESG retrieval corpus.

Implements the four P1 items from the 2026-07-23 pilot audit (section 9):

  1. report_year          - real integer column, extracted once from pdf_stem
  2. company_name         - canonical name joined from the accepted-company manifest
  3. chunk_quality_tier   - narrative / layout_sensitive / noise, deterministic rule
  4. embedding_text_plain - normalized copy for embedding; source text untouched

Also implements Task 3 (2026-07-29, ESG Chunk Management Contract Principle 9
and field_dictionary.csv line 26): frozen processing versions and a
deterministic dataset_id.

  5. parser_version / sectioner_version / chunker_version / dataset_id
     - the first two are joined per-document from the parse and sections
       indexes; chunker_version is a constant imported from esg_chunker.py;
       dataset_id is a SHA256 over the versions plus the input index's own
       hash and row count, so re-running on an unchanged corpus reproduces
       the same id.

Design constraints taken directly from the audit:

  * The rule set must be deterministic. Same input -> byte-identical output.
    No model calls, no generated prose, no random sampling.
  * chunk_text stays byte-stable. Normalization writes a *copy* to a parallel
    tree; the original chunk files and their hashes are never modified.
  * Nothing is guessed. A year that cannot be parsed is left empty and flagged
    rather than inferred; a chunk whose text is not on disk is tiered
    "pending_text" rather than assumed narrative.

The script is read-only with respect to every existing input. It writes:

  data/00_reference/esg_chunks_index_enriched.csv
  data/05_embedding/esg/<TICKER>/<chunk>.txt
  reports/esg_p1_enrichment_qa.csv
  reports/esg_p1_enrichment_summary.md
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import os
import re
import sys
import unicodedata
from collections import Counter, defaultdict
import _bootstrap  # noqa: F401  (import path: config, pipeline src, common)

import config

# The year rule lives in exactly one module. These names are re-exported so
# callers and tests can keep using p1.extract_report_year/YEAR_MIN/YEAR_MAX,
# but they ARE esg_year's -- not copies that can drift from it.
from esg_year import YEAR_MAX, YEAR_MIN, extract_report_year  # noqa: F401

# Same reasoning for CHUNKER_VERSION: it versions the chunking rule set and
# lives next to those rules in esg_chunker.py, not as a second copy here.
from esg_chunker import CHUNKER_VERSION  # noqa: F401

# Rule-set versions. Bump when a rule changes so downstream runs stay traceable.
YEAR_RULE_VERSION = "esg_year_v1"
TIER_RULE_VERSION = "esg_tier_v1"
NORMALIZATION_VERSION = "esg_embed_norm_v2"
# Versions the shape of the embedding heading prefix (which labels appear, in
# what order). Bump when the prefix changes so old embeddings stay traceable.
EMBEDDING_PREFIX_VERSION = "esg_embed_prefix_v1"

REPO_ROOT = str(config.REPO_ROOT)


def _rel(path) -> str:
    """Repo-relative POSIX form of a config path.

    This module joins every path against ``--repo-root`` and writes
    ``rel_embed`` into the output index, so its constants must stay
    relative. They are still derived from config so the layout lives in
    exactly one place.
    """
    return config.as_repo_relative(path).as_posix()


CHUNKS_INDEX = _rel(config.ESG_CHUNKS_INDEX_CSV)
COMPANY_MANIFEST = _rel(config.ESG_ACCEPTED_COMPANY_MANIFEST_CSV)
COMPANIES = _rel(config.COMPANIES_CSV)
SOURCE_REGISTRY = _rel(config.ESG_SOURCE_REGISTRY_CSV)
PARSE_INDEX = _rel(config.ESG_PARSE_INDEX_CSV)
SECTIONS_INDEX = _rel(config.ESG_SECTIONS_INDEX_CSV)
DRIVE_MANIFEST = _rel(config.ESG_DRIVE_MANIFEST_CSV)

OUT_INDEX = _rel(config.ESG_CHUNKS_INDEX_ENRICHED_CSV)
EMBED_ROOT = _rel(config.ESG_EMBEDDING_DIR)
QA_REPORT = _rel(config.ESG_P1_ENRICHMENT_QA_CSV)
SUMMARY_REPORT = _rel(config.ESG_P1_ENRICHMENT_SUMMARY_MD)

NEW_COLUMNS = [
    "report_year",
    "report_year_span",
    "report_year_status",
    "company_name",
    "company_name_status",
    "chunk_quality_tier",
    "chunk_quality_tier_reason",
    "embedding_text_plain_file",
    "embedding_text_plain_sha256",
    "embedding_normalization_version",
    "parser_version",
    "sectioner_version",
    "chunker_version",
    "company_id",
    "cik",
    "source_url",
    "source_retrieved_at_utc",
    "section_title_original",
    "embedding_prefix_version",
    "dataset_id",
]

# ---------------------------------------------------------------------------
# 1. report_year
# ---------------------------------------------------------------------------
#
# `extract_report_year` is imported from src/esg_year.py at the top of this
# module. It used to be defined here as a second implementation of the same
# rule; that copy agreed with the canonical one, but a fork that agrees today is
# still a fork. The manifest builder held a *third* implementation that took the
# first year token instead of max(), which is how VFC-VF CORP-2023-2024 came to
# be labelled 2023 in the manifest and 2024 here, for the same 103 chunks.


# ---------------------------------------------------------------------------
# 3. chunk_quality_tier
# ---------------------------------------------------------------------------

# Thresholds are named constants so the rule is auditable and independently
# testable, per the audit's "deterministic and independently testable" gate.
MIN_NARRATIVE_WORDS = 40
TOC_LINE_RATIO = 0.30
NUMERIC_CHAR_RATIO = 0.15
SHORT_LINE_MEDIAN = 30
SHORT_LINE_MIN_COUNT = 5
ALPHA_RATIO_FLOOR = 0.55

_DOT_LEADER = re.compile(r"(\.{4,}|·{4,}|_{4,}|-{6,})")
# A contents line is alpha-dominant title text followed by a short page number.
# The trailing run is capped at 3 digits and the line must be mostly letters, so
# a table header ("2021 2022 2023") or a metrics row ("Water withdrawn 1,204
# 1,150 1,001") does not qualify -- those are table evidence, not navigation.
_TOC_TAIL = re.compile(r"\s\d{1,3}\s*$")
TOC_LINE_ALPHA_FLOOR = 0.60
_REPEAT_RUN = re.compile(r"(.)\1{5,}")
REPLACEMENT_CHAR = "�"


def _alpha_ratio(text: str) -> float:
    dense = sum(1 for c in text if not c.isspace())
    if not dense:
        return 0.0
    return sum(1 for c in text if c.isalpha()) / dense


def _is_toc_line(line: str) -> bool:
    if _DOT_LEADER.search(line):
        return True
    return bool(_TOC_TAIL.search(line)) and _alpha_ratio(line) >= TOC_LINE_ALPHA_FLOOR


def _median(values):
    if not values:
        return 0.0
    ordered = sorted(values)
    mid = len(ordered) // 2
    if len(ordered) % 2:
        return float(ordered[mid])
    return (ordered[mid - 1] + ordered[mid]) / 2.0


def classify_chunk_tier(text: str):
    """Return (tier, reason) from chunk text alone. Pure function, no I/O."""
    lines = [ln.strip() for ln in text.splitlines()]
    lines = [ln for ln in lines if ln]
    words = text.split()
    n_chars = len(text)

    if n_chars == 0 or not words:
        return "noise", "empty_text"

    space = sum(1 for c in text if c.isspace())
    dense = max(1, n_chars - space)
    alpha_ratio = sum(1 for c in text if c.isalpha()) / dense
    numeric_ratio = sum(1 for c in text if c.isdigit()) / dense

    # Rule order matters and is deliberate.
    #
    # A table of contents is noise even though it is numeric, so it is tested
    # first. But numeric density is then tested *before* the short/low-alpha
    # noise rule, because a dense metrics row ("Scope 1 2019 2020 2021 4,102
    # 3,880 3,511") is short and low-alpha precisely because it is a table.
    # Testing low-alpha first would file the corpus's numeric evidence as noise
    # and down-rank it -- the opposite of the audit's "retrieve, but require
    # table-aware reconstruction" handling for this material.
    toc_hits = sum(1 for ln in lines if _is_toc_line(ln))
    toc_ratio = toc_hits / len(lines) if lines else 0.0
    if lines and toc_ratio >= TOC_LINE_RATIO:
        return "noise", f"toc_like:{toc_ratio:.2f}"

    if numeric_ratio >= NUMERIC_CHAR_RATIO:
        return "layout_sensitive", f"numeric_dense:{numeric_ratio:.2f}"

    # Short and low-alpha but *not* numeric: stray labels, nav fragments, page
    # furniture. No evidence value.
    if len(words) < MIN_NARRATIVE_WORDS and alpha_ratio < ALPHA_RATIO_FLOOR:
        return "noise", f"short_low_alpha:words={len(words)},alpha={alpha_ratio:.2f}"

    if len(lines) >= SHORT_LINE_MIN_COUNT:
        med = _median([len(ln) for ln in lines])
        if med < SHORT_LINE_MEDIAN:
            return "layout_sensitive", f"fragmented_lines:median={med:.0f}"

    if _REPEAT_RUN.search(text) or _DOT_LEADER.search(text):
        return "layout_sensitive", "repeated_char_run"

    if REPLACEMENT_CHAR in text:
        return "layout_sensitive", "unicode_replacement_char"

    # Short but alpha-dominant text is still narrative. Tagging it
    # layout_sensitive would route plain sentences to table-aware reconstruction.
    # The audit's "fewer than 40 words" signal is thin context, not a layout
    # risk, so it is recorded in the reason and left for the retrieval layer.
    if len(words) < MIN_NARRATIVE_WORDS:
        return "narrative", f"short_narrative:words={len(words)}"

    return "narrative", "narrative_prose"


# ---------------------------------------------------------------------------
# 4. embedding_text_plain
# ---------------------------------------------------------------------------

_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_HYPHEN_BREAK = re.compile(r"(\w)[-‐‑]\n(\w)")
_INLINE_WS = re.compile(r"[ \t  -   　]+")
_BLANK_RUN = re.compile(r"\n{3,}")


def normalize_for_embedding(text: str) -> str:
    """Cleaned copy of chunk text for embedding. Never written back to source.

    NFKC folds ligatures, full-width forms and non-breaking spaces into plain
    equivalents so the embedder sees consistent tokens.
    """
    out = unicodedata.normalize("NFKC", text)
    out = out.replace(REPLACEMENT_CHAR, "")
    out = _CONTROL.sub("", out)
    out = _HYPHEN_BREAK.sub(r"\1\2", out)          # rejoin words split at line breaks
    out = _INLINE_WS.sub(" ", out)
    out = "\n".join(ln.strip() for ln in out.split("\n"))
    out = _BLANK_RUN.sub("\n\n", out)
    return out.strip()


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def read_csv(path: str):
    with open(path, encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)
        return reader.fieldnames or [], list(reader)


# ---------------------------------------------------------------------------
# 5. dataset_id -- deterministic release identity
# ---------------------------------------------------------------------------
#
# Derived, not stamped: re-running on an unchanged corpus must reproduce the
# same id. The six inputs are, in order, the sorted distinct parser_version
# and sectioner_version values actually present in the run, CHUNKER_VERSION,
# the three P1 rule-set versions, the SHA256 of the input chunk index bytes,
# and the row count. Returns (payload, full_hash, dataset_id) so the payload
# and untruncated hash can be recorded for audit rather than left opaque.


DOC_TYPE_LABELS = {
    "sustainability": "Sustainability report",
    "10-K": "Form 10-K",
}


def build_embedding_text(row: dict, normalized_text: str) -> str:
    """Prepend controlled heading context to the text sent to the embedder.

    `chunk_text` is never touched: the contract keeps readable citation text
    separate from any embedding-only prefix, and allows controlled heading
    context to be added here. This is the mitigation for chunks that are
    ambiguous once separated from their heading.

    Deliberately excludes a 'Content type' line. The nearest column we have is
    chunk_quality_tier, which is a quality signal rather than a description of
    the content, and emitting the literal token 'layout_sensitive' into ~22% of
    chunks would put non-semantic text into the embedding space.

    Lines whose value is empty are omitted entirely rather than emitted blank,
    so the prefix never contains a dangling label.
    """
    year = (row.get("report_year") or "").strip()
    if (row.get("report_year_status") or "").strip() == "multi_year_range":
        # Say the honest thing for the spans we could not resolve to one year.
        year = (row.get("report_year_span") or "").strip() or year

    doc_type = (row.get("doc_type") or "").strip()
    topic = (row.get("section_code") or "").strip().replace("_", " ")

    fields = (
        ("Company", (row.get("company_name") or "").strip()),
        ("Ticker", (row.get("ticker") or "").strip()),
        ("CIK", (row.get("cik") or "").strip()),
        ("Document", DOC_TYPE_LABELS.get(doc_type, doc_type)),
        ("Reporting year", year),
        ("ESG topic", topic[:1].upper() + topic[1:] if topic else ""),
        ("Section", (row.get("section_title_original") or "").strip()),
    )
    header = "\n".join(f"{label}: {value}" for label, value in fields if value)
    return f"{header}\n\n{normalized_text}" if header else normalized_text


def compute_dataset_id(parser_versions, sectioner_versions,
                        chunks_index_sha256: str, row_count: int):
    lines = [
        ",".join(sorted(set(parser_versions))),
        ",".join(sorted(set(sectioner_versions))),
        CHUNKER_VERSION,
        ",".join([YEAR_RULE_VERSION, TIER_RULE_VERSION, NORMALIZATION_VERSION]),
        chunks_index_sha256,
        str(row_count),
    ]
    payload = "\n".join(lines)
    full_hash = hashlib.sha256(payload.encode("utf-8")).hexdigest()
    return payload, full_hash, f"esg_{full_hash[:12]}"


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------


def run(repo_root: str, write_embeddings: bool = True,
        dataset_id_override: str | None = None) -> dict:
    join = lambda rel: os.path.join(repo_root, rel)

    fieldnames, chunks = read_csv(join(CHUNKS_INDEX))
    _, manifest = read_csv(join(COMPANY_MANIFEST))
    _, companies = read_csv(join(COMPANIES))
    if not os.path.exists(join(SOURCE_REGISTRY)):
        raise SystemExit(
            f"missing {SOURCE_REGISTRY}: this is curated exception data "
            "(duplicates/exclusions/supplements), not derivable from the chunk "
            "index -- it must be hand-authored, not regenerated."
        )
    _, registry = read_csv(join(SOURCE_REGISTRY))
    _, parse_index = read_csv(join(PARSE_INDEX))
    _, sections_index = read_csv(join(SECTIONS_INDEX))
    drive_manifest_path = join(DRIVE_MANIFEST)
    # Optional: the Drive manifest is an intake artifact, so a repo that has not
    # synced yet still enriches -- those rows simply carry no source URL.
    _, drive_manifest = (
        read_csv(drive_manifest_path) if os.path.exists(drive_manifest_path) else ([], [])
    )

    # parser_version is a per-document policy, not a corpus-wide constant --
    # different documents were parsed under different policies. Join it from
    # esg_parse_index.csv, keyed on pdf_stem (pdf_file minus its extension).
    parser_version_by_stem = {
        os.path.splitext(r["pdf_file"])[0]: r["parser_policy"]
        for r in parse_index
    }
    # sectioner_version already exists as provenance_version on every sections
    # row; read it from the index rather than importing PROVENANCE_VERSION
    # directly, so a future sectioner bump propagates without a second edit.
    sectioner_version_by_stem = {
        r["pdf_stem"]: r["provenance_version"] for r in sections_index
    }

    chunk_stems = {r["pdf_stem"] for r in chunks}
    missing_parser = sorted(chunk_stems - parser_version_by_stem.keys())
    missing_sectioner = sorted(chunk_stems - sectioner_version_by_stem.keys())
    if missing_parser or missing_sectioner:
        rows_affected = sum(
            1 for r in chunks
            if r["pdf_stem"] in missing_parser or r["pdf_stem"] in missing_sectioner
        )
        raise SystemExit(
            "cannot join parser_version/sectioner_version for all rows: "
            f"{rows_affected} rows affected. "
            f"stems missing parser_version ({len(missing_parser)}): {missing_parser}; "
            f"stems missing sectioner_version ({len(missing_sectioner)}): {missing_sectioner}"
        )

    collision = set(fieldnames) & set(NEW_COLUMNS)
    if collision:
        raise SystemExit(f"refusing to overwrite existing columns: {sorted(collision)}")

    # --- 2. company_name lookup -------------------------------------------
    manifest_name = {r["ticker"]: r["company_name"].strip() for r in manifest}
    companies_name = {r["ticker"]: r["name"].strip() for r in companies}
    # company_id is the foreign key to the company master. The contract warns
    # against treating the ticker as permanent identity, so carry the id too.
    companies_id = {r["ticker"]: (r.get("company_id") or "").strip() for r in companies}
    # Retained where known, per the contract: ESG issuers that also file with
    # the SEC keep the same CIK, which is what joins the two corpora.
    companies_cik = {r["ticker"]: (r.get("cik") or "").strip() for r in companies}
    # Source URL and retrieval time. drive_file_name is the raw PDF filename, so
    # its stem is the join key; drive_file_id builds the canonical Drive URL.
    source_url_by_stem: dict[str, str] = {}
    source_retrieved_by_stem: dict[str, str] = {}
    for r in drive_manifest:
        stem_key = os.path.splitext((r.get("drive_file_name") or "").strip())[0]
        file_id = (r.get("drive_file_id") or "").strip()
        if not stem_key or not file_id:
            continue
        source_url_by_stem[stem_key] = f"https://drive.google.com/file/d/{file_id}/view"
        source_retrieved_by_stem[stem_key] = (r.get("updated_at_utc") or "").strip()
    # The original heading, kept alongside the normalized section_code. Keyed on
    # (pdf_stem, section_instance_id) because a stem holds many sections.
    section_title_by_instance = {
        (r["pdf_stem"], r["section_instance_id"]): (r.get("section_title") or "").strip()
        for r in sections_index
    }

    # --- registry cross-check for year ------------------------------------
    # The registry holds 26 exception rows (duplicates, exclusions, supplements),
    # not a full document list, so it is a cross-check -- never the join base.
    registry_year = {}
    for r in registry:
        yr, status, _span = extract_report_year(r["pdf_stem"])
        if status == "parsed":
            registry_year[r["pdf_stem"]] = yr

    # Year is a document property: resolve once per stem, then fan out to rows.
    stem_year = {}
    for stem in {r["pdf_stem"] for r in chunks}:
        stem_year[stem] = extract_report_year(stem)

    embed_root = join(EMBED_ROOT)
    out_rows = []
    stats = Counter()
    tier_by_doc = defaultdict(Counter)
    qa_rows = []

    for row in chunks:
        new = dict(row)
        stem = row["pdf_stem"]
        ticker = row["canonical_ticker"] or row["ticker"]

        # 5. processing versions -- frozen per contract Principle 9
        new["parser_version"] = parser_version_by_stem[stem]
        new["sectioner_version"] = sectioner_version_by_stem[stem]
        new["chunker_version"] = CHUNKER_VERSION

        # 1. report_year
        year, ystatus, yspan = stem_year[stem]
        reg_year = registry_year.get(stem)
        if year is not None and reg_year is not None and reg_year != year:
            ystatus = "registry_mismatch"
        new["report_year"] = "" if year is None else str(year)
        new["report_year_span"] = yspan
        new["report_year_status"] = ystatus
        stats[f"year:{ystatus}"] += 1
        if ystatus != "parsed":
            detail = f"stem={stem}"
            if ystatus == "registry_mismatch":
                detail += f" parsed={year} registry={reg_year}"
            qa_rows.append({
                "chunk_id": row["chunk_id"], "issue": f"year_{ystatus}",
                "detail": detail,
            })

        # 2. company_name
        name = manifest_name.get(ticker) or companies_name.get(ticker) or ""
        if manifest_name.get(ticker):
            nstatus = "manifest"
        elif companies_name.get(ticker):
            nstatus = "companies_fallback"
        else:
            nstatus = "unresolved"
            qa_rows.append({
                "chunk_id": row["chunk_id"], "issue": "company_name_unresolved",
                "detail": f"ticker={ticker}",
            })
        new["company_name"] = name
        new["company_name_status"] = nstatus
        stats[f"name:{nstatus}"] += 1

        # 2b. contract fields: company_id, source URL + retrieval time, heading
        new["company_id"] = companies_id.get(ticker, "")
        new["cik"] = companies_cik.get(ticker, "")
        new["source_url"] = source_url_by_stem.get(stem, "")
        new["source_retrieved_at_utc"] = source_retrieved_by_stem.get(stem, "")
        new["section_title_original"] = section_title_by_instance.get(
            (stem, row.get("section_instance_id", "")), ""
        )
        for field, issue in (
            ("company_id", "company_id_unresolved"),
            ("source_url", "source_url_unresolved"),
            ("section_title_original", "section_title_unresolved"),
        ):
            if not new[field]:
                stats[f"{field}:unresolved"] += 1
                qa_rows.append({
                    "chunk_id": row["chunk_id"], "issue": issue,
                    "detail": f"ticker={ticker} stem={stem}",
                })
            else:
                stats[f"{field}:resolved"] += 1

        # 3 + 4 require the chunk text on disk.
        chunk_path = join(row["chunk_file"])
        if os.path.exists(chunk_path):
            with open(chunk_path, encoding="utf-8") as fh:
                text = fh.read()
            tier, reason = classify_chunk_tier(text)
            # A chunk the pipeline already excluded is noise regardless of shape.
            if row.get("rag_action") == "exclude_from_esg_index":
                tier, reason = "noise", "excluded_by_pipeline"

            normalized = normalize_for_embedding(text)
            embedding_text = build_embedding_text(new, normalized)
            rel_embed = os.path.join(EMBED_ROOT, ticker,
                                     os.path.basename(row["chunk_file"]))
            if write_embeddings:
                dest = join(rel_embed)
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                with open(dest, "w", encoding="utf-8", newline="\n") as fh:
                    fh.write(embedding_text)
            new["embedding_text_plain_file"] = rel_embed.replace("\\", "/")
            new["embedding_text_plain_sha256"] = sha256_text(embedding_text)
            new["embedding_normalization_version"] = NORMALIZATION_VERSION
            new["embedding_prefix_version"] = EMBEDDING_PREFIX_VERSION
        else:
            tier, reason = "pending_text", "chunk_file_absent"
            new["embedding_text_plain_file"] = ""
            new["embedding_text_plain_sha256"] = ""
            new["embedding_normalization_version"] = ""
            new["embedding_prefix_version"] = ""

        new["chunk_quality_tier"] = tier
        new["chunk_quality_tier_reason"] = reason
        stats[f"tier:{tier}"] += 1
        tier_by_doc[stem][tier] += 1
        out_rows.append(new)

    # --- 5. dataset_id: computed once per run, identical on every row ------
    chunks_index_sha256 = sha256_file(join(CHUNKS_INDEX))
    dataset_payload, dataset_full_hash, derived_dataset_id = compute_dataset_id(
        parser_versions=(r["parser_version"] for r in out_rows),
        sectioner_versions=(r["sectioner_version"] for r in out_rows),
        chunks_index_sha256=chunks_index_sha256,
        row_count=len(out_rows),
    )
    dataset_id = dataset_id_override or derived_dataset_id
    for new in out_rows:
        new["dataset_id"] = dataset_id

    # --- write enriched index ---------------------------------------------
    out_fields = fieldnames + NEW_COLUMNS
    out_path = join(OUT_INDEX)
    os.makedirs(os.path.dirname(out_path), exist_ok=True)
    with open(out_path, "w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=out_fields, lineterminator="\n")
        writer.writeheader()
        writer.writerows(out_rows)

    # --- QA + summary ------------------------------------------------------
    qa_path = join(QA_REPORT)
    os.makedirs(os.path.dirname(qa_path), exist_ok=True)
    with open(qa_path, "w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=["chunk_id", "issue", "detail"],
                                lineterminator="\n")
        writer.writeheader()
        writer.writerows(qa_rows)

    total = len(out_rows)
    unresolved_stems = sorted({f"{r['pdf_stem']}  -> {stem_year[r['pdf_stem']][0]} "
                               f"[{stem_year[r['pdf_stem']][1]}]"
                               for r in chunks
                               if stem_year[r["pdf_stem"]][1] != "parsed"})
    lines = [
        "# P1 enrichment summary",
        "",
        f"- Rows processed: {total}",
        f"- Documents: {len({r['pdf_stem'] for r in chunks})}",
        f"- Tickers: {len({r['canonical_ticker'] for r in chunks})}",
        f"- Rule versions: year={YEAR_RULE_VERSION}, tier={TIER_RULE_VERSION}, "
        f"norm={NORMALIZATION_VERSION}",
        "",
        "## report_year",
    ]
    for key in sorted(k for k in stats if k.startswith("year:")):
        lines.append(f"- {key.split(':', 1)[1]}: {stats[key]} "
                     f"({100 * stats[key] / total:.2f}%)")
    if unresolved_stems:
        lines += ["", "Stems needing manual year assignment:"]
        lines += [f"  - {s}" for s in unresolved_stems]
    lines += ["", "## company_name"]
    for key in sorted(k for k in stats if k.startswith("name:")):
        lines.append(f"- {key.split(':', 1)[1]}: {stats[key]} "
                     f"({100 * stats[key] / total:.2f}%)")
    lines += ["", "## chunk_quality_tier"]
    for key in sorted(k for k in stats if k.startswith("tier:")):
        lines.append(f"- {key.split(':', 1)[1]}: {stats[key]} "
                     f"({100 * stats[key] / total:.2f}%)")
    lines += [
        "",
        "`pending_text` means the chunk .txt is not on disk, so no tier or",
        "embedding copy was produced. These rows are not assumed narrative.",
        "",
        f"- QA exceptions written: {len(qa_rows)}",
    ]

    parser_counts = Counter(r["parser_version"] for r in out_rows)
    sectioner_counts = Counter(r["sectioner_version"] for r in out_rows)
    lines += [
        "",
        "## Dataset release",
        "",
        f"- dataset_id: `{dataset_id}`",
    ]
    if dataset_id_override:
        lines.append(f"- derived dataset_id (for comparison): `{derived_dataset_id}`")
        if dataset_id_override != derived_dataset_id:
            lines.append("  - **mismatch**: override does not match the derived value")
    lines += [
        f"- full hash (untruncated): `{dataset_full_hash}`",
        f"- esg_chunks_index.csv sha256: `{chunks_index_sha256}`",
        f"- row count: {len(out_rows)}",
        "",
        "Derivation inputs, in order, joined by `\\n` and SHA256'd (first 12 hex",
        "chars of the digest become the `esg_<hex>` id):",
        "",
        f"1. distinct parser_version values: `{dataset_payload.splitlines()[0]}`",
        f"2. distinct sectioner_version values: `{dataset_payload.splitlines()[1]}`",
        f"3. chunker_version: `{CHUNKER_VERSION}`",
        f"4. rule-set versions: `{dataset_payload.splitlines()[3]}`",
        f"5. esg_chunks_index.csv sha256: `{chunks_index_sha256}`",
        f"6. row count: `{len(out_rows)}`",
        "",
        "### parser_version",
    ]
    for key in sorted(parser_counts):
        lines.append(f"- {key}: {parser_counts[key]}")
    lines += ["", "### sectioner_version"]
    for key in sorted(sectioner_counts):
        lines.append(f"- {key}: {sectioner_counts[key]}")

    with open(join(SUMMARY_REPORT), "w", encoding="utf-8", newline="\n") as fh:
        fh.write("\n".join(lines) + "\n")

    return {
        "rows": total, "stats": dict(stats), "qa": len(qa_rows),
        "out_index": out_path,
        "dataset_id": dataset_id,
        "derived_dataset_id": derived_dataset_id,
        "dataset_full_hash": dataset_full_hash,
        "chunks_index_sha256": chunks_index_sha256,
    }


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--repo-root", default=REPO_ROOT)
    ap.add_argument("--no-embeddings", action="store_true",
                    help="compute tiers and hashes without writing text files")
    ap.add_argument("--dataset-id", default=None,
                    help="human-assigned dataset_id override; the derived "
                         "value is still computed and reported so a "
                         "mismatch is visible")
    args = ap.parse_args(argv)

    result = run(args.repo_root, write_embeddings=not args.no_embeddings,
                 dataset_id_override=args.dataset_id)
    print(f"rows={result['rows']} qa_exceptions={result['qa']}")
    for key in sorted(result["stats"]):
        print(f"  {key}: {result['stats'][key]}")
    print(f"dataset_id={result['dataset_id']} (derived={result['derived_dataset_id']})")
    print(f"wrote {result['out_index']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
