"""Every module in both pipelines must still import after a directory moves.

This is the test the pipeline split needed and did not have. Before the split,
37 files each carried their own

    ROOT = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(ROOT / "src"))

with a hand-counted depth. Moving a directory broke all 37 at once, silently,
at import time -- and the PowerShell runners only found out mid-corpus. The
depth now lives in four `_bootstrap.py` modules and this file fails the moment
one of them is wrong.

Each module is imported in a subprocess whose sys.path is seeded exactly the
way a real `python esg/src/pdf_parser.py` run seeds it: the module's own
directory and nothing else. Importing them in-process would pass on the
conftest path setup and prove nothing.

Around a dozen of the older 10-K helpers are unguarded scripts that do their
work at import -- `document_scanner.py` walks the raw corpus and writes
`document_scan.csv` the moment it is imported. Those are detected and skipped,
because a test has no business writing to `data/`. See `_runs_work_at_import`.
Coverage is asserted so the skip rule cannot quietly hollow the test out.
"""

import ast
import subprocess
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
PYTHON = sys.executable

PIPELINE_DIRS = (
    "common",
    "esg/src",
    "esg/scripts",
    "filings/src",
)


def _is_main_guard(node: ast.stmt) -> bool:
    return (
        isinstance(node, ast.If)
        and isinstance(node.test, ast.Compare)
        and isinstance(node.test.left, ast.Name)
        and node.test.left.id == "__name__"
    )


def _runs_work_at_import(path: Path) -> bool:
    """True if importing this module would execute pipeline work.

    Eleven of the older 10-K helpers are scripts with no
    ``if __name__ == "__main__":`` guard: ``document_scanner.py`` walks the raw
    corpus and writes ``document_scan.csv`` the moment it is imported, and
    ``stats_report.py`` reads three index CSVs. Importing those to prove they
    parse would read and write ``data/`` -- which this test has no business
    touching.

    The tell is a module-level statement that reads ``config``: that is a
    pipeline path, so using one outside a function is I/O at import time. A
    module-level call on its own is not enough to flag -- ``models.py`` does
    ``Base = declarative_base()`` and is perfectly safe to import.

    Detected rather than hand-listed, so a new script cannot quietly join the
    exemption. Statements inside a ``__main__`` guard do not count; that is the
    guard's whole purpose.
    """
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))

    # A __main__ guard is the author's statement that importing this module
    # does nothing. Every module that has one is safe to import; the eleven
    # that do pipeline work at import are exactly the ones that lack it.
    if any(_is_main_guard(node) for node in tree.body):
        return False

    for node in tree.body:
        # A def/class body does not run at import, so `config` inside one is
        # not import-time I/O -- only the statements that actually execute now
        # count.
        if isinstance(
            node,
            (
                ast.FunctionDef,
                ast.AsyncFunctionDef,
                ast.ClassDef,
                ast.Import,
                ast.ImportFrom,
            ),
        ):
            continue
        nodes = list(ast.walk(node))
        has_call = any(isinstance(n, ast.Call) for n in nodes)
        reads_config = any(
            isinstance(n, ast.Name) and n.id == "config" for n in nodes
        )

        # A loop or context manager at module level is script behaviour.
        # document_scanner.py walks the raw corpus in a top-level for.
        if isinstance(node, (ast.For, ast.While, ast.With, ast.Try)) and has_call:
            return True
        # A bare expression-statement call (print(...), main()).
        if isinstance(node, ast.Expr) and isinstance(node.value, ast.Call):
            return True
        # A call on a config path: pd.read_csv(config.CHUNKS_INDEX_CSV).
        # A plain `DEFAULT_INDEX = config.ESG_PARSE_INDEX_CSV` has no call and
        # is just a constant, so it does not count.
        if has_call and reads_config:
            return True
    return False


def _import_in_subprocess(directory: Path, module: str):
    code = (
        f"import sys; sys.path.insert(0, r'{directory}');"
        f"import importlib; importlib.import_module('{module}')"
    )
    return subprocess.run(
        [PYTHON, "-c", code], capture_output=True, text=True, cwd=REPO_ROOT
    )


class ImportBootstrapTests(unittest.TestCase):
    def test_every_pipeline_module_imports(self):
        failures, checked, skipped = [], 0, []
        for rel in PIPELINE_DIRS:
            directory = REPO_ROOT / rel
            for path in sorted(directory.glob("*.py")):
                if path.stem in ("_bootstrap", "__init__"):
                    continue
                if _runs_work_at_import(path):
                    skipped.append(f"{rel}/{path.name}")
                    continue
                checked += 1
                result = _import_in_subprocess(directory, path.stem)
                if result.returncode != 0:
                    tail = [ln for ln in result.stderr.strip().splitlines() if ln.strip()]
                    failures.append(f"{rel}/{path.name}: {tail[-1] if tail else '?'}")

        self.assertEqual(
            failures,
            [],
            "These modules no longer import. If a directory just moved, the "
            "matching _bootstrap.py depth is stale:\n  " + "\n  ".join(failures),
        )
        # Guard against the skip rule quietly swallowing the whole suite.
        self.assertGreater(
            checked, 40, f"only {checked} modules were import-checked; skipped {skipped}"
        )

    def test_bootstrap_modules_exist_where_consumers_expect_them(self):
        """A `import _bootstrap` with no _bootstrap.py beside it is a hard error."""
        missing = []
        for rel in ("esg/src", "esg/scripts", "filings/src", "filings/scripts"):
            if not (REPO_ROOT / rel / "_bootstrap.py").is_file():
                missing.append(f"{rel}/_bootstrap.py")
        self.assertEqual(missing, [], f"missing bootstrap modules: {missing}")

    def test_bootstrap_resolves_to_the_actual_repo_root(self):
        """The depth bug this whole file exists to catch, asserted directly."""
        for rel in ("esg/src", "esg/scripts", "filings/src", "filings/scripts"):
            directory = REPO_ROOT / rel
            code = (
                f"import sys; sys.path.insert(0, r'{directory}');"
                "import _bootstrap; print(_bootstrap.REPO_ROOT)"
            )
            result = subprocess.run(
                [PYTHON, "-c", code], capture_output=True, text=True, cwd=REPO_ROOT
            )
            self.assertEqual(result.returncode, 0, f"{rel}: {result.stderr}")
            self.assertEqual(
                Path(result.stdout.strip()),
                REPO_ROOT,
                f"{rel}/_bootstrap.py computes the wrong repo root — its "
                f"parent count is stale after a move",
            )


if __name__ == "__main__":
    unittest.main()
