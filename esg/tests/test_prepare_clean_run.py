"""Guards on the clean-run reset.

The script deletes files. The risk is not that it fails loudly -- it is that a
hand-curated file quietly ends up on the DERIVED list, or that a new pipeline
output is never classified and so survives a "clean" run and poisons the next
one. These tests pin both directions.
"""

from __future__ import annotations

import os
import subprocess
import sys

import pytest

import config
import prepare_clean_run as pcr

REPO_ROOT = config.REPO_ROOT

# The synthetic trees these tests build must mirror the real layout, so the
# relative form comes from config rather than being spelled out again here.
REF_REL = config.as_repo_relative(config.REFERENCE_DIR).as_posix()


def test_derived_and_curated_are_disjoint() -> None:
    """A file cannot be both regenerable and hand-authored."""
    for a, b in (("DERIVED", "CURATED"), ("DERIVED", "PRESERVED_OTHER"),
                 ("CURATED", "PRESERVED_OTHER")):
        overlap = set(getattr(pcr, a)) & set(getattr(pcr, b))
        assert not overlap, f"classified as both {a} and {b}: {sorted(overlap)}"


def test_the_source_registry_is_never_derived() -> None:
    """src/esg_p1_enrichment.py refuses to run without it and cannot rebuild it.

    Named explicitly rather than left to the disjointness test, because this is
    the one file whose loss cannot be undone by re-running anything.
    """
    assert "esg_source_registry.csv" in pcr.CURATED
    assert "esg_source_registry.csv" not in pcr.DERIVED


@pytest.mark.parametrize("name", [
    "esg_layout_gold_labels.csv",
    "esg_layout_gold_annotator1.csv",
    "esg_layout_gold_annotator2.csv",
    "esg_layout_gold_disagreements.csv",
    "esg_lines_area_signals.csv",
    "esg_manual_headings_clean.csv",
    "esg_parser_overrides.csv",
    "companies.csv",
])
def test_research_assets_are_curated(name: str) -> None:
    """Independently produced assets. No pipeline stage can recreate them."""
    assert name in pcr.CURATED, f"{name} must be preserved by a clean run"
    assert name not in pcr.DERIVED


def test_every_upserting_index_is_cleared() -> None:
    """The whole point: an index that merges must be cleared or it keeps ghosts."""
    for name in ("esg_parse_index.csv", "esg_sections_index.csv",
                 "esg_chunks_index.csv", "esg_page_layout_qa.csv"):
        assert name in pcr.DERIVED, (
            f"{name} upserts on reparse; leaving it in place preserves rows for "
            f"documents that no longer exist"
        )


def test_dry_run_is_the_default_and_deletes_nothing(tmp_path, monkeypatch) -> None:
    ref = tmp_path / REF_REL
    ref.mkdir(parents=True)
    for name in list(pcr.CURATED) + list(pcr.DERIVED):
        (ref / name).write_text("a,b\n1,2\n", encoding="utf-8")

    monkeypatch.setattr(pcr, "ROOT", tmp_path)
    monkeypatch.setattr(pcr, "REF", ref)
    assert pcr.main([]) == 0
    for name in pcr.DERIVED:
        assert (ref / name).exists(), f"dry run deleted {name}"


def test_aborts_when_a_curated_file_is_missing(tmp_path, monkeypatch, capsys) -> None:
    """A damaged tree must not be damaged further."""
    ref = tmp_path / REF_REL
    ref.mkdir(parents=True)
    for name in list(pcr.CURATED) + list(pcr.DERIVED):
        (ref / name).write_text("a,b\n1,2\n", encoding="utf-8")
    (ref / "esg_source_registry.csv").unlink()

    monkeypatch.setattr(pcr, "ROOT", tmp_path)
    monkeypatch.setattr(pcr, "REF", ref)
    assert pcr.main(["--execute"]) == 2
    for name in pcr.DERIVED:
        assert (ref / name).exists(), "aborted run still deleted something"


def test_unrecognised_files_are_preserved(tmp_path, monkeypatch, capsys) -> None:
    """A file in neither list survives -- silence must never mean deletion."""
    ref = tmp_path / REF_REL
    ref.mkdir(parents=True)
    for name in list(pcr.CURATED) + list(pcr.DERIVED):
        (ref / name).write_text("a,b\n1,2\n", encoding="utf-8")
    (ref / "something_new_nobody_classified.csv").write_text("x\n", encoding="utf-8")

    monkeypatch.setattr(pcr, "ROOT", tmp_path)
    monkeypatch.setattr(pcr, "REF", ref)
    assert pcr.main(["--execute", "--no-backup"]) == 0
    assert (ref / "something_new_nobody_classified.csv").exists()
    assert "something_new_nobody_classified.csv" in capsys.readouterr().out


def test_execute_clears_derived_and_keeps_curated(tmp_path, monkeypatch) -> None:
    ref = tmp_path / REF_REL
    ref.mkdir(parents=True)
    for name in list(pcr.CURATED) + list(pcr.DERIVED):
        (ref / name).write_text("a,b\n1,2\n", encoding="utf-8")

    monkeypatch.setattr(pcr, "ROOT", tmp_path)
    monkeypatch.setattr(pcr, "REF", ref)
    assert pcr.main(["--execute"]) == 0

    for name in pcr.DERIVED:
        assert not (ref / name).exists(), f"{name} should have been cleared"
    for name in pcr.CURATED:
        assert (ref / name).exists(), f"{name} must survive"

    backups = list((tmp_path / "backups").glob("clean_run_*"))
    assert len(backups) == 1
    assert (backups[0] / "RUN_STATE.json").exists()
    assert (tmp_path / "data/CLEAN_RUN_STATE.json").exists()


def test_live_tree_has_no_unclassified_pipeline_output() -> None:
    """Every file in the real reference directory must be classified.

    This is the test that stops the design rotting: add a new pipeline output
    and forget to classify it, and this fails rather than letting it silently
    survive the next clean run.
    """
    ref = config.REFERENCE_DIR
    if not ref.is_dir():
        pytest.skip("no reference tree in this checkout")
    known = set(pcr.DERIVED) | set(pcr.CURATED) | set(pcr.PRESERVED_OTHER)
    unknown = sorted(p.name for p in ref.iterdir() if p.is_file() and p.name not in known)
    # Files here are preserved, so this is a review prompt, not a safety hole.
    assert not unknown, (
        "unclassified files in data/00_reference -- add each to DERIVED (pipeline "
        f"output), CURATED (hand-authored, must exist) or PRESERVED_OTHER "
        f"(out of scope) in scripts/prepare_clean_run.py: {unknown}"
    )
