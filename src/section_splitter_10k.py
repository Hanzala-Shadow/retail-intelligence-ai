import re
from pathlib import Path
import csv

# Same pattern from html_parser.py - finds "Item 1.", "Item 1A.", etc.
ITEM_HEADING_RE = re.compile(
    r'^\s*item\s*(\d{1,2}[A-Za-z]?)\s*[\.\:\-]?\s*(.*)$',
    re.IGNORECASE
)

def split_sections(text):
    """Split clean 10-K text into sections by Item headings."""
    sections = {}
    current_section = 'HEADER'
    current_lines = []

    for line in text.split('\n'):
        m = ITEM_HEADING_RE.match(line.strip())
        if m and len(line.strip()) < 200:
            # Save previous section
            if current_lines:
                sections[current_section] = '\n'.join(current_lines).strip()
            # Start new section
            num = m.group(1).upper()
            title = m.group(2).strip()
            current_section = f"Item_{num}"
            current_lines = [line]
        else:
            current_lines.append(line)

    # Save last section
    if current_lines:
        sections[current_section] = '\n'.join(current_lines).strip()

    return sections

def process_company(txt_file, output_dir):
    """Split one company's parsed text into sections."""
    text = txt_file.read_text(encoding='utf-8')
    sections = split_sections(text)

    company = txt_file.stem.split('__')[0]
    output_dir.mkdir(parents=True, exist_ok=True)

    results = []
    for section_code, section_text in sections.items():
        if not section_text.strip():
            continue
        out_file = output_dir / f"{txt_file.stem}__{section_code}.txt"
        out_file.write_text(section_text, encoding='utf-8')
        results.append({
            'company': company,
            'section_code': section_code,
            'char_count': len(section_text),
            'file': str(out_file)
        })

    return results

def main():
    input_dir = Path('data/02_parsed')
    output_dir = Path('data/03_sections')
    output_dir.mkdir(parents=True, exist_ok=True)

    txt_files = list(input_dir.rglob('*.txt'))
    print(f"Found {len(txt_files)} parsed files to split")

    all_results = []
    for txt_file in txt_files:
        results = process_company(txt_file, output_dir)
        all_results.extend(results)
        print(f"  {txt_file.stem}: {len(results)} sections")

    # Save index
    index_path = Path('data/00_reference/sections_index.csv')
    with open(index_path, 'w', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=['company', 'section_code', 'char_count', 'file'])
        writer.writeheader()
        writer.writerows(all_results)

    print(f"\nDone! {len(all_results)} sections extracted.")
    print(f"Index saved to {index_path}")

if __name__ == '__main__':
    main()