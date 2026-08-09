from scripts.audit_v29_final_chunks import comparable_prose_boundary


def row(
    chunk_type: str,
    subsection: str,
    start: int,
    end: int,
) -> dict[str, object]:
    return {
        "chunk_type": chunk_type,
        "subsection_heading": subsection,
        "section_start_char": start,
        "section_end_char": end,
    }


def test_table_to_narrative_is_not_a_prose_truncation():
    assert not comparable_prose_boundary(
        row("table", "Deferred Income Tax Assets", 100, 200),
        row("narrative", "Deferred Income Tax Assets", 225, 400),
    )


def test_page_or_heading_gap_is_not_a_prose_truncation():
    assert not comparable_prose_boundary(
        row("narrative", "Other Matters", 100, 200),
        row("narrative", "(Continued)", 278, 500),
    )


def test_heading_to_narrative_is_not_a_prose_truncation():
    assert not comparable_prose_boundary(
        row("list", "Revision of Prior Statements", 100, 200),
        row("narrative", "Notes", 202, 500),
    )


def test_contiguous_same_subsection_prose_remains_audited():
    assert comparable_prose_boundary(
        row("narrative", "Liquidity", 100, 200),
        row("narrative", "Liquidity", 202, 500),
    )
