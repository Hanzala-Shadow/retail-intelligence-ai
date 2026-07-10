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



ITEM_SEQUENCE = [
    "Item_1", "Item_1A", "Item_1B", "Item_1C",
    "Item_2", "Item_3", "Item_4",
    "Item_5", "Item_6", "Item_7", "Item_7A",
    "Item_8", "Item_9", "Item_9A", "Item_9B", "Item_9C",
    "Item_10", "Item_11", "Item_12", "Item_13",
    "Item_14", "Item_15", "Item_16",
]

EXPECTED_ITEM_TITLES = {
    "Item_1": [
        "business",
        "description of business",
        "business overview",
        "general",
    ],
    "Item_1A": ["risk factors"],
    "Item_1B": ["unresolved staff comments"],
    "Item_1C": ["cybersecurity"],
    "Item_2": ["properties"],
    "Item_3": ["legal proceedings"],
    "Item_4": ["mine safety disclosures"],
    "Item_5": [
        "market for registrant",
        "market for the registrant",
    ],
    "Item_6": [
        "reserved",
        "selected financial data",
    ],
    "Item_7": [
        "management's discussion and analysis",
        "managements discussion and analysis",
        "management discussion and analysis",
    ],
    "Item_7A": [
        "quantitative and qualitative disclosures",
    ],
    "Item_8": [
        "financial statements and supplementary data",
        "consolidated financial statements and supplementary data",
        "financial statements",
        "consolidated financial statements",
    ],
    "Item_9": [
        "changes in and disagreements with accountants",
    ],
    "Item_9A": ["controls and procedures"],
    "Item_9B": ["other information"],
    "Item_9C": [
        "disclosure regarding foreign jurisdictions",
    ],
    "Item_10": [
        "directors executive officers and corporate governance",
        "directors and executive officers",
        "directors",
    ],
    "Item_11": ["executive compensation"],
    "Item_12": ["security ownership"],
    "Item_13": [
        "certain relationships and related transactions",
    ],
    "Item_14": ["principal accountant fees and services"],
    "Item_15": [
        "exhibits and financial statement schedules",
        "exhibits financial statement schedules",
        "exhibits",
    ],
    "Item_16": ["form 10 k summary", "form 10-k summary"],
}

REFERENCE_PHRASES = (
    "of this report",
    "of this annual report",
    "included in item",
    "included elsewhere",
    "refer to item",
    "see item",
    "as discussed in",
    "appearing in item",
    "information required by this item",
)


