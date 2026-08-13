from __future__ import annotations

import argparse
import csv
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from esg.scripts import bridge_docling_to_pipeline as bridge
from esg.scripts import run_docling_gold_spike as spike


def _fuse_args(root: Path, layout: Path, fused: Path, pdf_dir: Path, **overrides):
    args = argparse.Namespace(
        layout_dir=layout,
        fused_dir=fused,
        fused_summary=root / "fused_summary.json",
        work_dir=root,
        items="",
        limit=0,
        pdf_dir=pdf_dir,
        force=False,
        snap=12.0,
        table_mode="grid",
        table_assign="cell",
    )
    for key, value in overrides.items():
        setattr(args, key, value)
    return args


class _FuseFixture:
    """A layout cache, one fused page, and a stub PDF, in a temp tree."""

    def __init__(self, root: Path, telemetry: bool = True):
        self.root = root
        self.layout = root / "layout"
        self.fused = root / "fused"
        self.pdf_dir = root / "pdfs"
        for directory in (self.layout, self.fused, self.pdf_dir):
            directory.mkdir()
        (self.pdf_dir / "TEST.pdf").write_bytes(b"not opened when reused")
        (self.layout / "TEST.pages.json").write_text(
            json.dumps({"pdf_file": "TEST.pdf", "pdf_stem": "TEST", "pages": {"1": []}}),
            encoding="utf-8",
        )
        self.page = self.fused / "TEST_p1.txt"
        self.page.write_text("already fused", encoding="utf-8")
        entry = {"placed_words": 2, "unplaced_words": 0, "total_words": 2} if telemetry else {"reused": True}
        (root / "fused_summary.json").write_text(
            json.dumps({"TEST_p1": entry}), encoding="utf-8"
        )

    def record_settings(self, **overrides):
        settings = {"table_mode": "grid", "table_assign": "cell", "snap": 12.0}
        settings.update(overrides)
        (self.root / "fused_summary.settings.json").write_text(
            json.dumps(settings), encoding="utf-8"
        )

    def args(self, **overrides):
        return _fuse_args(self.root, self.layout, self.fused, self.pdf_dir, **overrides)


