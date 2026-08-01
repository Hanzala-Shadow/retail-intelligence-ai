"""Tests for independent recovery-review sample selection."""

from __future__ import annotations

import importlib.util
import unittest
from pathlib import Path
from unittest.mock import patch

import config


SCRIPT = Path(config.REPO_ROOT) / "esg/scripts/prepare_esg_recovery_terra_sample.py"
SPEC = importlib.util.spec_from_file_location("prepare_esg_recovery_terra_sample", SCRIPT)
assert SPEC and SPEC.loader
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def _row(ticker: str, score: int) -> dict[str, str]:
    return {
        "ticker": ticker,
        "pdf_file": f"{ticker}-REPORT.pdf",
        "pdf_stem": f"{ticker}-REPORT",
        "page": "1",
        "decision": "auto_pass_recovered_region_order",
        "recovery_parser": "current",
        "recovery_metrics": (
            f"candidate_tokens=100;column_inversions={score};"
            f"segments_consumed={score};tokens_unmatched=0;candidates_passing=1"
        ),
        "mixed_column_lines": str(score),
        "visual_object_count": str(score),
        "native_word_count": str(score),
    }


class RecoverySampleSelectionTests(unittest.TestCase):
    def test_explicitly_excluded_issuer_is_not_selected(self) -> None:
        rows = [_row("UEIC", 99)] + [
            _row(f"T{index}", index) for index in range(1, 9)
        ]
        with (
            patch.object(MODULE, "old_gold_keys", return_value=set()),
            patch.object(MODULE, "reviewed_keys", return_value=set()),
        ):
            selected = MODULE.select_sample(
                rows,
                sample_size=6,
                seed=2026073102,
                excluded_issuers={"ueic"},
            )

        self.assertEqual(6, len(selected))
        self.assertNotIn("UEIC", {row["ticker"] for row in selected})
        self.assertEqual(6, len({row["ticker"] for row in selected}))


if __name__ == "__main__":
    unittest.main()
