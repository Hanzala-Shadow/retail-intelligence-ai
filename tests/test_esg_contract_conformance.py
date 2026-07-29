"""Calibration tests for scripts/check_esg_contract_conformance.py.

The conformance checker is only trustworthy if it passes the contract's own
verified 100-chunk sample. These tests run that calibration and assert the
recomputed integrity counters agree with `sampling_manifest.json`.

The full ESG corpus run reads ~33k files and is deliberately not exercised
here; run the script directly for that.
"""

from __future__ import annotations

import importlib.util
import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts/check_esg_contract_conformance.py"

_spec = importlib.util.spec_from_file_location("check_esg_contract_conformance", SCRIPT)
mod = importlib.util.module_from_spec(_spec)
assert _spec.loader is not None
_spec.loader.exec_module(mod)


@unittest.skipUnless(mod.REF_JSONL.exists(), "reference handoff package not present")
class ReferenceCalibrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.records = mod.load_reference()
        cls.result = mod.check_records(
            "reference", cls.records, mod.MANDATORY_HEADER_KEYS_REF, None
        )

    def test_reference_sample_has_100_rows(self):
        self.assertEqual(len(self.records), 100)

    def test_checker_passes_the_known_good_reference(self):
        failing = [(n, f) for n, f, _ in self.result.checks if f]
        self.assertEqual(failing, [], f"checker is wrong, not the corpus: {failing}")

    def test_recomputed_counters_match_the_shipped_manifest(self):
        declared = json.loads(mod.REF_MANIFEST.read_text(encoding="utf-8"))["integrity"]
        recomputed = {n: f for n, f, _ in self.result.checks if n in declared}
        self.assertEqual(recomputed, declared)

    def test_every_reference_row_carries_the_seven_header_keys(self):
        for r in self.records:
            head, sep, body = r["embedding_text"].partition("\n\n")
            self.assertEqual(sep, "\n\n")
            keys = [l.split(": ", 1)[0] for l in head.split("\n") if ": " in l]
            self.assertEqual(keys[:7], mod.MANDATORY_HEADER_KEYS_REF)
            self.assertEqual(body, r["chunk_text"])

    def test_token_bounds_are_not_treated_as_a_contract_rule(self):
        """The reference runs 51-400 tokens; the ESG window is 100-600.

        Applying one corpus's chunker bounds to the other is a category error,
        so the check must report n/a rather than a failure count.
        """
        tokens = [r["token_count"] for r in self.records]
        self.assertLess(min(tokens), 100)
        entry = [c for c in self.result.checks if c[0] == "token_count_within_pipeline_bounds"]
        self.assertEqual(len(entry), 1)
        self.assertIsNone(entry[0][1])


@unittest.skipUnless(mod.REF_FIELD_DICT.exists(), "reference handoff package not present")
class FieldMapTests(unittest.TestCase):
    def test_every_required_contract_field_is_mapped_or_explicitly_missing(self):
        import csv

        with mod.REF_FIELD_DICT.open(encoding="utf-8", newline="") as fh:
            required = [r["field"] for r in csv.DictReader(fh)
                        if r["esg_requirement"].lower().startswith("required")]
        unmapped = [f for f in required if f not in mod.ESG_FIELD_MAP]
        self.assertEqual(unmapped, [], f"unmapped required contract fields: {unmapped}")

    def test_known_gaps_are_declared_as_none(self):
        self.assertIsNone(mod.ESG_FIELD_MAP["dataset_id"])
        self.assertIsNone(mod.ESG_FIELD_MAP["company_id"])


if __name__ == "__main__":
    unittest.main()
