from __future__ import annotations

import argparse
import csv
import hashlib
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
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
    "source_size_bytes",
    "source_mtime_utc",
    "source_sha256",
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


@dataclass(frozen=True)
class SourceFingerprint:
    """Stable metadata used to tell whether a parsed-text input has changed."""

    size_bytes: int
    mtime_utc: str
    sha256: str


@dataclass(frozen=True)
class CompletionValidation:
    """Why an existing section set can or cannot be safely resumed."""

    complete: bool
    reasons: tuple[str, ...]

    @property
    def stale(self) -> bool:
        # A brand-new input has no rows by definition; do not report it as stale.
        if not self.reasons:
            return False
        new_input_reasons = {"missing_index_rows", "section_rows_do_not_match_expected"}
        return not (
            "missing_index_rows" in self.reasons
            and set(self.reasons).issubset(new_input_reasons)
        )


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


def _mtime_utc(mtime_ns: int) -> str:
    """Render an mtime with UTC and nanosecond precision for index comparison."""
    seconds, nanoseconds = divmod(mtime_ns, 1_000_000_000)
    timestamp = datetime.fromtimestamp(seconds, tz=timezone.utc)
    return f"{timestamp.strftime('%Y-%m-%dT%H:%M:%S')}.{nanoseconds:09d}Z"


def fingerprint_source_file(path: Path) -> SourceFingerprint:
    """Hash an input only when it remains stable while we read it."""
    for _ in range(2):
        before = path.stat()
        digest = hashlib.sha256()
        with path.open("rb") as source:
            for block in iter(lambda: source.read(1024 * 1024), b""):
                digest.update(block)
        after = path.stat()
        if before.st_size == after.st_size and before.st_mtime_ns == after.st_mtime_ns:
            return SourceFingerprint(
                size_bytes=after.st_size,
                mtime_utc=_mtime_utc(after.st_mtime_ns),
                sha256=digest.hexdigest(),
            )
    raise RuntimeError(f"Input changed while fingerprinting: {path}")


def _fingerprint_matches(row: dict, fingerprint: SourceFingerprint) -> bool:
    return (
        str(row.get("source_size_bytes", "")) == str(fingerprint.size_bytes)
        and str(row.get("source_mtime_utc", "")) == fingerprint.mtime_utc
        and str(row.get("source_sha256", "")) == fingerprint.sha256
    )


