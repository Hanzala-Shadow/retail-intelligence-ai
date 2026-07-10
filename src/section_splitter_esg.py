from __future__ import annotations

import argparse
import csv
import re
from dataclasses import dataclass
from pathlib import Path


CANONICAL_SECTION_CODES = {
    "ceo_letter",
    "about_this_report",
    "environmental",
    "climate",
    "energy",
    "emissions",
    "waste",
    "water",
    "social",
    "human_capital",
    "diversity_equity_inclusion",
    "supply_chain_ethics",
    "community",
    "governance",
    "ethics_compliance",
    "data_summary",
    "appendix",
    "other",
    "full_document",
}

SECTION_INDEX_FIELDS = [
    "ticker",
    "pdf_stem",
    "section_code",
    "section_title",
    "section_file",
    "source_start_char",
    "source_end_char",
    "page_start",
    "page_end",
    "char_count",
    "word_count",
    "split_method",
    "confidence",
]

MIN_SECTION_CHARS = 300
BODY_SENTENCE_RE = re.compile(
    r"\b(describes?|includes?|focus(?:es)?|supports?|helps?|manages?|reports?|"
    r"provides?|improves?|reduces?|accounted|complaining|read|learn|refresh(?:ed)?|"
    r"conduct(?:ed|ing)?|complet(?:ed|ing)?|perform(?:ed|ing)?|identif(?:y|ied|ies)|"
    r"partner(?:ed|ing)?|committ(?:ed|ing)?|encompasses?|connect|source(?:d|s)?|"
    r"joined|announced|submitted|uses?|obtained|launched|incorporate|promoted|"
    r"established|weigh|record|continued|expanded|across|within|through)\b",
    re.IGNORECASE,
)

OPEN_ENDED_LAST_WORDS = {
    "about",
    "and",
    "for",
    "from",
    "in",
    "of",
    "on",
    "our",
    "the",
    "to",
    "with",
}


