#!/usr/bin/env python3
"""Transactionally load one validated historical parse/section/chunk batch."""
from __future__ import annotations

import argparse, csv, hashlib, json, os
from pathlib import Path
import psycopg2
from dotenv import load_dotenv
from psycopg2.extras import Json, execute_values

DATASET = "annual-10k-fy2015-2025-v1"

def sha(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()

def jsonl(path: Path):
    with path.open(encoding="utf-8") as f:
        for n,line in enumerate(f,1):
            if line.strip():
                try: yield json.loads(line)
                except Exception as e: raise RuntimeError(f"{path}:{n}: invalid JSON") from e

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument('--batch-id',required=True); ap.add_argument('--manifest',type=Path,required=True)
    ap.add_argument('--parsed-root',type=Path,required=True); ap.add_argument('--sections-root',type=Path,required=True)
    ap.add_argument('--chunks-root',type=Path,required=True); ap.add_argument('--dataset-manifest-sha',required=True)
    ap.add_argument('--chunker-config-sha',required=True); ap.add_argument('--env-file',type=Path,default=Path('.env'))
    a=ap.parse_args(); load_dotenv(a.env_file); db=os.environ.get('DB_URL')
    if not db: raise RuntimeError('DB_URL unavailable')
    with a.manifest.open(encoding='utf-8',newline='') as f: filings=list(csv.DictReader(f))
    documents=list(jsonl(a.parsed_root/'parsed_documents.jsonl'))
    sections=list(jsonl(a.sections_root/'sections.jsonl'))
    chunks=list(jsonl(a.chunks_root/'chunks.jsonl'))
    if len(documents)!=len(filings): raise RuntimeError('document/filing count mismatch')
    if any(r['parse_status']!='passed' for r in documents): raise RuntimeError('batch contains non-passed parse')
    if {r['accession_number'] for r in documents}!={r['accession_number'] for r in filings}: raise RuntimeError('document accession mismatch')
    conn=psycopg2.connect(db); conn.autocommit=False
    try:
        cur=conn.cursor()
        cur.execute("""INSERT INTO annual_history_datasets
          (dataset_id,status,manifest_sha256,parser_version,splitter_version,chunker_version,chunker_config_sha256)
          VALUES (%s,'building',%s,'fy2325-html-v2.2','fy2325-section-v2.7','fy2325-chunker-v2.16',%s)
          ON CONFLICT (dataset_id) DO NOTHING""",(DATASET,a.dataset_manifest_sha,a.chunker_config_sha))
        cur.execute("SELECT status,manifest_sha256,chunker_config_sha256 FROM annual_history_datasets WHERE dataset_id=%s FOR UPDATE",(DATASET,))
        ds=cur.fetchone()
        if ds[0] not in ('building','failed') or ds[1]!=a.dataset_manifest_sha or ds[2]!=a.chunker_config_sha:
            raise RuntimeError(f'dataset identity/status mismatch: {ds}')
        cur.execute("SELECT status,manifest_sha256 FROM annual_history_batches WHERE dataset_id=%s AND batch_id=%s",(DATASET,a.batch_id))
        prior=cur.fetchone(); batch_sha=sha(a.manifest)
        if prior:
            if prior==('committed',batch_sha): print(json.dumps({'status':'ALREADY_COMMITTED','batch_id':a.batch_id})); conn.rollback(); return
            raise RuntimeError(f'batch identity/status conflict: {prior}')
        cur.execute("INSERT INTO annual_history_batches(dataset_id,batch_id,status,manifest_sha256,filing_count) VALUES (%s,%s,'loading',%s,%s)",(DATASET,a.batch_id,batch_sha,len(filings)))
        execute_values(cur,"""INSERT INTO annual_history_filings
          (dataset_id,batch_id,company_id,ticker,cik,coverage_year,filing_year,filing_date,accession_number,report_date,dei_fiscal_year_focus,form_type,source_file,source_sha256,fiscal_year_source,resolution_confidence,resolution_evidence)
          VALUES %s""",[(DATASET,a.batch_id,int(r['company_id']),r['ticker'],r['cik'],int(r['coverage_year']),int(r['filing_year']),r['filing_date'],r['accession_number'],r['report_date'] or None,int(r['dei_fiscal_year_focus']) if r['dei_fiscal_year_focus'] else None,r['form_type'],r['source_file'],r['source_sha256'],r['fiscal_year_source'],r['resolution_confidence'],Json(json.loads(r['resolution_evidence']))) for r in filings])
        cur.execute("SELECT accession_number,filing_pk FROM annual_history_filings WHERE dataset_id=%s",(DATASET,)); fmap=dict(cur.fetchall())
        execute_values(cur,"""INSERT INTO annual_history_documents
          (dataset_id,filing_pk,accession_number,company_id,ticker,coverage_year,source_sha256,text_sha256,parser_version,parser_config_sha256,parse_status,char_count,semantic_table_count,layout_table_count,quality_flags) VALUES %s""",[(DATASET,fmap[r['accession_number']],r['accession_number'],int(r['company_id']),r['ticker'],int(r['coverage_year']),r['source_sha256'],r['text_sha256'],r['parser_version'],r['parser_config_sha256'],r['parse_status'],int(r['char_count']),int(r['semantic_table_count']),int(r['layout_table_count']),Json(r['quality_flags'])) for r in documents])
        cur.execute("SELECT accession_number,document_pk FROM annual_history_documents WHERE dataset_id=%s",(DATASET,)); dmap=dict(cur.fetchall())
        svals=[]
        for r in sections:
            p=Path(r['output_file']); text=p.read_text(encoding='utf-8')
            if hashlib.sha256(text.encode()).hexdigest()!=r['section_text_sha256']: raise RuntimeError(f"section hash mismatch {r['section_id']}")
            svals.append((DATASET,dmap[r['accession_number']],r['section_id'],r['accession_number'],int(r['company_id']),r['ticker'],int(r['coverage_year']),r['canonical_section_code'],r['section_heading'],r['subsection_heading'],text,int(r['source_start_char']),int(r['source_end_char']),r['source_text_sha256'],r['section_text_sha256'],r['splitter_version'],r['splitter_config_sha256'],r['boundary_method'],r['boundary_confidence'],r['quality_status'],Json(r['quality_flags']),r['rag_action']))
        execute_values(cur,"""INSERT INTO annual_history_sections
          (dataset_id,document_pk,source_section_id,accession_number,company_id,ticker,coverage_year,canonical_section_code,section_heading,subsection_heading,section_text,source_start_char,source_end_char,source_text_sha256,section_text_sha256,splitter_version,splitter_config_sha256,boundary_method,boundary_confidence,quality_status,quality_flags,rag_action) VALUES %s""",svals,page_size=500)
        cur.execute("SELECT source_section_id,section_pk,document_pk FROM annual_history_sections WHERE dataset_id=%s",(DATASET,)); smap={x[0]:(x[1],x[2]) for x in cur.fetchall()}
        cvals=[]
        for r in chunks:
            sp,dp=smap[r['section_id']]
            cvals.append((DATASET,sp,dp,r['chunk_id'],r['section_id'],r['accession_number'],int(r['company_id']),r['ticker'],int(r['coverage_year']),int(r['chunk_index']),r['canonical_section_code'],r['rag_section_code'],r['subsection_heading'],r['chunk_type'],r['chunk_text'],r['embedding_text'],int(r['token_count']),int(r['embedding_token_count']),int(r['source_start_char']),int(r['source_end_char']),int(r['section_start_char']),int(r['section_end_char']),r['chunk_text_sha256'],r['embedding_text_sha256'],r['chunker_version'],r['chunker_config_sha256'],r['boundary_start_type'],r['boundary_end_type'],bool(r['continuation_from_previous']),bool(r['continues_to_next']),r['quality_status'],Json(r['quality_flags']),r['rag_action']))
        execute_values(cur,"""INSERT INTO annual_history_chunks
          (dataset_id,section_pk,document_pk,source_chunk_id,source_section_id,accession_number,company_id,ticker,coverage_year,chunk_index,canonical_section_code,rag_section_code,subsection_heading,chunk_type,chunk_text,embedding_text,token_count,embedding_token_count,source_start_char,source_end_char,section_start_char,section_end_char,chunk_text_sha256,embedding_text_sha256,chunker_version,chunker_config_sha256,boundary_start_type,boundary_end_type,continuation_from_previous,continues_to_next,quality_status,quality_flags,rag_action) VALUES %s""",cvals,page_size=500)
        cur.execute("UPDATE annual_history_batches SET status='committed',document_count=%s,section_count=%s,chunk_count=%s,committed_at=now() WHERE dataset_id=%s AND batch_id=%s",(len(documents),len(sections),len(chunks),DATASET,a.batch_id))
        conn.commit(); print(json.dumps({'status':'PASS','batch_id':a.batch_id,'filings':len(filings),'sections':len(sections),'chunks':len(chunks)},indent=2))
    except Exception:
        conn.rollback(); raise
    finally: conn.close()
if __name__=='__main__': main()
