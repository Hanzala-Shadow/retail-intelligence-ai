from pathlib import Path

ROOT=Path(__file__).resolve().parents[2]

def test_schema_is_relational_only_and_isolated():
    sql=(ROOT/'database/annual_history_v1/V1__Annual_History_Relational_Schema.sql').read_text()
    assert 'annual_history_filings' in sql
    assert 'annual_history_chunks' in sql
    assert 'vector(' not in sql
    assert 'rag_eligible_10k_chunks' not in sql
    assert 'fy2325_v2_' not in sql

def test_manifest_builder_has_frozen_gates():
    source=(ROOT/'scripts/annual_history/build_manifest.py').read_text()
    assert '1752' in source and '1743' in source
    assert 'source hash mismatch' in source
    assert 'duplicate' not in source.lower() or "len({r['accession_number']" in source

def test_cleanup_is_scoped_and_requires_commit():
    source=(ROOT/'scripts/annual_history/cleanup_batch.py').read_text()
    assert "02_work" in source
    assert "('committed',)" in source
    assert 'shutil.rmtree(target)' in source
