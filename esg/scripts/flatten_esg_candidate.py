#!/usr/bin/env python3
"""Create a path-independent ESG baseline from a complete candidate snapshot."""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import shutil
from datetime import date
from pathlib import Path


EVIDENCE_FILES = (
    "AMZN_TABLE_BOUNDARY_REVIEW.md",
    "LOW_OPERATIONAL_EXCELLENCE_FINAL_REVIEW.md",
    "RECONSTRUCTED_TABLE_REVIEW.md",
    "BBWI_COMPACT_TOC_FINAL_REVIEW.md",
    "BEFORE_AFTER_BBWI_COMPARISON.md",
    "BEFORE_AFTER_LOW_COMPARISON.md",
    "FULL_QA_RESULTS.md",
    "candidate_validation.json",
    "manual_source_page_review.json",
    "MANUAL_SOURCE_PAGE_REVIEW.md",
    "final_focused_tests.txt",
    "final_full_tests.txt",
    "final_tier_qa.txt",
)

SUPPORT_FILES = (
    "clone_flattened_baseline.py",
    "verify_flattened_baseline.py",
    "rebuild_baseline.ps1",
    "README.md",
    "ESG_REPAIR_PROGRESS_2026-08-01.md",
)


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def read_rows(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if reader.fieldnames is None:
            raise ValueError(f"CSV has no header: {path}")
        return list(reader.fieldnames), list(reader)


def write_rows(path: Path, fields: list[str], rows: list[dict[str, str]]) -> None:
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, lineterminator="\n")
        writer.writeheader()
        writer.writerows(rows)


def local_path(root_relative: str, kind: str, ticker: str, old_path: str) -> str:
    name = Path(old_path.replace("\\", "/")).name
    if not name:
        raise ValueError(f"Missing file name in {kind} path: {old_path!r}")
    return f"{root_relative}/{kind}/{ticker}/{name}"


def copy_evidence(source: Path, output: Path) -> None:
    evidence_out = output / "evidence"
    source_evidence = source / "evidence"
    if source_evidence.is_dir():
        shutil.copytree(source_evidence, evidence_out)
    else:
        evidence_out.mkdir()
        for name in EVIDENCE_FILES:
            item = source / name
            if item.is_file():
                shutil.copy2(item, evidence_out / name)

        tier_qa = source / "final_qa" / "tier_qa"
        for name in ("qa_report.md", "qa_report.json", "manual_section_quality_review.csv"):
            item = tier_qa / name
            if item.is_file():
                shutil.copy2(item, evidence_out / name)

    manual_source = source / "manual_pdf_review"
    if manual_source.is_dir():
        shutil.copytree(manual_source, output / "manual_pdf_review")