def _atomic_write_text(path: Path, text: str) -> None:
    """Write a section through ``.tmp`` so a disconnect cannot truncate it."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = path.with_name(f"{path.name}.tmp")
    try:
        with tmp_path.open("w", encoding="utf-8", newline="\n") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, path)
    except BaseException:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def clear_existing_sections(
    out_dir: Path,
    pdf_stem: str,
    keep_files: set[Path] | None = None,
) -> None:
    """Remove only obsolete files for one reprocessed parsed-text input."""
    if not out_dir.exists():
        return

    keep_names = {path.name for path in (keep_files or set())}
    prefix = f"{pdf_stem}__"
    for stale_file in out_dir.iterdir():
        if not stale_file.is_file() or not stale_file.name.startswith(prefix):
            continue
        if not (stale_file.name.endswith(".txt") or stale_file.name.endswith(".txt.tmp")):
            continue
        if stale_file.name.endswith(".txt") and stale_file.name in keep_names:
            continue
        stale_file.unlink()


def _output_sections(text: str) -> list[SectionSegment]:
    sections = [section for section in split_esg_sections(text) if section.text.strip()]
    if not sections:
        raise ValueError("Parsed text produced no non-empty ESG sections")

    seen_codes: set[str] = set()
    for section in sections:
        if section.section_code in seen_codes:
            raise ValueError(f"Duplicate generated section code: {section.section_code}")
        seen_codes.add(section.section_code)
    return sections


def _section_output_path(output_root: Path, ticker: str, pdf_stem: str, section_code: str) -> Path:
    return output_root / ticker / f"{pdf_stem}__{section_code}.txt"


def process_text_file(
    txt_file: Path,
    output_root: Path,
    *,
    text: str | None = None,
    source_fingerprint: SourceFingerprint | None = None,
    sections: list[SectionSegment] | None = None,
) -> list[dict]:
    """Build one PDF stem's sections, replacing no other PDF's files."""
    ticker = txt_file.parent.name.upper()
    pdf_stem = txt_file.stem
    text = text if text is not None else txt_file.read_text(encoding="utf-8", errors="replace")
    source_fingerprint = source_fingerprint or fingerprint_source_file(txt_file)
    sections = sections if sections is not None else _output_sections(text)
    if not sections:
        raise ValueError("Parsed text produced no non-empty ESG sections")

    page_spans = read_page_map(txt_file)
    ticker_out = output_root / ticker
    ticker_out.mkdir(parents=True, exist_ok=True)
    rows: list[dict] = []
    output_plan: list[tuple[Path, str]] = []

    search_pos = 0
    seen_codes: set[str] = set()
    for section in sections:
        section_text = section.text.strip()
        if not section_text:
            continue
        if section.section_code in seen_codes:
            raise ValueError(f"Duplicate generated section code: {section.section_code}")
        seen_codes.add(section.section_code)

        source_start, source_end = locate_text_span(text, section_text, search_pos)
        if source_start is not None and source_end is not None:
            search_pos = max(search_pos, source_start + 1)
        page_start, page_end = pages_for_span(page_spans, source_start, source_end)
        section_file = _section_output_path(output_root, ticker, pdf_stem, section.section_code)
        output_plan.append((section_file, section_text))
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
                "source_size_bytes": source_fingerprint.size_bytes,
                "source_mtime_utc": source_fingerprint.mtime_utc,
                "source_sha256": source_fingerprint.sha256,
            }
        )

    if not output_plan:
        raise ValueError("Parsed text produced no writable ESG sections")

    # Keep old files in place until every replacement has been atomically written.
    for section_file, section_text in output_plan:
        _atomic_write_text(section_file, section_text)
    clear_existing_sections(ticker_out, pdf_stem, {path for path, _ in output_plan})
    return rows


def discover_text_files(input_root: Path, ticker: str | None = None) -> list[Path]:
    if not input_root.exists():
        return []
    if ticker:
        return sorted((input_root / ticker.upper()).glob("*.txt"))
    return sorted(input_root.glob("*/*.txt"))


def _normalize_index_row(row: dict) -> dict:
    normalized = {
        field: "" if row.get(field) is None else str(row.get(field, ""))
        for field in SECTION_INDEX_FIELDS
    }
    normalized["ticker"] = normalized["ticker"].strip().upper()
    normalized["pdf_stem"] = normalized["pdf_stem"].strip()
    normalized["section_code"] = normalized["section_code"].strip().lower()
    return normalized


def _index_key(row: dict) -> tuple[str, str, str] | None:
    ticker = str(row.get("ticker", "")).strip().upper()
    pdf_stem = str(row.get("pdf_stem", "")).strip()
    section_code = str(row.get("section_code", "")).strip().lower()
    if not ticker or not pdf_stem or not section_code:
        return None
    return ticker, pdf_stem, section_code


def _index_rows_by_key(rows: list[dict]) -> tuple[dict[tuple[str, str, str], dict], set[tuple[str, str, str]]]:
    """Return one authoritative row per logical section-index key."""
    index_rows: dict[tuple[str, str, str], dict] = {}
    duplicate_keys: set[tuple[str, str, str]] = set()
    for raw_row in rows:
        row = _normalize_index_row(raw_row)
        key = _index_key(row)
        if key is None:
            continue
        if key in index_rows:
            duplicate_keys.add(key)
        # The newest row wins; successful reprocessing always writes it last.
        index_rows[key] = row
    return index_rows, duplicate_keys


def _resolve_stored_section_path(value: str) -> Path | None:
    if not value:
        return None
    path = Path(value)
    return path if path.is_absolute() else Path.cwd() / path


def _has_section_files_for_pdf(output_root: Path, ticker: str, pdf_stem: str) -> bool:
    out_dir = output_root / ticker
    if not out_dir.exists():
        return False
    prefix = f"{pdf_stem}__"
    return any(
        path.is_file() and path.name.startswith(prefix) and path.name.endswith(".txt")
        for path in out_dir.iterdir()
    )


def validate_completed_text_file(
    txt_file: Path,
    output_root: Path,
    source_fingerprint: SourceFingerprint,
    sections: list[SectionSegment],
    index_rows: dict[tuple[str, str, str], dict],
    duplicate_keys: set[tuple[str, str, str]] | None = None,
) -> CompletionValidation:
    """Validate rows, files, and fingerprints before a resume run skips a PDF."""
    ticker = txt_file.parent.name.upper()
    pdf_stem = txt_file.stem
    expected_by_code = {section.section_code: section.text.strip() for section in sections if section.text.strip()}
    reasons: set[str] = set()

    if not expected_by_code:
        reasons.add("no_expected_sections")

    matching_rows = {
        key[2]: row
        for key, row in index_rows.items()
        if key[0] == ticker and key[1] == pdf_stem
    }
    if not matching_rows:
        reasons.add("missing_index_rows")
        if _has_section_files_for_pdf(output_root, ticker, pdf_stem):
            reasons.add("unindexed_section_files")

    if set(matching_rows) != set(expected_by_code):
        reasons.add("section_rows_do_not_match_expected")
    if duplicate_keys and any(
        key[0] == ticker and key[1] == pdf_stem for key in duplicate_keys
    ):
        reasons.add("duplicate_index_rows")
    if matching_rows and any(
        not _fingerprint_matches(row, source_fingerprint) for row in matching_rows.values()
    ):
        reasons.add("source_fingerprint_mismatch")

    for section_code, expected_text in expected_by_code.items():
        row = matching_rows.get(section_code)
        if row is None:
            continue

        expected_path = _section_output_path(output_root, ticker, pdf_stem, section_code)
        if not expected_path.is_file() or expected_path.stat().st_size == 0:
            reasons.add("section_file_missing_or_empty")
            continue

        stored_path = _resolve_stored_section_path(row.get("section_file", ""))
        if stored_path is None or not stored_path.is_file():
            reasons.add("indexed_section_file_missing")
        elif stored_path.resolve() != expected_path.resolve():
            reasons.add("indexed_section_file_path_mismatch")

        actual_text = expected_path.read_text(encoding="utf-8", errors="replace")
        if actual_text != expected_text:
            reasons.add("section_file_content_mismatch")
        if str(row.get("char_count", "")) != str(len(expected_text)):
            reasons.add("char_count_mismatch")
        if str(row.get("word_count", "")) != str(word_count(expected_text)):
            reasons.add("word_count_mismatch")

    return CompletionValidation(complete=not reasons, reasons=tuple(sorted(reasons)))


def read_existing_index(index_path: Path) -> list[dict]:
    if not index_path.exists():
        return []
    with index_path.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        return [_normalize_index_row(row) for row in reader]


def write_index(index_path: Path, rows: list[dict]) -> None:
    """Atomically write a de-duplicated section index."""
    index_path.parent.mkdir(parents=True, exist_ok=True)
    index_rows, _ = _index_rows_by_key(rows)
    normalized_rows = sorted(
        index_rows.values(),
        key=lambda row: (row["ticker"], row["pdf_stem"], row["section_code"]),
    )
    tmp_path = index_path.with_name(f"{index_path.name}.tmp")
    try:
        with tmp_path.open("w", newline="", encoding="utf-8") as f:
            writer = csv.DictWriter(f, fieldnames=SECTION_INDEX_FIELDS)
            writer.writeheader()
            writer.writerows(normalized_rows)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, index_path)
    except BaseException:
        try:
            tmp_path.unlink(missing_ok=True)
        except OSError:
            pass
        raise


def upsert_index(
    index_path: Path,
    new_rows: list[dict],
    processed_keys: set[tuple[str, str]],
    replace_all: bool = False,
) -> None:
    """Replace a processed PDF stem's rows without duplicating any index keys."""
    if replace_all:
        index_rows, _ = _index_rows_by_key(new_rows)
    else:
        index_rows, _ = _index_rows_by_key(read_existing_index(index_path))
        normalized_processed_keys = {
            (str(processed_ticker).upper(), str(processed_stem))
            for processed_ticker, processed_stem in processed_keys
        }
        for key in list(index_rows):
            if (key[0], key[1]) in normalized_processed_keys:
                del index_rows[key]

    for raw_row in new_rows:
        row = _normalize_index_row(raw_row)
        key = _index_key(row)
        if key is not None:
            index_rows[key] = row
    write_index(index_path, list(index_rows.values()))


