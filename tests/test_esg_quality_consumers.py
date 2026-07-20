from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path

import pandas as pd


REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

import esg_pipeline_qa


def load_publication_audit_module():
    path = REPO_ROOT / "scripts" / "esg_publication_quality_audit.py"
    spec = importlib.util.spec_from_file_location("esg_publication_quality_audit", path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Cannot import {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


publication_audit = load_publication_audit_module()


class ESGQualityConsumerTests(unittest.TestCase):
    def test_citation_gate_requires_semantic_v1_and_verified_status(self) -> None:
        valid = {
            "citation_ready": "true",
            "citation_validation_version": "semantic_v1",
            "citation_validation_status": "verified_exact",
        }
        self.assertTrue(esg_pipeline_qa.is_semantically_citation_ready(valid))
        self.assertTrue(publication_audit.semantically_citation_ready(valid))

        for override in [
            {"citation_ready": "false"},
            {"citation_validation_version": "legacy_v0"},
            {"citation_validation_status": "invalid_source_span"},
            {"citation_validation_status": ""},
        ]:
            row = {**valid, **override}
            self.assertFalse(esg_pipeline_qa.is_semantically_citation_ready(row))
            self.assertFalse(publication_audit.semantically_citation_ready(row))

    def test_section_instance_is_join_key_but_code_remains_distribution(self) -> None:
        sections = pd.DataFrame(
            [
                {
                    "ticker": "TEST",
                    "pdf_stem": "TEST-Report-2024",
                    "section_code": "climate",
                    "section_instance_id": "climate__0001",
                },
                {
                    "ticker": "TEST",
                    "pdf_stem": "TEST-Report-2024",
                    "section_code": "climate",
                    "section_instance_id": "climate__0002",
                },
            ]
        )
        chunks = pd.DataFrame(
            [
                {
                    "ticker": "TEST",
                    "pdf_stem": "TEST-Report-2024",
                    "section_instance_id": "climate__0001",
                },
                {
                    "ticker": "TEST",
                    "pdf_stem": "TEST-Report-2024",
                    "section_instance_id": "climate__0002",
                },
            ]
        )

        instance_counts = (
            chunks.groupby(publication_audit.SECTION_INSTANCE_KEYS)
            .size()
            .reset_index(name="chunk_rows")
        )
        joined = sections.merge(
            instance_counts,
            on=publication_audit.SECTION_INSTANCE_KEYS,
            how="left",
        )

        self.assertEqual(joined["chunk_rows"].tolist(), [1, 1])
        self.assertEqual(sections.groupby("section_code").size().to_dict(), {"climate": 2})

    def test_chunk_id_uses_source_and_section_instance(self) -> None:
        row = {
            "source_id": "TEST__Report_2024",
            "section_instance_id": "climate__0002",
            "chunk_index": 3,
            "chunk_id": "TEST__Report_2024__climate__0002__chunk_0003",
        }
        self.assertTrue(publication_audit.chunk_id_matches_provenance(row))
        self.assertFalse(
            publication_audit.chunk_id_matches_provenance(
                {**row, "section_instance_id": "climate__0001"}
            )
        )


if __name__ == "__main__":
    unittest.main()
