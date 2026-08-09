"""Semantic, offset-preserving chunk candidate generation for FY2325 v2."""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import time
from bisect import bisect_right
from collections import Counter
from dataclasses import asdict, dataclass
from pathlib import Path

import tiktoken

CHUNKER_VERSION = "fy2325-chunker-v2.17"
ENCODING_NAME = "cl100k_base"
BGE_MODEL = "BAAI/bge-base-en-v1.5"
BGE_REVISION = "a5beb1e3e68b9ab74eb54cfd186867f64f240e1a"
BGE_MAX_TOKENS = 512
TABLE_START_RE = re.compile(r"^\[TABLE_START:(table_\d+)\]$")
TABLE_END_RE = re.compile(r"^\[TABLE_END:(table_\d+)\]$")
TABLE_MARKER_RE = re.compile(
    r"\[TABLE_(?:START:[^\]\r\n]+|END:[^\]\r\n]+)\]"
)
LIST_RE = re.compile(r"^\s*(?:[-*•▪◦]|\(?\d+[.)]|\(?[a-zA-Z][.)])\s+")
SENTENCE_BREAK_RE = re.compile(r"(?<=[.!?])\s+(?=[A-Z0-9(\"'\[])")
TOC_RE = re.compile(r"table\s+of\s+contents", re.I)
AUDITOR_RE = re.compile(
    r"report\s+of\s+independent\s+registered\s+public\s+accounting\s+firm",
    re.I,
)
HEADING_STOPWORDS = {
    "a", "an", "and", "as", "at", "for", "if", "in", "item", "no",
    "of", "on", "or", "our", "pursuant", "the", "these", "this", "those",
    "we",
}

AUDITOR_BLOCK_RE = re.compile(
    r"(?i)(?:"
    r"report\s+of\s+independent\s+registered\s+public\s+accounting\s+firm|"
    r"opinion\s+on\s+(?:the\s+)?(?:consolidated\s+)?financial\s+statements|"
    r"we\s+have\s+audited\s+the\s+accompanying|"
    r"basis\s+for\s+opinion|"
    r"critical\s+audit\s+matters?|"
    r"public\s+company\s+accounting\s+oversight\s+board|"
    r"\bPCAOB\b"
    r")"
)
AUDITOR_REGION_START_RE = re.compile(
    r"(?im)(?:"
    r"^\s*report\s+of\s+independent\s+registered\s+public"
    r"\s+accounting\s+firm\b|"
    r"\bopinion\s+on\s+(?:the\s+)?(?:consolidated\s+)?"
    r"financial\s+statements\b|"
    r"\bwe\s+have\s+audited\s+the\s+accompanying\b|"
    r"\bbasis\s+for\s+opinion\b|"
    r"\bcritical\s+audit\s+matters?\b|"
    r"\bhow\s+we\s+addressed\s+the\s+matter\b|"
    r"\bour\s+audit\s+procedures?\b|"
    r"\bwe\s+performed\s+(?:audit\s+)?procedures?\b|"
    r"\bpublic\s+company\s+accounting\s+oversight\s+board\b|"
    r"\bPCAOB\b"
    r")"
)

FILING_BOILERPLATE_RE = re.compile(
    r"(?i)(?:website.*(?:not|is not).*(?:incorporated|part of this report)|"
    r"sec(?:urities and exchange commission)? website|"
    r"available free of charge.*annual report)"
)
EXHIBIT_INDEX_RE = re.compile(
    r"(?i)^\s*(?:exhibits?|exhibit\s+index)\s*:?\s*$"
)
EXHIBIT_CONTENT_RE = re.compile(
    r"(?i)(?:"
    r"\bindex\s+to\s+exhibits?\b|"
    r"\bexhibit\s+index\b|"
    r"\bincorporated\s+(?:herein\s+)?by\s+reference\b|"
    r"\bcommission\s+file\s+(?:number|no\.?)\b|"
    r"\bexhibit\s+(?:number|no\.?|description)\b"
    r")"
)
EXHIBIT_ROW_RE = re.compile(
    r"(?im)^\s*(?:exhibit\s+)?"
    r"(?:\d{1,3}(?:\.\d+){1,3}|10\d(?:\.[A-Z]+)?)"
    r"[*†+]?\s*\|"
)
EXHIBIT_TRAILING_RE = re.compile(
    r"(?i)(?:"
    r"\bfiled\s+herewith\b|"
    r"\bfurnished\s+herewith\b|"
    r"\bexhibits?\s+to\s+this\s+report\s+(?:are|is)\s+listed\b|"
    r"\binstruments?\s+defining\s+the\s+rights\b.*"
    r"\bomitted\s+pursuant\s+to\s+item\s+601\b|"
    r"\bform\s+(?:s-1(?:/a)?|8-k|10-q|10-k)\b"
    r")"
)
SIGNATURE_CONTENT_RE = re.compile(
    r"(?i)(?:"
    r"\bpursuant\s+to\s+the\s+requirements\s+of\b|"
    r"\bpower\s+of\s+attorney\b|"
    r"\battorney-in-fact\b|"
    r"\bprincipal\s+executive\s+officer\b.*\bsigned\b"
    r")"
)
CONSENT_CONTENT_RE = re.compile(
    r"(?i)(?:"
    r"\bconsent\s+of\s+independent\s+registered\s+public\s+accounting"
    r"\s+firm\b|"
    r"\bwe\s+hereby\s+consent\s+to\s+the\s+incorporation\s+by\s+reference\b"
    r")"
)
FINANCIAL_HEADING_RE = re.compile(
    r"(?i)^\s*(?:"
    r"(?:consolidated\s+)?(?:balance\s+sheets?|statements?\s+of\s+"
    r"(?:operations|income|cash\s+flows|stockholders.?\s+equity|"
    r"comprehensive\s+income|financial\s+position))|"
    r"notes?\s+to\s+(?:the\s+)?(?:consolidated\s+)?financial\s+statements|"
    r"schedule\s+ii\b|"
    r"valuation\s+and\s+qualifying\s+accounts"
    r")"
)
FINANCIAL_NOTES_HEADING_RE = re.compile(
    r"(?i)\bnotes?\s+to\s+(?:the\s+)?"
    r"(?:consolidated\s+)?financial\s+statements\b"
)

FINANCIAL_NOTE_SUBSECTION_RE = re.compile(
    r"(?i)^\s*(?:"
    r"notes?\s+\d+\b|"
    r"\d{1,2}\s*[–—-]\s*"
    r"(?:earnings|segment|restructuring|income taxes?|leases?|"
    r"debt|inventory|goodwill|intangibles?|cash flow)|"
    r"deferred\s+taxes?|"
    r"segment\s+information|"
    r"vehicle\s+floor\s+plan\s+notes?\s+payable|"
    r"annual\s+impairment\s+test"
    r")"
)

