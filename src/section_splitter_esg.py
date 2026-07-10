import re
from pathlib import Path
import csv
import logging

log_dir = Path('logs')
log_dir.mkdir(exist_ok=True)
logging.basicConfig(
    filename=log_dir / 'esg_parse_errors.log',
    level=logging.WARNING,
    format='%(asctime)s - %(levelname)s - %(message)s'
)

# Common ESG chapter headings - keyword based since no standard "Item X" format exists
ESG_HEADING_MAP = {
    'ceo letter': 'CEO_Letter',
    'message from': 'CEO_Letter',
    'message from our ceo': 'CEO_Letter',
    'about this report': 'About_This_Report',
    'about the report': 'About_This_Report',
    'reporting scope': 'About_This_Report',
    
    'environmental': 'Environmental',
    'environment': 'Environmental',
    'climate': 'Environmental',
    'climate change': 'Environmental',
    'our planet': 'Environmental',
    
    'social': 'Social',
    'our people': 'Social',
    'people': 'Social',
    'community': 'Social',
    'diversity': 'Social',
    'workforce': 'Social',
    
    'governance': 'Governance',
    'corporate governance': 'Governance',
    'ethics': 'Governance',
    'board oversight': 'Governance',
    
    'data summary': 'Data_Summary',
    'esg data': 'Data_Summary',
    'performance data': 'Data_Summary',
    'metrics': 'Data_Summary',
    
    'awards': 'Awards_Recognition',
    'recognition': 'Awards_Recognition',
}

MIN_HEADING_LEN = 3
MAX_HEADING_LEN = 60

def is_esg_heading(line):
    """Check if a line matches a known ESG section heading."""
    cleaned = line.strip().lower()
    cleaned = re.sub(r'[^\w\s]', '', cleaned)  # remove punctuation
    
    if not (MIN_HEADING_LEN <= len(cleaned) <= MAX_HEADING_LEN):
        return None
    
    for keyword, section_code in ESG_HEADING_MAP.items():
        if cleaned == keyword or cleaned.startswith(keyword):
            return section_code
    
    return None

def split_esg_sections(text, filename="Unknown"):
    """Split ESG report text into sections by common chapter headings."""
    sections = {}
    current_section = 'HEADER'
    current_lines = []

    for line in text.split('\n'):
        cleaned_line = line.strip()
        section_code = is_esg_heading(cleaned_line)

        if section_code:
            if current_lines:
                sections[current_section] = '\n'.join(current_lines).strip()
            current_section = section_code
            current_lines = [line]
        else:
            current_lines.append(line)

    if current_lines:
        sections[current_section] = '\n'.join(current_lines).strip()

    if len(sections) <= 1:
        logging.warning(f"File {filename} failed ESG section extraction. Using full-text fallback.")
        sections['FULL_DOCUMENT_FALLBACK'] = text

    return sections

def process_esg_file(txt_file, output_dir):
    """Split one ESG report's parsed text into sections."""
    try:
        text = txt_file.read_text(encoding='utf-8')
    except Exception as e:
        logging.error(f"Failed to read file {txt_file.name}: {str(e)}")
        return []

    sections = split_esg_sections(text, filename=txt_file.name)
    company = txt_file.stem.split('_')[0] if '_' in txt_file.stem else txt_file.stem
    output_dir.mkdir(parents=True, exist_ok=True)

    results = []
    for section_code, section_text in sections.items():
        if not section_text.strip():
            continue
        out_file = output_dir / f"{txt_file.stem}__{section_code}.txt"
        try:
            out_file.write_text(section_text, encoding='utf-8')
            results.append({
                'company': company,
                'section_code': section_code,
                'char_count': len(section_text),
                'file': str(out_file)
            })
        except Exception as e:
            logging.error(f"Failed to write {out_file.name}: {str(e)}")

    return results

def main():
    input_dir = Path('data/02_interim/esg_text')
    output_dir = Path('data/03_sections/esg')
    output_dir.mkdir(parents=True, exist_ok=True)

    txt_files = list(input_dir.rglob('*.txt'))
    print(f"Found {len(txt_files)} parsed ESG text files to split.")

    all_results = []
    for txt_file in txt_files:
        results = process_esg_file(txt_file, output_dir)
        all_results.extend(results)
        print(f"  {txt_file.stem}: {len(results)} sections")

    index_path = Path('data/00_reference/esg_sections_index.csv')
    with open(index_path, 'w', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=['company', 'section_code', 'char_count', 'file'])
        writer.writeheader()
        writer.writerows(all_results)

    print(f"\nDone! {len(all_results)} ESG sections saved.")
    print(f"Index saved to {index_path}")

if __name__ == '__main__':
    main()