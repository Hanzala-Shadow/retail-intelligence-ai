"""Make both pipelines importable from any test, without per-file path hacks.

pytest imports the rootdir conftest before collecting anything, so every test
under ``esg/tests/``, ``filings/tests/`` and ``tests/`` gets these entries for
free. That is what replaced the 20 copies of

    ROOT = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(ROOT / "src"))

that each test carried before the pipelines were split.

Both pipelines are on the path at once, which is fine and deliberate: the two
``config`` modules are ``esg/config.py`` and ``filings/config.py``, so exactly
one of them wins ``import config`` — the ESG one, because ``esg/`` is inserted
last and therefore sits first. Every test that imports bare ``config`` today is
an ESG test. A filings test wanting its own layout should import it explicitly:

    from filings import config as filings_config   # unambiguous

``common`` is a package reached from the repo root, so it never competes for
the name ``config``.
"""

import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent

# Order matters: later inserts land earlier in sys.path. `esg/` goes last so a
# bare `import config` resolves to the ESG pipeline, matching every existing
# test's expectation.
_ENTRIES = (
    REPO_ROOT,                      # `common` package
    REPO_ROOT / "filings",          # filings/config.py
    REPO_ROOT / "filings" / "src",
    REPO_ROOT / "esg" / "scripts",  # scripts under test (vector manifest, provenance)
    REPO_ROOT / "esg" / "src",
    REPO_ROOT / "esg",              # esg/config.py -> wins bare `import config`
)

for _entry in _ENTRIES:
    _path = str(_entry)
    if _path not in sys.path:
        sys.path.insert(0, _path)
