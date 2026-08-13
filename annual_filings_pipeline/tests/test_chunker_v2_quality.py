from src.chunker_v2 import (
    SemanticBoundaryIndex,
    Unit,
    boundary_contexts,
    continuation_context,
    embedding_text,
    enforce_embedding_limit,
    forward_continuation_context,
    grammatical_continuation_end,
    grammatical_continuation_start,
    inside_semantic_unit,
    is_heading,
    late_region_policy,
    quality,
    rag_content_policy,
    semantic_units,
    strong_exhibit_boundary,
)


def test_table_units_exclude_internal_markers():
    text = (
        "[TABLE_START:table_0001]\n"
        "Metric | FY2025 | FY2024\n"
        "Net sales | 100 | 90\n"
        "[TABLE_END:table_0001]"
    )

    units = semantic_units(text)

    assert len(units) == 1
    assert units[0].kind == "table"

    value = text[units[0].start:units[0].end]
    assert "[TABLE_START:" not in value
    assert "[TABLE_END:" not in value
    assert "Net sales | 100 | 90" in value


def test_short_inline_fragments_are_not_headings():
    assert not is_heading("We")
    assert not is_heading("Our")
    assert not is_heading("The Company")
    assert is_heading("Overview")
    assert is_heading("Liquidity")
    assert is_heading("Item No. 7")


def test_embedding_prefix_is_rag_specific():
    value = embedding_text(
        "Example Retailer",
        "EXM",
        2025,
        "Item_7",
        "Liquidity",
        "narrative",
        "Cash generated from operations increased.",
    )

    assert "Document: Form 10-K" in value
    assert "Fiscal year: FY2025" in value
    assert "SEC section: Item 7" in value
    assert "Subsection: Liquidity" in value


def test_auditor_subsections_are_excluded():
    status, flags, action = quality(
        "narrative",
        "These financial statements are management’s responsibility.",
        80,
        40,
        "Basis for Opinion",
    )

    assert status == "failed"
    assert "auditor_opinion" in flags
    assert action == "exclude"


def test_financial_statement_table_remains_eligible():
    status, flags, action = quality(
        "table",
        "Net sales | 100 | 90",
        80,
        40,
        "Consolidated Statements of Operations",
    )

    assert status == "passed"
    assert flags == []
    assert action == "include"


def test_auditor_state_never_inherits_into_nonfinancial_sec_sections():
    region, rag_section, flags, forced = late_region_policy(
        "auditor",
        "table",
        "Fiscal year | 2025 | 2024\nNet sales | 100 | 90",
        "Business",
        "Item_1",
    )

    assert region == ""
    assert rag_section == "Item_1"
    assert "auditor_opinion" not in flags
    assert forced is None


def test_financial_table_ends_stale_item8_auditor_region():
    region, rag_section, flags, forced = late_region_policy(
        "auditor",
        "table",
        (
            "2025 | 2024\n"
            "Cash and cash equivalents | 500 | 400\n"
            "Total assets | 2,000 | 1,800"
        ),
        "Critical Audit Matter",
        "Item_8",
    )

    assert region == ""
    assert rag_section == "Item_8"
    assert "auditor_opinion" not in flags
    assert forced is None


def test_stale_auditor_subsection_does_not_exclude_financial_table():
    status, flags, action = quality(
        "table",
        (
            "2025 | 2024\n"
            "Net sales | 100 | 90\n"
            "Operating income | 20 | 18"
        ),
        80,
        40,
        "Critical Audit Matter",
        "Item_8",
    )

    assert status == "passed"
    assert "auditor_opinion" not in flags
    assert action == "include"


def test_company_prefixed_statement_title_ends_auditor_region():
    region, rag_section, flags, forced = late_region_policy(
        "auditor",
        "narrative",
        (
            "39\n"
            "MACY'S, INC. CONSOLIDATED STATEMENTS OF INCOME "
            "(millions, except per share data)"
        ),
        "March 22, 2024",
        "Item_8",
    )

    assert region == ""
    assert rag_section == "Item_8"
    assert "auditor_opinion" not in flags
    assert forced is None


