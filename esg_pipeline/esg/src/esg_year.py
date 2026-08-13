"""Canonical year extraction for ESG pdf stems and filenames.

One rule, one place. Every consumer that needs "the year of this document"
imports from here so the corpus cannot end up with two disagreeing answers for
the same stem.

The rule
--------
A stem may carry more than one 4-digit year token: this corpus uses both
orderings, ACI-...-2021-2022 ascending and GES-GUESS-2022-2021 descending, so
any positional rule ("first token", "last token") assigns different semantics
to the two forms. The canonical ``report_year`` is therefore ``max(years)`` --
the latest year the document covers, which is order-independent. The full span
is available via :func:`extract_report_year` so a question about an earlier
covered year can still be matched.

A stem with no parseable in-range year yields ``None``/``"unresolved"`` rather
than a guess. DLTR-...-202E is the known real case: "202E" is a typo, not a
year, and must not become 2020.
"""

from __future__ import annotations

import re

YEAR_MIN, YEAR_MAX = 1990, 2030

# Trailing decorations seen in real stems: "-Report", ".pdf", "(Italian)",
# "(Climate Index)". Stripped before year extraction so they cannot be mistaken
# for a year token.
_STEM_DECORATION = re.compile(r"(\([^)]*\)|\.pdf|-Report)\s*$", re.IGNORECASE)

# Lookaround guards keep "12023" and "20231" from yielding a year; a bare
# \b(20\d{2})\b does not, because \b sits between "1" and "2" in "12023".
_YEAR_TOKEN = re.compile(r"(?<!\d)((?:19|20)\d{2})(?!\d)")


def strip_stem_decorations(stem: str) -> str:
    """Remove trailing "(...)", ".pdf" and "-Report" decorations from a stem."""
    for _ in range(4):
        stripped = _STEM_DECORATION.sub("", stem).strip()
        if stripped == stem:
            break
        stem = stripped
    return stem


def years_in_stem(stem: str | None) -> list[int]:
    """Every distinct in-range year token in ``stem``, ascending."""
    cleaned = strip_stem_decorations(stem or "")
    return sorted({int(m) for m in _YEAR_TOKEN.findall(cleaned)
                   if YEAR_MIN <= int(m) <= YEAR_MAX})


def extract_report_year(pdf_stem: str | None) -> tuple[int | None, str, str]:
    """Return ``(year_or_None, status, span)`` for a pdf stem.

    ``year`` is ``max(years)``; ``status`` is one of "parsed",
    "multi_year_range", "unresolved"; ``span`` is "2022" or "2021-2022".
    """
    years = years_in_stem(pdf_stem)
    if not years:
        return None, "unresolved", ""
    span = str(years[0]) if len(years) == 1 else f"{years[0]}-{years[-1]}"
    status = "parsed" if len(years) == 1 else "multi_year_range"
    return years[-1], status, span


def report_year(value: object) -> int | None:
    """The canonical single year for ``value``, or ``None`` if unparseable.

    Convenience wrapper for callers that only want the year -- pandas ``.map``,
    manifest columns, year filters. Accepts any object; non-strings are coerced.
    """
    return extract_report_year("" if value is None else str(value))[0]