def _normalize_candidate_title(value):
    value = value.lower()
    value = value.replace("’", "'").replace("—", "-").replace("–", "-")
    value = re.sub(r"^[\s\.,:;\"'“”()\-]+", "", value)
    value = re.sub(r"[^a-z0-9'\-]+", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def _candidate_score(section_code, raw_title, full_line):
    normalized = _normalize_candidate_title(raw_title)
    line_normalized = _normalize_candidate_title(full_line)

    if len(full_line.strip()) >= 240:
        return None

    expected = EXPECTED_ITEM_TITLES.get(section_code, [])

    exact_prefix = any(
        normalized.startswith(title)
        for title in expected
    )

    truncated_prefix = (
        len(normalized) >= 2
        and any(
            title.startswith(normalized)
            for title in expected
        )
    )

    # Some parsers split "Business" after its first character.
    item_1_business_fragment = (
        section_code == "Item_1"
        and normalized == "b"
    )

    if exact_prefix:
        score = 100
    elif truncated_prefix:
        score = 90
    elif item_1_business_fragment:
        score = 85
    elif not normalized:
        # Valid filings sometimes use standalone Item headings.
        score = 55
    else:
        return None

    reference_text = f"{normalized} {line_normalized}"
    if any(phrase in reference_text for phrase in REFERENCE_PHRASES):
        score -= 70

    return score if score >= 50 else None


def _collect_item_candidates(text):
    candidates = {code: [] for code in ITEM_SEQUENCE}

    bare_item_re = re.compile(
        r"^\s*(\d{1,2}[A-Za-z]?)\s*[\.\:\-\u2013\u2014]\s*(.*)$",
        re.IGNORECASE,
    )

    lines = text.splitlines(keepends=True)
    positions = []
    position = 0

    for line in lines:
        positions.append(position)
        position += len(line)

    def following_title(index):
        checked = 0

        for next_index in range(index + 1, len(lines)):
            value = lines[next_index].strip()

            if not value:
                continue

            checked += 1

            if checked > 3:
                break

            return value

        return ""

    for index, line in enumerate(lines):
        cleaned = line.strip()
        standard_match = ITEM_HEADING_RE.match(cleaned)
        bare_match = None if standard_match else bare_item_re.match(cleaned)
        match = standard_match or bare_match

        if not match:
            continue

        code = f"Item_{match.group(1).upper()}"

        if code not in candidates:
            continue

        raw_title = match.group(2).strip()
        full_line = cleaned

        score = _candidate_score(
            code,
            raw_title,
            full_line,
        )

        # Layout tables frequently place "Item 7." and its title in
        # adjacent cells/lines. Combine them for candidate scoring.
        if score is None or not _normalize_candidate_title(raw_title):
            continuation = following_title(index)

            if continuation:
                continuation_score = _candidate_score(
                    code,
                    continuation,
                    f"{cleaned} {continuation}",
                )

                if continuation_score is not None:
                    raw_title = continuation
                    full_line = f"{cleaned} {continuation}"
                    score = continuation_score

        if score is None:
            continue

        # Bare numbered candidates are accepted only with a recognized or
        # truncated SEC title. This prevents financial-note numbers from
        # becoming section boundaries.
        if bare_match and score < 85:
            continue

        candidates[code].append({
            "code": code,
            "position": positions[index],
            "line_number": index + 1,
            "score": score,
            "line": full_line,
        })

    # Add title-only candidates for filings where layout-table
    # extraction strips or truncates the "Item N" prefix.
    existing_pairs = {
        (candidate["code"], candidate["position"])
        for values in candidates.values()
        for candidate in values
    }

    for index, line in enumerate(lines):
        cleaned = line.strip()

        if not cleaned or len(cleaned) > 220:
            continue

        normalized = _normalize_candidate_title(cleaned)
        code = None

        if normalized in {
            "business",
            "business overview",
            "description of business",
        }:
            code = "Item_1"

        elif normalized in {
            "risk factors",
            "risk factors risk factors",
        } or normalized.startswith("risk factors "):
            code = "Item_1A"

        elif (
            normalized.startswith("management's discussion")
            or normalized.startswith("managements discussion")
            or normalized.startswith("management discussion")
        ):
            code = "Item_7"

        elif (
            normalized.startswith(
                "financial statements and supplementary"
            )
            or normalized.startswith(
                "consolidated financial statements and supplementary"
            )
        ):
            code = "Item_8"

        if (
            code
            and (code, positions[index]) not in existing_pairs
        ):
            candidates[code].append({
                "code": code,
                "position": positions[index],
                "line_number": index + 1,
                "score": 85,
                "line": cleaned,
                "title_only": True,
            })
            existing_pairs.add((code, positions[index]))

    # Penalize dense clusters containing several different SEC Items.
    # Such clusters are normally tables of contents rather than narrative
    # section starts.
    all_candidates = [
        candidate
        for values in candidates.values()
        for candidate in values
    ]

    for candidate in all_candidates:
        nearby_codes = {
            other["code"]
            for other in all_candidates
            if abs(
                other["line_number"] - candidate["line_number"]
            ) <= 40
        }

        if len(nearby_codes) >= 4:
            candidate["score"] = max(
                50,
                candidate["score"] - 60,
            )
            candidate["toc_cluster"] = True
        else:
            candidate["toc_cluster"] = False

    return candidates


def _select_ordered_candidates(candidates):
    """Select the best globally ordered SEC Item sequence.

    Dynamic selection prevents a late note heading such as
    "1. Business Operations" from blocking real Items 1A through 8.
    """
    order_index = {
        code: index
        for index, code in enumerate(ITEM_SEQUENCE)
    }

    nodes = sorted(
        [
            candidate
            for code in ITEM_SEQUENCE
            for candidate in candidates.get(code, [])
        ],
        key=lambda candidate: (
            order_index[candidate["code"]],
            candidate["position"],
        ),
    )

    if not nodes:
        return []

    minimum_major_lengths = {
        "Item_1": 1000,
        "Item_1A": 1000,
        "Item_7": 1000,
        "Item_8": 1000,
    }

    best_score = []
    previous_node = []

    for current_index, current in enumerate(nodes):
        # Recovering another canonical Item is more important than a
        # small difference in heading confidence.
        major_bonus = (
            5000
            if current["code"] in {
                "Item_1", "Item_1A", "Item_7", "Item_8"
            }
            else 0
        )
        current_score = 1000 + current["score"] + major_bonus
        best_score.append(current_score)
        previous_node.append(None)

        for prior_index in range(current_index):
            prior = nodes[prior_index]

            if (
                order_index[prior["code"]]
                >= order_index[current["code"]]
            ):
                continue

            if prior["position"] >= current["position"]:
                continue

            minimum_length = minimum_major_lengths.get(
                prior["code"],
                0,
            )
            actual_length = (
                current["position"] - prior["position"]
            )

            if minimum_length and actual_length < minimum_length:
                continue

            proposed_score = (
                best_score[prior_index]
                + 1000
                + current["score"]
                + major_bonus
            )

            if proposed_score > best_score[current_index]:
                best_score[current_index] = proposed_score
                previous_node[current_index] = prior_index

    final_index = max(
        range(len(nodes)),
        key=lambda index: best_score[index],
    )

    selected = []

    while final_index is not None:
        selected.append(nodes[final_index])
        final_index = previous_node[final_index]

    selected.reverse()
    return selected


def _find_signature_candidate(text, after_position):
    position = 0
    matches = []

    for line_number, line in enumerate(text.splitlines(keepends=True), 1):
        cleaned = line.strip()

        if (
            position > after_position
            and len(cleaned) < 80
            and SIGNATURES_RE.match(cleaned)
        ):
            matches.append({
                "code": "Signatures",
                "position": position,
                "line_number": line_number,
                "score": 100,
                "line": cleaned,
            })

        position += len(line)

    # The final exact Signatures heading is normally the actual signature block.
    return matches[-1] if matches else None


def _split_at_selected_boundaries(text, selected):
    sections = {}

    if not selected:
        return sections

    selected = sorted(selected, key=lambda candidate: candidate["position"])

    first_position = selected[0]["position"]
    header = text[:first_position].strip()

    if header:
        sections["HEADER"] = header

    for index, candidate in enumerate(selected):
        start = candidate["position"]
        end = (
            selected[index + 1]["position"]
            if index + 1 < len(selected)
            else len(text)
        )

        section_text = text[start:end].strip()

        if section_text:
            sections[candidate["code"]] = section_text

    return sections


def _collect_major_recovery_anchors(text):
    """Find major-section anchors even when Item prefixes are damaged."""
    anchors = []
    lines = text.splitlines(keepends=True)
    position = 0

    corrupted_item_re = re.compile(
        r"^\s*(?:TEM|M)\s*"
        r"(1A|1|7|8)"
        r"[\.\:\-\s]+(.*)$",
        re.IGNORECASE,
    )

    for line_number, line in enumerate(lines, 1):
        cleaned = line.strip()
        normalized = _normalize_candidate_title(cleaned)
        code = None
        score = 80

        if not cleaned or len(cleaned) > 280:
            position += len(line)
            continue

        if normalized in {
            "business",
            "business overview",
            "description of business",
        }:
            code = "Item_1"

        elif (
            normalized == "risk factors"
            or normalized.startswith("risk factors ")
            or normalized.startswith(
                "risk related to our business"
            )
            or normalized.startswith(
                "risks related to our business"
            )
            or normalized.startswith(
                "business and operating risks"
            )
        ):
            code = "Item_1A"

        elif (
            normalized.startswith(
                "management's discussion"
            )
            or normalized.startswith(
                "managements discussion"
            )
            or normalized.startswith(
                "management discussion"
            )
            or normalized.startswith(
                "nagement's discussion"
            )
            or normalized.startswith(
                "nagements discussion"
            )
        ):
            code = "Item_7"

        elif (
            normalized.startswith(
                "financial statements and supplementary"
            )
            or normalized.startswith(
                "consolidated financial statements"
            )
            or normalized.startswith(
                "index to consolidated financial statements"
            )
            or normalized.startswith(
                "index to financial statements"
            )
        ):
            code = "Item_8"

        if code is None:
            corrupted = corrupted_item_re.match(cleaned)

            if corrupted:
                item_number = corrupted.group(1).upper()
                damaged_title = corrupted.group(2).strip()
                candidate_code = f"Item_{item_number}"

                candidate_score = _candidate_score(
                    candidate_code,
                    damaged_title,
                    cleaned,
                )

                if candidate_score is not None:
                    code = candidate_code
                    score = candidate_score

        if code:
            anchors.append({
                "code": code,
                "position": position,
                "line_number": line_number,
                "line": cleaned,
                "score": score,
            })

        position += len(line)

    return anchors


def _recover_missing_major_sections(text, sections, filename):
    """Recover missing major sections from independent title anchors.

    This is intentionally limited to missing or implausibly short major
    sections and therefore cannot overwrite a healthy primary extraction.
    """
    major_codes = (
        "Item_1",
        "Item_1A",
        "Item_7",
        "Item_8",
    )

    needs_recovery = {
        code
        for code in major_codes
        if len(sections.get(code, "")) < 1000
    }

    if not needs_recovery:
        return sections

    anchors = _collect_major_recovery_anchors(text)

    if not anchors:
        return sections

    anchors = sorted(
        anchors,
        key=lambda anchor: anchor["position"],
    )

    for code in major_codes:
        if code not in needs_recovery:
            continue

        candidates = []

        for anchor in anchors:
            if anchor["code"] != code:
                continue

            start = anchor["position"]

            later_different = [
                other
                for other in anchors
                if (
                    other["position"] > start
                    and other["code"] != code
                )
            ]

            if later_different:
                end = later_different[0]["position"]
            else:
                end = len(text)

            recovered_text = text[start:end].strip()
            recovered_length = len(recovered_text)

            if recovered_length >= 1000:
                candidates.append({
                    "text": recovered_text,
                    "length": recovered_length,
                    "start": start,
                    "line": anchor["line"],
                    "score": anchor["score"],
                })

        if not candidates:
            continue

        # Prefer the largest plausible narrative span. A TOC heading
        # normally produces only a few hundred characters and is excluded.
        chosen = max(
            candidates,
            key=lambda candidate: (
                candidate["length"],
                candidate["score"],
            ),
        )

        sections[code] = chosen["text"]

        logging.warning(
            f"Recovered {code} for {filename} from "
            f"title anchor {chosen['line']!r}; "
            f"chars={chosen['length']}"
        )

    return sections


def split_sections(text, filename="Unknown"):
    """Split a 10-K using preselected, ordered SEC Item headings.

    Heading candidates are selected before section slicing. This prevents
    table-of-contents entries and later cross-references from overwriting or
    fragmenting valid narrative sections.
    """
    candidates = _collect_item_candidates(text)
    selected = _select_ordered_candidates(candidates)

    if selected:
        signature = _find_signature_candidate(
            text,
            selected[-1]["position"],
        )

        if signature:
            selected.append(signature)

    sections = _split_at_selected_boundaries(text, selected)
    sections = _recover_missing_major_sections(
        text,
        sections,
        filename,
    )

    useful_sections = {
        code for code in sections
        if code not in {"HEADER", "Signatures"}
    }

    if len(useful_sections) <= 2:
        title_sections = split_sections_by_title_fallback(
            text,
            filename=filename,
        )
        title_useful = {
            code for code in title_sections
            if code != "HEADER"
        }

        if len(title_useful) > 2:
            logging.warning(
                f"File {filename} needed title-based fallback. "
                f"Generated sections: {sorted(title_useful)}"
            )
            sections = title_sections
        else:
            anchor_sections = split_sections_by_anchor_fallback(
                text,
                filename=filename,
            )
            anchor_useful = {
                code for code in anchor_sections
                if code != "HEADER"
            }

            if len(anchor_useful) > 2:
                logging.warning(
                    f"File {filename} needed anchor-based fallback. "
                    f"Generated sections: {sorted(anchor_useful)}"
                )
                sections = anchor_sections
            else:
                logging.warning(
                    f"File {filename} failed systematic parsing extraction. "
                    "Invoking full-text fallback mechanism."
                )
                sections = {
                    "FULL_DOCUMENT_FALLBACK": text,
                }

    selected_codes = [candidate["code"] for candidate in selected]

    duplicate_candidates = {
        code: len(values)
        for code, values in candidates.items()
        if len(values) > 1
    }

    if duplicate_candidates:
        logging.warning(
            f"File {filename} heading candidates={duplicate_candidates}; "
            f"selected={selected_codes}"
        )

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
    input_dir = Path('data/02_interim/html_text')
    output_dir = Path('data/03_sections/10k')
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
    index_path = Path('data/00_reference/sections_index.csv')
    index_path.parent.mkdir(parents=True, exist_ok=True)
    
    with open(index_path, 'w', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(
            f,
            fieldnames=[
                'company',
                'section_code',
                'char_count',
                'file',
            ],
            lineterminator="\n",
        )
        writer.writeheader()
        writer.writerows(all_results)

    print(f"\nExecution Finished! {len(all_results)} total document sections saved to storage.")
    print(f"Production index mapping updated at {index_path}")

if __name__ == '__main__':
    main()