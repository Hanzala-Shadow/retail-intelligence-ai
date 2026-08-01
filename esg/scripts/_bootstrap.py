"""Put the ESG pipeline's directories on ``sys.path``, for ``esg/scripts/``.

Import this first, before ``config``, any ``esg/src`` module, or any
``common`` module::

    import _bootstrap  # noqa: F401
    import config
    import pdf_parser

Running ``python esg/scripts/qa_tier1_invariants.py`` puts ``esg/scripts/`` on
``sys.path`` automatically, which is why this module needs no path hack of its
own. See ``esg/src/_bootstrap.py`` for why the depth arithmetic lives in these
four files instead of in every consumer.

Adds, in order:
    esg/src/    the pipeline stages the scripts drive
    esg/        so ``import config`` finds esg/config.py
    <repo root> so ``from common.models import ...`` resolves
"""

import sys
from pathlib import Path

PIPELINE_ROOT = Path(__file__).resolve().parent.parent  # esg/
REPO_ROOT = PIPELINE_ROOT.parent
SRC_DIR = PIPELINE_ROOT / "src"

for _entry in (str(REPO_ROOT), str(PIPELINE_ROOT), str(SRC_DIR)):
    if _entry not in sys.path:
        sys.path.insert(0, _entry)
