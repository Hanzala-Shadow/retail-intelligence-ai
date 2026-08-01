import re
from pathlib import Path
import csv
import logging
import _bootstrap  # noqa: F401  (import path: config, pipeline src, common)

import config

# Set up logging to catch parsing anomalies cleanly
log_dir = config.LOGS_DIR
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


# Last-resort fallback for filings where Item labels are stripped but section titles remain.
TITLE_HEADING_MAP = {
    "general": "Item_1",
    "business": "Item_1",
    "business overview": "Item_1",
    "overview": "Item_1",

    "risk factors": "Item_1A",
    "business and industry risks": "Item_1A",

    "unresolved staff comments": "Item_1B",
    "cybersecurity": "Item_1C",

    "properties": "Item_2",
    "legal proceedings": "Item_3",
    "mine safety disclosures": "Item_4",

    "market for registrant's common equity, related stockholder matters and issuer purchases of equity securities": "Item_5",
    "market for registrant’s common equity, related stockholder matters and issuer purchases of equity securities": "Item_5",
    "market for registrant's common equity": "Item_5",
    "market for registrant’s common equity": "Item_5",

    "selected financial data": "Item_6",

    "management's discussion and analysis of financial condition and results of operations": "Item_7",
    "management’s discussion and analysis of financial condition and results of operations": "Item_7",
    "management discussion and analysis of financial condition and results of operations": "Item_7",
    "management's discussion and analysis": "Item_7",
    "management’s discussion and analysis": "Item_7",

    "quantitative and qualitative disclosures about market risk": "Item_7A",
    "financial statements and supplementary data": "Item_8",

    "changes in and disagreements with accountants on accounting and financial disclosure": "Item_9",
    "controls and procedures": "Item_9A",
    "other information": "Item_9B",
    "disclosure regarding foreign jurisdictions that prevent inspections": "Item_9C",

    "directors, executive officers and corporate governance": "Item_10",
    "directors and executive officers": "Item_10",
    "executive officers": "Item_10",

    "executive compensation": "Item_11",

    "security ownership of certain beneficial owners and management and related stockholder matters": "Item_12",
    "security ownership of certain beneficial owners and management": "Item_12",

    "certain relationships and related transactions, and director independence": "Item_13",
    "certain relationships and related transactions": "Item_13",

    "principal accountant fees and services": "Item_14",

    "exhibits, financial statement schedules": "Item_15",
    "exhibits": "Item_15",

    "signatures": "Signatures",
    "signature": "Signatures",
}

def normalize_title_heading(line: str) -> str:
    s = line.strip().lower()
    s = s.replace("—", "-").replace("–", "-")
    s = s.replace("’", "'")
    s = re.sub(r"\s+", " ", s)
    s = re.sub(r"^[\.\-\:\s]+|[\.\-\:\s]+$", "", s)
    return s

def split_sections_by_title_fallback(text, filename="Unknown"):
    sections = {}
    current_section = "HEADER"
    current_lines = []

    for line in text.split("\n"):
        cleaned = line.strip()
        normalized = normalize_title_heading(cleaned)

        new_section_name = None
        if cleaned and len(cleaned) < 180:
            if normalized in TITLE_HEADING_MAP:
                new_section_name = TITLE_HEADING_MAP[normalized]

        if new_section_name:
            if current_lines:
                sections[current_section] = "\n".join(current_lines).strip()
            current_section = new_section_name
            current_lines = [line]
        else:
            current_lines.append(line)

    if current_lines:
        sections[current_section] = "\n".join(current_lines).strip()

    return sections

# Expected mandatory items we want to verify for tracking completeness
MANDATORY_ITEMS = {f"Item_{i}" for i in range(1, 16)}.union({
    "Item_1A", "Item_1B", "Item_1C", "Item_7A", "Item_9A", "Item_9B", "Signatures"
})


