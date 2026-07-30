import sys
import unittest
from pathlib import Path


import config
import drive_to_db


class EsgDbIdentityTests(unittest.TestCase):
    def test_catalog_duplicates_share_version_but_keep_aliases(self):
        plan = drive_to_db.LoadPlan(companies={"AAA": {"ticker": "AAA"}})
        base = {
            "logical_source_id": "ls_" + "1" * 24,
            "source_version_id": "sv_" + "2" * 24,
            "extraction_artifact_id": "ea_" + "3" * 24,
            "canonical_ticker": "AAA",
            "observed_ticker": "AAA",
            "artifact_role": "original",
            "sha256": "a" * 64,
            "active": "true",
        }
        rows = [
            {**base, "file_alias_id": "fa_" + "4" * 24, "file_path": "a/report.pdf"},
            {**base, "file_alias_id": "fa_" + "5" * 24, "file_path": "renamed/report.pdf"},
        ]
        lookup = drive_to_db.build_identity_plan(rows, [], plan.companies, plan)
        self.assertEqual(len(plan.logical_sources), 1)
        self.assertEqual(len(plan.source_versions), 1)
        self.assertEqual(len(plan.extraction_artifacts), 1)
        self.assertEqual(len(plan.file_aliases), 2)
        self.assertEqual(
            lookup.original_by_path["renamed/report.pdf"]["source_version_id"],
            base["source_version_id"],
        )

    def test_migration_is_idempotent_and_preserves_legacy_rows(self):
        sql = (config.MIGRATIONS_DIR / "V6__Source_Identity_And_Lineage.sql").read_text(encoding="utf-8")
        for table in ("logical_sources", "source_versions", "file_aliases", "extraction_artifacts", "source_approvals"):
            self.assertIn(f"CREATE TABLE IF NOT EXISTS {table}", sql)
        self.assertIn("ON CONFLICT DO NOTHING", sql)
        self.assertNotIn("DELETE FROM documents", sql.upper())
        self.assertNotIn("DROP TABLE documents", sql.upper())
        for table in ("documents", "sections", "chunks"):
            self.assertIn(f"ALTER TABLE {table}", sql)
        self.assertIn("legacy_source_version_id", sql)
        self.assertIn("VALIDATE CONSTRAINT fk_documents_logical_version", sql)


if __name__ == "__main__":
    unittest.main()
