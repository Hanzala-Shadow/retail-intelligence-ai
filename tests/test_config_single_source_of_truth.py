"""The two config modules are the only place a data/reports/logs path lives.

Every other module imports its paths from `config`, so both pipelines resolve
the same directories no matter which working directory a script is invoked
from.

Since the pipelines were split, "config" is three files -- common/config.py
plus the ESG pipeline -- and the shell runners read their union via
`python common/config.py --json`. That union is what this test polices: a
lookup naming a constant no config defines is an empty argument at runtime, and
the split is exactly what could have introduced one.
"""

import ast
import re
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
from common import config  # noqa: E402

# Every directory holding pipeline code. Missing one here means it stops being
# policed silently, so the test asserts below that each still exists.
SCANNED_DIRS = (
    "common",
    "esg/src",
    "esg/tests",
    "tests",
)

# Shell/PowerShell cannot import config, so they read it through
# `python common/config.py --json` (see esg/scripts/PipelinePaths.ps1). They
# are scanned with a text rule rather than the AST rule used for Python.
# The fusion runner has user-editable CLI defaults for its input corpus and
# data outputs. It does not use PipelinePaths.ps1, which belongs to the
# excluded legacy runner set, so there is no shell config bridge to police.
SCANNED_SHELL = ()

# A quoted "data/..." / 'reports/...' / data\... in shell or PowerShell.
SHELL_PATH_LITERAL = re.compile(r"""["'](data|reports|logs)[/\\][^"']*["']""")

# $Paths.FOO and $Paths.Absolute.FOO lookups in the PowerShell runners.
PS_PATHS_LOOKUP = re.compile(r"\$Paths(?:\.Absolute)?\.([A-Za-z_][A-Za-z0-9_]*)")

# CFG_FOO shell variables produced by the config --json bridge.
SH_CFG_LOOKUP = re.compile(r"\$\{?CFG_([A-Z0-9_]+)\}?")

# The pipeline's stage directories. The rule targets the layout config owns,
# so "data/00_reference/x.csv" is a finding while a synthetic fixture value
# like "data/AAP/report-2024.pdf" -- which names no stage dir and points at
# nothing on disk -- is not.
STAGE_DIRS = (
    "00_reference", "01_raw", "02_interim", "03_sections",
    "04_chunks", "04_vlm", "05_db", "05_embedding", "tables",
)

# "data/00_reference/x.csv", "reports/out.md", "logs/errors.log"
PATH_LITERAL = re.compile(
    r"^(?:data[/\\](?:%s)(?:[/\\]|$)|(?:reports|logs)[/\\])" % "|".join(STAGE_DIRS)
)

# A bare "data" is usually a vocabulary word (esg_chunker's section codes use
# one), so it only counts when joined with a stage dir --
# REPO_ROOT / "data" / "00_reference" is the same hardcoding, in segments.
PIPELINE_ROOTS = {"data", "reports", "logs"}

# config.py defines the layout; that is the whole point of the file. This
# test names paths in its own assertions, so it cannot police itself.
EXEMPT_FILES = {"config.py", Path(__file__).name}


def _string_literals(tree: ast.AST):
    """Yield (lineno, value) for every real string literal.

    Docstrings are skipped: a path inside prose documents an example, it
    does not open a file.
    """
    docstrings = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            doc = ast.get_docstring(node, clean=False)
            if doc is not None and node.body:
                first = node.body[0]
                if isinstance(first, ast.Expr) and isinstance(first.value, ast.Constant):
                    docstrings.add(id(first.value))

    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if id(node) not in docstrings:
                yield node.lineno, node.value


def _segment_joined_paths(tree: ast.AST):
    """Yield (lineno, segments) for each `a / "data" / "00_reference"` chain.

    Path composition spelled in segments hardcodes the layout just as much as
    a single literal does, but no one literal looks like a path.
    """
    inner = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div):
            if isinstance(node.left, ast.BinOp) and isinstance(node.left.op, ast.Div):
                inner.add(id(node.left))

    for node in ast.walk(tree):
        if not (isinstance(node, ast.BinOp) and isinstance(node.op, ast.Div)):
            continue
        if id(node) in inner:  # only look at the outermost link of a chain
            continue
        segments = [
            part.value
            for part in ast.walk(node)
            if isinstance(part, ast.Constant) and isinstance(part.value, str)
        ]
        if segments:
            yield node.lineno, segments


