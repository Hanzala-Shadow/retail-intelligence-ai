from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import re
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from pathlib import Path
import _bootstrap  # noqa: F401  (import path: config, pipeline src, common)

import config
from esg_compact_toc import CompactTocEntry, detect_compact_toc_entries


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
    "section_instance_id",
    "section_code",
    "section_title",
    "subsection_spans_json",
    "section_file",
    "source_start_char",
    "source_end_char",
    "provenance_version",
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

PROVENANCE_VERSION = "contiguous_v3"

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

LONG_PROSE_PREDICATE_RE = re.compile(
    r"\b(?:is|are|was|were|has|have|had|powered|drives?|redesigned|addressed|"
    r"serves?|speaks?|summarizes?|implements?|implementing|leads?|called|filed|"
    r"represents?|estimates?|calculates?|developed|issued|reported|proceeds|"
    r"strives?|encourages?|recognizes?|depends?|suffers?|remains?|played|"
    r"initiated|mobilized|deployed|conducted|piloted|purchased|participated|"
    r"conserves?|keeps?|investigates?|requires?|makes?|works?|working)\b|"
    r"\b(?:we|they|it|apple|nike|deckers|target|tjx)['\u2019]?(?:ve|re|s|d)\b",
    re.IGNORECASE,
)

OPEN_ENDED_LAST_WORDS = {
    "a",
    "an",
    "about",
    "among",
    "and",
    "at",
    "by",
    "can",
    "for",
    "from",
    "in",
    "including",
    "of",
    "on",
    "or",
    "other",
    "our",
    "pushing",
    "the",
    "to",
    "with",
}

PAGE_CHROME_MAX_OFFSET = 500
PAGE_CHROME_MIN_PAGES = 2
PAGE_CHROME_MAX_PAGE_GAP = 2
NAVIGATION_CHROME_MIN_OCCURRENCES = 3

NAVIGATION_CHROME_TERMS = {
    "appendix",
    "approach",
    "communities",
    "contents",
    "environment",
    "environmental",
    "glossary",
    "governance",
    "indexes",
    "initiatives",
    "introduction",
    "overview",
    "social",
    "targets",
    "workplace",
}

# These are report-navigation labels, not section-code keywords.  Keeping this
# list separate avoids teaching the splitter company-specific title strings.
NAVIGATION_TOPIC_LABELS = (
    "introduction",
    "climate action",
    "circular economy",
    "digital inclusion",
    "inclusive workforce",
    "people in supply chains",
    "product supply chain sustainability",
    "animal welfare",
    "environmental",
    "social",
    "governance",
    "appendix",
    "indexes and glossary",
)


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
    heading_confidence: str = "medium"


@dataclass(frozen=True)
class SubsectionSpan:
    """One accepted heading and the source range where it is active."""

    title: str
    section_code: str
    source_start_char: int
    source_end_char: int
    heading_confidence: str


@dataclass
class SectionSegment:
    section_code: str
    title: str
    text: str
    split_method: str
    confidence: str
    source_start_char: int | None = None
    source_end_char: int | None = None
    section_instance_id: str = ""
    # This describes the detected boundary, independently of the amount of
    # body text that later landed in the section.
    heading_confidence: str = "low"
    subsection_spans: tuple[SubsectionSpan, ...] = ()


