"""Test the ESG corpus against the ESG Chunk Management Contract.

The contract ships with a verified 100-chunk reference sample. This script
derives its checks from that package rather than from prose:

* `sampling_manifest.json` records six integrity counters the package authors
  ran and reported as zero. Those are reproduced exactly.
* `field_dictionary.csv` marks which fields are `Required` for ESG. Coverage is
  driven by reading that file, not by a hardcoded list.
* The 100 JSONL rows define the `embedding_text` header shape empirically.

The checker runs against BOTH corpora. The reference sample is known-good, so
it acts as the calibration: any check that fails on the reference is a bug in
the checker, not a finding about the ESG corpus.

Usage:
    python scripts/check_esg_contract_conformance.py
    python scripts/check_esg_contract_conformance.py --sample 500
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PKG = ROOT / "esg_chunk_handoff_100_20260728/esg_chunk_handoff_100_20260728"

REF_JSONL = PKG / "representative_100_chunks.jsonl"
REF_FIELD_DICT = PKG / "field_dictionary.csv"
REF_MANIFEST = PKG / "sampling_manifest.json"

ESG_CHUNKS = ROOT / "data/00_reference/esg_chunks_index_enriched.csv"
ESG_CONTEXT = ROOT / "data/00_reference/esg_chunk_embedding_context.csv"

OUT_REPORT = ROOT / "reports/esg_contract_conformance.md"

csv.field_size_limit(10_000_000)

# How each contract field is satisfied on the ESG side. `None` means the
# contract requires it and the ESG pipeline has no equivalent yet.
ESG_FIELD_MAP = {
    "source_chunk_id": "chunk_id",
    "coverage_year": "report_year",
    "doc_type": "doc_type",
    "section_code": "section_code",
    "chunk_index": "chunk_index",
    "chunk_text": "chunk_file",
    "embedding_text": "embedding_text_ctx_file",
    "token_count": "token_count",
    "doc_quality_status": "doc_quality_status",
    "rag_action": "rag_action",
    "citation_ready": "citation_ready",
    "quality_flags": "quality_flags",
    "chunk_text_sha256": "chunk_text_sha256",
    "embedding_text_sha256": "embedding_text_ctx_sha256",
    "dataset_id": None,
    "ticker": "ticker",
    "chunk_id": "chunk_id",
    "doc_id": "source_id",
    "company_id": None,
    "section_id": "section_instance_id",
}

MANDATORY_HEADER_KEYS_REF = [
    "Company", "Ticker", "Document", "Fiscal year", "SEC section", "Subsection", "Content type",
]
MANDATORY_HEADER_KEYS_ESG = [
    "Company", "Ticker", "Document", "Reporting year", "ESG topic", "Subsection", "Content type",
]

# Token bounds are an ESG-PIPELINE invariant, not a contract requirement. The
# contract only says "Record token count using the tokenizer associated with the
# embedding model" and never fixes a range. The reference sample's own chunks run
# 51-400 tokens, so applying the ESG 100-600 window to them is meaningless.
# Passing token_bounds=None skips the check for that corpus.
ESG_TOKEN_BOUNDS = (100, 600)
SHORT_EVIDENCE_MIN_TOKENS = 25


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class Result:
    def __init__(self, corpus: str, rows: int):
        self.corpus = corpus
        self.rows = rows
        self.checks: list[tuple[str, int, str]] = []

    def add(self, name: str, failures: int | None, note: str = "") -> None:
        self.checks.append((name, failures, note))

    @property
    def failed(self) -> int:
        return sum(1 for _, f, _ in self.checks if f)


def check_records(
    corpus: str,
    records: list[dict],
    header_keys: list[str],
    token_bounds: tuple[int, int] | None,
) -> Result:
    """records: {source_chunk_id, chunk_id, chunk_text, embedding_text,
    chunk_text_sha256, embedding_text_sha256, token_count, chunk_type,
    quality_flags, citation_ready, rag_action}"""
    res = Result(corpus, len(records))

    # --- the six integrity counters from sampling_manifest.json ---
    res.add("missing_source_chunk_id", sum(1 for r in records if not r.get("source_chunk_id")))
    ids = [r.get("source_chunk_id") for r in records if r.get("source_chunk_id")]
    res.add("duplicate_source_chunk_id", len(ids) - len(set(ids)))
    cids = [r.get("chunk_id") for r in records if r.get("chunk_id")]
    res.add("duplicate_chunk_id", len(cids) - len(set(cids)))
    res.add("empty_chunk_text", sum(1 for r in records if not (r.get("chunk_text") or "").strip()))
    res.add(
        "chunk_text_sha256_failures",
        sum(1 for r in records if r.get("chunk_text") is not None
            and sha256_text(r["chunk_text"]) != r.get("chunk_text_sha256")),
    )
    res.add(
        "embedding_text_sha256_failures",
        sum(1 for r in records if r.get("embedding_text") is not None
            and sha256_text(r["embedding_text"]) != r.get("embedding_text_sha256")),
    )

    # --- structural properties observed on all 100 reference rows ---
    bad_sep = bad_keys = bad_body = 0
    for r in records:
        et, ct = r.get("embedding_text"), r.get("chunk_text")
        if et is None or ct is None:
            continue
        head, sep, body = et.partition("\n\n")
        if sep != "\n\n":
            bad_sep += 1
            continue
        keys = [l.split(": ", 1)[0] for l in head.split("\n") if ": " in l]
        if keys[: len(header_keys)] != header_keys:
            bad_keys += 1
        if body != ct:
            bad_body += 1
    res.add("embedding_text_missing_blank_line_separator", bad_sep)
    res.add("header_missing_mandatory_keys_in_order", bad_keys, " | ".join(header_keys))
    res.add("embedding_text_minus_header_ne_chunk_text", bad_body)

    # --- contract quality / eligibility rules ---
    res.add("quality_flags_blank_not_empty_collection",
            sum(1 for r in records if r.get("quality_flags") is None or r.get("quality_flags") == ""))
    if token_bounds is None:
        res.add("token_count_within_pipeline_bounds", None, "n/a — pipeline-local, not a contract rule")
    else:
        lo, hi = token_bounds
        bad_tok = 0
        for r in records:
            try:
                tok = int(r.get("token_count") or 0)
            except (TypeError, ValueError):
                bad_tok += 1
                continue
            floor = SHORT_EVIDENCE_MIN_TOKENS if r.get("chunk_type") == "short_evidence" else lo
            if not (floor <= tok <= hi):
                bad_tok += 1
        res.add(f"token_count_outside_[{lo}..{hi}]", bad_tok,
                f"short_evidence floor {SHORT_EVIDENCE_MIN_TOKENS}")
    res.add("citation_ready_not_boolean",
            sum(1 for r in records if str(r.get("citation_ready")).lower() not in ("true", "false")))
    res.add("rag_action_empty", sum(1 for r in records if not (r.get("rag_action") or "").strip()))
    return res


def load_reference() -> list[dict]:
    out = []
    for line in REF_JSONL.open(encoding="utf-8"):
        r = json.loads(line)
        out.append({
            "source_chunk_id": r.get("source_chunk_id"),
            "chunk_id": r.get("chunk_id"),
            "chunk_text": r.get("chunk_text"),
            "embedding_text": r.get("embedding_text"),
            "chunk_text_sha256": r.get("chunk_text_sha256"),
            "embedding_text_sha256": r.get("embedding_text_sha256"),
            "token_count": r.get("token_count"),
            "chunk_type": "normal",
            "quality_flags": r.get("quality_flags"),
            "citation_ready": r.get("citation_ready"),
            "rag_action": r.get("rag_action"),
        })
    return out


def load_esg(sample: int) -> list[dict]:
    with ESG_CHUNKS.open(encoding="utf-8", errors="replace", newline="") as fh:
        base = {r["chunk_id"]: r for r in csv.DictReader(fh)}
    with ESG_CONTEXT.open(encoding="utf-8", errors="replace", newline="") as fh:
        ctx = list(csv.DictReader(fh))
    if sample:
        ctx = ctx[:: max(len(ctx) // sample, 1)][:sample]

    out = []
    for c in ctx:
        b = base.get(c["chunk_id"])
        if b is None:
            continue
        try:
            chunk_text = (ROOT / b["chunk_file"]).read_text(encoding="utf-8")
        except OSError:
            chunk_text = None
        try:
            emb_text = (ROOT / c["embedding_text_ctx_file"]).read_text(encoding="utf-8")
        except OSError:
            emb_text = None
        out.append({
            "source_chunk_id": c["chunk_id"],
            "chunk_id": c["chunk_id"],
            "chunk_text": chunk_text,
            "embedding_text": emb_text,
            "chunk_text_sha256": c.get("chunk_text_sha256"),
            "embedding_text_sha256": c.get("embedding_text_ctx_sha256"),
            "token_count": b.get("token_count"),
            "chunk_type": b.get("chunk_type"),
            "quality_flags": b.get("quality_flags"),
            "citation_ready": b.get("citation_ready"),
            "rag_action": b.get("rag_action"),
        })
    return out


def field_coverage() -> list[tuple[str, str, str, str]]:
    """(field, esg_requirement, mapped_to, status) for contract-required fields."""
    with ESG_CONTEXT.open(encoding="utf-8", errors="replace", newline="") as fh:
        ctx_cols = set(next(csv.reader(fh)))
    with ESG_CHUNKS.open(encoding="utf-8", errors="replace", newline="") as fh:
        base_cols = set(next(csv.reader(fh)))
    have = ctx_cols | base_cols

    rows = []
    with REF_FIELD_DICT.open(encoding="utf-8", newline="") as fh:
        for r in csv.DictReader(fh):
            req = r["esg_requirement"]
            if not req.lower().startswith("required"):
                continue
            field = r["field"]
            mapped = ESG_FIELD_MAP.get(field, "")
            if mapped is None:
                status = "MISSING"
                mapped = "-"
            elif mapped in have:
                status = "ok"
            else:
                status = "MISSING"
                mapped = mapped or "-"
            rows.append((field, req, mapped, status))
    return rows


def render(res: Result) -> list[str]:
    lines = [f"### {res.corpus} — {res.rows} chunks", "", "| check | failures |", "|---|---:|"]
    for name, fails, note in res.checks:
        label = f"`{name}`" + (f" — {note}" if note else "")
        if fails is None:
            cell = "n/a"
        elif fails == 0:
            cell = "0"
        else:
            cell = f"**{fails}**"
        lines.append(f"| {label} | {cell} |")
    return lines + [""]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--sample", type=int, default=0, help="check only N ESG chunks (evenly spaced)")
    args = ap.parse_args(argv)

    for p in (REF_JSONL, REF_FIELD_DICT, ESG_CHUNKS, ESG_CONTEXT):
        if not p.exists():
            print(f"missing input: {p}", file=sys.stderr)
            return 2

    ref = check_records(
        "Reference sample (contract's own 100)", load_reference(), MANDATORY_HEADER_KEYS_REF, None
    )
    esg = check_records(
        f"ESG corpus{' (sampled)' if args.sample else ''}",
        load_esg(args.sample),
        MANDATORY_HEADER_KEYS_ESG,
        ESG_TOKEN_BOUNDS,
    )

    declared = json.loads(REF_MANIFEST.read_text(encoding="utf-8"))["integrity"]
    recomputed = {n: f for n, f, _ in ref.checks if n in declared}
    manifest_agrees = all(declared[k] == recomputed.get(k) for k in declared)

    cov = field_coverage()
    missing = [c for c in cov if c[3] == "MISSING"]

    lines = [
        "# ESG contract conformance",
        "",
        "Checks derived from `esg_chunk_handoff_100_20260728/`: the six integrity",
        "counters in `sampling_manifest.json`, the `Required` rows of",
        "`field_dictionary.csv`, and the header shape observed on all 100 reference",
        "chunks. The reference sample is run first as calibration — it is known-good,",
        "so a failure there would mean the checker is wrong.",
        "",
        "## Calibration",
        "",
        f"- Recomputed integrity counters match `sampling_manifest.json`: **{manifest_agrees}**",
        f"- Reference checks failing: **{ref.failed}** (expected 0)",
        "",
        "## Integrity and structure",
        "",
    ]
    lines += render(ref)
    lines += render(esg)
    lines += [
        "## Required-field coverage",
        "",
        "Driven by the `esg_requirement` column of `field_dictionary.csv`.",
        "",
        "| contract field | requirement | ESG field | status |",
        "|---|---|---|---|",
    ]
    for field, req, mapped, status in cov:
        mark = "ok" if status == "ok" else "**MISSING**"
        lines.append(f"| `{field}` | {req} | `{mapped}` | {mark} |")
    lines += ["", f"Required fields present: **{len(cov) - len(missing)}/{len(cov)}**", ""]

    report = "\n".join(lines) + "\n"
    OUT_REPORT.parent.mkdir(parents=True, exist_ok=True)
    OUT_REPORT.write_text(report, encoding="utf-8")
    print(report)

    if ref.failed:
        print("CALIBRATION FAILED: checker disagrees with the known-good reference", file=sys.stderr)
        return 2
    return 1 if (esg.failed or missing) else 0


if __name__ == "__main__":
    raise SystemExit(main())
