from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional


@dataclass
class TableRef:
    table_id: str
    csv_path: str
    n_rows: int
    n_cols: int


@dataclass
class ParsedDocument:
    source_file: str
    company: Optional[str] = None
    doc_type: Optional[str] = None
    parser_used: str = ""
    status: str = "ok"
    error_message: Optional[str] = None
    raw_text: str = ""
    tables: list[TableRef] = field(default_factory=list)
    char_count: int = 0
    table_count: int = 0
    parsed_at: str = field(
        default_factory=lambda: datetime.now(timezone.utc).isoformat()
    )
    content_hash: Optional[str] = None

    def finalize(self) -> "ParsedDocument":
        self.char_count = len(self.raw_text)
        self.table_count = len(self.tables)
        self.content_hash = hashlib.sha256(
            self.raw_text.encode("utf-8", "ignore")
        ).hexdigest()
        return self

    def to_dict(self) -> dict:
        return {
            "source_file": self.source_file,
            "company": self.company,
            "doc_type": self.doc_type,
            "parser_used": self.parser_used,
            "status": self.status,
            "error_message": self.error_message,
            "raw_text": self.raw_text,
            "tables": [t.__dict__ for t in self.tables],
            "char_count": self.char_count,
            "table_count": self.table_count,
            "parsed_at": self.parsed_at,
            "content_hash": self.content_hash,
        }


class BaseParser:
    name: str = "BaseParser"

    def __init__(self, table_output_dir: str | Path = "./tables"):
        self.table_output_dir = Path(table_output_dir)
        self.table_output_dir.mkdir(parents=True, exist_ok=True)

    def parse(self, file_path: str | Path, **kwargs) -> ParsedDocument:
        raise NotImplementedError

    def _error_doc(self, file_path: str | Path, exc: Exception) -> ParsedDocument:
        doc = ParsedDocument(
            source_file=str(file_path),
            parser_used=self.name,
            status="error",
            error_message=f"{type(exc).__name__}: {exc}",
        )
        return doc.finalize()