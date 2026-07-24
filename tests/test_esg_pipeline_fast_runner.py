from __future__ import annotations

import hashlib
import shutil
import subprocess
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
RUNNER = REPO_ROOT / "scripts" / "run_esg_pipeline_fast.ps1"
POWERSHELL = shutil.which("powershell.exe") or shutil.which("powershell")


def run_runner(*arguments: str) -> subprocess.CompletedProcess[str]:
    if not POWERSHELL:
        raise unittest.SkipTest("Windows PowerShell is not installed")
    return subprocess.run(
        [
            POWERSHELL,
            "-NoProfile",
            "-ExecutionPolicy",
            "Bypass",
            "-File",
            str(RUNNER),
            *arguments,
        ],
        cwd=REPO_ROOT,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        check=False,
    )


def file_digest(path: Path) -> str | None:
    if not path.exists():
        return None
    return hashlib.sha256(path.read_bytes()).hexdigest()


class ESGFastPipelineRunnerTests(unittest.TestCase):
    def test_default_all_order_includes_manifest(self) -> None:
        result = run_runner("-WhatIf")
        output = result.stdout + result.stderr
        self.assertEqual(result.returncode, 0, output)

        labels = [
            "[intake]",
            "[parse]",
            "[remediate]",
            "[section]",
            "[chunk]",
            "[layout]",
            "[vlm]",
            "[qa]",
            "[manifest]",
            "[provenance]",
            "[tests]",
        ]
        positions = [output.index(label) for label in labels]
        self.assertEqual(positions, sorted(positions), output)
        self.assertIn("scripts/build_esg_vector_manifest.py", output)

    def test_default_all_never_starts_paid_vlm_work(self) -> None:
        result = run_runner("-WhatIf")
        output = result.stdout + result.stderr
        self.assertEqual(result.returncode, 0, output)
        self.assertIn("No VLM API call will run", output)
        self.assertNotIn("scripts/run_esg_vlm.py", output)
        self.assertNotIn("--vlm-dir", output)

    def test_explicit_vlm_integration_only_changes_manifest_inputs(self) -> None:
        result = run_runner("-Stage", "vlm", "-EnableVlmIntegration", "-WhatIf")
        output = result.stdout + result.stderr
        self.assertEqual(result.returncode, 0, output)
        self.assertIn("[vlm]", output)
        self.assertIn("[manifest]", output)
        self.assertIn("--vlm-dir data/04_vlm", output)
        self.assertNotIn("scripts/run_esg_vlm.py", output)

    def test_unscoped_force_is_blocked(self) -> None:
        result = run_runner("-Stage", "parse", "-Force", "-WhatIf")
        output = result.stdout + result.stderr
        self.assertNotEqual(result.returncode, 0, output)
        self.assertIn("full-corpus forced rebuild is intentionally blocked", output)

    def test_pymupdf_parser_is_scoped_and_opt_in(self) -> None:
        unscoped = run_runner("-Stage", "parse", "-EnablePyMuPdfParser", "-WhatIf")
        self.assertNotEqual(unscoped.returncode, 0)
        self.assertIn("must be scoped with -Ticker", unscoped.stdout + unscoped.stderr)

        scoped = run_runner(
            "-Stage", "parse", "-Ticker", "WMT", "-PdfFile",
            "WMT-Report.pdf", "-EnablePyMuPdfParser", "-WhatIf"
        )
        output = scoped.stdout + scoped.stderr
        self.assertEqual(scoped.returncode, 0, output)
        parse_lines = [line for line in output.splitlines() if line.startswith("[parse")]
        self.assertTrue(parse_lines, output)
        self.assertTrue(all("--prefer-pymupdf" in line for line in parse_lines), output)

    def test_pdf_stem_scope_is_preserved(self) -> None:
        result = run_runner(
            "-Stage", "intake", "-Ticker", "LOVE", "-PdfStem", "LOVE-Report-2024", "-WhatIf"
        )
        output = result.stdout + result.stderr
        self.assertEqual(result.returncode, 0, output)
        self.assertIn("--ticker LOVE", output)
        self.assertIn("--pdf-stem LOVE-Report-2024", output)
        self.assertNotIn("--pdf-file", output)

    def test_scoped_qa_and_manifest_receive_pdf_scope(self) -> None:
        result = run_runner(
            "-Ticker", "LOVE", "-PdfFile", "LOVE-Report-2024.pdf", "-WhatIf"
        )
        output = result.stdout + result.stderr
        self.assertEqual(result.returncode, 0, output)
        qa_line = next(line for line in output.splitlines() if line.startswith("[qa]"))
        manifest_line = next(line for line in output.splitlines() if line.startswith("[manifest]"))
        for line in (qa_line, manifest_line):
            self.assertIn("--ticker LOVE", line)
            self.assertIn("--pdf-file LOVE-Report-2024.pdf", line)

    def test_scoped_provenance_receives_pdf_scope(self) -> None:
        result = run_runner(
            "-Stage", "validate", "-Ticker", "LOVE", "-PdfFile", "LOVE-Report-2024.pdf", "-WhatIf"
        )
        output = result.stdout + result.stderr
        self.assertEqual(result.returncode, 0, output)
        provenance_line = next(line for line in output.splitlines() if line.startswith("[provenance]"))
        self.assertIn("--ticker LOVE", provenance_line)
        self.assertIn("--pdf-file LOVE-Report-2024.pdf", provenance_line)

    def test_scope_and_force_reach_pipeline_without_conflicting_scope_flags(self) -> None:
        result = run_runner(
            "-Ticker",
            "love",
            "-PdfFile",
            "LOVE-Report-2024.pdf",
            "-Force",
            "-WhatIf",
        )
        output = result.stdout + result.stderr
        self.assertEqual(result.returncode, 0, output)
        intake_line = next(line for line in output.splitlines() if line.startswith("[intake]"))
        remediate_line = next(line for line in output.splitlines() if line.startswith("[remediate]"))
        for line in (intake_line, remediate_line):
            self.assertIn("--ticker LOVE", line)
            self.assertIn("--pdf-file LOVE-Report-2024.pdf", line)
            self.assertNotIn("--pdf-stem", line)
        self.assertNotIn("--force", intake_line)
        self.assertIn("--force", remediate_line)
        self.assertIn("--force", next(line for line in output.splitlines() if line.startswith("[parse]")))

    def test_manifest_whatif_does_not_write_manifest_or_lock(self) -> None:
        manifest = REPO_ROOT / "data" / "00_reference" / "vector_index_manifest.csv"
        lock = REPO_ROOT / "tmp" / "esg_pipeline_fast.lock"
        before_manifest = file_digest(manifest)
        before_lock = file_digest(lock)

        result = run_runner("-Stage", "manifest", "-WhatIf")
        output = result.stdout + result.stderr

        self.assertEqual(result.returncode, 0, output)
        self.assertEqual(file_digest(manifest), before_manifest)
        self.assertEqual(file_digest(lock), before_lock)
        self.assertIn("WhatIf complete: no pipeline files or indexes were changed", output)


if __name__ == "__main__":
    unittest.main()