def make_manifest(output: Path) -> tuple[int, int]:
    manifest_path = output / "content_manifest.csv"
    file_count = 0
    byte_count = 0
    with manifest_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle, lineterminator="\n")
        writer.writerow(("relative_path", "size_bytes", "sha256"))
        for kind in ("sections", "chunks"):
            for path in sorted((output / kind).rglob("*.txt")):
                relative = path.relative_to(output).as_posix()
                size = path.stat().st_size
                writer.writerow((relative, size, sha256(path)))
                file_count += 1
                byte_count += size
    return file_count, byte_count


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--source-root", required=True, type=Path)
    parser.add_argument("--output-root", required=True, type=Path)
    args = parser.parse_args()

    source = args.source_root.resolve()
    output = args.output_root.resolve()
    repo = Path.cwd().resolve()
    reports = (repo / "reports").resolve()

    if not source.is_dir():
        raise FileNotFoundError(f"Source candidate does not exist: {source}")
    if output == source:
        raise ValueError("Output must differ from the source candidate")
    if reports not in output.parents:
        raise ValueError(f"Output must be below reports/: {output}")
    if output.exists():
        raise FileExistsError(f"Refusing to overwrite existing output: {output}")

    required = (
        source / "sections",
        source / "chunks",
        source / "esg_sections_index.csv",
        source / "esg_chunks_index.csv",
        source / "esg_section_hold.csv",
    )
    missing = [str(path) for path in required if not path.exists()]
    if missing:
        raise FileNotFoundError(f"Source is incomplete: {missing}")

    output.mkdir(parents=True)
    shutil.copytree(source / "sections", output / "sections")
    shutil.copytree(source / "chunks", output / "chunks")
    shutil.copy2(source / "esg_section_hold.csv", output / "esg_section_hold.csv")
    copy_evidence(source, output)
    for name in SUPPORT_FILES:
        item = source / name
        if item.is_file():
            shutil.copy2(item, output / name)

    output_relative = output.relative_to(repo).as_posix()
    section_fields, sections = read_rows(source / "esg_sections_index.csv")
    for row in sections:
        row["section_file"] = local_path(
            output_relative, "sections", row["ticker"], row["section_file"]
        )
    write_rows(output / "esg_sections_index.csv", section_fields, sections)

    chunk_fields, chunks = read_rows(source / "esg_chunks_index.csv")
    for row in chunks:
        row["chunk_file"] = local_path(
            output_relative, "chunks", row["ticker"], row["chunk_file"]
        )
        row["source_section_file"] = local_path(
            output_relative,
            "sections",
            row["ticker"],
            row["source_section_file"],
        )
    write_rows(output / "esg_chunks_index.csv", chunk_fields, chunks)

    _, holds = read_rows(output / "esg_section_hold.csv")
    hold_keys = {
        (row["ticker"], row["pdf_stem"], row["section_instance_id"])
        for row in holds
    }
    held_chunks = [
        row
        for row in chunks
        if (row["ticker"], row["pdf_stem"], row["section_instance_id"])
        in hold_keys
    ]
    held_eligible = [
        row
        for row in held_chunks
        if row["include_in_esg_index"].strip().lower() in {"1", "true", "yes"}
    ]
    if held_eligible:
        raise ValueError(f"Held chunks remain eligible: {len(held_eligible)}")

    for row in sections:
        if not (repo / row["section_file"]).is_file():
            raise FileNotFoundError(row["section_file"])
    for row in chunks:
        if not (repo / row["chunk_file"]).is_file():
            raise FileNotFoundError(row["chunk_file"])
        if not (repo / row["source_section_file"]).is_file():
            raise FileNotFoundError(row["source_section_file"])

    manifest_files, manifest_bytes = make_manifest(output)
    source_validation = source / "candidate_validation.json"
    if not source_validation.is_file():
        source_validation = source / "evidence" / "candidate_validation.json"
    validation = json.loads(source_validation.read_text(encoding="utf-8-sig"))
    tier_totals = validation.get("tier_qa", {}).get("status_totals", {})

    build_info = {
        "status": "PASS",
        "created_on": date.today().isoformat(),
        "purpose": "path-independent flattened baseline for later isolated repairs",
        "source_candidate": source.relative_to(repo).as_posix(),
        "baseline_root": output_relative,
        "counts": {
            "sections": len(sections),
            "chunks": len(chunks),
            "eligible_chunks": sum(
                row["include_in_esg_index"].strip().lower() in {"1", "true", "yes"}
                for row in chunks
            ),
            "held_sections": len(holds),
            "held_chunks": len(held_chunks),
            "held_eligible_chunks": len(held_eligible),
            "manifest_files": manifest_files,
            "manifest_bytes": manifest_bytes,
            "max_tokens": max(int(row["token_count"]) for row in chunks),
        },
        "holds": sorted(row["ticker"] for row in holds),
        "tier_qa": tier_totals,
        "source_index_sha256": {
            "sections": sha256(source / "esg_sections_index.csv"),
            "chunks": sha256(source / "esg_chunks_index.csv"),
        },
        "flattened_index_sha256": {
            "sections": sha256(output / "esg_sections_index.csv"),
            "chunks": sha256(output / "esg_chunks_index.csv"),
        },
        "safety": {
            "promotion_performed": False,
            "embeddings_built": False,
            "vector_index_touched": False,
            "live_data_touched": False,
        },
    }
    (output / "build_info.json").write_text(
        json.dumps(build_info, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(build_info, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