def _replace_pdf_rows(
    index_rows: dict[tuple[str, str, str], dict],
    ticker: str,
    pdf_stem: str,
    new_rows: list[dict],
) -> None:
    for key in list(index_rows):
        if key[0] == ticker and key[1] == pdf_stem:
            del index_rows[key]
    for raw_row in new_rows:
        row = _normalize_index_row(raw_row)
        key = _index_key(row)
        if key is not None:
            index_rows[key] = row


def run(
    input_root: str | Path,
    out: str | Path,
    index: str | Path,
    ticker: str | None = None,
    resume: bool = True,
    force: bool = False,
    checkpoint_every: int = 1,
) -> list[dict]:
    """Section parsed ESG text with restart-safe per-file checkpoints."""
    if checkpoint_every < 1:
        raise ValueError("checkpoint_every must be at least 1")

    input_root = Path(input_root)
    output_root = Path(out)
    index_path = Path(index)

    txt_files = discover_text_files(input_root, ticker=ticker)
    print(f"Found {len(txt_files)} parsed ESG text file(s) under {input_root}")

    existing_rows = read_existing_index(index_path)
    index_rows, duplicate_keys = _index_rows_by_key(existing_rows)
    rows: list[dict] = []
    summary = {
        "found_text_files": len(txt_files),
        "skipped_complete": 0,
        "sectioned": 0,
        "reprocessed_stale": 0,
        "failed": 0,
    }
    completed_since_checkpoint = 0

    for txt_file in txt_files:
        ticker_name = txt_file.parent.name.upper()
        try:
            source_fingerprint = fingerprint_source_file(txt_file)
            text = txt_file.read_text(encoding="utf-8", errors="replace")
            sections = _output_sections(text)
            validation = validate_completed_text_file(
                txt_file,
                output_root,
                source_fingerprint,
                sections,
                index_rows,
                duplicate_keys,
            )

            if resume and not force and validation.complete:
                summary["skipped_complete"] += 1
                print(f"  {ticker_name} {txt_file.stem}: skipped (complete)")
                continue

            file_rows = process_text_file(
                txt_file,
                output_root,
                text=text,
                source_fingerprint=source_fingerprint,
                sections=sections,
            )
            _replace_pdf_rows(index_rows, ticker_name, txt_file.stem, file_rows)
            duplicate_keys = {
                key for key in duplicate_keys if not (key[0] == ticker_name and key[1] == txt_file.stem)
            }
            rows.extend(file_rows)
            summary["sectioned"] += 1
            if validation.stale:
                summary["reprocessed_stale"] += 1
            completed_since_checkpoint += 1
            print(f"  {ticker_name} {txt_file.stem}: {len(file_rows)} section(s)")

            if completed_since_checkpoint >= checkpoint_every:
                write_index(index_path, list(index_rows.values()))
                completed_since_checkpoint = 0
        except Exception as exc:
            summary["failed"] += 1
            print(f"  {ticker_name} {txt_file.stem}: failed ({exc})")

    # A final atomic write also removes legacy duplicate/malformed rows after an all-skip resume run.
    write_index(index_path, list(index_rows.values()))
    print(f"Index saved to: {index_path}")
    print("Summary:")
    for field in ("found_text_files", "skipped_complete", "sectioned", "reprocessed_stale", "failed"):
        print(f"{field}: {summary[field]}")
    return rows