HEADING_PATTERNS: list[tuple[str, str]] = [
    ("ceo_letter", r"\bceo\s+welcome\b|\bceo\s+message\b|\b(?:letter|message)\s+(?:from\s+)?(?:our\s+)?(?:ceo|chief executive|leadership|president|founder)\b|\ba\s+message\s+from\s+our\s+ceo\b"),
    ("data_summary", r"\b(data\s+summary|esg\s+data|performance\s+data|metrics|kpis?|scorecard|performance\s+tables?|performance\s+at\s+a\s+glance|by\s+the\s+numbers|esg\s+ratings?|target\s+commitment\s+report|base\s+year\s+emissions\s+report|mitigation\s+action\s+report|voluntary\s+carbon\s+market\s+disclosure)\b"),
    ("appendix", r"\b(appendix|appendices|annex|assurance|limited\s+assurance|verification\s+statement|memberships?\s+and\s+associations?|complete\s+material\s+topic\s+list|(?:gri|sasb|tcfd|ungprf|sdgs?|esg|sustainability|disclosure|content|report(?:ing)?|context)\s+(?:content\s+)?index|(?:content|disclosure|report(?:ing)?|tcfd|sasb|gri|sdgs?|ungprf)\s+index|indices)\b"),
    ("about_this_report", r"^about$|\b(about\s+this\s+report|about\s+the\s+report|introduction|company\s+overview|about\s+[a-z0-9&.,'\s]+\s+brands?|sustainable\s+value\s+creation|value\s+creation\s+model|our\s+journey|esg\s+through\s+the\s+years|reporting\s+scope|reporting\s+framework|framework\s+alignment|gri|sasb|tcfd|materiality\s+(?:assessment|analysis|matrix|approach)?|double\s+materiality|material\s+esg\s+topics?|material\s+topics?)\b"),
    ("emissions", r"\b(greenhouse\s+gas|ghg|scope\s+1|scope\s+2|scope\s+3|emissions?|emissions?\s+reduction|carbon\s+footprints?|carbon\s+footprint\s+transparency|annual\s+ghg\s+inventory)\b"),
    ("climate", r"\b(climate|climate\s+change|climate\s+risk|climate\s+stability|decarbonization|net\s+zero|comfort\s+without\s+carbon|carbon\s+reduction)\b"),
    ("energy", r"\b(energy|renewable\s+energy|renewables?|electricity|powered\s+by\s+renewables?)\b"),
    ("waste", r"\b(waste|recycling|circularity|circular\s+economy|circular\s+design|end-of-life|packaging|bags\s+and\s+boxes|warding\s+off\s+waste)\b"),
    ("water", r"\b(water|wastewater|water\s+stewardship|water\s+leadership|water\s+goals|woman\s+\+\s+water|women\s+\+\s+water)\b"),
    ("environmental", r"\b(environmental|environment|planet|sustainability\s+strategy|sustainable\s+(raw\s+)?materials?|responsible\s+materials?|preferred\s+materials?|recycled\s+materials?|raw\s+materials?|materials?\s+(?:management|targets?|choices?|innovation|traceability|sourcing)|sustainable\s+cotton|sustainable\s+polyester|sustainable\s+ingredients?|chemicals?\s+management|chemical\s+safety|resource\s+management|resource\s+efficiency|material\s+footprint|bio-circular|product\s+carbon|product\s+life\s+cycle|biodiversity|natural\s+fibers?|synthetics|cellulosics|advanced\s+resource\s+recovery|circular\s+business\s+models?)\b|^materials?$"),
    ("diversity_equity_inclusion", r"\b(diversity|equity|inclusion|inclusivity|inclusive|dei|idea:|idea\s+alliance|belonging|representation|racial\s+justice|inclusive\s+behaviors?|empowering\s+women|women['’]s\s+equity|equality\s+and\s+belonging)\b"),
    ("supply_chain_ethics", r"\b(supply\s+chain|responsible\s+sourcing|sourcing|human\s+rights|supplier|vendor|factory|labor\s+rights|purchasing\s+practices|betterwork|workers?\s+voice|women\s+workers|fair\s+compensation|due\s+diligence|supply\s+chain\s+standards?|supplier\s+performance|communicating\s+with\s+workers|safe\s+working\s+conditions|assessment\s+and\s+remediation|capability\s+building|digital\s+wage\s+payments|gender-based\s+violence|gbv|textile\s+manufacturing|tier\s+[12]\s+suppliers|product\s+and\s+finishing)\b"),
    ("human_capital", r"\b(human\s+capital|employees?|associates?|workforce|our\s+people|people|talent|workplace|culture|our\s+values|total\s+rewards?|compensation|benefits?|wellbeing|well-being|mental\s+health|physical\s+health|health\s+and\s+safety|safe\s+and\s+inclusive\s+workplace|new\s+hires?|associate\s+networks?|people\s+data|real\s+opportunities|opportunity\s+hiring|enabling\s+opportunity|supervisory\s+skills|skills\s+training|personalized\s+growth|mentorship)\b"),
    ("community", r"\b(community|communities|philanthropy|volunteer|giving\s+back|giving|social\s+impact|nonprofit|relief\s+efforts|disaster\s+relief|crocs\s+cares|impact\s+partnerships?|partnerships?\s+that\s+make\s+a\s+difference|supporting\s+people\s+and\s+strengthening\s+our\s+communities|refugees|opportunity\s+village|honoring\s+native\s+origins|this\s+way\s+onward)\b"),
    ("social", r"^social$|^society$|\b(corporate\s+social\s+responsibility|social\s+impact|social\s+sustainability|social\s+compliance|social\s+responsibility)\b"),
    ("ethics_compliance", r"\b(ethics|compliance|code\s+of\s+conduct|anti-corruption|privacy|data\s+privacy|data\s+security|cyber\s*security|cybersecurity|consumer\s+trust|product\s+and\s+consumer\s+safety|product\s+safety)\b"),
    ("governance", r"\b(governance|corporate\s+governance|board\s+of\s+directors|board\s+(?:composition|oversight|committees?)|oversight|esg\s+oversight|risk\s+management|managing\s+esg\s+risk|responsible\s+business|business\s+practices|policies\s+and\s+guidelines|public\s+policy|stakeholder\s+engagement|external\s+commitments|general\s+disclosures)\b"),
]


@dataclass
class HeadingCandidate:
    line_index: int
    char_offset: int
    section_code: str
    title: str
    toc_like: bool


@dataclass
class SectionSegment:
    section_code: str
    title: str
    text: str
    split_method: str
    confidence: str


