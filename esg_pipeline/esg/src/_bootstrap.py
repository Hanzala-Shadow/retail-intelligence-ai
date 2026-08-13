"""Put the ESG pipeline's own directories on ``sys.path``.

Import this first, before ``config`` or any ``common`` module::

    import _bootstrap  # noqa: F401
    import config

Running ``python esg/src/pdf_parser.py`` puts ``esg/src/`` on ``sys.path``
automatically, which is why this module is importable with no path hack of its
own — and why it is the one place the depth arithmetic has to live. Before the
pipeline split every consumer carried its own
``sys.path.insert(0, ROOT / "src")``; 37 of them, each with a hand-counted
``parents[N]``. Moving a directory silently broke all 37 at once. Now it
breaks four files, and ``tests/test_import_bootstrap.py`` fails when it does.

Adds, in order:
    esg/src/    already implicit when run as a script, explicit when imported
    esg/        so ``import config`` finds esg/config.py
    <repo root> so ``from common.models import ...`` resolves

It adds ``esg/src`` too, which is redundant for a direct script run, so that
this and ``esg/scripts/_bootstrap.py`` have identical effect. Four modules
share the name ``_bootstrap``; making them interchangeable means it never
matters which one a given ``sys.path`` order resolves to.
"""

import sys
from pathlib import Path

PIPELINE_ROOT = Path(__file__).resolve().parent.parent  # esg/
REPO_ROOT = PIPELINE_ROOT.parent
SRC_DIR = PIPELINE_ROOT / "src"

for _entry in (str(REPO_ROOT), str(PIPELINE_ROOT), str(SRC_DIR)):
    if _entry not in sys.path:
        sys.path.insert(0, _entry)