def test_direct_auditor_procedure_remains_excluded():
    region, _, flags, forced = late_region_policy(
        "",
        "narrative",
        (
            "We have audited the accompanying consolidated balance "
            "sheets and performed audit procedures over impairment."
        ),
        "Critical Audit Matter",
        "Item_8",
    )

    assert region == "auditor"
    assert flags == ["auditor_opinion"]
    assert forced == "exclude"



def test_oversized_table_row_becomes_bounded_continuations():
    import tiktoken

    from src.chunker_v2 import split_large_unit, token_count

    text = (
        "[TABLE_START:table_0001]\n"
        + "Long disclosure | "
        + ("company-specific financial explanation " * 300)
        + "\n[TABLE_END:table_0001]"
    )

    encoder = tiktoken.get_encoding("cl100k_base")
    table = semantic_units(text)[0]
    units = split_large_unit(
        text,
        table,
        encoder,
        hard_max=120,
    )

    assert len(units) > 1
    assert all(
        unit.kind == "table_continuation"
        for unit in units
    )
    assert all(
        token_count(
            encoder,
            text[unit.start:unit.end],
        ) <= 120
        for unit in units
    )



def test_adjacent_semantic_tables_never_share_a_chunk():
    import tiktoken

    from src.chunker_v2 import group_units, token_count

    text = (
        "[TABLE_START:table_0001]\n"
        "Metric | FY2025\n"
        "Net sales | 100\n"
        "[TABLE_END:table_0001]\n\n"
        "[TABLE_START:table_0002]\n"
        "Metric | FY2024\n"
        "Net sales | 90\n"
        "[TABLE_END:table_0002]"
    )

    encoder = tiktoken.get_encoding("cl100k_base")
    units = semantic_units(text)
    groups = group_units(
        text,
        units,
        encoder,
        target=300,
        hard_max=400,
        overlap=35,
    )

    assert len(groups) == 2

    for group in groups:
        value = text[group[0].start:group[-1].end]
        assert "[TABLE_START:" not in value
        assert "[TABLE_END:" not in value
        assert token_count(encoder, value) <= 400



def test_overlap_is_dropped_before_near_maximum_unit():
    import tiktoken

    from src.chunker_v2 import group_units, token_count

    text = (
        ("Prior context word " * 10)
        + ".\n\n"
        + ("Large substantive disclosure " * 125)
        + "."
    )

    encoder = tiktoken.get_encoding("cl100k_base")
    units = semantic_units(text)
    groups = group_units(
        text,
        units,
        encoder,
        target=25,
        hard_max=400,
        overlap=35,
    )

    assert all(
        token_count(
            encoder,
            text[group[0].start:group[-1].end],
        ) <= 400
        for group in groups
    )


class _CharacterTokenizer:
    model_max_length = 512

    def encode(
        self,
        text,
        add_special_tokens=True,
        truncation=False,
    ):
        extra = 2 if add_special_tokens else 0
        return list(range(len(text) + extra))


class _FastCharacterTokenizer(_CharacterTokenizer):
    is_fast = True

    def __call__(
        self,
        text,
        add_special_tokens=False,
        truncation=False,
        return_offsets_mapping=False,
    ):
        return {
            "offset_mapping": [
                (index, index + 1)
                for index in range(len(text))
            ]
        }


def test_full_embedding_limit_splits_table_at_source_offsets():
    text = (
        "[TABLE_START:table_0001]\n"
        "Metric | FY2025 | FY2024\n"
        + ("Long financial table disclosure | 100 | 90\n" * 20)
        + "[TABLE_END:table_0001]"
    )
    group = semantic_units(text)
    bounded = enforce_embedding_limit(
        text,
        group,
        _CharacterTokenizer(),
        512,
        "Example Retailer",
        "EXM",
        2025,
        "Item_8",
    )

    assert len(bounded) > 1
    for values in bounded:
        value = embedding_text(
            "Example Retailer",
            "EXM",
            2025,
            "Item_8",
            values[0].subsection,
            values[0].kind,
            text[values[0].start:values[-1].end],
            "",
        )
        assert len(_CharacterTokenizer().encode(value)) <= 512