def display_path(path: str | Path) -> str:
    path = Path(path)
    try:
        return path.resolve().relative_to(Path.cwd().resolve()).as_posix()
    except ValueError:
        return path.as_posix()


def normalize_heading_text(line: str) -> str:
    cleaned = line.strip()
    cleaned = re.sub(r"\.{2,}\s*\d+\s*$", "", cleaned)
    cleaned = re.sub(r"\s{2,}\d{1,4}\s*$", "", cleaned)
    cleaned = re.sub(r"^[\dIVXLCM]+\s*[\.\)]\s+", "", cleaned, flags=re.IGNORECASE)
    cleaned = re.sub(r"\s+", " ", cleaned)
    return cleaned.strip(" -:\t")


def line_looks_structural(line: str) -> bool:
    stripped = line.strip()
    if not stripped:
        return False
    if len(stripped) > 180:
        return False
    words = re.findall(r"[A-Za-z][A-Za-z-]*", stripped)
    if len(words) > 10:
        return False
    lower = stripped.lower()
    lower = re.sub(r"\s+", " ", lower).strip()
    if words:
        last_word = words[-1].lower()
        if lower != "about" and last_word in OPEN_ENDED_LAST_WORDS:
            return False
    if re.search(r"^(read|learn)\s+more\b", lower):
        return False
    if re.search(r"^(you|we|this|these|those|they|it)\b", lower) and len(words) > 3:
        return False
    if re.search(r"^(in|during)\s+\d{4}\b", lower):
        return False
    if re.search(r"^last\s+year\b", lower):
        return False
    first_alpha = next((ch for ch in stripped if ch.isalpha()), "")
    if first_alpha and not first_alpha.isupper():
        return False
    if len(words) > 3 and BODY_SENTENCE_RE.search(stripped):
        return False
    if stripped.endswith(".") and len(stripped.split()) > 5:
        return False
    digit_count = sum(ch.isdigit() for ch in stripped)
    if digit_count and digit_count / max(len(stripped), 1) > 0.45:
        return False
    if stripped.count(",") >= 5:
        return False
    return True


def code_allowed_for_heading(code: str, normalized: str) -> bool:
    """Reject broad keyword hits that are usually body text, awards, or links."""
    if code == "about_this_report":
        if normalized == "about":
            return True
        if normalized.endswith(" about") or " read about" in normalized:
            return False
        if re.search(r"\b(complaining|accounted for|excited about|care about|passionate about)\b", normalized):
            return False

    if code == "appendix" and "index" in normalized:
        allowed_index = re.search(
            r"\b(gri|sasb|tcfd|sdg|sdgs|ungprf|esg|sustainability|disclosure|content|reporting|report|context)\b",
            normalized,
        )
        if not allowed_index and not re.search(r"\b(appendix|appendices|annex|assurance|verification)\b", normalized):
            return False

    if code == "environmental":
        if re.search(r"\b(educational|training|cash|incident|breach|handler|handling)\s+materials?\b", normalized):
            return False
        if re.search(r"\bmaterial\s+(cash|breach|incident|handler|handling|adverse|weakness)\b", normalized):
            return False

    if code == "human_capital":
        if re.search(r"\bpeople\s+and\s+wildlife\b", normalized):
            return False
        if re.search(r"\bconnect\s+people\s+and\b", normalized):
            return False
        if re.search(r"^at\s+[a-z0-9&.'-]+,\s+we\b", normalized):
            return False

    return True


def map_heading_to_code(line: str) -> str | None:
    title = normalize_heading_text(line)
    if not line_looks_structural(title):
        return None

    normalized = title.lower()
    normalized = normalized.replace("&", " and ")
    normalized = re.sub(r"[^a-z0-9\s-]", " ", normalized)
    normalized = re.sub(r"\s+", " ", normalized).strip()

    if len(normalized) < 3:
        return None

    for code, pattern in HEADING_PATTERNS:
        if re.search(pattern, normalized, flags=re.IGNORECASE):
            if not code_allowed_for_heading(code, normalized):
                continue
            return code
    return None


def has_page_reference(line: str) -> bool:
    stripped = line.strip()
    return bool(
        re.search(r"\.{2,}\s*\d{1,4}\s*$", stripped)
        or re.search(r"\s{2,}\d{1,4}\s*$", stripped)
    )


