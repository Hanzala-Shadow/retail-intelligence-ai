"""Put the 10-K pipeline's directories on ``sys.path``, for ``filings/scripts/``.

Import this first, before ``config``, any ``filings/src`` module, or any
``common`` module::

    import _bootstrap  # noqa: F401
    import config

``filings/scripts/`` has no scripts yet — every runner in the repo today
drives the ESG pipeline. This exists so the first 10-K script added here has
the same one-line import contract as everything else, rather than reintroducing
a hand-counted ``parents[N]``. See ``esg/src/_bootstrap.py``.

Adds, in order:
    filings/src/  the pipeline stages the scripts drive
    filings/      so ``import config`` finds filings/config.py
    <repo root>   so ``from common.models import ...`` resolves
"""

import sys
from pathlib import Path

PIPELINE_ROOT = Path(__file__).resolve().parent.parent  # filings/
REPO_ROOT = PIPELINE_ROOT.parent
SRC_DIR = PIPELINE_ROOT / "src"

for _entry in (str(REPO_ROOT), str(PIPELINE_ROOT), str(SRC_DIR)):
    if _entry not in sys.path:
        sys.path.insert(0, _entry)