def test_indexed_boundary_context_matches_unit_list():
    text = (
        "A complete sentence. "
        "A long continuation that must be split inside its semantic unit."
    )
    units = semantic_units(text)
    position = units[0].start + 25
    indexed = SemanticBoundaryIndex.build(units)

    assert boundary_contexts(
        text, position, units[0].end, "narrative", units
    ) == boundary_contexts(
        text, position, units[0].end, "narrative", indexed
    )


def test_fast_offset_split_preserves_complete_source_and_limit():
    text = (
        "[TABLE_START:table_0001]\n"
        "Metric | FY2025 | FY2024\n"
        + ("Long financial table disclosure | 100 | 90\n" * 100)
        + "[TABLE_END:table_0001]"
    )
    units = semantic_units(text)
    bounded = enforce_embedding_limit(
        text,
        units,
        _FastCharacterTokenizer(),
        512,
        "Example Retailer",
        "EXM",
        2025,
        "Item_8",
    )
    pieces = [
        text[group[0].start:group[-1].end]
        for group in bounded
    ]
    original = text[units[0].start:units[0].end]

    assert "".join(pieces) == original
    assert all(
        len(
            _FastCharacterTokenizer().encode(
                embedding_text(
                    "Example Retailer",
                    "EXM",
                    2025,
                    "Item_8",
                    group[0].subsection,
                    group[0].kind,
                    text[group[0].start:group[-1].end],
                    "",
                )
            )
        ) <= 512
        for group in bounded
    )


def test_item_15_exhibit_index_is_excluded():
    status, flags, action = quality(
        "table",
        "10.1 | Material agreement incorporated by reference",
        80,
        50,
        "EXHIBIT INDEX",
        "Item_15",
    )

    assert status == "failed"
    assert "exhibit_index_non_rag" in flags
    assert action == "exclude"


def test_orphan_table_end_marker_never_becomes_chunk_content():
    text = (
        "ITEM 16. FORM 10-K SUMMARY\n"
        "[TABLE_END:table_0047]\n\n"
        "Not applicable.\n"
    )

    units = semantic_units(text)
    values = [
        text[unit.start:unit.end]
        for unit in units
    ]

    assert values
    assert any("Not applicable." in value for value in values)
    assert all("[TABLE_END:" not in value for value in values)


def test_pursuant_signature_language_is_not_a_subsection_heading():
    assert not is_heading("Pursuant")


def test_late_financial_table_routes_to_item_8_without_losing_text():
    text = (
        "Net sales | $ | 11,287 | $ | 11,154\n"
        "Net income | $ | 29 | $ | 464"
    )
    rag_section, flags, forced_action = rag_content_policy(
        "table",
        text,
        "Consolidated Statements of Operations",
        "Item_15",
        "Consolidated Statements of Operations / FY2023 / FY2022",
    )

    assert rag_section == "Item_8"
    assert "late_financial_content_routed_to_item_8" in flags
    assert forced_action is None


def test_exhibit_inventory_is_excluded_even_after_item_16():
    text = (
        "Exhibit | Document\n"
        "10.1 | Credit Agreement incorporated herein by reference"
    )
    rag_section, flags, forced_action = rag_content_policy(
        "table",
        text,
        "Index to Exhibits",
        "Item_16",
    )

    assert rag_section == "Exhibit_Index"
    assert "exhibit_index_non_rag" in flags
    assert forced_action == "exclude"


def test_signature_container_is_excluded():
    rag_section, flags, forced_action = rag_content_policy(
        "narrative",
        (
            "Pursuant to the requirements of Section 13 or 15(d), "
            "the registrant has duly caused this report to be signed."
        ),
        "",
        "Signatures",
    )

    assert rag_section == "Signatures"
    assert "signature_non_rag" in flags
    assert forced_action == "exclude"


def test_item_16_auditor_consent_is_excluded():
    rag_section, flags, forced_action = rag_content_policy(
        "narrative",
        (
            "Consent of Independent Registered Public Accounting Firm. "
            "We hereby consent to the incorporation by reference."
        ),
        "",
        "Item_16",
    )

    assert rag_section == "Auditor_Consent"
    assert "auditor_consent_non_rag" in flags
    assert forced_action == "exclude"


