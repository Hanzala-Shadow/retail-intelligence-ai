"""Strict detection for compact tables of contents without dot leaders."""

from __future__ import annotations

import re
from dataclasses import dataclass


COMPACT_TOC_MIN_ENTRIES = 4
COMPACT_TOC_MAX_TITLE_WORDS = 12
COMPACT_TOC_MAX_TITLE_CHARS = 96
_TOC_CONTEXT_RE = re.compile(r"^(?:table\s+of\s+contents|contents)$", re.IGNORECASE)
_PAGE_NUMBER_RE = re.compile(r"(?<![\d%])(?P<page>\d{1,3})(?![\d%])")
_TITLE_WORD_RE = re.compile(r"[A-Za-z][A-Za-z&'\u2019/-]*")
_LOWERCASE_TITLE_WORDS = {
    "a",
    "an",
    "and",
    "at",
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


@dataclass(frozen=True)
class CompactTocEntry:
    """One title and destination page found inside a compact TOC cluster."""

    line_index: int
    title: str
    page_number: int


def _entry_from_line(
    line: str,
    line_index: int,
    max_page_number: int | None,
) -> CompactTocEntry | None:
    """Read a title/page prefix, including lines collided with another column."""
    stripped = line.strip()
    if not stripped or len(stripped) > 260:
        return None

    for match in _PAGE_NUMBER_RE.finditer(stripped):
        page_number = int(match.group("page"))
        if page_number < 1 or (
            max_page_number is not None and page_number > max_page_number
        ):
            continue

        title = stripped[: match.start()].rstrip(" .\t")
        words = _TITLE_WORD_RE.findall(title)
        if not (
            1 <= len(words) <= COMPACT_TOC_MAX_TITLE_WORDS
            and 2 <= len(title) <= COMPACT_TOC_MAX_TITLE_CHARS
        ):
            continue
        if not title[0].isalpha() or "|" in title:
            continue
        if not next((char for char in title if char.isalpha()), "").isupper():
            continue
        if re.search(r"[.!?;]", title):
            continue

        significant = [word for word in words if word.casefold() not in _LOWERCASE_TITLE_WORDS]
        title_case = sum(word[0].isupper() for word in significant)
        if significant and title_case / len(significant) < 0.75:
            continue
        return CompactTocEntry(line_index, re.sub(r"\s+", " ", title), page_number)
    return None


def detect_compact_toc_entries(
    lines: list[str],
    *,
    max_page_number: int | None = None,
) -> tuple[CompactTocEntry, ...]:
    """Return entries only for a strict, ordered, context-backed TOC cluster."""
    has_context = any(_TOC_CONTEXT_RE.fullmatch(line.strip()) for line in lines)
    if not has_context:
        return ()

    entries = tuple(
        entry
        for line_index, line in enumerate(lines)
        if (entry := _entry_from_line(line, line_index, max_page_number)) is not None
    )
    if len(entries) < COMPACT_TOC_MIN_ENTRIES:
        return ()
    page_numbers = [entry.page_number for entry in entries]
    if len(set(page_numbers)) < COMPACT_TOC_MIN_ENTRIES:
        return ()
    if any(left >= right for left, right in zip(page_numbers, page_numbers[1:])):
        return ()
    return entries


def has_compact_toc_cluster(
    text: str,
    *,
    max_page_number: int | None = None,
) -> bool:
    """Return whether text contains a strict compact-TOC cluster."""
    return bool(
        detect_compact_toc_entries(
            text.splitlines(),
            max_page_number=max_page_number,
        )
    )