FINANCIAL_TABLE_RE = re.compile(
    r"(?i)(?:"
    r"\b(?:net\s+sales|revenue|total\s+assets|total\s+liabilities|"
    r"net\s+income|operating\s+income|cash\s+and\s+cash\s+equivalents|"
    r"stockholders.?\s+equity)\b[^\n]{0,100}\|"
    r")"
)
AUDITOR_REGION_SECTIONS = {
    "Item_8",
    "Item_15",
    "Item_16",
    "Signatures",
}
FINANCIAL_STATEMENT_BOUNDARY_RE = re.compile(
    r"(?im)^\s*(?:\d+\s+)?"
    r"(?:[A-Z][A-Z0-9&.,’'()\- ]{2,80}\s+)?"
    r"(?:consolidated\s+)?(?:"
    r"balance\s+sheets?|"
    r"statements?\s+of\s+(?:operations|income|cash\s+flows?|"
    r"stockholders.?\s+equity|shareholders.?\s+equity|"
    r"comprehensive\s+(?:income|loss)|financial\s+position)|"
    r"notes?\s+to\s+(?:the\s+)?(?:consolidated\s+)?"
    r"financial\s+statements"
    r")\s*(?:\(|$)"
)
FINANCIAL_STATEMENT_INDEX_RE = re.compile(
    r"(?i)^\s*index\s+to\s+(?:consolidated\s+)?"
    r"financial\s+statements(?:\s+and\s+supplementary\s+data)?\s*$"
)
LATE_TRAILING_SECTIONS = {"Item_15", "Item_16", "Signatures"}
CONTINUATION_WORD_RE = re.compile(
    r"(?i)^(?:and|or|but|with|of|to|from|which|that|including|"
    r"excluding|through|under|over|as|for|by|in)\b"
)
INCOMPLETE_END_RE = re.compile(
    r"(?i)(?:"
    r"[,;]\s*(?:and|or)?|"
    r"\(\s*such\s+as|"
    r"\b(?:and|or|but|with|of|to|from|which|that|including|"
    r"excluding|through|under|over|as|for|by|in|the|a|an|our|"
    r"their|its|such\s+as)"
    r")\s*$"
)
AUDITOR_PROCEDURE_RE = re.compile(
    r"(?i)(?:"
    r"\bwith\s+the\s+assistance\s+of\s+our\s+"
    r"(?:fair\s+value|valuation|tax|information\s+technology)\s+"
    r"specialists?\b|"
    r"\bwe\s+(?:evaluated|tested)\s+(?:the\s+)?(?:"
    r"reasonableness|effectiveness|source\s+information|"
    r"mathematical\s+accuracy|management(?:'s|\s+and)|controls?)\b|"
    r"\bwe\s+performed\s+(?:audit\s+)?procedures?\b|"
    r"\bour\s+audit\s+procedures?\b|"
    r"\bhow\s+we\s+addressed\s+the\s+matter\b|"
    r"\bdescription\s+of\s+the\s+matter\b|"
    r"\bespecially\s+challenging(?:\s+or\s+subjective)?\s+"
    r"(?:audit\s+effort|judgment)\b|"
    r"\bcommunicated\s+(?:or\s+required\s+to\s+be\s+communicated\s+)"
    r"to\s+the\s+audit\s+committee\b"
    r")"
)


@dataclass(frozen=True)
class Unit:
    start: int
    end: int
    kind: str
    subsection: str


@dataclass(frozen=True)
class SemanticBoundaryIndex:
    """O(log n) lookup for positions inside original semantic units."""

    units: tuple[Unit, ...]
    starts: tuple[int, ...]

    @classmethod
    def build(cls, units: list[Unit]) -> SemanticBoundaryIndex:
        values = tuple(units)
        return cls(values, tuple(unit.start for unit in values))

    def contains(self, position: int) -> bool:
        index = bisect_right(self.starts, position) - 1
        return bool(
            index >= 0
            and self.units[index].start < position < self.units[index].end
        )


@dataclass(frozen=True)
class ChunkRecord:
    chunk_id: str
    section_id: str
    company_id: str
    ticker: str
    coverage_year: int
    accession_number: str
    canonical_section_code: str
    rag_section_code: str
    subsection_heading: str
    chunk_type: str
    chunk_index: int
    source_start_char: int
    source_end_char: int
    section_start_char: int
    section_end_char: int
    chunk_text: str
    embedding_text: str
    token_count: int
    embedding_token_count: int
    embedding_model: str
    embedding_model_revision: str
    embedding_max_tokens: int
    chunk_text_sha256: str
    embedding_text_sha256: str
    chunker_version: str
    chunker_config_sha256: str
    boundary_start_type: str
    boundary_end_type: str
    semantic_topic_count: int
    continuation_from_previous: bool
    continues_to_next: bool
    quality_status: str
    quality_flags: tuple[str, ...]
    rag_action: str