def test_mid_sentence_split_gets_bounded_context_without_changing_chunk():
    text = (
        "The company may be unable to respond to changing consumer "
        "preferences, or to introduce new products on a timely basis."
    )
    start = text.index("or to introduce")
    context = continuation_context(text, start)
    chunk = text[start:]
    embedded = embedding_text(
        "Example Retailer",
        "EXM",
        2025,
        "Item_1A",
        "Risk Factors",
        "narrative",
        chunk,
        continuation_context=context,
    )

    assert context
    assert "changing consumer preferences," in context
    assert "Continuation context:" in embedded
    assert embedded.endswith(chunk)



def test_continuation_context_never_contains_table_markers():
    text = (
        "[TABLE_START:table_0001]\n"
        "Metric | FY2025 | FY2024\n"
        "[TABLE_END:table_0001]\n\n"
        "continued financial explanation without a new sentence."
    )

    start = text.index("continued financial")
    context = continuation_context(text, start)

    assert "[TABLE_START:" not in context
    assert "[TABLE_END:" not in context



def test_semantic_unit_boundaries_are_not_continuations():
    text = (
        "Liquidity\n\n"
        "Cash generated from operations increased.\n\n"
        "Capital expenditures remained stable."
    )

    units = semantic_units(text)

    assert len(units) == 2
    assert not inside_semantic_unit(units, units[0].start)
    assert not inside_semantic_unit(units, units[1].start)

    middle = units[0].start + 10
    assert inside_semantic_unit(units, middle)


def test_late_financial_region_inherits_across_note_narrative():
    region, rag_section, flags, forced = late_region_policy(
        "financial",
        "narrative",
        (
            "The Company operates in foreign countries and uses "
            "forward contracts to manage exchange-rate exposure."
        ),
        "Foreign Currency",
        "Item_16",
    )

    assert region == "financial"
    assert rag_section == "Item_8"
    assert flags == ["inherited_late_financial_region"]
    assert forced is None


def test_strong_exhibit_boundary_ends_financial_region():
    region, rag_section, flags, forced = late_region_policy(
        "financial",
        "table",
        "10.1 | Credit Agreement incorporated herein by reference",
        "EXHIBIT INDEX",
        "Signatures",
    )

    assert region == "exhibit"
    assert rag_section == "Exhibit_Index"
    assert "exhibit_index_non_rag" in flags
    assert forced == "exclude"


def test_cross_unit_grammar_gets_bidirectional_context():
    text = (
        "The Company depends on its systems and vendors, "
        "or may experience disruption and increased costs,"
        " including remediation expenses."
    )
    first_end = text.index(" including")
    second_start = text.index("or may")
    units = [
        Unit(0, second_start, "narrative", "Risk"),
        Unit(second_start, first_end, "narrative", "Risk"),
        Unit(first_end, len(text), "narrative", "Risk"),
    ]

    prior, forward = boundary_contexts(
        text,
        second_start,
        first_end,
        "narrative",
        units,
    )

    assert prior
    assert forward
    assert "systems and vendors" in prior
    assert "including remediation" in forward


def test_forward_context_is_embedding_metadata_not_chunk_mutation():
    chunk = "The credit facility remains committed through"
    value = embedding_text(
        "Example Retailer",
        "EXM",
        2025,
        "Item_8",
        "Debt",
        "narrative",
        chunk,
        forward_context="December 2028 subject to customary covenants.",
    )

    assert "Forward continuation context:" in value
    assert value.endswith(chunk)



def test_company_prefixed_financial_notes_heading_routes_to_item_8():
    region, rag_section, flags, forced_action = late_region_policy(
        "non_rag",
        "narrative",
        (
            "As of December 31, 2024, renegotiations represented "
            "1.4% of the loans receivable portfolio."
        ),
        (
            "MercadoLibre, Inc. Notes to Consolidated "
            "Financial Statements"
        ),
        "Signatures",
    )

    assert region == "financial"
    assert rag_section == "Item_8"
    assert flags == ["late_financial_content_routed_to_item_8"]
    assert forced_action is None