def _positive_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be an integer") from exc
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return parsed


def main():
    parser = argparse.ArgumentParser(description="Split parsed ESG report text into canonical sections.")
    parser.add_argument("--input", default="data/02_interim/esg_text")
    parser.add_argument("--out", default="data/03_sections/esg")
    parser.add_argument("--index", default="data/00_reference/esg_sections_index.csv")
    parser.add_argument("--ticker", default=None)
    resume_group = parser.add_mutually_exclusive_group()
    resume_group.add_argument(
        "--resume",
        dest="resume",
        action="store_true",
        help="Skip complete section sets (the default, recommended for EC2 runs).",
    )
    resume_group.add_argument(
        "--no-resume",
        dest="resume",
        action="store_false",
        help="Rebuild selected inputs without using completion checks.",
    )
    parser.set_defaults(resume=True)
    parser.add_argument("--force", action="store_true", help="Rebuild selected inputs even when complete.")
    parser.add_argument(
        "--checkpoint-every",
        type=_positive_int,
        default=1,
        metavar="N",
        help="Atomically write the index after every N sectioned files (default: 1).",
    )
    args = parser.parse_args()

    run(
        input_root=args.input,
        out=args.out,
        index=args.index,
        ticker=args.ticker.upper() if args.ticker else None,
        resume=args.resume,
        force=args.force,
        checkpoint_every=args.checkpoint_every,
    )


if __name__ == "__main__":
    main()
