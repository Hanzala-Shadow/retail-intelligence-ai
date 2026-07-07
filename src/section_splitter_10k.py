import re
from pathlib import Path
import csv
import logging

# Set up logging to catch parsing anomalies cleanly
log_dir = Path('logs')
log_dir.mkdir(exist_ok=True)
logging.basicConfig(
    filename=log_dir / 'parse_errors.log',
    level=logging.WARNING,
    format='%(asctime)s - %(levelname)s - %(message)s'
)

# HARDENED REGEX: Captures multi-spaces, varied punctuation, and optional item labels
ITEM_HEADING_RE = re.compile(
    r'^\s*ITEM\s+(\d{1,2}[A-Za-z]?)(?:\b|[\.\:\-\—])\s*(.*)$',
    re.IGNORECASE
)

SIGNATURES_RE = re.compile(
    r'^\s*(?:SIGNATURES|SIGNATURE)\s*$',
    re.IGNORECASE
)

# Fix Type 1: PART I/II/III/IV style companies like AMZN, KR, VFC
PART_RE = re.compile(
    r'^\s*PART\s+(I{1,3}V?|IV)\s*$',
    re.IGNORECASE
)

# Expected mandatory items we want to verify for tracking completeness
MANDATORY_ITEMS = {f"Item_{i}" for i in range(1, 16)}.union({
    "Item_1A", "Item_1B", "Item_1C", "Item_7A", "Item_9A", "Item_9B", "Signatures"
})

def split_sections(text, filename="Unknown"):
    """Split clean 10-K text into sections by Item headings and Signatures.
    
    Hardened against TOC listings using character position.
    Supports PART I/II/III/IV style companies.
    Supports Item numbers without titles (e.g. EBAY).
    """
    sections = {}
    current_section = 'HEADER'
    current_lines = []
    char_count = 0  # Track position in document

    for line in text.split('\n'):
        char_count += len(line) + 1
        cleaned_line = line.strip()
        
        # Check for standard item match, signature block, and PART heading
        m = ITEM_HEADING_RE.match(cleaned_line)
        is_sig = SIGNATURES_RE.match(cleaned_line)
        is_part = PART_RE.match(cleaned_line)
        
        is_divider = False
        new_section_name = None
        
        # Check signatures
        if is_sig and len(cleaned_line) < 200:
            is_divider = True
            new_section_name = "Signatures"

        # Fix Type 1: Check PART headings (AMZN, KR, VFC style)
        elif is_part and len(cleaned_line) < 50:
            is_divider = True
            part_num = is_part.group(1).upper()
            new_section_name = f"Part_{part_num}"

        # Check standard Item headings
        elif m and len(cleaned_line) < 200:
            num = m.group(1).upper()
            title = m.group(2).strip()
            cleaned_title = re.sub(r'[\.\:\-\—\s]', '', title)
            
            # Fix Type 2: Use character position instead of title length for TOC detection
            # If we're past 10,000 chars, empty title is a real heading (EBAY style)
            # If we're before 10,000 chars and title is empty, it's a TOC entry
            is_toc_line = (len(cleaned_title) < 3 and char_count < 10000)
            
            if not is_toc_line:
                is_divider = True
                new_section_name = f"Item_{num}"

        if is_divider and new_section_name:
            # Save previous section
            if current_lines:
                sections[current_section] = '\n'.join(current_lines).strip()
            
            # Start new section
            current_section = new_section_name
            current_lines = [line]
        else:
            current_lines.append(line)

    # Catch the remaining tail section
    if current_lines:
        sections[current_section] = '\n'.join(current_lines).strip()

    # Fallback Mechanism: If parsing failed entirely, dump full text into a fallback bucket
    if len(sections) <= 2:
        logging.warning(f"File {filename} failed systematic parsing extraction. Invoking full-text fallback mechanism.")
        sections['FULL_DOCUMENT_FALLBACK'] = text

    return sections

def process_company(txt_file, output_dir):
    """Split one company's parsed text into sections with integrated QA logging."""
    try:
        text = txt_file.read_text(encoding='utf-8')
    except Exception as e:
        logging.error(f"Failed to read file {txt_file.name}: {str(e)}")
        return []

    sections = split_sections(text, filename=txt_file.name)
    
    # Parse out company details safely matching expected naming schema
    company = txt_file.stem.split('_')[0] if '_' in txt_file.stem else txt_file.stem
    output_dir.mkdir(parents=True, exist_ok=True)

    # QA Check: Check what we missed against our expected target items
    found_sections = set(sections.keys())
    missing_items = MANDATORY_ITEMS - found_sections
    if missing_items and 'FULL_DOCUMENT_FALLBACK' not in sections:
        logging.warning(f"Company {company} ({txt_file.name}) missing sections: {sorted(list(missing_items))}")

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
            logging.error(f"Failed to write output section file {out_file.name}: {str(e)}")

    return results

def main():
    # Input and output directory reflecting repository folder conventions
    input_dir = Path('data/02_interim')
    output_dir = Path('data/03_sections')
    output_dir.mkdir(parents=True, exist_ok=True)

    txt_files = list(input_dir.rglob('*.txt'))
    print(f"Found {len(txt_files)} parsed interim text files to split.")

    all_results = []
    for txt_file in txt_files:
        results = process_company(txt_file, output_dir)
        all_results.extend(results)
        print(f"  Processed {txt_file.stem}: Generated {len(results)} distinct text sections.")

    # Save tracking summary table
    index_path = Path('data/00_reference/sections_index.csv')
    index_path.parent.mkdir(parents=True, exist_ok=True)
    
    with open(index_path, 'w', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=['company', 'section_code', 'char_count', 'file'])
        writer.writeheader()
        writer.writerows(all_results)

    print(f"\nExecution Finished! {len(all_results)} total document sections saved to storage.")
    print(f"Production index mapping updated at {index_path}")

if __name__ == '__main__':
    main()