def test_numbered_and_named_financial_notes_restart_late_region():
    for subsection in (
        "NOTE 9 – LEASES",
        "18 – EARNINGS (LOSS) PER SHARE",
        "Segment Information and Concentrations",
        "Deferred Taxes",
        "Vehicle Floor Plan Notes Payable",
    ):
        region, rag_section, flags, forced_action = late_region_policy(
            "non_rag",
            "narrative",
            "Substantive company financial-note disclosure.",
            subsection,
            "Signatures",
        )

        assert region == "financial"
        assert rag_section == "Item_8"
        assert flags == ["late_financial_content_routed_to_item_8"]
        assert forced_action is None


def test_financial_decimal_row_is_not_an_exhibit_boundary():
    assert not strong_exhibit_boundary(
        "10.1 | 12.4 | 13.8",
        "Deferred Taxes",
    )

    assert strong_exhibit_boundary(
        (
            "10.1 | Credit Agreement incorporated by reference\n"
            "10.2 | Employment Agreement incorporated by reference"
        ),
        "EXHIBIT INDEX",
    )


def test_semicolon_continuation_retains_prior_context():
    text = (
        "(i) assets and liabilities at the reporting date;\n"
        "and (ii) revenue and expenses during the reported period."
    )
    position = text.index("and (ii)")

    assert continuation_context(text, position)


def test_dangling_boundary_variants_get_bidirectional_context():
    cases = (
        ("assets of the guarantors; and", "•a first-priority lien"),
        ("services for most RV components. In", "addition, we offer"),
        ("communicable diseases (such as", "COVID-19) could reduce demand"),
        ("management time. The", "Company may not prevail"),
        ("existing dealers; and", "•our ability to integrate locations"),
    )

    for prior, following in cases:
        text = prior + "\n\n" + following
        position = text.index(following)

        assert grammatical_continuation_end(text, position)
        assert grammatical_continuation_start(text, position)

        backward, _ = boundary_contexts(
            text,
            position,
            len(text),
            "narrative",
            [
                Unit(0, position, "narrative", "Risk"),
                Unit(position, len(text), "narrative", "Risk"),
            ],
        )
        _, forward = boundary_contexts(
            text,
            0,
            position,
            "narrative",
            [
                Unit(0, position, "narrative", "Risk"),
                Unit(position, len(text), "narrative", "Risk"),
            ],
        )

        assert backward
        assert forward


def test_grouping_joins_cross_unit_sentence_when_it_fits():
    import tiktoken

    from src.chunker_v2 import group_units

    text = (
        "The company offers repair services. In\n\n"
        "addition, it provides protection plans to customers."
    )
    split = text.index("addition")
    units = [
        Unit(0, split, "narrative", "Services"),
        Unit(split, len(text), "narrative", "Protection Plans"),
    ]
    groups = group_units(
        text,
        units,
        tiktoken.get_encoding("cl100k_base"),
        target=5,
        hard_max=100,
        overlap=0,
    )

    assert len(groups) == 1
    assert text[groups[0][0].start:groups[0][-1].end] == text


def test_page_number_between_sentence_fragments_is_ignored():
    text = (
        "decisions regarding product and marketing"
        "\n\n54\n\n"
        "spending across channels."
    )
    position = text.index("\n\n54")

    assert grammatical_continuation_end(text, position)
    assert "54" not in forward_continuation_context(text, position)


def test_complete_sentence_before_page_number_is_not_continuation():
    text = "The disclosure is complete.\n\n54\n\nNext Heading"
    position = text.index("\n\n54")

    assert not grammatical_continuation_end(text, position)


def test_auditor_region_propagates_through_cam_procedures():
    region, _, flags, forced = late_region_policy(
        "",
        "narrative",
        "Critical audit matters were communicated to the audit committee.",
        "Critical Audit Matters",
        "Signatures",
    )

    assert region == "auditor"
    assert flags == ["auditor_opinion"]
    assert forced == "exclude"

    region, _, flags, forced = late_region_policy(
        region,
        "narrative",
        (
            "With the assistance of our fair value specialists, "
            "we evaluated the weighted average cost of capital."
        ),
        "F-2",
        "Signatures",
    )

    assert region == "auditor"
    assert flags == ["auditor_opinion"]
    assert forced == "exclude"


