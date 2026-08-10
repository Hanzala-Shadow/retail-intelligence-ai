#!/usr/bin/env python3
"""Remove one committed batch workspace after verifying its database receipt."""
import argparse,os,shutil
from pathlib import Path
import psycopg2
from dotenv import load_dotenv

DATASET='annual-10k-fy2015-2025-v1'
ap=argparse.ArgumentParser(); ap.add_argument('batch_id'); ap.add_argument('--repo-root',type=Path,default=Path.cwd()); a=ap.parse_args()
if not a.batch_id.startswith('batch-') or not a.batch_id[6:].isdigit(): raise RuntimeError('invalid batch id')
root=(a.repo_root/'data_history/annual_10k_fy2015_2025_v1').resolve(); target=(root/'02_work'/a.batch_id).resolve()
if target.parent!=(root/'02_work').resolve() or not target.is_dir(): raise RuntimeError(f'unsafe or missing target: {target}')
receipt=root/'03_receipts'/f'{a.batch_id}.json'
if not receipt.is_file(): raise RuntimeError(f'missing receipt: {receipt}')
load_dotenv(a.repo_root/'.env'); conn=psycopg2.connect(os.environ['DB_URL']); conn.set_session(readonly=True,autocommit=False)
try:
 cur=conn.cursor(); cur.execute("SELECT status FROM annual_history_batches WHERE dataset_id=%s AND batch_id=%s",(DATASET,a.batch_id)); row=cur.fetchone()
 if row!=('committed',): raise RuntimeError(f'batch is not committed: {row}')
finally: conn.rollback(); conn.close()
shutil.rmtree(target)
print(f'PASS: removed committed workspace {target}')
