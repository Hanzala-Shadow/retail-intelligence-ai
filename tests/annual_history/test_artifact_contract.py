from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]


def test_base_schema_matches_emitted_domains():
    sql = (
        ROOT
        / "database/annual_history_v1/"
        "V1__Annual_History_Relational_Schema.sql"
    ).read_text()

    assert (
        "parse_status IN ('passed','review_required')"
        in sql
    )
    assert (
        "boundary_confidence IN ('high','medium','low')"
        in sql
    )
    assert (
        "quality_status IN ('passed','review_required')"
        in sql
    )
    assert (
        "rag_action IN "
        "('include','exclude','review_required')"
        in sql
    )


def test_domain_alignment_migration():
    sql = (
        ROOT
        / "database/annual_history_v1/"
        "V3__Align_Artifact_Domains.sql"
    ).read_text()

    assert (
        "parse_status IN ('passed','review_required')"
        in sql
    )
    assert (
        "boundary_confidence IN ('high','medium','low')"
        in sql
    )


def test_loader_validates_before_connecting():
    source = (
        ROOT / "scripts/annual_history/load_batch.py"
    ).read_text()

    call = source.rindex("validate_artifact_contract(")
    connection = source.index("conn=psycopg2.connect(db)")

    assert call < connection