class ConfigIsSingleSourceOfTruthTests(unittest.TestCase):
    def test_no_module_spells_out_a_data_path(self):
        offenders = []
        for rel_dir in SCANNED_DIRS:
            for path in sorted((REPO_ROOT / rel_dir).glob("*.py")):
                if path.name in EXEMPT_FILES:
                    continue
                tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
                for lineno, segments in _segment_joined_paths(tree):
                    if segments[0] in PIPELINE_ROOTS and any(
                        s in STAGE_DIRS for s in segments
                    ):
                        offenders.append(
                            f"{rel_dir}/{path.name}:{lineno}: joins {'/'.join(segments)}"
                        )
                for lineno, value in _string_literals(tree):
                    if PATH_LITERAL.match(value):
                        offenders.append(f"{rel_dir}/{path.name}:{lineno}: {value!r}")

        self.assertEqual(
            offenders,
            [],
            "These modules hardcode a pipeline path. Add a constant to the "
            "config that owns it (common/, esg/ or filings/) and import it "
            "instead:\n  " + "\n  ".join(offenders),
        )

    def test_every_scanned_dir_exists(self):
        """A renamed directory would drop out of the scan without a failure."""
        missing = [d for d in SCANNED_DIRS if not (REPO_ROOT / d).is_dir()]
        self.assertEqual(
            missing,
            [],
            f"SCANNED_DIRS names directories that no longer exist: {missing}. "
            "Update it, or these paths stop being policed silently.",
        )

    def test_every_config_path_is_absolute_and_under_the_repo(self):
        # The merged table, not one module's namespace: after the split, each
        # config holds only its own third.
        for name, value in config.merged_path_constants()["absolute"].items():
            path = Path(value)
            if name.endswith("_REL"):
                continue
            self.assertTrue(
                path.is_absolute(),
                f"config.{name} must be absolute so it is CWD-independent",
            )
            self.assertEqual(
                config.REPO_ROOT,
                Path(*path.parts[: len(config.REPO_ROOT.parts)]),
                f"config.{name} must live under REPO_ROOT",
            )

    def test_repo_relative_forms_stay_relative(self):
        for name, value in config.merged_path_constants()["relative"].items():
            if not name.endswith("_REL"):
                continue
            self.assertFalse(
                Path(value).is_absolute(),
                f"config.{name} is a repo-relative form but is absolute",
            )

    def test_no_shell_runner_spells_out_a_data_path(self):
        offenders = []
        for rel in SCANNED_SHELL:
            path = REPO_ROOT / rel
            if not path.exists():
                continue
            for lineno, line in enumerate(
                path.read_text(encoding="utf-8", errors="replace").splitlines(), 1
            ):
                stripped = line.lstrip()
                if stripped.startswith("#"):  # comments may name a path
                    continue
                for match in SHELL_PATH_LITERAL.finditer(line):
                    offenders.append(f"{rel}:{lineno}: {match.group(0)}")

        self.assertEqual(
            offenders,
            [],
            "These runners hardcode a pipeline path. Read it from config "
            "instead -- PowerShell via esg/scripts/PipelinePaths.ps1, bash via "
            "`python common/config.py --json`:\n  " + "\n  ".join(offenders),
        )

    def test_runner_path_lookups_name_real_config_constants(self):
        """A typo'd $Paths key silently yields an empty argument, so pin them.

        Checked against the MERGED table, which is what the runners actually
        read. Checking one config's namespace would fail every lookup naming a
        constant the other pipeline owns -- and `scripts_project_snapshot.sh`
        legitimately reads both.
        """
        known = set(config.merged_path_constants()["relative"])
        offenders = []
        for rel in SCANNED_SHELL:
            path = REPO_ROOT / rel
            if not path.exists():
                continue
            text = path.read_text(encoding="utf-8", errors="replace")
            pattern = SH_CFG_LOOKUP if path.suffix == ".sh" else PS_PATHS_LOOKUP
            for name in pattern.findall(text):
                if name == "Absolute":
                    continue
                if name not in known:
                    offenders.append(f"{rel}: unknown config constant {name!r}")

        self.assertEqual(sorted(set(offenders)), [], "\n  ".join(sorted(set(offenders))))

    def test_json_bridge_exposes_absolute_and_relative_forms(self):
        payload = config.merged_path_constants()
        self.assertIn("absolute", payload)
        self.assertIn("relative", payload)
        self.assertEqual(sorted(payload["absolute"]), sorted(payload["relative"]))
        self.assertEqual(
            payload["relative"]["ESG_PARSE_INDEX_CSV"],
            "data/00_reference/esg_parse_index.csv",
        )
        self.assertEqual(
            payload["relative"]["ESG_PARSE_INDEX_V2_CSV"],
            "data/00_reference/esg_parse_index_v2.csv",
        )
        self.assertTrue(Path(payload["absolute"]["ESG_PARSE_INDEX_CSV"]).is_absolute())

    def test_merged_table_spans_every_config(self):
        """The bridge the runners read must not lose a pipeline.

        This is the failure the split could have caused: path_constants() walks
        one namespace, so pointing the runners at a pipeline config yields a
        table missing the other pipeline's constants entirely. A missing key
        becomes an empty command-line argument -- which fails deep inside a
        corpus run, not at startup.
        """
        merged = config.merged_path_constants()["relative"]

        # One sentinel per config, spelled out so the assertion names what is
        # missing rather than just a count.
        for owner, key, expected in (
            ("common", "REFERENCE_DIR", "data/00_reference"),
            ("esg", "ESG_PARSE_INDEX_CSV", "data/00_reference/esg_parse_index.csv"),
        ):
            self.assertIn(
                key, merged, f"merged path table lost {owner}/config.py ({key})"
            )
            self.assertEqual(merged[key], expected)

        # The union must be strictly larger than any one config's share.
        shared_only = set(config.path_constants())
        self.assertGreater(len(merged), len(shared_only))

    def test_as_repo_relative_round_trips(self):
        merged = config.merged_path_constants()["absolute"]
        esg_index = Path(merged["ESG_PARSE_INDEX_CSV"])
        self.assertEqual(
            config.as_repo_relative(esg_index).as_posix(),
            "data/00_reference/esg_parse_index.csv",
        )
        # Already-relative and outside-the-repo paths pass through unchanged.
        self.assertEqual(config.as_repo_relative("data/x.csv"), Path("data/x.csv"))


if __name__ == "__main__":
    unittest.main()