# Last-resort anchor fallback for companies where SEC Item headings are stripped
# and only narrative / company-style headings remain.
ANCHOR_FALLBACK_RULES = [
    # COLM / Columbia Sportswear style
    (r"^PRODUCT DESIGN AND INNOVATION$", "Item_1"),
    (r"^RISK FACTORS$", "Item_1A"),
    (r"^PROPERTIES$", "Item_2"),
    (r"^LEGAL PROCEEDINGS$", "Item_3"),
    (r"^MINE SAFETY DISCLOSURES$", "Item_4"),
    (r"^MARKET FOR REGISTRANT", "Item_5"),
    (r"^MANAGEMENT.?S DISCUSSION AND ANALYSIS", "Item_7"),
    (r"^QUANTITATIVE AND QUALITATIVE DISCLOSURES", "Item_7A"),
    (r"^FINANCIAL STATEMENTS", "Item_8"),
    (r"^CHANGES IN AND DISAGREEMENTS", "Item_9"),
    (r"^CONTROLS AND PROCEDURES$", "Item_9A"),
    (r"^OTHER INFORMATION$", "Item_9B"),
    (r"^EXHIBITS", "Item_15"),

    # SFIX / Stitch Fix style
    (r"^OVERVIEW$", "Item_1"),
    (r"^BUSINESS OVERVIEW$", "Item_1"),
    (r"^OUR BUSINESS$", "Item_1"),
    (r"^OUR COMPANY$", "Item_1"),
    (r"^RISK FACTOR SUMMARY$", "Item_1A"),
    (r"^RISK FACTORS$", "Item_1A"),
    (r"^RISKS RELATING TO OUR BUSINESS$", "Item_1A"),
    (r"^FINANCIAL OVERVIEW$", "Item_7"),
    (r"^INTEREST RATE RISK$", "Item_7A"),
    (r"^INFLATION RISK$", "Item_7A"),
    (r"^EVALUATION OF DISCLOSURE CONTROLS AND PROCEDURES$", "Item_9A"),
    (r"^MANAGEMENT.?S REPORT ON INTERNAL CONTROL OVER FINANCIAL REPORTING$", "Item_9A"),
    (r"^CHANGES IN INTERNAL CONTROL OVER FINANCIAL REPORTING$", "Item_9A"),
    (r"^EVALUATION OF DISCLOSURE CONTROLS AND PROCEDURES$", "Item_9A"),
    (r"^MANAGEMENT.?S REPORT ON INTERNAL CONTROL OVER FINANCIAL REPORTING$", "Item_9A"),
    (r"^CHANGES IN INTERNAL CONTROL OVER FINANCIAL REPORTING$", "Item_9A"),

    # VZ / Verizon style
    (r"^Verizon Communications Inc\. \(the Company\) is a holding company", "Item_1"),
    (r"^Verizon Communications Inc\. \(the Company\) is a holding company", "Item_1"),
    (r"^We have two reportable segments that we operate and manage as strategic business units", "Item_1"),
    (r"^Business Overview$", "Item_7"),
    (r"^Highlights of Our .* Financial Results$", "Item_7"),
    (r"^Critical Accounting Estimates$", "Item_7"),
    (r"^Opinion on Internal Control Over Financial Reporting$", "Item_8"),
    (r"^Opinion on the Financial Statements$", "Item_8"),
    (r"^Description of Business$", "Item_8"),

    # VZ risk-factor sections often appear as risk headlines instead of a Risk Factors heading.
    (r"^Adverse conditions in the .* economies could impact our results", "Item_1A"),
    (r"^Cyberattacks impacting our networks or systems could have an adverse effect", "Item_1A"),
    (r"^Cyber attacks impacting our networks or systems could have an adverse effect", "Item_1A"),
    (r"^We depend on key suppliers and vendors", "Item_1A"),
    (r"^Damage to our reputation or brands could adversely affect our business", "Item_1A"),
    (r"^Public health crises could materially adversely affect our business", "Item_1A"),
    (r"^Changes in the regulatory framework under which we operate", "Item_1A"),
    (r"^Our business may be impacted by changes in tax laws", "Item_1A"),
    (r"^Adverse changes in the financial markets", "Item_1A"),
    (r"^We are subject to risks associated with mergers", "Item_1A"),

    # Cybersecurity section inside Item 1C
    (r"^Integrated Cybersecurity Risk Management$", "Item_1C"),
    (r"^Board Oversight of Cybersecurity Risk$", "Item_1C"),
    (r"^Risks from Cybersecurity Threats$", "Item_1C"),

    # Generic
    (r"^SIGNATURES$", "Signatures"),
    (r"^Signature$", "Signatures"),
]

