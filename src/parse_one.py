import sys
sys.path.insert(0, 'src')
from html_parser import HTMLParser
from pathlib import Path

parser = HTMLParser(table_output_dir=Path('data/02_tables'))
out_dir = Path('data/02_interim/html_text')
out_dir.mkdir(parents=True, exist_ok=True)

files = list(Path('data/01_raw/10k/TBHC').glob('*.htm'))
print(f"Found {len(files)} TBHC files")

for f in files:
    doc = parser.parse(f, company='TBHC', doc_type='10-K')
    print(f"  {f.name}: {doc.status}, {doc.char_count} chars")
    if doc.status == 'ok':
        out_path = out_dir / f"TBHC__10-K__{f.stem}.txt"
        out_path.write_text(doc.raw_text, encoding='utf-8')
        print(f"  Saved: {out_path.name}")