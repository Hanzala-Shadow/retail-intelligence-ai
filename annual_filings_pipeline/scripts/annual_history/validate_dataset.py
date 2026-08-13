#!/usr/bin/env python3
"""Database-only final acceptance gate for the corrected historical dataset."""
import json,os
from pathlib import Path
import psycopg2
from dotenv import load_dotenv

DATASET='annual-10k-fy2015-2025-v1'
EXPECTED={2015:129,2016:134,2017:138,2018:143,2019:147,2020:154,2021:161,2022:179,2023:186,2024:186,2025:186}
load_dotenv(Path('.env')); conn=psycopg2.connect(os.environ['DB_URL']); conn.autocommit=False
try:
 cur=conn.cursor(); checks={}
 for n,t in [('batches','annual_history_batches'),('filings','annual_history_filings'),('documents','annual_history_documents'),('sections','annual_history_sections'),('chunks','annual_history_chunks')]:
  cur.execute(f'SELECT count(*) FROM {t} WHERE dataset_id=%s',(DATASET,)); checks[n]=cur.fetchone()[0]
 cur.execute('SELECT coverage_year,count(*) FROM annual_history_filings WHERE dataset_id=%s GROUP BY coverage_year ORDER BY coverage_year',(DATASET,)); checks['coverage']=dict(cur.fetchall())
 cur.execute("SELECT count(*) FROM annual_history_batches WHERE dataset_id=%s AND status<>'committed'",(DATASET,)); checks['noncommitted_batches']=cur.fetchone()[0]
 cur.execute('SELECT count(*)-count(DISTINCT accession_number) FROM annual_history_filings WHERE dataset_id=%s',(DATASET,)); checks['duplicate_accessions']=cur.fetchone()[0]
 cur.execute('SELECT count(*)-count(DISTINCT (ticker,coverage_year)) FROM annual_history_filings WHERE dataset_id=%s',(DATASET,)); checks['duplicate_ticker_year']=cur.fetchone()[0]
 cur.execute('SELECT count(*) FROM annual_history_documents d JOIN annual_history_filings f ON f.filing_pk=d.filing_pk WHERE d.dataset_id=%s AND (d.coverage_year<>f.coverage_year OR d.source_sha256<>f.source_sha256)',(DATASET,)); checks['document_identity_mismatch']=cur.fetchone()[0]
 failures={}
 for key,val in {'filings':1743,'documents':1743,'coverage':EXPECTED,'noncommitted_batches':0,'duplicate_accessions':0,'duplicate_ticker_year':0,'document_identity_mismatch':0}.items():
  if checks.get(key)!=val: failures[key]={'actual':checks.get(key),'expected':val}
 status='PASS' if not failures else 'FAIL'; print(json.dumps({'status':status,'dataset':DATASET,'checks':checks,'failures':failures},indent=2))
 if failures: raise RuntimeError('dataset validation failed')
 cur.execute("UPDATE annual_history_datasets SET status='validated',validated_at=now() WHERE dataset_id=%s AND status='building'",(DATASET,)); conn.commit()
finally: conn.close()
