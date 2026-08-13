from src.chunker_v2 import late_region_policy, quality


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


def test_substantive_controls_cross_reference_remains_eligible():
    status, flags, action = quality(
        "narrative",
        (
            "Management concluded that internal control over financial "
            "reporting was effective. See the Report of Independent "
            "Registered Public Accounting Firm in Item 8."
        ),
        80,
        40,
        "Management’s Annual Report on Internal Control",
        "Item_9A",
    )
    assert status == "passed"
    assert "auditor_opinion" not in flags
    assert action == "include"


def test_substantive_note_cross_reference_remains_eligible():
    status, flags, action = quality(
        "narrative",
        (
            "The weighted average remaining lease term was 5.5 years. "
            "See report of independent registered public accounting "
            "firm and notes to consolidated financial statements."
        ),
        80,
        40,
        "Leases",
        "Signatures",
        policy_result=(
            "Item_8",
            ["inherited_late_financial_region"],
            None,
        ),
    )
    assert status == "passed"
    assert "auditor_opinion" not in flags
    assert action == "include"


def test_financial_statement_index_remains_non_rag():
    status, flags, action = quality(
        "table",
        (
            "Consolidated Financial Statements | Page\n"
            "Consolidated Balance Sheets | 40\n"
            "Consolidated Statements of Income | 41"
        ),
        80,
        40,
        "Index to Consolidated Financial Statements",
        "Item_8",
    )
    assert status == "failed"
    assert "financial_statement_index_non_rag" in flags
    assert action == "exclude"
