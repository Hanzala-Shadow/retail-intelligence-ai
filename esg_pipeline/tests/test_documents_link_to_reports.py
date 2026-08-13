"""Every ESG document must resolve to exactly one sustainability report.

The report year is the axis every ESG research query filters or groups by, and
before this link existed it was unreachable from a chunk: sustainability_reports
carries the year but keys only to companies, so documents -> companies ->
reports is a cross join. AAPL has nine documents and nine reports and that path
returned 81 rows.

These tests guard the two halves separately. The mapping tests run against the
tracker CSV and need no database; the integrity tests run against data/esg.db
and skip when it is absent, so a clean checkout without a built corpus still
passes the suite.
"""
from __future__ import annotations

import csv
import sqlite3
from pathlib import Path

import pytest

import config
from drive_to_db import build_report_year_by_filename, tracker_filename

csv.field_size_limit(2**31 - 1)


@pytest.fixture(scope="module")
def tracker_rows() -> list[dict]:
    path = Path(config.ESG_TRACKER_CSV) if hasattr(config, "ESG_TRACKER_CSV") else \
        config.REFERENCE_DIR / "sustainability_report_tracker.csv"
    if not path.exists():
        pytest.skip(f"tracker not present: {path}")
    with path.open(newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


@pytest.fixture(scope="module")
def db() -> sqlite3.Connection:
    path = Path(config.ESG_DB)
    if not path.exists():
        pytest.skip(f"no database at {path}")
    con = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    yield con
    con.close()


class TestTrackerMapping:
    def test_filename_strips_the_trailing_comment(self):
        """12 tracker rows append a note after the filename."""
        assert tracker_filename(
            "AAP-ADVANCE AUTO PARTS INC-2017-2018.pdf (filename spans 2017-2018)"
        ) == "AAP-ADVANCE AUTO PARTS INC-2017-2018.pdf"
        assert tracker_filename("GAP-GAP INC-2017.pdf") == "GAP-GAP INC-2017.pdf"
        assert tracker_filename("") is None
        assert tracker_filename(None) is None

    def test_every_tracker_row_names_a_file(self, tracker_rows):
        missing = [r["ticker"] for r in tracker_rows if not tracker_filename(r.get("notes"))]
        assert not missing, f"{len(missing)} tracker rows name no file: {missing[:5]}"

    def test_no_filename_is_claimed_by_two_years(self, tracker_rows):
        anomalies: list[str] = []
        mapping = build_report_year_by_filename(tracker_rows, anomalies)
        assert not anomalies, anomalies[:5]
        assert len(mapping) == len(tracker_rows)

    def test_multi_year_filenames_take_the_later_year(self, tracker_rows):
        """The case a naive filename parse gets wrong: 2017-2018 is 2018."""
        mapping = build_report_year_by_filename(tracker_rows, [])
        assert mapping["aap-advance auto parts inc-2017-2018.pdf"] == 2018
        assert mapping["nke-nike inc -cl b-2014-2015.pdf"] == 2015


class TestDatabaseIntegrity:
    def test_every_document_with_chunks_has_a_report(self, db):
        """The invariant that matters: anything researchers can retrieve.

        Deliberately not "every document". The loader also creates a row for
        any PDF sitting in the raw directory, even one with no parse record --
        an out-of-scope file left on disk produces a document with no tracker
        row, no report and no chunks. That is a housekeeping matter, not a
        broken link, and it is invisible to every query because the document
        contributes no retrievable text.
        """
        orphans = db.execute(
            "select d.filepath, count(c.chunk_id) from documents d "
            "join chunks c on c.doc_id = d.doc_id "
            "where d.report_id is null group by d.filepath"
        ).fetchall()
        assert not orphans, (
            f"{len(orphans)} documents have chunks but no report_id, "
            f"e.g. {[o[0] for o in orphans[:3]]}"
        )

    def test_documents_without_a_report_contribute_nothing(self, db):
        """If a document has no report it must also have no retrievable text."""
        rows = db.execute(
            "select d.filepath, d.parse_status from documents d where d.report_id is null"
        ).fetchall()
        for filepath, parse_status in rows:
            n = db.execute(
                "select count(*) from chunks c join documents d on d.doc_id = c.doc_id "
                "where d.filepath = ?", (filepath,)
            ).fetchone()[0]
            assert n == 0, f"{filepath} has no report but {n} chunks"

    def test_report_id_points_at_a_real_report(self, db):
        dangling = db.execute(
            "select count(*) from documents d "
            "left join sustainability_reports s on s.report_id = d.report_id "
            "where d.report_id is not null and s.report_id is null"
        ).fetchone()[0]
        assert dangling == 0

    def test_document_and_report_agree_on_company(self, db):
        mismatched = db.execute(
            "select count(*) from documents d "
            "join sustainability_reports s on s.report_id = d.report_id "
            "where d.company_id <> s.company_id"
        ).fetchone()[0]
        assert mismatched == 0, "a document is linked to another company's report"

    def test_the_join_resolves_to_one_row_per_document(self, db):
        """The defect this link exists to fix, stated as a test."""
        docs = db.execute(
            "select count(*) from documents where doc_type = 'sustainability'"
        ).fetchone()[0]
        joined = db.execute(
            "select count(*) from documents d "
            "join sustainability_reports s on s.report_id = d.report_id "
            "where d.doc_type = 'sustainability'"
        ).fetchone()[0]
        assert joined == docs, f"join returned {joined} rows for {docs} documents"

    def test_a_chunk_reaches_exactly_one_year(self, db):
        """End to end: the path the query cookbook depends on."""
        rows = db.execute(
            "select count(*) from chunks c "
            "join documents d on d.doc_id = c.doc_id "
            "join sustainability_reports s on s.report_id = d.report_id"
        ).fetchone()[0]
        total = db.execute("select count(*) from chunks").fetchone()[0]
        assert rows == total, f"{total - rows} chunks cannot reach a report year"