def sha256_text(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def read_jsonl(path: Path) -> list[dict[str, object]]:
    with path.open(encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def token_count(encoder, text: str) -> int:
    return len(encoder.encode(text, disallowed_special=()))


def embedding_token_count(tokenizer, text: str) -> int:
    return len(
        tokenizer.encode(
            text,
            add_special_tokens=True,
            truncation=False,
        )
    )


def line_spans(text: str) -> list[tuple[int, int, str]]:
    output = []
    position = 0
    for line in text.splitlines(keepends=True):
        content_end = position + len(line.rstrip("\r\n"))
        output.append((position, content_end, text[position:content_end]))
        position += len(line)
    if position < len(text):
        output.append((position, len(text), text[position:]))
    return output


def is_heading(line: str) -> bool:
    value = line.strip()
    if not value or len(value) > 160 or LIST_RE.match(value):
        return False
    if value.startswith("[TABLE_"):
        return False
    if re.match(
        r"(?i)^item\s+(?:no\.?\s*)?\d{1,2}[a-c]?\b",
        value,
    ):
        return True
    words = re.findall(r"[A-Za-z][A-Za-z'’-]*", value)
    if not 1 <= len(words) <= 18:
        return False
    lowered = [word.lower() for word in words]
    if len(words) == 1:
        if lowered[0] in HEADING_STOPWORDS:
            return False
        if len(words[0]) < 5 and not words[0].isupper():
            return False
    if len(words) <= 3 and lowered[0] in HEADING_STOPWORDS:
        return False
    if value.endswith((".", ";", ",")) and not value.lower().endswith("inc."):
        return False
    upper_ratio = sum(word.isupper() for word in words) / len(words)
    title_ratio = sum(word[:1].isupper() for word in words) / len(words)
    return upper_ratio >= 0.70 or title_ratio >= 0.75


def sentence_units(
    text: str, start: int, end: int, kind: str, subsection: str
) -> list[Unit]:
    value = text[start:end]
    boundaries = [0]
    boundaries.extend(match.end() for match in SENTENCE_BREAK_RE.finditer(value))
    boundaries.append(len(value))
    units = []
    for left, right in zip(boundaries, boundaries[1:]):
        absolute_left = start + left
        absolute_right = start + right
        while absolute_left < absolute_right and text[absolute_left].isspace():
            absolute_left += 1
        while absolute_right > absolute_left and text[absolute_right - 1].isspace():
            absolute_right -= 1
        if absolute_right > absolute_left:
            units.append(Unit(absolute_left, absolute_right, kind, subsection))
    return units


def semantic_units(text: str) -> list[Unit]:
    lines = line_spans(text)
    units = []
    subsection = ""
    paragraph_start = None
    paragraph_end = None
    index = 0

    def flush_paragraph() -> None:
        nonlocal paragraph_start, paragraph_end
        if paragraph_start is not None and paragraph_end is not None:
            units.extend(
                sentence_units(
                    text,
                    paragraph_start,
                    paragraph_end,
                    "narrative",
                    subsection,
                )
            )
        paragraph_start = None
        paragraph_end = None

    while index < len(lines):
        start, end, line = lines[index]
        stripped = line.strip()
        table_match = TABLE_START_RE.match(stripped)
        if table_match:
            flush_paragraph()
            content_start = None
            content_end = None
            index += 1

            while index < len(lines):
                row_start, row_end, table_line = lines[index]

                if TABLE_END_RE.match(table_line.strip()):
                    if (
                        content_start is not None
                        and content_end is not None
                    ):
                        units.append(
                            Unit(
                                content_start,
                                content_end,
                                "table",
                                subsection,
                            )
                        )
                    break

                if table_line.strip():
                    if content_start is None:
                        content_start = row_start
                    content_end = row_end

                index += 1

            index += 1
            continue
        if TABLE_END_RE.match(stripped):
            # A section boundary can fall inside an HTML layout table,
            # leaving its closing parser marker in the next SEC section.
            # Control markers are not filing content and must never become
            # narrative chunks.
            flush_paragraph()
            index += 1
            continue
        if not stripped:
            flush_paragraph()
            index += 1
            continue
        if is_heading(stripped):
            flush_paragraph()
            subsection = stripped
            index += 1
            continue
        if LIST_RE.match(stripped):
            flush_paragraph()
            units.append(Unit(start, end, "list", subsection))
            index += 1
            continue
        if paragraph_start is None:
            paragraph_start = start
        paragraph_end = end
        index += 1
    flush_paragraph()
    return units


def split_large_unit(text: str, unit: Unit, encoder, hard_max: int) -> list[Unit]:
    if token_count(encoder, text[unit.start : unit.end]) <= hard_max:
        return [unit]
    if unit.kind == "table":
        spans = line_spans(text[unit.start : unit.end])
        output = []
        group_start = None
        group_end = None
        for local_start, local_end, line in spans:
            if TABLE_START_RE.match(line.strip()) or TABLE_END_RE.match(line.strip()):
                continue
            start = unit.start + local_start
            end = unit.start + local_end
            row_text = text[start:end]

            if token_count(encoder, row_text) > hard_max:
                if group_start is not None:
                    output.append(
                        Unit(
                            group_start,
                            group_end,
                            "table",
                            unit.subsection,
                        )
                    )
                    group_start = None
                    group_end = None

                tokens = encoder.encode(
                    row_text,
                    disallowed_special=(),
                )
                decoded, offsets = encoder.decode_with_offsets(tokens)

                if decoded != row_text:
                    raise ValueError(
                        "token round-trip mismatch in oversized table row"
                    )

                token_start = 0

                while token_start < len(tokens):
                    token_end = min(
                        token_start + hard_max,
                        len(tokens),
                    )

                    if token_end < len(tokens):
                        while (
                            token_end
                            > token_start + hard_max // 2
                        ):
                            character = offsets[token_end]
                            if (
                                character > 0
                                and row_text[character - 1].isspace()
                            ):
                                break
                            token_end -= 1

                    char_start = offsets[token_start]
                    char_end = (
                        offsets[token_end]
                        if token_end < len(tokens)
                        else len(row_text)
                    )

                    output.append(
                        Unit(
                            start + char_start,
                            start + char_end,
                            "table_continuation",
                            unit.subsection,
                        )
                    )
                    token_start = token_end

                continue

            if group_start is None:
                group_start, group_end = start, end
                continue
            candidate = text[group_start:end]
            if token_count(encoder, candidate) > hard_max:
                output.append(Unit(group_start, group_end, "table", unit.subsection))
                group_start, group_end = start, end
            else:
                group_end = end
        if group_start is not None:
            output.append(Unit(group_start, group_end, "table", unit.subsection))
        return output
    value = text[unit.start : unit.end]
    tokens = encoder.encode(value, disallowed_special=())
    decoded, offsets = encoder.decode_with_offsets(tokens)
    if decoded != value:
        raise ValueError("token round-trip mismatch in oversized unit")
    output = []
    token_start = 0
    while token_start < len(tokens):
        token_end = min(token_start + hard_max, len(tokens))
        if token_end < len(tokens):
            while token_end > token_start + hard_max // 2:
                character = offsets[token_end]
                if character > 0 and value[character - 1].isspace():
                    break
                token_end -= 1
        char_start = offsets[token_start]
        char_end = offsets[token_end] if token_end < len(tokens) else len(value)
        output.append(
            Unit(
                unit.start + char_start,
                unit.start + char_end,
                unit.kind,
                unit.subsection,
            )
        )
        token_start = token_end
    return output


def group_units(
    text: str,
    units: list[Unit],
    encoder,
    target: int,
    hard_max: int,
    overlap: int,
) -> list[list[Unit]]:
    expanded = []
    for unit in units:
        expanded.extend(split_large_unit(text, unit, encoder, hard_max))
    groups = []
    current = []
    last_emitted_end = -1

    def overlap_tail(values: list[Unit]) -> list[Unit]:
        tail = []
        for prior in reversed(values):
            candidate = [prior, *tail]
            if token_count(
                encoder, text[candidate[0].start : candidate[-1].end]
            ) > overlap:
                break
            tail = candidate
        return tail

    narrative_kinds = {"narrative", "list"}

    for unit_index, unit in enumerate(expanded):
        crosses_table_boundary = bool(
            current
            and "[TABLE_" in text[current[-1].end:unit.start]
        )

        should_break = bool(current and (
            unit.kind != current[-1].kind
            or unit.subsection != current[-1].subsection
            or crosses_table_boundary
            or token_count(
                encoder,
                text[current[0].start:unit.end],
            ) > hard_max
        ))
        cross_unit_continuation = bool(
            current
            and not crosses_table_boundary
            and current[-1].kind in narrative_kinds
            and unit.kind in narrative_kinds
            and grammatical_boundary_split(text, current[-1].end)
            and token_count(
                encoder,
                text[current[0].start:unit.end],
            ) <= hard_max
        )

        if should_break and not cross_unit_continuation:
            if current[-1].end > last_emitted_end:
                groups.append(current)
                last_emitted_end = current[-1].end
            overlap_units = []
            if (
                unit.kind == current[-1].kind
                and unit.subsection == current[-1].subsection
                and not crosses_table_boundary
            ):
                candidate_overlap = overlap_tail(current)

                if (
                    candidate_overlap
                    and token_count(
                        encoder,
                        text[candidate_overlap[0].start:unit.end],
                    ) <= hard_max
                ):
                    overlap_units = candidate_overlap

            current = overlap_units
        current.append(unit)
        if token_count(encoder, text[current[0].start : current[-1].end]) >= target:
            next_unit = (
                expanded[unit_index + 1]
                if unit_index + 1 < len(expanded)
                else None
            )
            can_finish_thought = bool(
                next_unit is not None
                and current[-1].kind in narrative_kinds
                and next_unit.kind in narrative_kinds
                and "[TABLE_"
                not in text[current[-1].end:next_unit.start]
                and grammatical_boundary_split(text, current[-1].end)
                and token_count(
                    encoder,
                    text[current[0].start:next_unit.end],
                ) <= hard_max
            )
            if can_finish_thought:
                continue
            groups.append(current)
            last_emitted_end = current[-1].end
            current = overlap_tail(current)
    if current and current[-1].end > last_emitted_end:
        groups.append(current)
    return groups


def embedding_text(
    company_name: str,
    ticker: str,
    coverage_year: int,
    section: str,
    subsection: str,
    chunk_type: str,
    chunk_text: str,
    table_context: str = "",
    canonical_section: str = "",
    continuation_context: str = "",
    forward_context: str = "",
) -> str:
    human_section = section.replace("_", " ")
    prefix = (
        f"Company: {company_name}\n"
        f"Ticker: {ticker}\n"
        f"Document: Form 10-K\n"
        f"Fiscal year: FY{coverage_year}\n"
        f"SEC section: {human_section}\n"
        f"Subsection: {subsection or 'Not specified'}\n"
        f"Content type: {chunk_type}\n"
    )
    if canonical_section and canonical_section != section:
        prefix += (
            "Source SEC container: "
            f"{canonical_section.replace('_', ' ')}\n"
        )
    if table_context:
        prefix += f"Table context: {table_context}\n"
    if continuation_context:
        prefix += f"Continuation context: {continuation_context}\n"
    if forward_context:
        prefix += f"Forward continuation context: {forward_context}\n"
    return f"{prefix}\n{chunk_text}"


def inside_semantic_unit(
    units: list[Unit],
    position: int,
) -> bool:
    """Return true only when a boundary splits an original semantic unit."""
    return any(
        unit.start < position < unit.end
        for unit in units
    )


def continuation_context(text: str, position: int, maximum: int = 96) -> str:
    """Return bounded prior source context for a genuine mid-thought split."""
    if position <= 0:
        return ""
    prior, current = normalized_boundary_sides(text, position)
    prior = prior[-(maximum * 3):]
    if not prior or prior[-1] in ".!?":
        return ""
    if not current:
        return ""
    if current[0].isupper() and prior[-1] in ")]\"'":
        return ""
    raw_context = prior
    raw_context = TABLE_MARKER_RE.sub(" ", raw_context)
    value = " ".join(raw_context.split())
    return value[-maximum:].lstrip()


def normalized_boundary_sides(
    text: str,
    position: int,
) -> tuple[str, str]:
    """Return filing text around a boundary without page-control noise."""
    prior = text[max(0, position - 384):position].rstrip()
    following = text[position:position + 384].lstrip()
    prior = TABLE_MARKER_RE.sub(" ", prior).rstrip()
    following = TABLE_MARKER_RE.sub(" ", following).lstrip()
    prior = re.sub(
        r"(?:\s|\u200e|\u200f)*\d+(?:\s|\u200e|\u200f)*$",
        "",
        prior,
    ).rstrip()
    following = re.sub(
        r"^(?:(?:\u200e|\u200f)?\d{1,3}\s+)+",
        "",
        following,
    ).lstrip()
    return prior, following


def grammatical_boundary_split(text: str, position: int) -> bool:
    """Detect a thought split even when HTML created a semantic-unit edge."""
    prior, following = normalized_boundary_sides(text, position)
    if not prior or not following:
        return False
    if INCOMPLETE_END_RE.search(prior):
        return True
    if prior[-1] in ".!?":
        return False
    if following[0].islower() or following[0].isdigit():
        return True
    if following[0] in "([•▪◦":
        return True
    return False


def grammatical_continuation_start(text: str, position: int) -> bool:
    prior, current = normalized_boundary_sides(text, position)
    if not current or not prior:
        return False
    return bool(
        grammatical_boundary_split(text, position)
        or (
            CONTINUATION_WORD_RE.match(current)
            and prior[-1] not in ".!?"
        )
    )


def grammatical_continuation_end(text: str, position: int) -> bool:
    return grammatical_boundary_split(text, position)


def forward_continuation_context(
    text: str,
    position: int,
    maximum: int = 96,
) -> str:
    if position >= len(text):
        return ""
    _, raw_context = normalized_boundary_sides(text, position)
    raw_context = raw_context[:maximum * 3]
    raw_context = TABLE_MARKER_RE.sub(" ", raw_context)
    value = " ".join(raw_context.split())
    return value[:maximum].rstrip()


def boundary_contexts(
    text: str,
    start: int,
    end: int,
    chunk_type: str,
    units: list[Unit] | SemanticBoundaryIndex,
) -> tuple[str, str]:
    boundary_index = (
        units
        if isinstance(units, SemanticBoundaryIndex)
        else SemanticBoundaryIndex.build(units)
    )
    narrative_like = chunk_type in {"narrative", "list", "mixed_approved"}
    prior = bool(
        boundary_index.contains(start)
        or (
            narrative_like
            and grammatical_continuation_start(text, start)
        )
    )
    forward = bool(
        boundary_index.contains(end)
        or (
            narrative_like
            and grammatical_continuation_end(text, end)
        )
    )
    return (
        continuation_context(text, start) if prior else "",
        forward_continuation_context(text, end) if forward else "",
    )


def substantive_financial_content(
    chunk_type: str,
    text: str,
    subsection: str,
    table_context: str,
) -> bool:
    context = "\n".join((subsection, table_context, text[:500]))
    if FINANCIAL_HEADING_RE.search(subsection):
        return True
    if FINANCIAL_NOTES_HEADING_RE.search(subsection):
        return True
    if FINANCIAL_NOTE_SUBSECTION_RE.search(subsection):
        return True
    if FINANCIAL_HEADING_RE.search(table_context):
        return True
    if FINANCIAL_HEADING_RE.search(text[:300]):
        return True
    if chunk_type in {"table", "table_continuation", "mixed_approved"}:
        return len(FINANCIAL_TABLE_RE.findall(context)) >= 1
    return False


def rag_content_policy(
    chunk_type: str,
    text: str,
    subsection: str,
    canonical_section: str,
    table_context: str = "",
) -> tuple[str, list[str], str | None]:
    """Return semantic RAG section, policy flags, and optional forced action."""
    rag_section = canonical_section
    flags: list[str] = []
    forced_action = None

    if canonical_section not in LATE_TRAILING_SECTIONS:
        return rag_section, flags, forced_action

    financial = substantive_financial_content(
        chunk_type,
        text,
        subsection,
        table_context,
    )
    exhibit = bool(
        EXHIBIT_INDEX_RE.search(subsection)
        or EXHIBIT_CONTENT_RE.search(subsection)
        or EXHIBIT_CONTENT_RE.search(text)
        or EXHIBIT_ROW_RE.search(text)
    )
    signature = bool(SIGNATURE_CONTENT_RE.search(text))
    consent = bool(CONSENT_CONTENT_RE.search(text))

    # Actual statement/schedule content wins over its positional container,
    # but never over a clear signature, consent, or exhibit inventory.
    if financial and not (signature or consent or exhibit):
        rag_section = "Item_8"
        flags.append("late_financial_content_routed_to_item_8")
        return rag_section, flags, forced_action

    if exhibit:
        rag_section = "Exhibit_Index"
        flags.append("exhibit_index_non_rag")
        forced_action = "exclude"
    elif signature:
        rag_section = "Signatures"
        flags.append("signature_non_rag")
        forced_action = "exclude"
    elif consent:
        rag_section = "Auditor_Consent"
        flags.append("auditor_consent_non_rag")
        forced_action = "exclude"
    elif canonical_section == "Signatures":
        flags.append("unclassified_signature_container")
        forced_action = "exclude"
    elif canonical_section == "Item_16":
        flags.append("form_10k_summary_non_rag")
        forced_action = "exclude"

    return rag_section, flags, forced_action


def strong_exhibit_boundary(text: str, subsection: str) -> bool:
    first_lines = [
        line.strip()
        for line in text.splitlines()[:3]
        if line.strip()
    ]
    return bool(
        EXHIBIT_INDEX_RE.search(subsection)
        or re.search(r"(?i)\bindex\s+to\s+exhibits?\b", subsection)
        or any(EXHIBIT_INDEX_RE.search(line) for line in first_lines)
        or any(
            re.search(r"(?i)\bindex\s+to\s+exhibits?\b", line)
            for line in first_lines
        )
        or EXHIBIT_TRAILING_RE.search(text)
        or len(EXHIBIT_ROW_RE.findall(text)) >= 2
        or (
            len(EXHIBIT_ROW_RE.findall(text)) >= 2
            and EXHIBIT_CONTENT_RE.search(text)
        )
    )


def explicit_financial_region_start(
    text: str,
    subsection: str,
    table_context: str = "",
) -> bool:
    """Recognize a real statement/note boundary that ends an auditor report."""
    return bool(
        FINANCIAL_HEADING_RE.search(subsection)
        or FINANCIAL_NOTES_HEADING_RE.search(subsection)
        or FINANCIAL_HEADING_RE.search(table_context)
        or FINANCIAL_NOTES_HEADING_RE.search(table_context)
        or FINANCIAL_HEADING_RE.search(text[:300])
        or FINANCIAL_NOTES_HEADING_RE.match(text[:300])
        or FINANCIAL_STATEMENT_BOUNDARY_RE.search(text[:700])
        or FINANCIAL_STATEMENT_BOUNDARY_RE.search(table_context[:700])
        or re.search(
            r"(?i)^\s*\d{1,2}[.)]\s+[A-Z]",
            text[:300],
        )
    )


def financial_statement_boundary(
    chunk_type: str,
    text: str,
    subsection: str,
    table_context: str = "",
) -> bool:
    """Recognize an explicit statement/note boundary or financial data table."""
    return bool(
        explicit_financial_region_start(
            text,
            subsection,
            table_context,
        )
        or (
            chunk_type in {"table", "table_continuation", "mixed_approved"}
            and FINANCIAL_TABLE_RE.search(text)
        )
    )


def late_region_policy(
    current_region: str,
    chunk_type: str,
    text: str,
    subsection: str,
    canonical_section: str,
    table_context: str = "",
) -> tuple[str, str, list[str], str | None]:
    """Apply ordered financial, auditor, and non-RAG region inheritance."""
    rag_section, flags, forced_action = rag_content_policy(
        chunk_type,
        text,
        subsection,
        canonical_section,
        table_context,
    )
    # Auditor-report inheritance is valid only in financial-statement
    # containers. It must never leak into Business, Risk Factors, MD&A,
    # governance, or other SEC sections.
    if canonical_section not in AUDITOR_REGION_SECTIONS:
        return "", rag_section, flags, forced_action

    signature = bool(SIGNATURE_CONTENT_RE.search(text))
    consent = bool(CONSENT_CONTENT_RE.search(text))
    exhibit = strong_exhibit_boundary(text, subsection)
    direct_auditor = bool(
        AUDITOR_REGION_START_RE.search(text)
        or AUDITOR_PROCEDURE_RE.search(text)
    )
    subsection_auditor = bool(AUDITOR_BLOCK_RE.search(subsection))
    financial_start = financial_statement_boundary(
        chunk_type,
        text,
        subsection,
        table_context,
    )
    financial = substantive_financial_content(
        chunk_type,
        text,
        subsection,
        table_context,
    )

    if exhibit:
        return (
            "exhibit",
            "Exhibit_Index",
            ["exhibit_index_non_rag"],
            "exclude",
        )
    if signature or consent:
        return "non_rag", rag_section, flags, forced_action
    if direct_auditor or (
        subsection_auditor and not financial_start
    ):
        return (
            "auditor",
            rag_section,
            ["auditor_opinion"],
            "exclude",
        )

    if financial_start:
        if canonical_section in LATE_TRAILING_SECTIONS:
            return (
                "financial",
                "Item_8",
                ["late_financial_content_routed_to_item_8"],
                None,
            )
        return "", rag_section, flags, forced_action

    if current_region == "auditor":
        return (
            "auditor",
            rag_section,
            ["auditor_opinion"],
            "exclude",
        )
    if current_region == "exhibit" and not financial_start:
        return (
            "exhibit",
            "Exhibit_Index",
            ["inherited_exhibit_index_non_rag"],
            "exclude",
        )

    if canonical_section not in LATE_TRAILING_SECTIONS:
        return "", rag_section, flags, forced_action

    if financial:
        return (
            "financial",
            "Item_8",
            ["late_financial_content_routed_to_item_8"],
            None,
        )
    if current_region == "financial":
        return (
            "financial",
            "Item_8",
            ["inherited_late_financial_region"],
            None,
        )
    return current_region, rag_section, flags, forced_action


def table_embedding_context(text: str, position: int) -> str:
    marker = text.rfind("[TABLE_START:", 0, position + 1)
    if marker < 0:
        return ""
    prior_end = text.rfind("[TABLE_END:", 0, position + 1)
    if prior_end > marker:
        return ""
    lines = []
    cursor = text.find("\n", marker)
    if cursor < 0:
        return ""
    cursor += 1
    while cursor < len(text):
        line_end = text.find("\n", cursor)
        if line_end < 0:
            line_end = len(text)
        line = text[cursor:line_end]
        if TABLE_END_RE.match(line.strip()):
            break
        value = line.strip()
        if value:
            lines.append(value)
        if len(lines) == 3:
            break
        cursor = line_end + 1
    # Context is retrieval metadata, not a duplicate copy of long table rows.
    # Keep useful titles/headers while bounding their contribution.
    return " / ".join(lines)[:96].rstrip()


def split_unit_at_embedding_limit(
    text: str,
    unit: Unit,
    tokenizer,
    make_embedding,
    maximum: int,
    measure_embedding=None,
) -> list[Unit]:
    """Split one semantic unit at exact source offsets for the BGE limit."""
    output = []
    start = unit.start
    output_kind = (
        "table_continuation"
        if unit.kind in {"table", "table_continuation"}
        else unit.kind
    )
    measure = measure_embedding or (
        lambda start, end, kind: embedding_token_count(
            tokenizer,
            make_embedding(start, end, kind),
        )
    )

    # Hugging Face fast tokenizers expose source offsets. Tokenize the semantic
    # unit once, then jump directly to a safe content boundary. This avoids
    # repeatedly tokenizing large suffixes during a character-level binary
    # search. The measured-count loop remains the exact final authority.
    if callable(tokenizer) and bool(getattr(tokenizer, "is_fast", False)):
        encoded = tokenizer(
            text[unit.start:unit.end],
            add_special_tokens=False,
            truncation=False,
            return_offsets_mapping=True,
        )
        offsets = [
            (int(left), int(right))
            for left, right in encoded["offset_mapping"]
            if int(right) > int(left)
        ]
        token_cursor = 0
        while start < unit.end:
            if measure(start, unit.end, output_kind) <= maximum:
                output.append(
                    Unit(start, unit.end, unit.kind, unit.subsection)
                )
                break
            relative_start = start - unit.start
            while (
                token_cursor < len(offsets)
                and offsets[token_cursor][1] <= relative_start
            ):
                token_cursor += 1
            metadata_count = measure(start, start, output_kind)
            budget = maximum - metadata_count
            if budget <= 0:
                raise RuntimeError(
                    "embedding metadata alone exceeds model limit"
                )
            candidate_index = min(
                len(offsets) - 1,
                token_cursor + budget - 1,
            )
            split = unit.start + offsets[candidate_index][1]
            while (
                split > start
                and measure(start, split, output_kind) > maximum
            ):
                candidate_index -= 1
                if candidate_index < token_cursor:
                    raise RuntimeError(
                        "unable to fit content within embedding limit"
                    )
                split = unit.start + offsets[candidate_index][1]
            while True:
                raw_split = unit.start + offsets[candidate_index][1]
                split = raw_split

                while (
                    split > start
                    and not text[split - 1].isspace()
                ):
                    split -= 1

                if split <= start:
                    split = raw_split

                if (
                    split > start
                    and measure(start, split, output_kind) <= maximum
                ):
                    break

                candidate_index -= 1
                if candidate_index < token_cursor:
                    raise RuntimeError(
                        "unable to fit whitespace-aligned content "
                        "within embedding limit"
                    )
            output.append(
                Unit(start, split, output_kind, unit.subsection)
            )
            start = split
        return output

    while start < unit.end:
        if measure(start, unit.end, output_kind) <= maximum:
            output.append(Unit(start, unit.end, unit.kind, unit.subsection))
            break

        low = start + 1
        high = unit.end
        best = None
        while low <= high:
            middle = (low + high) // 2
            count = measure(start, middle, output_kind)
            if count <= maximum:
                best = middle
                low = middle + 1
            else:
                high = middle - 1

        if best is None:
            raise RuntimeError("embedding metadata alone exceeds model limit")

        split = best
        while split > start and not text[split - 1].isspace():
            split -= 1
        if split == start:
            split = best

        while (
            split > start
            and measure(start, split, output_kind) > maximum
        ):
            split -= 1
            while (
                split > start
                and not text[split - 1].isspace()
            ):
                split -= 1

        if split == start:
            raise RuntimeError("unable to advance BGE-limited unit split")

        output.append(
            Unit(start, split, output_kind, unit.subsection)
        )
        start = split

    return output


def enforce_embedding_limit(
    text: str,
    group: list[Unit],
    tokenizer,
    maximum: int,
    company_name: str,
    ticker: str,
    coverage_year: int,
    section: str,
    source_units: list[Unit] | SemanticBoundaryIndex | None = None,
    rag_section_override: str = "",
) -> list[list[Unit]]:
    """Partition a cl100k-bounded group so every full embedding fits BGE."""
    boundary_index = (
        source_units
        if isinstance(source_units, SemanticBoundaryIndex)
        else SemanticBoundaryIndex.build(source_units or group)
    )
    embedding_cache: dict[tuple[int, int, str], str] = {}
    count_cache: dict[tuple[int, int, str], int] = {}

    def make_embedding(start: int, end: int, kind: str) -> str:
        cache_key = (start, end, kind)
        cached = embedding_cache.get(cache_key)
        if cached is not None:
            return cached
        value = text[start:end]
        table_context = (
            table_embedding_context(text, start)
            if kind in {"table", "table_continuation"}
            else ""
        )
        rag_section = rag_section_override
        if not rag_section:
            rag_section, _, _ = rag_content_policy(
                kind,
                value,
                group[0].subsection,
                section,
                table_context,
            )
        prior_context, next_context = boundary_contexts(
            text,
            start,
            end,
            kind,
            boundary_index,
        )
        # Continuation metadata is required evidence, not optional decoration.
        # Only table context may be dropped when metadata pressure requires it.
        context_options = (
            (table_context, prior_context, next_context),
            ("", prior_context, next_context),
        )

        seen = set()

        for (
            candidate_table_context,
            candidate_prior_context,
            candidate_next_context,
        ) in context_options:
            option = (
                candidate_table_context,
                candidate_prior_context,
                candidate_next_context,
            )

            if option in seen:
                continue

            seen.add(option)

            metadata_probe = embedding_text(
                company_name,
                ticker,
                coverage_year,
                rag_section,
                group[0].subsection,
                kind,
                "",
                candidate_table_context,
                section,
                candidate_prior_context,
                candidate_next_context,
            )

            if (
                embedding_token_count(
                    tokenizer,
                    metadata_probe,
                )
                >= maximum
            ):
                continue

            result = embedding_text(
                company_name,
                ticker,
                coverage_year,
                rag_section,
                group[0].subsection,
                kind,
                value,
                candidate_table_context,
                section,
                candidate_prior_context,
                candidate_next_context,
            )
            embedding_cache[cache_key] = result
            return result

        raise RuntimeError(
            "required embedding metadata exceeds model limit"
        )

    def measured_count(start: int, end: int, kind: str) -> int:
        cache_key = (start, end, kind)
        cached = count_cache.get(cache_key)
        if cached is not None:
            return cached
        count = embedding_token_count(
            tokenizer,
            make_embedding(start, end, kind),
        )
        count_cache[cache_key] = count
        return count

    expanded = []
    for unit in group:
        kind = unit.kind
        if measured_count(unit.start, unit.end, kind) <= maximum:
            expanded.append(unit)
        else:
            expanded.extend(
                split_unit_at_embedding_limit(
                    text,
                    unit,
                    tokenizer,
                    make_embedding,
                    maximum,
                    measured_count,
                )
            )

    output = []
    current = []
    for unit in expanded:
        candidate = [*current, unit]
        kinds = {value.kind for value in candidate}
        chunk_type = next(iter(kinds)) if len(kinds) == 1 else "mixed_approved"
        candidate_count = measured_count(
            candidate[0].start,
            candidate[-1].end,
            chunk_type,
        )
        if current and candidate_count > maximum:
            output.append(current)
            current = [unit]
        else:
            current = candidate
    if current:
        output.append(current)

    for values in output:
        kinds = {value.kind for value in values}
        chunk_type = next(iter(kinds)) if len(kinds) == 1 else "mixed_approved"
        final_count = measured_count(
            values[0].start,
            values[-1].end,
            chunk_type,
        )
        if final_count > maximum:
            raise RuntimeError(
                "BGE maximum exceeded after partition: "
                f"start={values[0].start} "
                f"end={values[-1].end} "
                f"chunk_type={chunk_type} "
                f"count={final_count} "
                f"maximum={maximum} "
                f"unit_count={len(values)} "
                f"unit_kinds={[value.kind for value in values]}"
            )
    return output


def quality(
    chunk_type: str,
    text: str,
    count: int,
    minimum: int,
    subsection: str = "",
    section: str = "",
    table_context: str = "",
    policy_result: tuple[str, list[str], str | None] | None = None,
) -> tuple[str, list[str], str]:
    flags = []
    action = "include"
    if count < minimum:
        flags.append("below_meaningful_minimum")
        action = "exclude"
    if "\ufffd" in text:
        flags.append("replacement_character")
        action = "exclude"
    if TOC_RE.search(text) and len(TOC_RE.findall(text)) >= 2:
        flags.append("table_of_contents")
        action = "exclude"
    financial_index = bool(
        FINANCIAL_STATEMENT_INDEX_RE.search(subsection)
    )
    if financial_index:
        flags.append("financial_statement_index_non_rag")
        action = "exclude"
    text_auditor = bool(
        AUDITOR_REGION_START_RE.search(text)
        or AUDITOR_PROCEDURE_RE.search(text)
    )
    subsection_auditor = bool(AUDITOR_BLOCK_RE.search(subsection))
    strong_financial_boundary = financial_statement_boundary(
        chunk_type,
        text,
        subsection,
        table_context,
    )
    if text_auditor or (
        subsection_auditor and not strong_financial_boundary
    ):
        flags.append("auditor_opinion")
        action = "exclude"
    if FILING_BOILERPLATE_RE.search(text):
        flags.append("filing_boilerplate")
        action = "exclude"
    if policy_result is None:
        _, policy_flags, forced_action = rag_content_policy(
            chunk_type,
            text,
            subsection,
            section,
            table_context,
        )
    else:
        _, policy_flags, forced_action = policy_result
    flags.extend(policy_flags)
    if forced_action is not None:
        action = forced_action
    if chunk_type == "table" and "|" not in text:
        flags.append("orphan_table")
        action = "exclude"
    return ("passed" if action == "include" else "failed"), flags, action


def progress_bar(done: int, total: int, width: int = 30) -> str:
    ratio = done / total if total else 1.0
    filled = min(width, int(ratio * width))
    return "[" + ("#" * filled) + ("-" * (width - filled)) + "]"


def write_progress(path: Path, payload: dict[str, object]) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    temporary.replace(path)


def emit_progress(
    done: int,
    total: int,
    started: float,
    chunk_count: int,
    current_section: str,
    initial_done: int = 0,
) -> dict[str, object]:
    elapsed = max(0.001, time.monotonic() - started)
    session_done = max(0, done - initial_done)
    rate = session_done / elapsed
    remaining = total - done
    eta_seconds = remaining / rate if rate else None
    percent = (100.0 * done / total) if total else 100.0
    eta_text = (
        time.strftime("%H:%M:%S", time.gmtime(eta_seconds))
        if eta_seconds is not None
        else "unknown"
    )
    print(
        f"PROGRESS {progress_bar(done, total)} "
        f"{done}/{total} ({percent:.1f}%) "
        f"elapsed={time.strftime('%H:%M:%S', time.gmtime(elapsed))} "
        f"eta={eta_text} chunks={chunk_count} "
        f"current={current_section}",
        flush=True,
    )
    return {
        "status": "running" if done < total else "completed",
        "completed_sections": done,
        "total_sections": total,
        "percent": round(percent, 4),
        "elapsed_seconds": round(elapsed, 3),
        "eta_seconds": (
            round(eta_seconds, 3) if eta_seconds is not None else None
        ),
        "chunks": chunk_count,
        "current_section": current_section,
    }


def run(
    sections_root: Path,
    companies_csv: Path,
    profiles_path: Path,
    profile_name: str,
    output_root: Path,
    resume: bool = False,
    progress_every: int = 1,
) -> dict[str, object]:
    sections = read_jsonl(sections_root / "sections.jsonl")
    companies = {}
    import csv

    with companies_csv.open(encoding="utf-8", newline="") as handle:
        for row in csv.DictReader(handle):
            companies[row["ticker"]] = row["name"]
    profiles = json.loads(profiles_path.read_text(encoding="utf-8"))
    profile = profiles[profile_name]
    from transformers import AutoTokenizer

    embedding_tokenizer = AutoTokenizer.from_pretrained(
        BGE_MODEL,
        revision=BGE_REVISION,
        local_files_only=True,
    )
    # We measure explicitly and reject above BGE_MAX_TOKENS; suppress the
    # transformer's advisory warning while measuring candidate partitions.
    embedding_tokenizer.model_max_length = 1_000_000
    config_hash = sha256_text(
        json.dumps(
            {
                "version": CHUNKER_VERSION,
                "encoding": ENCODING_NAME,
                "profile_name": profile_name,
                "profile": profile,
                "embedding_template": (
                    "v17-bounded-auditor-cross-reference-routing"
                ),
                "embedding_model": BGE_MODEL,
                "embedding_model_revision": BGE_REVISION,
                "embedding_max_tokens": BGE_MAX_TOKENS,
            },
            sort_keys=True,
            separators=(",", ":"),
        )
    )
    staging = output_root.with_name(output_root.name + ".staging")
    if output_root.exists():
        raise FileExistsError(f"refusing existing output root: {output_root}")
    if staging.exists() and not resume:
        raise FileExistsError(
            f"staging root exists; use --resume or choose another output: "
            f"{staging}"
        )
    staging.mkdir(parents=True, exist_ok=resume)
    checkpoints = staging / "section_checkpoints"
    checkpoints.mkdir(exist_ok=True)
    progress_path = staging / "progress.json"
    if resume and progress_path.is_file():
        prior_progress = json.loads(progress_path.read_text(encoding="utf-8"))
        prior_hash = prior_progress.get("chunker_config_sha256")
        if prior_hash != config_hash:
            raise RuntimeError(
                "resume checkpoint configuration mismatch: "
                f"{prior_hash} != {config_hash}"
            )
    encoder = tiktoken.get_encoding(ENCODING_NAME)
    actions = Counter()
    completed_ordinals = {
        int(path.stem)
        for path in checkpoints.glob("*.jsonl")
        if path.stem.isdigit()
    }
    resume_start_count = len(completed_ordinals)
    record_count = 0
    for ordinal in sorted(completed_ordinals):
        checkpoint = checkpoints / f"{ordinal:06d}.jsonl"
        for row in read_jsonl(checkpoint):
            record_count += 1
            actions[str(row["rag_action"])] += 1

    total_sections = len(sections)
    started = time.monotonic()
    last_report = started
    initial = emit_progress(
        resume_start_count,
        total_sections,
        started,
        record_count,
        "resuming" if completed_ordinals else "initializing",
        len(completed_ordinals),
    )
    initial["chunker_version"] = CHUNKER_VERSION
    initial["chunker_config_sha256"] = config_hash
    write_progress(progress_path, initial)

    for ordinal, section in enumerate(sections, start=1):
        if ordinal in completed_ordinals:
            continue
        section_records: list[ChunkRecord] = []
        current_payload = emit_progress(
            ordinal - 1,
            total_sections,
            started,
            record_count,
            str(section["section_id"]),
            resume_start_count,
        )
        current_payload["chunker_version"] = CHUNKER_VERSION
        current_payload["chunker_config_sha256"] = config_hash
        write_progress(progress_path, current_payload)
        if section["quality_status"] != "passed":
            pass
        elif (
            section["rag_action"] != "include"
            and section["canonical_section_code"] not in LATE_TRAILING_SECTIONS
        ):
            pass
        else:
            path = Path(section["output_file"])
            if not path.is_file():
                path = sections_root / "10k" / path.name
            text = path.read_text(encoding="utf-8")
            if sha256_text(text) != section["section_text_sha256"]:
                raise RuntimeError(f"section hash mismatch: {path}")
            units = semantic_units(text)
            boundary_index = SemanticBoundaryIndex.build(units)
            groups = group_units(
                text,
                units,
                encoder,
                int(profile["target_tokens"]),
                int(profile["hard_max_tokens"]),
                int(profile["overlap_tokens"]),
            )
            bounded_groups = []
            current_region = (
                "non_rag"
                if section["canonical_section_code"] in {"Item_16", "Signatures"}
                else ""
            )
            for group in groups:
                group_start = group[0].start
                group_end = group[-1].end
                group_text = text[group_start:group_end]
                group_kinds = {unit.kind for unit in group}
                group_type = (
                    next(iter(group_kinds))
                    if len(group_kinds) == 1
                    else "mixed_approved"
                )
                group_subsection = group[0].subsection
                group_table_context = (
                    table_embedding_context(text, group_start)
                    if group_type in {"table", "table_continuation"}
                    else ""
                )
                (
                    current_region,
                    group_rag_section,
                    group_policy_flags,
                    group_forced_action,
                ) = late_region_policy(
                    current_region,
                    group_type,
                    group_text,
                    group_subsection,
                    section["canonical_section_code"],
                    group_table_context,
                )
                for bounded in enforce_embedding_limit(
                    text,
                    group,
                    embedding_tokenizer,
                    BGE_MAX_TOKENS,
                    companies[section["ticker"]],
                    section["ticker"],
                    int(section["coverage_year"]),
                    section["canonical_section_code"],
                    boundary_index,
                    group_rag_section,
                ):
                    bounded_groups.append(
                        (
                            bounded,
                            (
                                group_rag_section,
                                group_policy_flags,
                                group_forced_action,
                            ),
                        )
                    )
            for index, (group, policy_result) in enumerate(bounded_groups):
                start = group[0].start
                end = group[-1].end
                chunk_text = text[start:end]
                count = token_count(encoder, chunk_text)
                if count > int(profile["hard_max_tokens"]):
                    raise RuntimeError(
                        f"hard maximum exceeded: {section['section_id']}"
                    )
                kinds = {unit.kind for unit in group}
                chunk_type = (
                    next(iter(kinds))
                    if len(kinds) == 1
                    else "mixed_approved"
                )
                subsection = group[0].subsection
                table_context = (
                    table_embedding_context(text, start)
                    if chunk_type in {"table", "table_continuation"}
                    else ""
                )
                rag_section = policy_result[0]
                prior_context, next_context = boundary_contexts(
                    text,
                    start,
                    end,
                    chunk_type,
                    boundary_index,
                )
                embedded = embedding_text(
                    companies[section["ticker"]],
                    section["ticker"],
                    int(section["coverage_year"]),
                    rag_section,
                    subsection,
                    chunk_type,
                    chunk_text,
                    table_context,
                    section["canonical_section_code"],
                    prior_context,
                    next_context,
                )
                embedded_count = embedding_token_count(
                    embedding_tokenizer,
                    embedded,
                )
                if embedded_count > BGE_MAX_TOKENS:
                    raise RuntimeError(
                        f"BGE maximum exceeded: {section['section_id']}"
                    )
                status, flags, action = quality(
                    chunk_type,
                    chunk_text,
                    count,
                    int(profile["minimum_tokens"]),
                    subsection,
                    section["canonical_section_code"],
                    table_context,
                    policy_result,
                )
                actions[action] += 1
                chunk_id = f"{section['section_id']}-chunk-{index:04d}"
                section_records.append(
                    ChunkRecord(
                        chunk_id=chunk_id,
                        section_id=section["section_id"],
                        company_id=section["company_id"],
                        ticker=section["ticker"],
                        coverage_year=int(section["coverage_year"]),
                        accession_number=section["accession_number"],
                        canonical_section_code=section[
                            "canonical_section_code"
                        ],
                        rag_section_code=rag_section,
                        subsection_heading=subsection,
                        chunk_type=chunk_type,
                        chunk_index=index,
                        source_start_char=(
                            int(section["source_start_char"]) + start
                        ),
                        source_end_char=(
                            int(section["source_start_char"]) + end
                        ),
                        section_start_char=start,
                        section_end_char=end,
                        chunk_text=chunk_text,
                        embedding_text=embedded,
                        token_count=count,
                        embedding_token_count=embedded_count,
                        embedding_model=BGE_MODEL,
                        embedding_model_revision=BGE_REVISION,
                        embedding_max_tokens=BGE_MAX_TOKENS,
                        chunk_text_sha256=sha256_text(chunk_text),
                        embedding_text_sha256=sha256_text(embedded),
                        chunker_version=CHUNKER_VERSION,
                        chunker_config_sha256=config_hash,
                        boundary_start_type=group[0].kind,
                        boundary_end_type=group[-1].kind,
                        semantic_topic_count=1,
                        continuation_from_previous=bool(prior_context),
                        continues_to_next=bool(next_context),
                        quality_status=status,
                        quality_flags=tuple(sorted(flags)),
                        rag_action=action,
                    )
                )

        checkpoint = checkpoints / f"{ordinal:06d}.jsonl"
        checkpoint_temporary = checkpoint.with_suffix(".jsonl.tmp")
        with checkpoint_temporary.open("w", encoding="utf-8") as handle:
            for record in section_records:
                handle.write(json.dumps(asdict(record), sort_keys=True) + "\n")
            handle.flush()
        checkpoint_temporary.replace(checkpoint)
        completed_ordinals.add(ordinal)
        record_count += len(section_records)

        now = time.monotonic()
        if (
            ordinal % max(1, progress_every) == 0
            or now - last_report >= 5
            or ordinal == total_sections
        ):
            payload = emit_progress(
                ordinal,
                total_sections,
                started,
                record_count,
                str(section["section_id"]),
                resume_start_count,
            )
            payload["chunker_version"] = CHUNKER_VERSION
            payload["chunker_config_sha256"] = config_hash
            write_progress(progress_path, payload)
            last_report = now

    with (staging / "chunks.jsonl").open("w", encoding="utf-8") as handle:
        for ordinal in range(1, total_sections + 1):
            checkpoint = checkpoints / f"{ordinal:06d}.jsonl"
            if not checkpoint.is_file():
                raise RuntimeError(f"missing section checkpoint: {checkpoint}")
            with checkpoint.open(encoding="utf-8") as source:
                for line in source:
                    handle.write(line)
    staging.replace(output_root)
    return {
        "profile": profile_name,
        "chunks": record_count,
        "rag_action_counts": dict(sorted(actions.items())),
        "output_root": str(output_root),
    }


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--sections-root", type=Path, required=True)
    parser.add_argument("--companies", type=Path, required=True)
    parser.add_argument("--profiles", type=Path, required=True)
    parser.add_argument("--profile", choices=("A", "B"), required=True)
    parser.add_argument("--output-root", type=Path, required=True)
    parser.add_argument(
        "--resume",
        action="store_true",
        help="Resume from atomic per-section checkpoints in the staging root.",
    )
    parser.add_argument(
        "--progress-every",
        type=int,
        default=1,
        help="Emit a flushed progress line every N completed sections.",
    )
    args = parser.parse_args()
    print(
        json.dumps(
            run(
                args.sections_root,
                args.companies,
                args.profiles,
                args.profile,
                args.output_root,
                args.resume,
                args.progress_every,
            ),
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
