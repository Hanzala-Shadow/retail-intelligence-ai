import csv
from pathlib import Path

import pytest

from scripts.build_semantic_adjudication_workbook_inputs import (
    load_needed_chunks, load_selected_retrieval,
)


def _write(path: Path, fields, rows):
    with path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)


def test_selected_retrieval_requires_contiguous_top_five(tmp_path):
    path = tmp_path / "retrieval.csv"
    rows = [
        {"model_id": "winner", "question_id": "q1", "rank": rank, "chunk_id": rank}
        for rank in range(1, 6)
    ]
    _write(path, ("model_id", "question_id", "rank", "chunk_id"), rows)
    assert len(load_selected_retrieval(path, "winner")["q1"]) == 5


def test_selected_retrieval_rejects_missing_rank(tmp_path):
    path = tmp_path / "retrieval.csv"
    rows = [
        {"model_id": "winner", "question_id": "q1", "rank": rank, "chunk_id": rank}
        for rank in (1, 2, 3, 5)
    ]
    _write(path, ("model_id", "question_id", "rank", "chunk_id"), rows)
    with pytest.raises(ValueError, match="ranks 1 through 5"):
        load_selected_retrieval(path, "winner")


def test_chunk_loader_reads_only_required_metadata(tmp_path):
    path = tmp_path / "chunks.csv"
    _write(path, ("chunk_id", "chunk_text"), [
        {"chunk_id": "1", "chunk_text": "one"},
        {"chunk_id": "2", "chunk_text": "two"},
    ])
    result = load_needed_chunks(path, {"2"})
    assert result == {"2": {"chunk_id": "2", "chunk_text": "two"}}
