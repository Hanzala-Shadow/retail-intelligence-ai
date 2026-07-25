"""P1 chunk-metadata enrichment for the ESG retrieval corpus.

Implements the four P1 items from the 2026-07-23 pilot audit (section 9):

  1. report_year          - real integer column, extracted once from pdf_stem
  2. company_name         - canonical name joined from the accepted-company manifest
  3. chunk_quality_tier   - narrative / layout_sensitive / noise, deterministic rule
  4. embedding_text_plain - normalized copy for embedding; source text untouched

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

# Rule-set versions. Bump when a rule changes so downstream runs stay traceable.
YEAR_RULE_VERSION = "esg_year_v1"
TIER_RULE_VERSION = "esg_tier_v1"
NORMALIZATION_VERSION = "esg_embed_norm_v1"

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

CHUNKS_INDEX = "data/00_reference/esg_chunks_index.csv"
COMPANY_MANIFEST = "data/00_reference/esg_accepted_company_manifest.csv"
COMPANIES = "data/00_reference/companies.csv"
SOURCE_REGISTRY = "data/00_reference/esg_source_registry.csv"

OUT_INDEX = "data/00_reference/esg_chunks_index_enriched.csv"
EMBED_ROOT = "data/05_embedding/esg"
QA_REPORT = "reports/esg_p1_enrichment_qa.csv"
SUMMARY_REPORT = "reports/esg_p1_enrichment_summary.md"

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
]

# ---------------------------------------------------------------------------
# 1. report_year
# ---------------------------------------------------------------------------

YEAR_MIN, YEAR_MAX = 1990, 2030

# Trailing decorations seen in real stems: "-Report", ".pdf", "(Italian)",
# "(Climate Index)". Stripped before year extraction so they cannot be mistaken
# for a year token.
_STEM_DECORATION = re.compile(r"(\([^)]*\)|\.pdf|-Report)\s*$", re.IGNORECASE)
_YEAR_TOKEN = re.compile(r"(?<!\d)((?:19|20)\d{2})(?!\d)")


def extract_report_year(pdf_stem: str):
    """Return (year_or_None, status, span) for a pdf_stem.

    Deterministic and conservative: a stem with no parseable 4-digit year in
    range yields (None, "unresolved") rather than a guess. DLTR-...-202E is the
    known real case -- "202E" is a typo, not a year, and must not become 2020.

    Multi-year stems use both orderings in this corpus -- ACI-...-2021-2022
    ascends while GES-GUESS-2021-2020 descends -- so positional rules ("take the
    last token") assign different semantics to the two forms. report_year is
    therefore defined as max(years): the latest year the document covers, which
    is order-independent. The full span is returned alongside so a question about
    an earlier covered year can still match via report_year_span.
    """
    stem = pdf_stem
    # Strip repeated trailing decorations, e.g. "...-2022.pdf" or "...(Italian)".
    for _ in range(4):
        stripped = _STEM_DECORATION.sub("", stem).strip()
        if stripped == stem:
            break
        stem = stripped

    years = sorted({int(m) for m in _YEAR_TOKEN.findall(stem)
                    if YEAR_MIN <= int(m) <= YEAR_MAX})
    if not years:
        return None, "unresolved", ""
    span = str(years[0]) if len(years) == 1 else f"{years[0]}-{years[-1]}"
    status = "parsed" if len(years) == 1 else "multi_year_range"
    return years[-1], status, span


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
# main
# ---------------------------------------------------------------------------


def run(repo_root: str, write_embeddings: bool = True) -> dict:
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

    collision = set(fieldnames) & set(NEW_COLUMNS)
    if collision:
        raise SystemExit(f"refusing to overwrite existing columns: {sorted(collision)}")

    # --- 2. company_name lookup -------------------------------------------
    manifest_name = {r["ticker"]: r["company_name"].strip() for r in manifest}
    companies_name = {r["ticker"]: r["name"].strip() for r in companies}

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
            rel_embed = os.path.join(EMBED_ROOT, ticker,
                                     os.path.basename(row["chunk_file"]))
            if write_embeddings:
                dest = join(rel_embed)
                os.makedirs(os.path.dirname(dest), exist_ok=True)
                with open(dest, "w", encoding="utf-8", newline="\n") as fh:
                    fh.write(normalized)
            new["embedding_text_plain_file"] = rel_embed.replace("\\", "/")
            new["embedding_text_plain_sha256"] = sha256_text(normalized)
            new["embedding_normalization_version"] = NORMALIZATION_VERSION
        else:
            tier, reason = "pending_text", "chunk_file_absent"
            new["embedding_text_plain_file"] = ""
            new["embedding_text_plain_sha256"] = ""
            new["embedding_normalization_version"] = ""

        new["chunk_quality_tier"] = tier
        new["chunk_quality_tier_reason"] = reason
        stats[f"tier:{tier}"] += 1
        tier_by_doc[stem][tier] += 1
        out_rows.append(new)

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
    with open(join(SUMMARY_REPORT), "w", encoding="utf-8", newline="\n") as fh:
        fh.write("\n".join(lines) + "\n")

    return {"rows": total, "stats": dict(stats), "qa": len(qa_rows),
            "out_index": out_path}


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--repo-root", default=REPO_ROOT)
    ap.add_argument("--no-embeddings", action="store_true",
                    help="compute tiers and hashes without writing text files")
    args = ap.parse_args(argv)

    result = run(args.repo_root, write_embeddings=not args.no_embeddings)
    print(f"rows={result['rows']} qa_exceptions={result['qa']}")
    for key in sorted(result["stats"]):
        print(f"  {key}: {result['stats'][key]}")
    print(f"wrote {result['out_index']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