class FusionContinuationTests(unittest.TestCase):
    def test_fuse_reuses_a_complete_page_file(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            fixture = _FuseFixture(Path(temp_dir))
            fixture.record_settings()

            with mock.patch.object(spike, "fuse_page") as fuse_page:
                self.assertEqual(spike.stage_fuse(fixture.args()), 0)
            fuse_page.assert_not_called()
            self.assertEqual(fixture.page.read_text(encoding="utf-8"), "already fused")

    def test_first_run_adopts_pages_fused_before_settings_were_recorded(self):
        """A corpus fused by an older build must not be thrown away."""
        with tempfile.TemporaryDirectory() as temp_dir:
            fixture = _FuseFixture(Path(temp_dir))  # no settings file written

            with mock.patch.object(spike, "fuse_page") as fuse_page:
                self.assertEqual(spike.stage_fuse(fixture.args()), 0)
            fuse_page.assert_not_called()
            recorded = json.loads(
                (fixture.root / "fused_summary.settings.json").read_text(encoding="utf-8")
            )
            self.assertEqual(recorded["table_mode"], "grid")

    def test_changed_table_mode_refuses_to_reuse_pages(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            fixture = _FuseFixture(Path(temp_dir))
            fixture.record_settings(table_mode="words")

            fused = {
                "fused_text": "refused",
                "region_count": 1,
                "placed_words": 1,
                "unplaced_words": 0,
                "total_words": 1,
            }
            with mock.patch.object(spike, "fuse_page", return_value=fused) as fuse_page:
                self.assertEqual(spike.stage_fuse(fixture.args()), 0)
            fuse_page.assert_called_once()
            self.assertEqual(fixture.page.read_text(encoding="utf-8"), "refused")

    def test_page_without_word_counts_is_refused_not_stubbed(self):
        """A page whose telemetry was lost cannot vouch for itself."""
        with tempfile.TemporaryDirectory() as temp_dir:
            fixture = _FuseFixture(Path(temp_dir), telemetry=False)
            fixture.record_settings()

            fused = {
                "fused_text": "refused",
                "region_count": 1,
                "placed_words": 1,
                "unplaced_words": 0,
                "total_words": 1,
            }
            with mock.patch.object(spike, "fuse_page", return_value=fused) as fuse_page:
                self.assertEqual(spike.stage_fuse(fixture.args()), 0)
            fuse_page.assert_called_once()
            summary = json.loads(
                (fixture.root / "fused_summary.json").read_text(encoding="utf-8")
            )
            self.assertIn("placed_words", summary["TEST_p1"])
            self.assertNotIn("reused", summary["TEST_p1"])

    def _bridge_fixture(self, root: Path, content_hash: str) -> dict:
        """Complete bridge output plus the v2 row that describes it."""
        output = root / "out" / "TST"
        output.mkdir(parents=True, exist_ok=True)
        txt = output / "TEST.txt"
        pages = output / "TEST.pages.csv"
        headings = output / "TEST.headings.csv"
        txt.write_text("body", encoding="utf-8")
        pages.write_text("page,char_start,char_end\n1,0,4\n", encoding="utf-8")
        headings.write_text("char_offset\n0\n", encoding="utf-8")
        index = root / "v2.csv"
        with index.open("w", newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(
                handle,
                fieldnames=[
                    "pdf_file", "parsed_text_file", "page_map_file", "page_count",
                    "char_count", "ticker", "parsed_at", "content_hash",
                ],
            )
            writer.writeheader()
            writer.writerow(
                {
                    "pdf_file": "TEST.pdf",
                    "parsed_text_file": txt.as_posix(),
                    "page_map_file": pages.as_posix(),
                    "page_count": "1",
                    "char_count": "4",
                    "ticker": "TST",
                    "parsed_at": "2026-08-05T00:00:00Z",
                    "content_hash": content_hash,
                }
            )
        completed = bridge.load_completed_bridge_records(index)
        self.assertIn("TEST", completed)
        return completed["TEST"]

    def _bridge_task(self, root: Path, resume_info: dict) -> dict:
        cache = root / "TEST.pages.json"
        cache.write_text(
            json.dumps({"pdf_stem": "TEST", "pages": {"1": []}}), encoding="utf-8"
        )
        return bridge._run_bridge_task(
            bridge.BridgeTask(
                cache_path=cache,
                fused_dir=root,
                out_dir=root / "out",
                ticker="TST",
                keep_band=True,
                keep_unplaced=True,
                drop_repeated_band=2,
                drop_repeated_unplaced=3,
                strip_md_prefix=True,
                resume_info=resume_info,
            )
        )

    def test_bridge_reuses_only_complete_sidecars_with_v2_metadata(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "TEST_p1.txt").write_text("[1:text]\nbody", encoding="utf-8")
            fingerprint = bridge.fused_fingerprint(root, "TEST", [1])
            resume_info = self._bridge_fixture(root, fingerprint)

            self.assertEqual(self._bridge_task(root, resume_info)["status"], "reused")

    def test_bridge_rebuilds_when_the_fused_pages_changed(self):
        """Re-fusing rewrites pages in place; existence alone cannot see that."""
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "TEST_p1.txt").write_text("[1:text]\nbody", encoding="utf-8")
            resume_info = self._bridge_fixture(
                root, bridge.fused_fingerprint(root, "TEST", [1])
            )

            # Same filename, same non-empty state, different content.
            (root / "TEST_p1.txt").write_text("[1:text]\nrefused", encoding="utf-8")

            result = self._bridge_task(root, resume_info)
            self.assertEqual(result["status"], "written")
            self.assertIn("refused", (root / "out" / "TST" / "TEST.txt").read_text(encoding="utf-8"))
            self.assertEqual(
                result["built"]["content_hash"], bridge.fused_fingerprint(root, "TEST", [1])
            )


if __name__ == "__main__":
    unittest.main()