def test_item_8_auditor_region_is_stateful():
    region, _, _, forced = late_region_policy(
        "",
        "narrative",
        "We have audited the accompanying consolidated balance sheets.",
        "Report of Independent Registered Public Accounting Firm",
        "Item_8",
    )
    assert region == "auditor"
    assert forced == "exclude"

    region, _, flags, forced = late_region_policy(
        region,
        "narrative",
        (
            "With the assistance of our valuation specialists, "
            "we tested management's forecast assumptions."
        ),
        "Goodwill Impairment Assessments",
        "Item_8",
    )
    assert region == "auditor"
    assert flags == ["auditor_opinion"]
    assert forced == "exclude"

    region, _, flags, forced = late_region_policy(
        region,
        "narrative",
        (
            "The matter is further described in the notes to the "
            "consolidated financial statements."
        ),
        "Description of the Matter",
        "Item_8",
    )
    assert region == "auditor"
    assert flags == ["auditor_opinion"]
    assert forced == "exclude"


def test_explicit_financial_statement_ends_auditor_region():
    region, rag_section, flags, forced = late_region_policy(
        "auditor",
        "table",
        "Cash and cash equivalents | $ | 100 | $ | 90",
        "Consolidated Balance Sheets",
        "Signatures",
        "Consolidated Balance Sheets / 2025 / 2024",
    )

    assert region == "financial"
    assert rag_section == "Item_8"
    assert flags == ["late_financial_content_routed_to_item_8"]
    assert forced is None


def test_company_management_evaluation_is_not_auditor_content():
    status, flags, action = quality(
        "narrative",
        (
            "We evaluated our store portfolio and performed remodeling "
            "work to improve the customer experience."
        ),
        80,
        50,
        "Store Operations",
        "Item_1",
    )

    assert status == "passed"
    assert "auditor_opinion" not in flags
    assert action == "include"


def test_item15_exhibit_rows_start_and_inherit_exhibit_region():
    region, rag_section, flags, forced = late_region_policy(
        "",
        "table",
        (
            "10.5 | Form of Restricted Stock Unit Award Agreement | "
            "S-1/A | 07/21/21 | 10.11\n"
            "10.6 | Form of Performance Award Agreement | "
            "S-1/A | 07/21/21 | 10.12"
        ),
        "Index To Consolidated Financial Statements",
        "Item_15",
    )
    assert region == "exhibit"
    assert rag_section == "Exhibit_Index"
    assert flags == ["exhibit_index_non_rag"]
    assert forced == "exclude"

    region, rag_section, flags, forced = late_region_policy(
        region,
        "narrative",
        (
            "*Filed herewith. The registrant undertakes to furnish "
            "copies of omitted instruments to the SEC upon request."
        ),
        "Apple Inc. | 2025 Form 10-K | 56",
        "Item_15",
    )
    assert region == "exhibit"
    assert rag_section == "Exhibit_Index"
    assert flags == ["exhibit_index_non_rag"]
    assert forced == "exclude"


def test_exhibit_region_resets_auditor_region():
    region, rag_section, flags, forced = late_region_policy(
        "auditor",
        "table",
        (
            "31.2 | Rule 13a-14(a) Certification of CFO\n"
            "32.1 | Section 1350 Certification of CEO"
        ),
        "Exhibit Index",
        "Item_15",
    )
    assert region == "exhibit"
    assert rag_section == "Exhibit_Index"
    assert flags == ["exhibit_index_non_rag"]
    assert forced == "exclude"


def test_auditor_cross_reference_does_not_restart_region():
    region, rag_section, flags, forced = late_region_policy(
        "financial",
        "narrative",
        (
            "See report of independent registered public accounting "
            "firm and notes to consolidated financial statements."
        ),
        "Stock Incentive Plans",
        "Signatures",
    )

    assert region == "financial"
    assert rag_section == "Item_8"
    assert flags == ["inherited_late_financial_region"]
    assert forced is None


def test_numbered_financial_note_ends_stale_auditor_region():
    region, rag_section, flags, forced = late_region_policy(
        "auditor",
        "list",
        "13. Other Shareholders’ Equity",
        "F-16",
        "Signatures",
    )

    assert region == "financial"
    assert rag_section == "Item_8"
    assert flags == ["late_financial_content_routed_to_item_8"]
    assert forced is None