def collect_heading_candidates(text: str) -> list[HeadingCandidate]:
    lines = text.splitlines()
    total_chars = max(len(text), 1)
    offsets: list[int] = []
    offset = 0
    raw_candidates: list[HeadingCandidate] = []

    for i, line in enumerate(lines):
        offsets.append(offset)
        code = map_heading_to_code(line)
        if code:
            raw_candidates.append(
                HeadingCandidate(
                    line_index=i,
                    char_offset=offset,
                    section_code=code,
                    title=normalize_heading_text(line),
                    toc_like=has_page_reference(line),
                )
            )
        offset += len(line) + 1

    early_candidates = [c for c in raw_candidates if c.char_offset < total_chars * 0.10]
    toc_heavy = (
        len(early_candidates) >= 5
        and sum(1 for c in early_candidates if c.toc_like) / len(early_candidates) >= 0.5
    )

    filtered: list[HeadingCandidate] = []
    seen_positions: set[tuple[int, str]] = set()
    for candidate in raw_candidates:
        if toc_heavy and candidate.char_offset < total_chars * 0.10 and candidate.toc_like:
            continue
        key = (candidate.line_index, candidate.section_code)
        if key in seen_positions:
            continue
        seen_positions.add(key)
        filtered.append(candidate)

    return filtered


def confidence_for(code: str, text: str, fallback: bool = False) -> str:
    if fallback:
        return "low"
    char_count = len(text.strip())
    if code == "other":
        return "low" if char_count < 1000 else "medium"
    if char_count >= 1000:
        return "high"
    if char_count >= MIN_SECTION_CHARS:
        return "medium"
    return "low"


def build_segments(text: str, candidates: list[HeadingCandidate]) -> list[SectionSegment]:
    if not candidates:
        return [
            SectionSegment(
                "full_document",
                "Full Document",
                text.strip(),
                "full_document_fallback",
                "low",
            )
        ]

    lines = text.splitlines()
    segments: list[SectionSegment] = []

    first_line = candidates[0].line_index
    preamble = "\n".join(lines[:first_line]).strip()
    if len(preamble) >= MIN_SECTION_CHARS:
        segments.append(
            SectionSegment(
                "other",
                "Preamble",
                preamble,
                "heading_regex",
                confidence_for("other", preamble),
            )
        )

    for index, candidate in enumerate(candidates):
        next_line = candidates[index + 1].line_index if index + 1 < len(candidates) else len(lines)
        body = "\n".join(lines[candidate.line_index:next_line]).strip()
        if not body:
            continue
        segments.append(
            SectionSegment(
                candidate.section_code,
                candidate.title or candidate.section_code.replace("_", " ").title(),
                body,
                "heading_regex",
                confidence_for(candidate.section_code, body),
            )
        )

    if not segments:
        return [
            SectionSegment(
                "full_document",
                "Full Document",
                text.strip(),
                "full_document_fallback",
                "low",
            )
        ]

    return merge_short_segments(segments)


def merge_short_segments(segments: list[SectionSegment]) -> list[SectionSegment]:
    if len(segments) <= 1:
        return segments

    merged: list[SectionSegment] = []
    carry_prefix = ""

    for segment in segments:
        current = segment
        if carry_prefix:
            current = SectionSegment(
                current.section_code,
                current.title,
                f"{carry_prefix}\n\n{current.text}".strip(),
                current.split_method,
                confidence_for(current.section_code, f"{carry_prefix}\n\n{current.text}"),
            )
            carry_prefix = ""

        if len(current.text.strip()) < MIN_SECTION_CHARS:
            if merged:
                previous = merged[-1]
                merged[-1] = SectionSegment(
                    previous.section_code,
                    previous.title,
                    f"{previous.text}\n\n{current.text}".strip(),
                    previous.split_method,
                    confidence_for(previous.section_code, f"{previous.text}\n\n{current.text}"),
                )
            else:
                carry_prefix = current.text
            continue

        merged.append(current)

    if carry_prefix:
        if merged:
            previous = merged[-1]
            merged[-1] = SectionSegment(
                previous.section_code,
                previous.title,
                f"{previous.text}\n\n{carry_prefix}".strip(),
                previous.split_method,
                confidence_for(previous.section_code, f"{previous.text}\n\n{carry_prefix}"),
            )
        else:
            merged.append(
                SectionSegment(
                    "full_document",
                    "Full Document",
                    carry_prefix.strip(),
                    "full_document_fallback",
                    "low",
                )
            )

    return merged


