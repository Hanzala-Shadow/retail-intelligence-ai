import sys
sys.path.insert(0, 'src')
from section_splitter_10k import process_company
from pathlib import Path

input_dir = Path('data/02_interim/html_text')
output_dir = Path('data/03_sections')

tbhc_files = list(input_dir.glob('TBHC*.txt'))
print(f"Found {len(tbhc_files)} TBHC parsed files")

for txt_file in tbhc_files:
    results = process_company(txt_file, output_dir)
    print(f"  {txt_file.stem}: {len(results)} sections")