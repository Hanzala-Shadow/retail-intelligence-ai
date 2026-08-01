"""Decide whether a page's *role* is navigation rather than content.

The page gate used to answer this question with the reading-order verdict
``navigation_contents_layout``.  That verdict answers a different question --
"can a defensible prose order be established for this page?" -- and measurement
against the AI gold set showed the two questions do not coincide:

* three of the six gold navigation pages never reach that verdict at all (they
  are certified by verified table extraction or by the plain no-signal pass), so
  they were indexed;
* two genuine content pages do reach it, so keying exclusion off that verdict
  alone would have dropped real content.

Role detection therefore lives here, reads only the parsed page text, and is
applied by the gate as an exclusion that pre-empts every ``auto_pass`` path.

Two page shapes are recognised:

``navigation_contents_page``
    A table of contents: a contents title plus a body dominated by entry lines
    that carry a page number.

``navigation_standards_index_page``
    A GRI/SASB/TCFD-style cross-reference index: a standards-index title plus a
    table body that mostly points at locations elsewhere in the report.  These
    carry no page numbers, so the contents rule cannot see them.

Both rules require a title.  On the 40 development pages the title signal alone
separated all 4 navigation pages from all 36 content pages with no overlap, and
the structural thresholds below act as a corroborating guard rather than as the
discriminator.  A titleless table of contents is therefore a known recall gap;
see the module tests for the pages that pin current behaviour.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

NAVIGATION_CONTENTS = "navigation_contents_page"
NAVIGATION_STANDARDS_INDEX = "navigation_standards_index_page"
NAVIGATION_LINK_HUB = "navigation_link_hub_page"

#: Gate decision emitted for a page whose role is navigation. Defined here so
#: that the audit gate and the gold-set evaluator name the same verdict.
AUTO_EXCLUDE_NAVIGATION = "auto_exclude_navigation"

# Scanned against the first lines of a page, anchored so that a passing mention
# of "contents" inside a sentence cannot match.
CONTENTS_TITLE = re.compile(
    r"^(table\s+of\s+contents|contents|in\s+this\s+report)$",
    re.IGNORECASE,
)
STANDARDS_INDEX_TITLE = re.compile(
    r"^(?:\S+\s+){0,3}(?:gri|sasb|tcfd|cdp|ungc|sdg)\b[^\n]{0,40}?\b(?:index|content\s+index)$",
    re.IGNORECASE,
)
# Column repair frequently splits a two-line "TABLE OF / CONTENTS" masthead and
# pushes the halves apart, so the halves are also matched anywhere on the page.
SPLIT_TITLE_HEAD = re.compile(r"^table\s+of(?:\s|$)", re.IGNORECASE)
SPLIT_TITLE_TAIL = re.compile(r"^contents(?:\s|$)", re.IGNORECASE)

ENTRY_PAGE_NUMBER_FIRST = re.compile(r"^\d{1,3}\s+\S")
ENTRY_PAGE_NUMBER_LAST = re.compile(r"\S\s+\d{1,3}(?:\s*[-–]\s*\d{1,3})?$")
DOT_LEADER = re.compile(r"\.{3,}")
STANDARD_REFERENCE = re.compile(r"\b(?:gri|sasb|tcfd|cdp)\b", re.IGNORECASE)
GO_TO_LINK = re.compile(r"\bgo\s+to\b", re.IGNORECASE)
PAGE_POINTER = re.compile(r"\bpage\s+\d{1,3}\b", re.IGNORECASE)

TITLE_SCAN_LINES = 6
MIN_CONTENTS_ENTRIES = 5
MIN_CONTENTS_ENTRY_RATIO = 0.30
MIN_INDEX_TABLE_LINE_RATIO = 0.50
MIN_INDEX_STANDARD_REFERENCES = 3


@dataclass(frozen=True)
class PageRoleResult:
    """Why a page was, or was not, judged to be navigation."""

    is_navigation: bool
    reason: str
    detail: str


def _lines(text: str) -> list[str]:
    return [line.strip() for line in (text or "").splitlines() if line.strip()]


def _has_contents_title(lines: list[str]) -> bool:
    if any(CONTENTS_TITLE.match(line) for line in lines[:TITLE_SCAN_LINES]):
        return True
    return any(SPLIT_TITLE_HEAD.match(line) for line in lines) and any(
        SPLIT_TITLE_TAIL.match(line) for line in lines
    )


def _has_standards_index_title(lines: list[str]) -> bool:
    return any(STANDARDS_INDEX_TITLE.match(line) for line in lines[:TITLE_SCAN_LINES])


def _entry_line_count(body: list[str]) -> int:
    return sum(
        1
        for line in body
        if ENTRY_PAGE_NUMBER_FIRST.match(line)
        or ENTRY_PAGE_NUMBER_LAST.search(line)
        or DOT_LEADER.search(line)
    )


def classify_page_role(text: str) -> PageRoleResult:
    """Return the navigation verdict for one page of parsed text."""

    lines = _lines(text)
    if not lines:
        return PageRoleResult(False, "", "empty_page_text")

    table_lines = [line for line in lines if line.startswith("|")]
    body = [line for line in lines if not line.startswith("|")]
    entries = _entry_line_count(body)
    entry_ratio = entries / len(body) if body else 0.0
    table_ratio = len(table_lines) / len(lines)
    standard_references = sum(1 for line in lines if STANDARD_REFERENCE.search(line))
    go_to_links = len(GO_TO_LINK.findall(text or ""))
    page_pointers = len(PAGE_POINTER.findall(text or ""))

    if (
        _has_contents_title(lines)
        and entries >= MIN_CONTENTS_ENTRIES
        and entry_ratio >= MIN_CONTENTS_ENTRY_RATIO
    ):
        return PageRoleResult(
            True,
            NAVIGATION_CONTENTS,
            f"entries={entries}; entry_ratio={entry_ratio:.4f}; body_lines={len(body)}",
        )

    if (
        _has_standards_index_title(lines)
        and table_ratio >= MIN_INDEX_TABLE_LINE_RATIO
        and standard_references >= MIN_INDEX_STANDARD_REFERENCES
    ):
        return PageRoleResult(
            True,
            NAVIGATION_STANDARDS_INDEX,
            f"table_ratio={table_ratio:.4f}; standard_references={standard_references}; "
            f"lines={len(lines)}",
        )

    if go_to_links >= 3 and page_pointers >= 3:
        return PageRoleResult(
            True,
            NAVIGATION_LINK_HUB,
            f"go_to_links={go_to_links}; page_pointers={page_pointers}",
        )

    return PageRoleResult(
        False,
        "",
        f"entries={entries}; entry_ratio={entry_ratio:.4f}; table_ratio={table_ratio:.4f}; "
        f"standard_references={standard_references}; go_to_links={go_to_links}; "
        f"page_pointers={page_pointers}",
    )


def apply_navigation_override(
    decision: str,
    reason: str,
    text: str,
) -> tuple[str, str, PageRoleResult]:
    """Fold the navigation role into an already-resolved layout decision.

    Role beats layout, and it beats a hold too. This overrides *every* other
    verdict, including ``auto_hold``.

    An earlier version left an existing hold alone, on the reasoning that hold
    and exclusion both keep the page out of the index. That was wrong: hold is
    not a terminal state. ``scripts/run_esg_vlm.py`` selects held pages for
    vision re-extraction, so a held navigation page would be re-read by a VLM
    and could re-enter the corpus through that path -- and would burn VLM spend
    on a page with no retrieval value either way. Exclusion is terminal; hold is
    a queue.

    The audit gate and the gold-set evaluator both call this, so the benchmark
    measures the rule the pipeline actually applies.
    """

    role = classify_page_role(text)
    if role.is_navigation:
        return AUTO_EXCLUDE_NAVIGATION, f"{role.reason}: {role.detail}", role
    return decision, reason, role