def aggregate_by_code(segments: list[SectionSegment]) -> list[SectionSegment]:
    ordered_codes: list[str] = []
    by_code: dict[str, SectionSegment] = {}

    for segment in segments:
        if segment.section_code not in CANONICAL_SECTION_CODES:
            segment = SectionSegment(
                "other",
                segment.title,
                segment.text,
                segment.split_method,
                "low",
            )

        existing = by_code.get(segment.section_code)
        if existing is None:
            ordered_codes.append(segment.section_code)
            by_code[segment.section_code] = segment
            continue

        combined_text = f"{existing.text}\n\n{segment.text}".strip()
        by_code[segment.section_code] = SectionSegment(
            existing.section_code,
            existing.title,
            combined_text,
            existing.split_method,
            confidence_for(existing.section_code, combined_text),
        )

    return [by_code[code] for code in ordered_codes]


def split_esg_sections(text: str) -> list[SectionSegment]:
    candidates = collect_heading_candidates(text)

    if len(candidates) == 1 and candidates[0].char_offset > len(text) * 0.75:
        return [
            SectionSegment(
                "full_document",
                "Full Document",
                text.strip(),
                "full_document_fallback",
                "low",
            )
        ]

    return aggregate_by_code(build_segments(text, candidates))


def word_count(text: str) -> int:
    return len(re.findall(r"\b\w+\b", text))


def parse_int(value: str | int | None) -> int | None:
    try:
        if value in (None, ""):
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


def read_page_map(txt_file: Path) -> list[dict]:
    page_map = txt_file.with_suffix(".pages.csv")
    if not page_map.exists():
        return []
    with page_map.open(newline="", encoding="utf-8") as f:
        return list(csv.DictReader(f))


def pages_for_span(page_spans: list[dict], start: int | None, end: int | None) -> tuple[str, str]:
    if start is None or end is None or not page_spans:
        return "", ""

    pages: list[int] = []
    for row in page_spans:
        page_start = parse_int(row.get("char_start"))
        page_end = parse_int(row.get("char_end"))
        page_number = parse_int(row.get("page"))
        if page_start is None or page_end is None or page_number is None:
            continue
        if page_end <= start or page_start >= end:
            continue
        pages.append(page_number)

    if not pages:
        return "", ""
    return str(min(pages)), str(max(pages))


def _first_searchable_snippet(text: str) -> str:
    for line in text.splitlines():
        candidate = line.strip()
        if len(candidate) >= 20:
            return candidate[:240]
    return text.strip()[:240]


def _last_searchable_snippet(text: str) -> str:
    for line in reversed(text.splitlines()):
        candidate = line.strip()
        if len(candidate) >= 20:
            return candidate[-240:]
    return text.strip()[-240:]


def locate_text_span(source_text: str, section_text: str, start_hint: int = 0) -> tuple[int | None, int | None]:
    needle = section_text.strip()
    if not needle:
        return None, None

    exact_start = source_text.find(needle, start_hint)
    if exact_start < 0:
        exact_start = source_text.find(needle)
    if exact_start >= 0:
        return exact_start, exact_start + len(needle)

    start_snippet = _first_searchable_snippet(needle)
    end_snippet = _last_searchable_snippet(needle)
    if not start_snippet:
        return None, None

    start = source_text.find(start_snippet, start_hint)
    if start < 0:
        start = source_text.find(start_snippet)
    if start < 0:
        return None, None

    end = -1
    if end_snippet:
        end = source_text.find(end_snippet, start + len(start_snippet))
        if end < 0:
            end = source_text.rfind(end_snippet)
    if end >= start:
        return start, end + len(end_snippet)
    return start, start + len(start_snippet)


def clear_existing_sections(out_dir: Path, pdf_stem: str) -> None:
    if not out_dir.exists():
        return
    for stale_file in out_dir.glob(f"{pdf_stem}__*.txt"):
        stale_file.unlink()


