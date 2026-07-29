"""Put the 10-K pipeline's own directories on ``sys.path``.

Import this first, before ``config`` or any ``common`` module::

    import _bootstrap  # noqa: F401
    import config

Running ``python filings/src/html_parser.py`` puts ``filings/src/`` on
``sys.path`` automatically, which is why this module is importable with no
path hack of its own. See ``esg/src/_bootstrap.py`` for why the depth
arithmetic lives in these four files instead of in every consumer.

Adds, in order:
    filings/src/  already implicit when run as a script, explicit when imported
    filings/      so ``import config`` finds filings/config.py
    <repo root>   so ``from common.models import ...`` resolves

It adds ``filings/src`` too, which is redundant for a direct script run, so
that this and ``filings/scripts/_bootstrap.py`` have identical effect. Four
modules share the name ``_bootstrap``; making them interchangeable means it
never matters which one a given ``sys.path`` order resolves to.
"""

import sys
from pathlib import Path

PIPELINE_ROOT = Path(__file__).resolve().parent.parent  # filings/
REPO_ROOT = PIPELINE_ROOT.parent
SRC_DIR = PIPELINE_ROOT / "src"

for _entry in (str(REPO_ROOT), str(PIPELINE_ROOT), str(SRC_DIR)):
    if _entry not in sys.path:
        sys.path.insert(0, _entry)
