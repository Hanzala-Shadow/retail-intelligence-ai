#!/usr/bin/env python3
"""Create deterministic ticker batches from the canonical historical manifest."""
import argparse,csv,hashlib,json
from pathlib import Path

def main():
    ap=argparse.ArgumentParser(); ap.add_argument('--manifest',type=Path,required=True); ap.add_argument('--output-dir',type=Path,required=True); ap.add_argument('--tickers-per-batch',type=int,default=10); a=ap.parse_args()
    with a.manifest.open(encoding='utf-8',newline='') as f: rows=list(csv.DictReader(f))
    tickers=sorted({r['ticker'] for r in rows}); a.output_dir.mkdir(parents=True,exist_ok=True)
    index=[]
    for n,start in enumerate(range(0,len(tickers),a.tickers_per_batch),1):
        group=tickers[start:start+a.tickers_per_batch]; selected=[r for r in rows if r['ticker'] in group]
        bid=f"batch-{n:03d}"; path=a.output_dir/f"{bid}.csv"
        with path.open('w',encoding='utf-8',newline='') as f:
            w=csv.DictWriter(f,fieldnames=rows[0].keys()); w.writeheader(); w.writerows(selected)
        sha=hashlib.sha256(path.read_bytes()).hexdigest(); index.append({'batch_id':bid,'tickers':group,'filings':len(selected),'manifest':path.name,'sha256':sha})
    (a.output_dir/'index.json').write_text(json.dumps({'status':'PASS','batches':index,'filings':sum(x['filings'] for x in index)},indent=2)+'\n')
    print(json.dumps({'status':'PASS','batches':len(index),'filings':sum(x['filings'] for x in index)},indent=2))
if __name__=='__main__': main()
