from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_schema_is_coexistence_safe():
    sql = (ROOT / "V2__FY2325_Coexistence_Schema.sql").read_text()
    for table in ("companies", "annual_filings", "documents", "sections", "chunks"):
        assert f"CREATE TABLE {table} " not in sql
    assert "fy2325_v2_chunks" in sql
    assert "source_chunk_id TEXT NOT NULL" in sql
    assert "coverage_year SMALLINT" in sql
    assert "vector(768)" in sql


def test_frozen_identities_are_pinned():
    source = (ROOT / "load_fy2325_v2_staging.py").read_text()
    assert "fd05c470aaf63be6ae0524c016f7eee61747976721e6f12aa242033b8badc4eb" in source
    assert "f79dc25715bc364ceb21d2a84e433066f8177aa0822e1db9c18a67b6514fd936" in source
    assert "a5beb1e3e68b9ab74eb54cfd186867f64f240e1a" in source
    assert '"filings": 561' in source
    assert '"embeddings": 158570' in source


def test_cutover_fails_closed_on_count():
    sql = (ROOT / "cutover_fy2325_v2.sql").read_text()
    assert "<> 158570" in sql
    assert "RAISE EXCEPTION" in sql