def process_text_file(txt_file: Path, output_root: Path) -> list[dict]:
    ticker = txt_file.parent.name.upper()
    pdf_stem = txt_file.stem
    text = txt_file.read_text(encoding="utf-8", errors="replace")
    page_spans = read_page_map(txt_file)
    ticker_out = output_root / ticker
    ticker_out.mkdir(parents=True, exist_ok=True)
    clear_existing_sections(ticker_out, pdf_stem)

    sections = split_esg_sections(text)
    rows: list[dict] = []

    search_pos = 0
    for section in sections:
        section_text = section.text.strip()
        if not section_text:
            continue
        source_start, source_end = locate_text_span(text, section_text, search_pos)
        if source_start is not None and source_end is not None:
            search_pos = max(search_pos, source_start + 1)
        page_start, page_end = pages_for_span(page_spans, source_start, source_end)
        section_file = ticker_out / f"{pdf_stem}__{section.section_code}.txt"
        section_file.write_text(section_text, encoding="utf-8")
        rows.append(
            {
                "ticker": ticker,
                "pdf_stem": pdf_stem,
                "section_code": section.section_code,
                "section_title": section.title,
                "section_file": display_path(section_file),
                "source_start_char": source_start if source_start is not None else "",
                "source_end_char": source_end if source_end is not None else "",
                "page_start": page_start,
                "page_end": page_end,
                "char_count": len(section_text),
                "word_count": word_count(section_text),
                "split_method": section.split_method,
                "confidence": section.confidence,
            }
        )

    return rows


def discover_text_files(input_root: Path, ticker: str | None = None) -> list[Path]:
    if not input_root.exists():
        return []
    if ticker:
        return sorted((input_root / ticker.upper()).glob("*.txt"))
    return sorted(input_root.glob("*/*.txt"))


def read_existing_index(index_path: Path) -> list[dict]:
    if not index_path.exists():
        return []
    with index_path.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        return [{field: row.get(field, "") for field in SECTION_INDEX_FIELDS} for row in reader]


def write_index(index_path: Path, rows: list[dict]) -> None:
    index_path.parent.mkdir(parents=True, exist_ok=True)
    rows = sorted(rows, key=lambda r: (r.get("ticker", ""), r.get("pdf_stem", ""), r.get("section_code", "")))
    with index_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=SECTION_INDEX_FIELDS)
        writer.writeheader()
        writer.writerows(rows)


def upsert_index(index_path: Path, new_rows: list[dict], processed_keys: set[tuple[str, str]], replace_all: bool) -> None:
    if replace_all:
        rows = new_rows
    else:
        existing = read_existing_index(index_path)
        rows = [
            row
            for row in existing
            if (row.get("ticker", ""), row.get("pdf_stem", "")) not in processed_keys
        ]
        rows.extend(new_rows)
    write_index(index_path, rows)


def run(input_root: str | Path, out: str | Path, index: str | Path, ticker: str | None = None) -> list[dict]:
    input_root = Path(input_root)
    output_root = Path(out)
    index_path = Path(index)

    txt_files = discover_text_files(input_root, ticker=ticker)
    print(f"Found {len(txt_files)} parsed ESG text file(s) under {input_root}")

    rows: list[dict] = []
    processed_keys: set[tuple[str, str]] = set()
    for txt_file in txt_files:
        file_rows = process_text_file(txt_file, output_root)
        rows.extend(file_rows)
        processed_keys.add((txt_file.parent.name.upper(), txt_file.stem))
        print(f"  {txt_file.parent.name.upper()} {txt_file.stem}: {len(file_rows)} section(s)")

    upsert_index(index_path, rows, processed_keys, replace_all=ticker is None)
    print(f"Index saved to: {index_path}")
    return rows


def main():
    parser = argparse.ArgumentParser(description="Split parsed ESG report text into canonical sections.")
    parser.add_argument("--input", default="data/02_interim/esg_text")
    parser.add_argument("--out", default="data/03_sections/esg")
    parser.add_argument("--index", default="data/00_reference/esg_sections_index.csv")
    parser.add_argument("--ticker", default=None)
    args = parser.parse_args()

    run(
        input_root=args.input,
        out=args.out,
        index=args.index,
        ticker=args.ticker.upper() if args.ticker else None,
    )


if __name__ == "__main__":
    main()
