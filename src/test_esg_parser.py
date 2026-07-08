import sys
sys.path.insert(0, 'src')
from pdf_parser import PDFParser
from pathlib import Path

parser = PDFParser()
input_dir = Path('data/01_raw/esg')
output_dir = Path('data/02_interim/esg_text')
output_dir.mkdir(parents=True, exist_ok=True)

pdf_files = list(input_dir.rglob('*.pdf'))
print(f"Found {len(pdf_files)} PDF files")

for pdf_file in pdf_files:
    company = pdf_file.parent.name
    print(f"\nParsing: {pdf_file.name} ({company})")
    
    doc = parser.parse(pdf_file, company=company, doc_type='sustainability')
    print(f"  Status: {doc.status}")
    print(f"  Characters: {doc.char_count}")
    
    if doc.status == 'ok' and doc.char_count > 0:
        out_file = output_dir / f"{company}__sustainability__{pdf_file.stem}.txt"
        out_file.write_text(doc.raw_text, encoding='utf-8')
        print(f"  Saved to: {out_file.name}")
    else:
        print(f"  Skipped - empty or error")