def split_sections_by_anchor_fallback(text, filename="Unknown"):
    """Last-resort fallback for filings where Item labels are absent from extracted text."""
    compiled = [(re.compile(pattern, re.IGNORECASE), section) for pattern, section in ANCHOR_FALLBACK_RULES]

    sections = {}
    current_section = "HEADER"
    current_lines = []
    seen_sections = set()

    for line in text.split("\n"):
        cleaned = line.strip()
        new_section_name = None

        if cleaned and len(cleaned) < 260:
            for rx, section_name in compiled:
                if rx.search(cleaned):
                    new_section_name = section_name
                    break

        if new_section_name:
            # Avoid repeatedly splitting the same SEC item on subheadings.
            # Exception: Signatures should always be allowed at the end.
            if new_section_name in seen_sections and new_section_name != "Signatures":
                current_lines.append(line)
                continue

            if current_lines:
                sections[current_section] = "\n".join(current_lines).strip()

            current_section = new_section_name
            seen_sections.add(new_section_name)
            current_lines = [line]
        else:
            current_lines.append(line)

    if current_lines:
        sections[current_section] = "\n".join(current_lines).strip()

    return sections


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

    # Fallback Mechanism: If standard Item/PART splitting failed,
    # try title-based fallback, then anchor-based fallback, before full-document fallback.
    if len(sections) <= 2:
        title_sections = split_sections_by_title_fallback(text, filename=filename)
        useful_sections = {k for k in title_sections if k != "HEADER"}

        if len(useful_sections) > 2:
            logging.warning(
                f"File {filename} needed title-based fallback. Generated sections: {sorted(useful_sections)}"
            )
            sections = title_sections
        else:
            anchor_sections = split_sections_by_anchor_fallback(text, filename=filename)
            anchor_useful_sections = {k for k in anchor_sections if k != "HEADER"}

            if len(anchor_useful_sections) > 2:
                logging.warning(
                    f"File {filename} needed anchor-based fallback. Generated sections: {sorted(anchor_useful_sections)}"
                )
                sections = anchor_sections
            else:
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
    company = txt_file.stem.split('__')[0] if '__' in txt_file.stem else txt_file.stem.split('_')[0]
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
    input_dir = config.HTML_TEXT_DIR
    output_dir = config.SECTIONS_10K_DIR
    output_dir.mkdir(parents=True, exist_ok=True)

    txt_files = [
        p for p in input_dir.rglob('*.txt')
        if '__10-K__' in p.name
    ]
    print(f"Found {len(txt_files)} parsed interim text files to split.")

    all_results = []
    for txt_file in txt_files:
        results = process_company(txt_file, output_dir)
        all_results.extend(results)
        print(f"  Processed {txt_file.stem}: Generated {len(results)} distinct text sections.")

    # Save tracking summary table
    index_path = config.SECTIONS_INDEX_CSV
    index_path.parent.mkdir(parents=True, exist_ok=True)
    
    with open(index_path, 'w', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=['company', 'section_code', 'char_count', 'file'])
        writer.writeheader()
        writer.writerows(all_results)

    print(f"\nExecution Finished! {len(all_results)} total document sections saved to storage.")
    print(f"Production index mapping updated at {index_path}")

if __name__ == '__main__':
    main()