@dataclass(frozen=True)
class CompactTocPage:
    """One page proven to be a compact table of contents."""

    page_number: int
    char_start: int
    char_end: int
    context_line_index: int
    entries: tuple[CompactTocEntry, ...]


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
    if re.search(r"\bintroduction\s+environmental\s+social\s+governance\s+indexes?\s+and\s+glossary\b", lower):
        return False
    if re.search(r"^(.{2,50}?)(?:\s+\1){2,}$", lower):
        return False
    if re.search(r"^(?:fy\d{2}\s+){2}", lower):
        return False
    if re.search(r"^(?:gri\s+)?\d{3}(?:-\d+)?\s*:", lower):
        return False
    if re.search(r"^\d{1,3}-\d{1,3}\b", lower):
        return False
    if re.search(r"^\d+\s+.*\bindex$", lower):
        return False
    if re.search(r"^no\.\s*\d+\b", lower):
        return False
    if re.search(r"\bpackaging\s+no\.\s*\d+\b", lower):
        return False
    if lower == "manufacturing factory":
        return False
    if lower.startswith("development") and "wbcsd" in lower:
        return False
    if re.search(r"^black\s+communities,\s+indigenous\s+communities\s+and\s+other\s+communities$", lower):
        return False
    alpha_tokens = re.findall(r"[a-z]+", lower)
    for phrase_length in range(2, min(6, len(alpha_tokens) // 2 + 1)):
        if alpha_tokens[:phrase_length] == alpha_tokens[phrase_length : phrase_length * 2]:
            return False
    if lower.count("vendor scorecard") >= 2:
        return False
    if re.search(r"\btarget\s+achieved\b", lower):
        return False
    if re.search(r"\bconversion\s+factor\b", lower):
        return False
    if re.search(r"^ghg\s+emissions\s*\(with\s+carbon\s+uptake\)$", lower):
        return False
    if re.search(r"^(?:\(raw\s+material\)|end\s+of\s+life)\s+impact\s+total$", lower):
        return False
    if words:
        last_word = words[-1].lower()
        if lower != "about" and last_word in OPEN_ENDED_LAST_WORDS:
            return False
    if re.search(r"^(read|learn)\s+more\b", lower):
        return False
    if re.search(r"^see\s+(?:chapter|chapters|section|sections)\b", lower):
        return False
    if re.search(r"^(?:for\s+instance|in\s+comparison|approximately)\b", lower):
        return False
    if re.search(r"^(?:disclose|describe|explain|indicate|report|provide)\b", lower) and len(words) > 4:
        return False
    if re.search(r"^positively\s+impact\b", lower):
        return False
    if re.match(r"^[\u2022\u00bb\u25aa\u25ba\u00ab?]", stripped):
        return False
    if re.search(r"^in\s+addition\s+to\b", lower):
        return False
    if len(words) >= 7 and re.search(r"\bwhere\b", lower):
        return False
    if re.search(r"^(?:in|during)\s+fy\d{2,4}\b", lower):
        return False
    if re.search(r"^as\s+of\b", lower) and len(words) > 4:
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
    if len(words) >= 4 and re.search(r"\b(?:has|have|had|requires?|makes?)\b", lower):
        return False
    if len(words) > 5 and re.search(r"\b(?:is|are|was|were)\s+(?:inspired|required|used|measured|calculated|verified)\b", lower):
        return False
    if (
        len(words) >= 7
        and not re.search(r"^(?:how|why)\b", lower)
        and LONG_PROSE_PREDICATE_RE.search(lower)
    ):
        return False
    if len(words) >= 7 and re.search(r"^for\s+[a-z0-9&.'-]+\s+to\b", lower):
        return False
    if len(words) >= 7 and re.search(r"\bto\s+(?:align|deliver)\b", lower):
        return False
    if len(words) >= 8 and re.search(r"\bto\s+create\b", lower) and not lower.startswith("how "):
        return False
    if re.search(r"^\([^)]{2,12}\)\s+to\b", lower):
        return False
    if re.search(r"^(?:contributed|detracted)\b", lower):
        return False
    if re.search(r"^open\s+discussions\s+with\b", lower):
        return False
    if re.search(r"\bprogram\s+where\b", lower):
        return False
    if re.search(r",\s+(?:achieving|including|using|supporting|resulting)\b", lower):
        return False
    if re.search(r"^program,\s+", lower) and len(words) > 4:
        return False
    if re.search(r"^international\s+nonprofit\b", lower):
        return False
    if len(words) > 4 and stripped.endswith((",", ";")):
        return False
    if len(words) >= 7 and ";" in stripped:
        return False
    if len(words) > 5 and stripped.count("(") > stripped.count(")"):
        return False
    if len(words) > 5 and stripped.count("(") != stripped.count(")"):
        return False
    if len(words) >= 7 and stripped.startswith("("):
        return False
    if len(words) >= 7 and re.search(r"[\u2022\u00bb\u25aa\u25ba\u00ab]", stripped):
        return False
    if len(words) > 5 and re.search(r"\.\s+[A-Z]", stripped):
        return False
    if re.search(r"\b(?:p\.?|pages?)\s*\d+(?:\s*[\u002d\u2013]\s*\d+)?\s*$", lower):
        return False
    if lower.endswith(" p."):
        return False
    if re.search(r"\bpg\.\s*\d+\b", lower):
        return False
    if re.search(r"\.\s*\(\d{4}\)\s*$", stripped):
        return False
    if stripped.endswith(".") and len(stripped.split()) > 5:
        return False
    numeric_values = re.findall(r"(?<![A-Za-z])[-+]?\d+(?:[.,]\d+)?%?", stripped)
    if len(numeric_values) >= 3 and not (
        len(numeric_values) == 3
        and not any("%" in value for value in numeric_values)
        and re.search(r"\bscope\s+[123]\b", lower)
    ):
        return False
    standards = re.findall(r"\b(?:gri|sasb|tcfd|ungprf)\b", lower)
    if len(standards) >= 2 and len(words) >= 6 and "index" not in lower:
        return False
    if stripped.count(",") >= 2 and len(words) >= 8 and ":" not in stripped:
        return False
    if len(re.findall(r"\([A-Z]{2,8}\)", stripped)) >= 2 and len(words) >= 7:
        return False
    if len(words) >= 7 and alpha_tokens.count("member") >= 2:
        return False
    if len(words) >= 7 and lower.endswith("factories"):
        return False
    if stripped.isupper() and lower.count("total") >= 2:
        return False
    if re.search(r"^co2\b.*\bper\s+pair\b", lower):
        return False
    if re.search(r"\b(?:packaging\s+breakdown|packaging\s+substrates)\b", lower):
        return False
    if len(re.findall(r"\b\d{1,3},\d{3}\b", stripped)) >= 2:
        return False
    if re.search(
        r"^(?:energy\s+saved|greenhouse\s+gas\s+emissions\s+saved|water\s+saved|"
        r"water\s+use|energy\s+use|ghg\s+emissions?)\s*\([^)]*(?:mj|mwh|kwh|liters?|lbs|kg|co2)[^)]*\)$",
        lower,
    ):
        return False
    if re.search(r"^total\s+(?:estimated\s+)?emissions?\b", lower) and numeric_values:
        return False
    if re.search(r"\s-\s(?:director|chief|manager|officer|president)\b", lower):
        return False
    if stripped.count(",") >= 1 and len(words) > 5 and re.search(
        r"\b(?:alliance|center|council|institute|university)\b",
        lower,
    ):
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
    if code == "appendix" and re.search(r"\bchief\s+assurance\s+officer\b", normalized):
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


def _markdown_table_row_shape(line: str) -> bool:
    """Return whether a line has strict multi-cell Markdown row syntax."""
    stripped = line.strip()
    return (
        len(stripped) >= 3
        and stripped.startswith("|")
        and stripped.endswith("|")
        and stripped.count("|") >= 3
    )


def _markdown_table_separator_row(line: str) -> bool:
    """Recognise ``| --- | :---: |`` without accepting ordinary pipe prose."""
    if not _markdown_table_row_shape(line):
        return False
    cells = [cell.strip() for cell in line.strip()[1:-1].split("|")]
    return bool(cells) and all(
        not cell or re.fullmatch(r":?-{3,}:?", cell) is not None
        for cell in cells
    ) and any(cell for cell in cells)


def is_markdown_table_row(lines: list[str], line_index: int) -> bool:
    """Detect a Markdown row using strict syntax plus local table evidence."""
    if not 0 <= line_index < len(lines):
        return False
    line = lines[line_index]
    if not _markdown_table_row_shape(line):
        return False
    if _markdown_table_separator_row(line):
        return True

    adjacent_rows = []
    for step in (-1, 1):
        index = line_index + step
        if 0 <= index < len(lines):
            adjacent_rows.append(lines[index])
    if any(_markdown_table_row_shape(value) for value in adjacent_rows):
        return True

    # Allow one blank line when looking for the standard separator row. This
    # still requires the candidate itself to use strict multi-cell row syntax.
    for index in range(max(0, line_index - 2), min(len(lines), line_index + 3)):
        if index != line_index and _markdown_table_separator_row(lines[index]):
            return True
    return False


def map_heading_to_code(line: str) -> str | None:
    raw = line.strip()
    # A complete multi-cell Markdown row is data, even when one of its labels
    # contains an ESG keyword. Context-aware confirmation also runs in the
    # document scan below.
    if _markdown_table_row_shape(raw):
        return None
    if len(re.findall(r"[A-Za-z][A-Za-z-]*", raw)) > 4 and raw.endswith(("-", "\u2013")):
        return None
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


def _nearest_nonempty_line(lines: list[str], start: int, step: int) -> str:
    index = start + step
    while 0 <= index < len(lines):
        if lines[index].strip():
            return lines[index].strip()
        index += step
    return ""


def _page_number(row: dict) -> int | None:
    return parse_int(row.get("page") or row.get("page_number"))


def _compact_toc_pages(
    text: str,
    page_spans: list[dict],
) -> tuple[CompactTocPage, ...]:
    """Find strict compact-TOC clusters independently on each source page."""
    if not page_spans:
        return ()
    lines_with_endings = text.splitlines(keepends=True)
    line_offsets: list[int] = []
    offset = 0
    for line in lines_with_endings:
        line_offsets.append(offset)
        offset += len(line)

    page_numbers = [number for row in page_spans if (number := _page_number(row))]
    max_page_number = max(page_numbers, default=None)
    detected: list[CompactTocPage] = []
    for row in page_spans:
        page_number = _page_number(row)
        char_start = parse_int(row.get("char_start"))
        char_end = parse_int(row.get("char_end"))
        if page_number is None or char_start is None or char_end is None:
            continue
        indexes = [
            index
            for index, line_offset in enumerate(line_offsets)
            if char_start <= line_offset < char_end
        ]
        if not indexes:
            continue
        page_lines = [lines_with_endings[index].rstrip("\r\n") for index in indexes]
        local_entries = detect_compact_toc_entries(
            page_lines,
            max_page_number=max_page_number,
        )
        if not local_entries:
            continue
        entries = tuple(
            replace(entry, line_index=indexes[entry.line_index])
            for entry in local_entries
        )
        context_line_index = next(
            index
            for index in indexes
            if re.fullmatch(
                r"(?:table\s+of\s+contents|contents)",
                lines_with_endings[index].strip(),
                flags=re.IGNORECASE,
            )
        )
        detected.append(
            CompactTocPage(
                page_number,
                char_start,
                char_end,
                context_line_index,
                entries,
            )
        )
    return tuple(detected)


def _front_matter_candidates_from_compact_toc(
    lines_with_endings: list[str],
    line_offsets: list[int],
    page_spans: list[dict],
    toc_pages: tuple[CompactTocPage, ...],
) -> list[HeadingCandidate]:
    """Restore nearby real page headings referenced by a proven compact TOC."""
    page_rows = {
        number: row
        for row in page_spans
        if (number := _page_number(row)) is not None
    }
    candidates: list[HeadingCandidate] = []
    for toc_page in toc_pages:
        for entry in toc_page.entries:
            if not toc_page.page_number < entry.page_number <= toc_page.page_number + 2:
                continue
            target = page_rows.get(entry.page_number)
            if target is None:
                continue
            target_start = parse_int(target.get("char_start"))
            target_end = parse_int(target.get("char_end"))
            if target_start is None or target_end is None:
                continue
            expected_words = re.findall(r"[a-z0-9]+", entry.title.casefold())
            if len(expected_words) < 2:
                continue
            for line_index, line_offset in enumerate(line_offsets):
                if not target_start <= line_offset < min(target_end, target_start + 500):
                    continue
                title = normalize_heading_text(lines_with_endings[line_index])
                actual_words = re.findall(r"[a-z0-9]+", title.casefold())
                if len(actual_words) < 2 or actual_words[:2] != expected_words[:2]:
                    continue
                code = map_heading_to_code(entry.title)
                if code is None and entry.title.casefold().startswith("welcome to "):
                    code = "about_this_report"
                if code is None:
                    continue
                candidates.append(
                    HeadingCandidate(
                        line_index=line_index,
                        char_offset=line_offset,
                        section_code=code,
                        title=entry.title,
                        toc_like=False,
                        heading_confidence="high",
                    )
                )
                break
    return candidates


def _looks_like_table_or_index_candidate(
    candidate: HeadingCandidate,
    lines: list[str],
) -> bool:
    """Reject topic words used as table cells or disclosure-index row labels."""
    previous = _nearest_nonempty_line(lines, candidate.line_index, -1)
    following = _nearest_nonempty_line(lines, candidate.line_index, 1)
    surrounding = f"{previous} {following}"
    title_words = re.findall(r"[A-Za-z][A-Za-z-]*", candidate.title)

    index_signal = re.search(
        r"\b(?:gri|sasb|tcfd|ungprf)\b.*(?:\b\d{3}(?:-\d+)?\b|\b[A-Z]{2}-[A-Z]{2}-\d)|"
        r"\b(?:p\.|pages)\s*\d+|\b[A-Z]{2}-[A-Z]{2}-\d",
        surrounding,
        flags=re.IGNORECASE,
    )
    if len(title_words) <= 6 and index_signal:
        return True

    previous_numbers = re.findall(r"\d+(?:[.,]\d+)?%?", previous)
    following_numbers = re.findall(r"\d+(?:[.,]\d+)?%?", following)
    if len(title_words) <= 3:
        both_sides_are_data = (
            (len(previous_numbers) >= 2 or "%" in previous)
            and (len(following_numbers) >= 2 or "%" in following)
        )
        numeric_row_continuation = previous_numbers and "%" in following and previous.rstrip().endswith(previous_numbers[-1])
        if both_sides_are_data or numeric_row_continuation:
            return True

    if len(title_words) <= 4 and re.fullmatch(r"[\d\s.,%+\-]+", previous):
        next_words = re.findall(r"[A-Za-z]+", following)
        if next_words and len(next_words) <= 3 and following.upper() == following:
            return True

    table_terms = {
        "end of life impact total",
        "ghg emissions",
        "impact total",
        "kg co2",
        "raw material",
        "water use",
    }
    surrounding_lower = f"{candidate.title} {surrounding}".lower()
    if len(title_words) <= 6 and sum(term in surrounding_lower for term in table_terms) >= 2:
        return True

    normalized_title = _title_key(candidate.title)
    if normalized_title == "material" and len(following_numbers) >= 4:
        return True
    if "sustainable development goals" in previous.lower() and normalized_title in previous.lower():
        return True
    if "gri standard number" in surrounding_lower or "gri disclosure location" in surrounding_lower:
        return True
    if "(continued)" in previous.lower() and "gri topic standards" in following.lower():
        return True
    toc_signals = sum(has_page_reference(line) for line in (candidate.title, previous, following))
    if toc_signals >= 2:
        return True
    if "table" in normalized_title and (
        has_page_reference(previous) or "index" in following.lower()
    ):
        return True
    return False


def _title_key(title: str) -> str:
    normalized = re.sub(r"\s+", " ", title).strip().casefold()
    return re.sub(
        r"^((?:fy\d{2}|\d{4})\b.*\b(?:impact|environmental|sustainability|esg)\b.*\breport)\s+\d+$",
        r"\1",
        normalized,
    )


def _chrome_tokens(title: str) -> frozenset[str]:
    """Return a stable title signature for page furniture comparisons."""
    normalized = _title_key(title)
    normalized = re.sub(r"\d+", " ", normalized)
    tokens = re.findall(r"[a-z]+", normalized)
    return frozenset(token for token in tokens if token not in {"and", "the", "of", "our"})


def _topic_label_count(title: str) -> int:
    normalized = " ".join(re.findall(r"[a-z]+", _title_key(title)))
    return sum(label in normalized for label in NAVIGATION_TOPIC_LABELS)


def _similar_chrome_titles(left: str, right: str) -> bool:
    """Match breadcrumb variants while requiring a substantial shared core."""
    left_tokens = _chrome_tokens(left)
    right_tokens = _chrome_tokens(right)
    if not left_tokens or not right_tokens:
        return False
    overlap = len(left_tokens & right_tokens)
    union = len(left_tokens | right_tokens)
    if overlap >= 3 and overlap / union >= 0.65:
        return True
    if overlap >= 3 and overlap / min(len(left_tokens), len(right_tokens)) >= 0.80:
        return True
    # Breadcrumbs are often extended at the end on alternate page templates.
    left_words = re.findall(r"[a-z]+", _title_key(left))
    right_words = re.findall(r"[a-z]+", _title_key(right))
    return len(left_words) >= 3 and left_words[:3] == right_words[:3]


def _looks_like_uppercase_column_header(title: str) -> bool:
    words = re.findall(r"[A-Za-z]+", title)
    if len(words) < 3:
        return False
    if _looks_like_narrative_heading_shape(title):
        return False
    letters = "".join(words)
    return letters.isupper() and ("/" in title or len(words) >= 4)


def _looks_like_narrative_heading_shape(title: str) -> bool:
    """Separate real long-form headings from terse uppercase column labels."""
    words = re.findall(r"[A-Za-z]+", title)
    if not 5 <= len(words) <= 14 or re.search(r"\d", title):
        return False
    connectors = {"and", "for", "in", "of", "our", "the", "to", "with"}
    return bool({word.lower() for word in words} & connectors)


def _nearby_table_signal(lines: list[str], line_index: int) -> bool:
    nearby = [
        _nearest_nonempty_line(lines, line_index, -1),
        _nearest_nonempty_line(lines, line_index, 1),
    ]
    for line in nearby:
        words = re.findall(r"[A-Za-z]+", line)
        numeric_cells = len(re.findall(r"\d+(?:[.,]\d+)?%?", line))
        if numeric_cells >= 2 or (len(words) >= 3 and "".join(words).isupper()):
            return True
    return False


def heading_confidence(
    candidate: HeadingCandidate,
    lines: list[str],
    page_position: tuple[int, int] | None = None,
) -> str:
    """Score boundary quality separately from the amount of following body text."""
    title = candidate.title.strip()
    words = re.findall(r"[A-Za-z]+", title)
    before_blank = candidate.line_index == 0 or not lines[candidate.line_index - 1].strip()
    after_blank = candidate.line_index + 1 >= len(lines) or not lines[candidate.line_index + 1].strip()
    score = 1 + int(before_blank) + int(after_blank)
    narrative_shape = _looks_like_narrative_heading_shape(title)
    uppercase_column_header = _looks_like_uppercase_column_header(title)
    if 1 <= len(words) <= 12 and len(title) <= 120 and not uppercase_column_header:
        score += 1
    if re.search(r"[.!?;]$", title) or ". " in title:
        score -= 2
    if (
        uppercase_column_header
        or (_nearby_table_signal(lines, candidate.line_index) and not narrative_shape)
    ):
        score -= 2
    if narrative_shape:
        score += 1
    if page_position is not None and page_position[1] <= PAGE_CHROME_MAX_OFFSET:
        score -= 1
    if _topic_label_count(title) >= 3:
        score -= 3
    if score >= 3:
        return "high"
    if score >= 1:
        return "medium"
    return "low"


def _has_substantial_narrative_following(
    candidate: HeadingCandidate,
    lines: list[str],
) -> bool:
    """Keep real headings that are followed by prose, regardless of casing."""
    following: list[str] = []
    for line in lines[candidate.line_index + 1 : candidate.line_index + 16]:
        stripped = line.strip()
        if stripped:
            following.append(stripped)
        if len(" ".join(following)) >= 900:
            break
    body = " ".join(following)
    words = re.findall(r"[A-Za-z]+", body)
    sentences = len(re.findall(r"[.!?](?:\s|$)", body))
    numeric_cells = len(re.findall(r"\b\d+(?:[.,]\d+)?%?\b", body))
    table_like = _nearby_table_signal(lines, candidate.line_index)
    prose_dense = len(words) >= 75 and numeric_cells * 8 < max(len(words), 1)
    return not table_like and prose_dense and (sentences >= 2 or len(body) >= 500)


def _is_navigation_or_report_chrome(title: str) -> bool:
    normalized = _title_key(title)
    if normalized == "appendix":
        return True
    # A normal heading can name one topic.  A run of several report topics is
    # navigation, even when small wording changes appear on different pages.
    if _topic_label_count(title) >= 3:
        return True
    words = set(re.findall(r"[a-z]+", normalized))
    navigation_hits = len(words & NAVIGATION_CHROME_TERMS)
    if normalized.startswith("contents ") and navigation_hits >= 2:
        return True
    if normalized.startswith("introduction ") and navigation_hits >= 3:
        return True
    if navigation_hits >= 2 and len(words) <= 3 and words & {"initiatives", "overview"}:
        return True
    if navigation_hits >= 4:
        return True
    return bool(re.match(r"^(?:fy\d{2}|\d{4})\b.*\b(?:impact|environmental|sustainability|esg)\b.*\breport$", normalized))


def _candidate_page_positions(
    candidates: list[HeadingCandidate],
    page_spans: list[dict],
) -> dict[int, tuple[int, int]]:
    """Map candidate indexes to ``(page number, offset within page)``."""
    valid_spans: list[tuple[int, int, int]] = []
    for row in page_spans:
        page = parse_int(row.get("page"))
        start = parse_int(row.get("char_start"))
        end = parse_int(row.get("char_end"))
        if page is None or start is None or end is None or end < start:
            continue
        valid_spans.append((start, end, page))
    valid_spans.sort()

    positions: dict[int, tuple[int, int]] = {}
    span_index = 0
    for candidate_index, candidate in enumerate(candidates):
        while span_index < len(valid_spans) and candidate.char_offset > valid_spans[span_index][1]:
            span_index += 1
        if span_index >= len(valid_spans):
            break
        start, end, page = valid_spans[span_index]
        if start <= candidate.char_offset <= end:
            positions[candidate_index] = (page, candidate.char_offset - start)
    return positions


def _navigation_term_hits(title: str) -> int:
    """Count distinct report-navigation words in one extracted line."""
    words = set(re.findall(r"[a-z]+", _title_key(title)))
    return len(words & NAVIGATION_CHROME_TERMS)


def _matching_section_code_count(title: str) -> int:
    """Count distinct section families named by one structural line."""
    normalized = normalize_heading_text(title).lower().replace("&", " and ")
    normalized = re.sub(r"[^a-z0-9\s-]", " ", normalized)
    normalized = re.sub(r"\s+", " ", normalized).strip()
    return len(
        {
            code
            for code, pattern in HEADING_PATTERNS
            if re.search(pattern, normalized, flags=re.IGNORECASE)
            and code_allowed_for_heading(code, normalized)
        }
    )


def _running_page_chrome_indexes(
    candidates: list[HeadingCandidate],
    page_spans: list[dict],
    total_chars: int,
    lines: list[str] | None = None,
) -> set[int]:
    """Reject high-confidence repeated headers/footers without losing chapters."""
    if not page_spans:
        return set()

    page_positions = _candidate_page_positions(candidates, page_spans)
    page_ends = {
        parse_int(row.get("page")): parse_int(row.get("char_end"))
        for row in page_spans
        if parse_int(row.get("page")) is not None
        and parse_int(row.get("char_end")) is not None
    }
    # Page furniture is removed upstream now, by
    # esg/scripts/bridge_docling_to_pipeline.py, which drops a header/footer
    # band block whose text repeats across pages. That rule works on the
    # geometry docling gives us -- it knows a block sits in the top or bottom
    # band of the page -- rather than inferring position from character offsets
    # in a flattened text stream, which is all this detector could see.
    #
    # The detector it replaces lives on Phase_4.3_Aziz along with its held-out
    # validation set (esg/scripts/build_furniture_validation_set.py). Recover
    # with: git checkout Phase_4.3_Aziz -- esg/src/section_splitter_esg.py
    rejected: set[int] = set()
    indexes_by_title: dict[str, list[int]] = {}
    for candidate_index, candidate in enumerate(candidates):
        indexes_by_title.setdefault(_title_key(candidate.title), []).append(candidate_index)

    # Exact keys handle ordinary repeating headings.  Navigation labels also
    # get a conservative fuzzy pass, so a breadcrumb with one added label does
    # not evade page-chrome detection.
    title_groups = list(indexes_by_title.values())
    navigation_indexes = [
        index for index, candidate in enumerate(candidates)
        if _topic_label_count(candidate.title) >= 2
    ]
    fuzzy_groups: list[list[int]] = []
    for index in navigation_indexes:
        for group in fuzzy_groups:
            if _similar_chrome_titles(candidates[index].title, candidates[group[0]].title):
                group.append(index)
                break
        else:
            fuzzy_groups.append([index])
    title_groups.extend(group for group in fuzzy_groups if len(group) >= PAGE_CHROME_MIN_PAGES)

    for indexes in title_groups:
        indexes = [index for index in indexes if index not in rejected]
        if len(indexes) < PAGE_CHROME_MIN_PAGES:
            continue

        paged_indexes = [index for index in indexes if index in page_positions]
        top_indexes = [
            index
            for index in paged_indexes
            if page_positions[index][1] <= PAGE_CHROME_MAX_OFFSET
        ]
        distinct_pages = sorted({page_positions[index][0] for index in paged_indexes})
        if len(distinct_pages) < PAGE_CHROME_MIN_PAGES:
            continue

        page_runs: list[list[int]] = []
        for page in distinct_pages:
            if page_runs and page - page_runs[-1][-1] <= PAGE_CHROME_MAX_PAGE_GAP:
                page_runs[-1].append(page)
            else:
                page_runs.append([page])

        qualifying_runs = [run for run in page_runs if len(run) >= PAGE_CHROME_MIN_PAGES]
        if not qualifying_runs:
            continue

        pages_in_runs = {page for run in qualifying_runs for page in run}
        for run in qualifying_runs:
            run_indexes = [index for index in indexes if page_positions.get(index, (None, None))[0] in run]
            top_indexes_in_run = []
            bottom_indexes_in_run = []
            for index in run_indexes:
                page, page_offset = page_positions[index]
                page_end = page_ends.get(page)
                near_top = page_offset <= PAGE_CHROME_MAX_OFFSET
                near_bottom = (
                    page_end is not None
                    and page_end - candidates[index].char_offset
                    <= PAGE_CHROME_MAX_OFFSET
                )
                if near_top or near_bottom:
                    if near_top:
                        top_indexes_in_run.append(index)
                    if near_bottom:
                        bottom_indexes_in_run.append(index)

            # Repeated bottom-band copies are unambiguous footers.
            if len(bottom_indexes_in_run) == len(run_indexes):
                rejected.update(run_indexes)
                continue

            # A standalone page number immediately before the same top-band
            # title on multiple pages is the motivating header pattern:
            # ``4 / HEADER``, ``5 / HEADER``. Reject every copy, not just all
            # but the first one.
            numbered_top = []
            if lines is not None:
                for index in top_indexes_in_run:
                    previous = _nearest_nonempty_line(
                        lines, candidates[index].line_index, -1
                    )
                    if re.fullmatch(
                        r"(?:p(?:age)?\.?\s*)?(?:\d{1,4}|[ivxlcdm]{1,8})",
                        previous.strip(),
                        flags=re.IGNORECASE,
                    ):
                        numbered_top.append(index)
            if (
                len(top_indexes_in_run) == len(run_indexes)
                and len(numbered_top) >= 2
                and len(numbered_top) * 2 >= len(run_indexes)
            ):
                rejected.update(run_indexes)
                continue

            # Otherwise preserve the v1 behavior: retain the first copy as the
            # chapter boundary and reject later repeats.
            canonical = min(run_indexes, key=lambda index: candidates[index].char_offset)
            rejected.update(index for index in run_indexes if index != canonical)

        first_run_offset = min(
            candidates[index].char_offset
            for index in indexes
            if page_positions.get(index, (None, None))[0] in pages_in_runs
        )
        rejected.update(
            index
            for index in top_indexes
            if candidates[index].char_offset < first_run_offset
            and candidates[index].char_offset < total_chars * 0.10
        )

    return rejected


def _repeated_table_header_indexes(
    candidates: list[HeadingCandidate],
    page_spans: list[dict],
    lines: list[str],
) -> set[int]:
    """Reject repeated uppercase column labels, but only with table evidence."""
    page_positions = _candidate_page_positions(candidates, page_spans)
    groups: dict[str, list[int]] = {}
    for index, candidate in enumerate(candidates):
        if _looks_like_uppercase_column_header(candidate.title):
            groups.setdefault(" ".join(sorted(_chrome_tokens(candidate.title))), []).append(index)

    rejected: set[int] = set()
    for indexes in groups.values():
        pages = {page_positions[index][0] for index in indexes if index in page_positions}
        if len(indexes) < 2 or len(pages) < 2:
            continue
        for index in indexes:
            page_offset = page_positions.get(index, (None, PAGE_CHROME_MAX_OFFSET + 1))[1]
            if page_offset <= PAGE_CHROME_MAX_OFFSET or _nearby_table_signal(lines, candidates[index].line_index):
                rejected.add(index)
    return rejected


def collect_heading_candidates(
    text: str,
    page_spans: list[dict] | None = None,
) -> list[HeadingCandidate]:
    lines_with_endings = text.splitlines(keepends=True)
    lines = [line.rstrip("\r\n") for line in lines_with_endings]
    total_chars = max(len(text), 1)
    offset = 0
    line_offsets: list[int] = []
    raw_candidates: list[HeadingCandidate] = []

    for i, line_with_ending in enumerate(lines_with_endings):
        line_offsets.append(offset)
        line = line_with_ending.rstrip("\r\n")
        if is_markdown_table_row(lines, i):
            offset += len(line_with_ending)
            continue
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
        offset += len(line_with_ending)

    toc_pages = _compact_toc_pages(text, page_spans or [])
    if toc_pages:
        raw_candidates = [
            candidate
            for candidate in raw_candidates
            if not any(
                page.char_start <= candidate.char_offset < page.char_end
                for page in toc_pages
            )
        ]
        for page in toc_pages:
            raw_candidates.append(
                HeadingCandidate(
                    line_index=page.context_line_index,
                    char_offset=line_offsets[page.context_line_index],
                    section_code="about_this_report",
                    title="Table of Contents",
                    toc_like=True,
                    heading_confidence="high",
                )
            )
        raw_candidates.extend(
            _front_matter_candidates_from_compact_toc(
                lines_with_endings,
                line_offsets,
                page_spans or [],
                toc_pages,
            )
        )
        raw_candidates.sort(key=lambda candidate: (candidate.char_offset, candidate.line_index))

    early_candidates = [c for c in raw_candidates if c.char_offset < total_chars * 0.10]
    toc_heavy = (
        len(early_candidates) >= 5
        and sum(1 for c in early_candidates if c.toc_like) / len(early_candidates) >= 0.5
    )
    repeated_chrome = _running_page_chrome_indexes(
        raw_candidates,
        page_spans or [],
        total_chars,
        lines,
    )
    repeated_table_headers = _repeated_table_header_indexes(
        raw_candidates, page_spans or [], lines
    )
    page_positions = _candidate_page_positions(raw_candidates, page_spans or [])

    filtered: list[HeadingCandidate] = []
    seen_positions: set[tuple[int, str]] = set()
    for candidate_index, candidate in enumerate(raw_candidates):
        if candidate_index in repeated_chrome:
            continue
        if candidate_index in repeated_table_headers:
            continue
        # A multi-topic line can still be a legitimate section heading.  Only
        # reject it as chrome when its following material is not normal prose.
        if (
            _is_navigation_or_report_chrome(candidate.title)
            and not _has_substantial_narrative_following(candidate, lines)
        ):
            continue
        if _looks_like_table_or_index_candidate(candidate, lines):
            continue
        if toc_heavy and candidate.char_offset < total_chars * 0.10 and candidate.toc_like:
            continue
        key = (candidate.line_index, candidate.section_code)
        if key in seen_positions:
            continue
        seen_positions.add(key)
        filtered.append(
            replace(
                candidate,
                heading_confidence=heading_confidence(
                    candidate, lines, page_positions.get(candidate_index)
                ),
            )
        )

    return filtered


def confidence_for(
    code: str,
    text: str,
    fallback: bool = False,
    heading_quality: str = "high",
) -> str:
    if fallback:
        return "low"
    char_count = len(text.strip())
    if code == "other":
        return "low" if char_count < 1000 else "medium"
    if heading_quality == "low":
        return "low" if char_count < 1500 else "medium"
    if char_count >= 1000:
        return "high"
    if char_count >= MIN_SECTION_CHARS:
        return "medium"
    return "low"


def _trimmed_source_span(source_text: str, start: int, end: int) -> tuple[str, int, int]:
    """Return an exact, non-whitespace-bounded slice and its adjusted offsets."""
    if not 0 <= start <= end <= len(source_text):
        raise ValueError(f"Invalid source span: {start}:{end} for {len(source_text)} characters")

    while start < end and source_text[start].isspace():
        start += 1
    while end > start and source_text[end - 1].isspace():
        end -= 1
    return source_text[start:end], start, end


def _segment_from_source(
    source_text: str,
    start: int,
    end: int,
    section_code: str,
    title: str,
    split_method: str,
    *,
    fallback: bool = False,
    heading_quality: str = "high",
) -> SectionSegment:
    body, source_start, source_end = _trimmed_source_span(source_text, start, end)
    subsection_spans = ()
    # Low-confidence boundaries are useful for broad tiling, but they are not
    # safe enough to become retrieval context. Medium/high headings have
    # already passed the chrome, navigation, table, and narrative guards.
    if (
        split_method == "heading_regex"
        and not fallback
        and heading_quality in {"medium", "high"}
        and title
    ):
        subsection_spans = (
            SubsectionSpan(
                title=title,
                section_code=section_code,
                source_start_char=source_start,
                source_end_char=source_end,
                heading_confidence=heading_quality,
            ),
        )
    return SectionSegment(
        section_code,
        title,
        body,
        split_method,
        confidence_for(section_code, body, fallback=fallback, heading_quality=heading_quality),
        source_start,
        source_end,
        heading_confidence=heading_quality,
        subsection_spans=subsection_spans,
    )


def build_segments(text: str, candidates: list[HeadingCandidate]) -> list[SectionSegment]:
    if not candidates:
        return [
            _segment_from_source(
                text,
                0,
                len(text),
                "full_document",
                "Full Document",
                "full_document_fallback",
                fallback=True,
            )
        ]

    segments: list[SectionSegment] = []

    preamble, _, _ = _trimmed_source_span(text, 0, candidates[0].char_offset)
    if preamble:
        segments.append(
            _segment_from_source(
                text,
                0,
                candidates[0].char_offset,
                "other",
                "Preamble",
                "preamble",
            )
        )

    for index, candidate in enumerate(candidates):
        next_offset = candidates[index + 1].char_offset if index + 1 < len(candidates) else len(text)
        segment = _segment_from_source(
            text,
            candidate.char_offset,
            next_offset,
            candidate.section_code,
            candidate.title or candidate.section_code.replace("_", " ").title(),
            "heading_regex",
            heading_quality=candidate.heading_confidence,
        )
        if segment.text:
            segments.append(segment)

    if not segments:
        return [
            _segment_from_source(
                text,
                0,
                len(text),
                "full_document",
                "Full Document",
                "full_document_fallback",
                fallback=True,
            )
        ]

    return merge_short_segments(segments, text)


def _merge_contiguous_segments(
    left: SectionSegment,
    right: SectionSegment,
    source_text: str | None,
    *,
    keep: SectionSegment,
) -> SectionSegment:
    source_start: int | None = None
    source_end: int | None = None
    combined_text = f"{left.text}\n\n{right.text}".strip()

    if (
        source_text is not None
        and left.source_start_char is not None
        and left.source_end_char is not None
        and right.source_start_char is not None
        and right.source_end_char is not None
        and left.source_start_char <= right.source_start_char
        and left.source_end_char <= right.source_end_char
    ):
        combined_text, source_start, source_end = _trimmed_source_span(
            source_text,
            left.source_start_char,
            right.source_end_char,
        )

    return SectionSegment(
        keep.section_code,
        keep.title,
        combined_text,
        keep.split_method,
        confidence_for(keep.section_code, combined_text),
        source_start,
        source_end,
        heading_confidence=keep.heading_confidence,
        subsection_spans=tuple(
            sorted(
                (
                    span
                    for span in (*left.subsection_spans, *right.subsection_spans)
                    if span.section_code == keep.section_code
                ),
                key=lambda span: (span.source_start_char, span.source_end_char),
            )
        ),
    )


def merge_short_segments(
    segments: list[SectionSegment],
    source_text: str | None = None,
) -> list[SectionSegment]:
    """Merge only adjacent short spans, preserving one contiguous source range."""
    if len(segments) <= 1:
        return segments

    merged: list[SectionSegment] = []
    carry_prefix: SectionSegment | None = None

    for segment in segments:
        current = segment
        if carry_prefix is not None:
            current = _merge_contiguous_segments(
                carry_prefix,
                current,
                source_text,
                keep=current,
            )
            carry_prefix = None

        if len(current.text.strip()) < MIN_SECTION_CHARS:
            if merged:
                merged[-1] = _merge_contiguous_segments(
                    merged[-1],
                    current,
                    source_text,
                    keep=merged[-1],
                )
            else:
                carry_prefix = current
            continue

        merged.append(current)

    if carry_prefix is not None:
        if merged:
            merged[-1] = _merge_contiguous_segments(
                merged[-1],
                carry_prefix,
                source_text,
                keep=merged[-1],
            )
        else:
            merged.append(
                replace(
                    carry_prefix,
                    section_code="full_document",
                    title="Full Document",
                    split_method="full_document_fallback",
                    confidence="low",
                )
            )

    return merged


def normalize_section_codes(segments: list[SectionSegment]) -> list[SectionSegment]:
    """Normalize unknown codes without coalescing separate occurrences."""
    return [
        segment
        if segment.section_code in CANONICAL_SECTION_CODES
        else replace(segment, section_code="other", confidence="low")
        for segment in segments
    ]


def coalesce_adjacent_same_code_segments(
    segments: list[SectionSegment],
    source_text: str,
) -> list[SectionSegment]:
    """Merge neighboring equal-code spans while retaining heading spans."""
    coalesced: list[SectionSegment] = []
    for segment in segments:
        if not coalesced:
            coalesced.append(segment)
            continue

        previous = coalesced[-1]
        can_coalesce = (
            previous.section_code == segment.section_code
            and previous.source_start_char is not None
            and previous.source_end_char is not None
            and segment.source_start_char is not None
            and segment.source_end_char is not None
            and previous.source_end_char <= segment.source_start_char
            and not source_text[previous.source_end_char : segment.source_start_char].strip()
        )
        if can_coalesce:
            coalesced[-1] = _merge_contiguous_segments(
                previous,
                segment,
                source_text,
                keep=previous,
            )
        else:
            coalesced.append(segment)
    return coalesced


def subsection_spans_json(
    section: SectionSegment,
    section_start: int,
    section_end: int,
) -> str:
    """Serialize accepted heading spans relative to one physical section."""
    spans: list[dict[str, str | int]] = []
    for span in section.subsection_spans:
        start = max(section_start, span.source_start_char)
        end = min(section_end, span.source_end_char)
        if end <= start:
            continue
        spans.append(
            {
                "title": span.title,
                "section_code": span.section_code,
                "start_char": start - section_start,
                "end_char": end - section_start,
                "heading_confidence": span.heading_confidence,
            }
        )
    return json.dumps(spans, ensure_ascii=False, separators=(",", ":"))


def split_esg_sections(
    text: str,
    page_spans: list[dict] | None = None,
) -> list[SectionSegment]:
    candidates = collect_heading_candidates(text, page_spans=page_spans)

    if len(candidates) == 1 and candidates[0].char_offset > len(text) * 0.75:
        return [
            _segment_from_source(
                text,
                0,
                len(text),
                "full_document",
                "Full Document",
                "full_document_fallback",
                fallback=True,
            )
        ]

    normalized_segments = normalize_section_codes(build_segments(text, candidates))
    return coalesce_adjacent_same_code_segments(normalized_segments, text)


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


def _output_sections(
    text: str,
    page_spans: list[dict] | None = None,
) -> list[SectionSegment]:
    sections = [
        section
        for section in split_esg_sections(text, page_spans=page_spans)
        if section.text.strip()
    ]
    if not sections:
        raise ValueError("Parsed text produced no non-empty ESG sections")

    counts_by_code: dict[str, int] = {}
    output_sections: list[SectionSegment] = []
    for section in sections:
        counts_by_code[section.section_code] = counts_by_code.get(section.section_code, 0) + 1
        section_instance_id = f"{section.section_code}__{counts_by_code[section.section_code]:04d}"
        output_section = replace(section, section_instance_id=section_instance_id)
        if output_section.source_start_char is None or output_section.source_end_char is None:
            raise ValueError(f"Section has no contiguous source span: {section_instance_id}")
        if text[output_section.source_start_char : output_section.source_end_char] != output_section.text:
            raise ValueError(f"Section source span is not exact: {section_instance_id}")
        output_sections.append(output_section)
    return output_sections


def _assign_section_instance_ids(sections: list[SectionSegment]) -> list[SectionSegment]:
    counts_by_code: dict[str, int] = {}
    assigned: list[SectionSegment] = []
    for section in sections:
        counts_by_code[section.section_code] = counts_by_code.get(section.section_code, 0) + 1
        assigned.append(
            replace(
                section,
                section_instance_id=f"{section.section_code}__{counts_by_code[section.section_code]:04d}",
            )
        )
    return assigned


def _section_output_path(
    output_root: Path,
    ticker: str,
    pdf_stem: str,
    section_instance_id: str,
) -> Path:
    return output_root / ticker / f"{pdf_stem}__{section_instance_id}.txt"


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
    page_spans = read_page_map(txt_file)
    sections = (
        _assign_section_instance_ids(sections)
        if sections is not None
        else _output_sections(text, page_spans=page_spans)
    )
    if not sections:
        raise ValueError("Parsed text produced no non-empty ESG sections")

    ticker_out = output_root / ticker
    ticker_out.mkdir(parents=True, exist_ok=True)
    rows: list[dict] = []
    output_plan: list[tuple[Path, str]] = []

    search_pos = 0
    seen_instances: set[str] = set()
    for section in sections:
        section_text = section.text.strip()
        if not section_text:
            continue
        if section.section_instance_id in seen_instances:
            raise ValueError(f"Duplicate generated section instance: {section.section_instance_id}")
        seen_instances.add(section.section_instance_id)

        if (section.source_start_char is None) != (section.source_end_char is None):
            raise ValueError(f"Incomplete source span: {section.section_instance_id}")
        if section.source_start_char is not None and section.source_end_char is not None:
            source_start, source_end = section.source_start_char, section.source_end_char
        else:
            source_start = text.find(section_text, search_pos)
            if source_start < 0:
                source_start = text.find(section_text)
            source_end = source_start + len(section_text) if source_start >= 0 else None
            if source_start < 0:
                source_start = None

        if (
            source_start is None
            or source_end is None
            or not 0 <= source_start <= source_end <= len(text)
            or text[source_start:source_end] != section_text
        ):
            raise ValueError(f"Section is not an exact contiguous source slice: {section.section_instance_id}")
        search_pos = max(search_pos, source_end)
        page_start, page_end = pages_for_span(page_spans, source_start, source_end)
        section_file = _section_output_path(
            output_root,
            ticker,
            pdf_stem,
            section.section_instance_id,
        )
        output_plan.append((section_file, section_text))
        rows.append(
            {
                "ticker": ticker,
                "pdf_stem": pdf_stem,
                "section_instance_id": section.section_instance_id,
                "section_code": section.section_code,
                "section_title": section.title,
                "subsection_spans_json": subsection_spans_json(
                    section, source_start, source_end
                ),
                "section_file": display_path(section_file),
                "source_start_char": source_start if source_start is not None else "",
                "source_end_char": source_end if source_end is not None else "",
                "provenance_version": PROVENANCE_VERSION,
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


def discover_text_files(
    input_root: Path,
    ticker: str | None = None,
    pdf_stem: str | None = None,
) -> list[Path]:
    if not input_root.exists():
        return []
    if ticker:
        text_files = sorted((input_root / ticker.upper()).glob("*.txt"))
    else:
        text_files = sorted(input_root.glob("*/*.txt"))
    if pdf_stem:
        text_files = [path for path in text_files if path.stem == pdf_stem]
    return text_files


def _normalize_index_row(row: dict) -> dict:
    normalized = {
        field: "" if row.get(field) is None else str(row.get(field, ""))
        for field in SECTION_INDEX_FIELDS
    }
    normalized["ticker"] = normalized["ticker"].strip().upper()
    normalized["pdf_stem"] = normalized["pdf_stem"].strip()
    normalized["section_code"] = normalized["section_code"].strip().lower()
    normalized["section_instance_id"] = normalized["section_instance_id"].strip().lower()
    if not normalized["section_instance_id"] and normalized["section_code"]:
        # Preserve pre-contiguous-v1 rows during a scoped checkpoint/upsert. The
        # legacy splitter emitted at most one row per canonical section code.
        normalized["section_instance_id"] = f"{normalized['section_code']}__0001"
    normalized["provenance_version"] = normalized["provenance_version"].strip()
    return normalized


def _index_key(row: dict) -> tuple[str, str, str] | None:
    ticker = str(row.get("ticker", "")).strip().upper()
    pdf_stem = str(row.get("pdf_stem", "")).strip()
    section_instance_id = str(row.get("section_instance_id", "")).strip().lower()
    if not ticker or not pdf_stem or not section_instance_id:
        return None
    return ticker, pdf_stem, section_instance_id


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
    expected_by_instance = {
        section.section_instance_id: section
        for section in sections
        if section.text.strip()
    }
    reasons: set[str] = set()

    if not expected_by_instance:
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

    if set(matching_rows) != set(expected_by_instance):
        reasons.add("section_rows_do_not_match_expected")
    if duplicate_keys and any(
        key[0] == ticker and key[1] == pdf_stem for key in duplicate_keys
    ):
        reasons.add("duplicate_index_rows")
    if matching_rows and any(
        not _fingerprint_matches(row, source_fingerprint) for row in matching_rows.values()
    ):
        reasons.add("source_fingerprint_mismatch")
    if matching_rows and any(
        row.get("provenance_version", "") != PROVENANCE_VERSION
        for row in matching_rows.values()
    ):
        reasons.add("provenance_version_mismatch")

    for section_instance_id, section in expected_by_instance.items():
        expected_text = section.text.strip()
        row = matching_rows.get(section_instance_id)
        if row is None:
            continue

        expected_path = _section_output_path(output_root, ticker, pdf_stem, section_instance_id)
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
        if row.get("section_code", "") != section.section_code:
            reasons.add("section_code_mismatch")
        expected_start = "" if section.source_start_char is None else str(section.source_start_char)
        expected_end = "" if section.source_end_char is None else str(section.source_end_char)
        if str(row.get("source_start_char", "")) != expected_start:
            reasons.add("source_start_char_mismatch")
        if str(row.get("source_end_char", "")) != expected_end:
            reasons.add("source_end_char_mismatch")

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
        key=lambda row: (row["ticker"], row["pdf_stem"], row["section_instance_id"]),
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
    pdf_stem: str | None = None,
    resume: bool = True,
    force: bool = False,
    checkpoint_every: int = 1,
    experimental_sectioning: bool = False,
) -> list[dict]:
    """Section parsed ESG text with restart-safe per-file checkpoints."""
    if not experimental_sectioning:
        # The broad heading/table changes in this module remain available for
        # isolated experiments, but they changed 365 sections in a full-corpus
        # frozen-text rebuild. Keep the proven 9,819-section implementation as
        # the pipeline default until the new rules pass semantic retrieval QA.
        import section_splitter_esg_legacy

        return section_splitter_esg_legacy.run(
            input_root=input_root,
            out=out,
            index=index,
            ticker=ticker,
            pdf_stem=pdf_stem,
            resume=resume,
            force=force,
            checkpoint_every=checkpoint_every,
        )
    if checkpoint_every < 1:
        raise ValueError("checkpoint_every must be at least 1")

    input_root = Path(input_root)
    output_root = Path(out)
    index_path = Path(index)

    txt_files = discover_text_files(input_root, ticker=ticker, pdf_stem=pdf_stem)
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
            page_spans = read_page_map(txt_file)
            sections = _output_sections(text, page_spans=page_spans)
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
    parser.add_argument("--input", default=str(config.ESG_TEXT_DIR))
    parser.add_argument("--out", default=str(config.ESG_SECTIONS_DIR))
    parser.add_argument("--index", default=str(config.ESG_SECTIONS_INDEX_CSV))
    parser.add_argument("--ticker", default=None)
    parser.add_argument(
        "--pdf-stem",
        default=None,
        help="Process only the parsed-text file whose filename stem exactly matches this value.",
    )
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
    parser.add_argument(
        "--experimental-sectioning",
        action="store_true",
        help=(
            "Use the unpromoted broad heading/table rules. The default uses the "
            "frozen full-corpus sectioner until semantic retrieval QA passes."
        ),
    )
    args = parser.parse_args()

    run(
        input_root=args.input,
        out=args.out,
        index=args.index,
        ticker=args.ticker.upper() if args.ticker else None,
        pdf_stem=args.pdf_stem,
        resume=args.resume,
        force=args.force,
        checkpoint_every=args.checkpoint_every,
        experimental_sectioning=args.experimental_sectioning,
    )


if __name__ == "__main__":
    main()
