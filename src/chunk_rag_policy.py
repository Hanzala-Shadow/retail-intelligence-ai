"""Persistent retrieval policy for generated 10-K chunks."""

import re


MIN_CHUNK_TOKENS = 50
MAX_CHUNK_TOKENS = 500
MIN_DISTINCT_TOC_ITEMS = 5

SEC_ITEM_PATTERN = re.compile(
    r"(?i)(?<![A-Za-z0-9])"
    r"item\s+"
    r"(1a|1b|1c|2|3|4|5|6|7a|7|8|9a|9b|9c|9|10|11|12|13|14|15|16)"
    r"(?=\s|[.:\-–—])"
)


def detected_sec_items(chunk_text: str) -> set[str]:
    """Return distinct SEC item labels explicitly present in chunk text."""
    return {
        match.group(1).upper()
        for match in SEC_ITEM_PATTERN.finditer(chunk_text or "")
    }


def is_high_confidence_item1_toc(
    *,
    doc_type: str,
    section_code: str,
    chunk_index: int,
    chunk_text: str,
) -> bool:
    """Identify the first Item 1 chunk when it contains a full SEC item list."""
    return (
        doc_type == "10-K"
        and section_code == "Item_1"
        and chunk_index == 0
        and len(detected_sec_items(chunk_text)) >= MIN_DISTINCT_TOC_ITEMS
    )


def chunk_rag_metadata(
    *,
    doc_type: str,
    section_code: str,
    chunk_index: int,
    chunk_text: str,
    token_count: int | None,
) -> dict[str, object]:
    """Return persistent database retrieval metadata for one chunk."""
    if (
        not chunk_text.strip()
        or token_count is None
        or token_count < MIN_CHUNK_TOKENS
        or token_count > MAX_CHUNK_TOKENS
    ):
        return {
            "doc_quality_status": "review_required",
            "rag_action": "review",
            "quality_flags": '["chunk_validation_failure"]',
            "citation_ready": False,
        }

    if section_code in {"HEADER", "Signatures"}:
        return {
            "doc_quality_status": "passed",
            "rag_action": "exclude_boilerplate",
            "quality_flags": '["retrieval_boilerplate"]',
            "citation_ready": False,
        }

    if is_high_confidence_item1_toc(
        doc_type=doc_type,
        section_code=section_code,
        chunk_index=chunk_index,
        chunk_text=chunk_text,
    ):
        return {
            "doc_quality_status": "passed",
            "rag_action": "exclude_boilerplate",
            "quality_flags": '["retrieval_boilerplate","sec_item_toc"]',
            "citation_ready": False,
        }

    return {
        "doc_quality_status": "passed",
        "rag_action": "include",
        "quality_flags": "[]",
        "citation_ready": True,
    }
