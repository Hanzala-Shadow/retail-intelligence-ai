from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[1]
MIGRATIONS = ROOT / "data" / "05_db" / "migrations"


class ESGMigrationContractTests(unittest.TestCase):
    def test_phase3_migration_versions_are_unique_and_contiguous(self):
        versions = []
        for path in MIGRATIONS.glob("V*.sql"):
            match = re.match(r"V(\d+)__", path.name)
            if match:
                versions.append(int(match.group(1)))

        self.assertEqual(sorted(versions), list(range(1, 15)))
        self.assertEqual(len(versions), len(set(versions)))

    def test_esg_provenance_migration_preserves_nullable_page_contract(self):
        sql = (MIGRATIONS / "V13__ESG_Provenance.sql").read_text(encoding="utf-8")

        for column in (
            "section_instance_id",
            "external_chunk_id",
            "source_id",
            "source_version_id",
            "citation_validation_status",
            "citation_validation_version",
        ):
            self.assertIn(column, sql)

        self.assertIn("UNIQUE (doc_id, section_instance_id)", sql)
        self.assertNotRegex(sql, r"page_(?:start|end)\s+SET\s+NOT\s+NULL")

    def test_short_evidence_migration_records_policy_metadata(self):
        sql = (MIGRATIONS / "V14__ESG_Short_Evidence_Chunks.sql").read_text(
            encoding="utf-8"
        )

        for column in (
            "chunk_type",
            "short_section_action",
            "short_section_reason",
            "merged_section_ids",
        ):
            self.assertIn(column, sql)


if __name__ == "__main__":
    unittest.main()
