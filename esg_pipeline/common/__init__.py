"""Modules shared by both pipelines.

`common` is a real package reached by putting REPO_ROOT on ``sys.path``, so
`from common import config` never collides with the per-pipeline
``esg/config.py`` and ``filings/config.py`` that callers import as bare
``config``. That collision is the whole reason this is a package and the
pipeline configs are not.
"""
