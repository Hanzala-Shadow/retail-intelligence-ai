"""Build a small, browsable copy of the local ESG database.

data/esg.db embeds the full corpus text (~189 MB of chunk_text/section_text),
which puts it past the size limit of lightweight GUI viewers. This writes
data/esg_browse.db with the same tables and every metadata column, but with the
two large text columns replaced by a 200-character preview plus the original
length. Everything needed to browse, filter, and QA the corpus is preserved;
only the full text bodies are dropped.

Read-only with respect to data/esg.db. Safe to re-run: the output is rebuilt
from scratch each time.

    python esg/scripts/build_esg_browse_db.py
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

import _bootstrap  # noqa: F401  (import path: config, pipeline src, common)
import config  # noqa: E402

PREVIEW_CHARS = 200
# column -> the table it lives on; replaced by <column>_preview + <column>_chars
LARGE_TEXT = {"chunks": "chunk_text", "sections": "section_text"}


def table_names(con: sqlite3.Connection) -> list[str]:
    rows = con.execute(
        "SELECT name FROM sqlite_master WHERE type='table' "
        "AND name NOT LIKE 'sqlite_%' ORDER BY name"
    ).fetchall()
    return [r[0] for r in rows]


def columns(con: sqlite3.Connection, table: str) -> list[str]:
    return [r[1] for r in con.execute(f'PRAGMA table_info("{table}")')]


def build(source: Path, dest: Path) -> None:
    if not source.exists():
        raise SystemExit(f"source database not found: {source}")
    if dest.exists():
        dest.unlink()

    con = sqlite3.connect(f"file:{source.as_posix()}?mode=ro", uri=True)
    con.execute("ATTACH DATABASE ? AS browse", (str(dest),))

    for table in table_names(con):
        cols = columns(con, table)
        big = LARGE_TEXT.get(table)

        select_parts, ddl_parts = [], []
        for col in cols:
            if col == big:
                select_parts.append(f'SUBSTR("{col}", 1, {PREVIEW_CHARS})')
                ddl_parts.append(f'"{col}_preview" TEXT')
                select_parts.append(f'LENGTH("{col}")')
                ddl_parts.append(f'"{col}_chars" INTEGER')
            else:
                select_parts.append(f'"{col}"')
                ddl_parts.append(f'"{col}"')

        con.execute(f'CREATE TABLE browse."{table}" ({", ".join(ddl_parts)})')
        con.execute(
            f'INSERT INTO browse."{table}" SELECT {", ".join(select_parts)} FROM main."{table}"'
        )
        n = con.execute(f'SELECT COUNT(*) FROM browse."{table}"').fetchone()[0]
        note = f"  ({big} -> preview + length)" if big else ""
        print(f"  {table:<24} {n:>8,} rows{note}")

    # Indexes that make the common browse filters usable.
    for stmt in (
        'CREATE INDEX browse.ix_chunks_company ON chunks(company_id)',
        'CREATE INDEX browse.ix_chunks_doc ON chunks(doc_id)',
        'CREATE INDEX browse.ix_chunks_gate ON chunks(rag_action, doc_quality_status)',
        'CREATE INDEX browse.ix_sections_doc ON sections(doc_id)',
        'CREATE INDEX browse.ix_documents_company ON documents(company_id)',
    ):
        con.execute(stmt)

    con.commit()
    con.execute("DETACH DATABASE browse")
    con.close()

    src_mb = source.stat().st_size / 1_000_000
    dst_mb = dest.stat().st_size / 1_000_000
    print(f"\n{source}  {src_mb:.1f} MB")
    print(f"{dest}  {dst_mb:.1f} MB  ({dst_mb / src_mb * 100:.0f}% of original)")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=config.ESG_DB)
    parser.add_argument("--dest", type=Path, default=config.ESG_BROWSE_DB)
    args = parser.parse_args()
    build(args.source, args.dest)


if __name__ == "__main__":
    main()
