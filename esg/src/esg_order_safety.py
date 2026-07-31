"""Deterministic retrieval-safety gate for a candidate page reading order.

The layout audit can prove a reading order two ways today: the parsed text
already matches an independent reconstruction, or the page has no multi-column
signal at all. Everything else holds. That is safe but expensive -- v8 holds
1,607 pages as ``auto_hold_structural_multi_column``.

This module adds a third kind of proof. Instead of asking "is this page hard?",
which the measured signals cannot answer (see
``reports/esg_reading_order_diagnosis_2026-07-31/``), it asks a question that
the page's own geometry *can* answer: **does this specific candidate text
respect the layout it came from?**

The gate never looks at gold. It aligns the candidate token stream back onto
the source word coordinates and then checks structural properties of that
alignment. No vision, no model call, no network.

Checks, all of which must hold:

``preserved``            every body segment survives, numbers exactly
``no_invention``         no token appears that is not on the page
``noise_removed``        rotated and repeated-edge navigation text is gone
``column_monotonic``     within a region, lines stay top-to-bottom
``regions_blocked``      panels and cards are not interleaved
``flow_broken``          nothing is inserted into a sentence or a lead-in list
``headings_lead``        a full-width heading precedes what sits below it
``heading_attached``     a heading is followed by the body it introduces
``not_grid``             table and grid pages are refused outright
``table_rows_unproven``  an unruled table whose lines are not whole records
``table_parallel``       two side-by-side tables read across the gutter
``blocks_ok``            the result splits into usable paragraph blocks

A page is recoverable only when a candidate passes every one of them.

The structural checks used to be keyed to columns found by
``esg_reading_order._cluster_starts``. That detector is built for full-height
prose columns and needs a column to carry six lines and 55 words, so on a page
of cards, map callouts or short table cells it finds nothing, every segment
lands in one bucket, and the interleaving, monotonicity and heading checks
cannot fail at all. That is how all twelve pages of the first Terra sample were
passed, six of them wrongly. The structure now comes from
``esg_page_geometry``, which measures regions and tables from evidence that
survives short content. See ``reports/esg_recovery_gate_diagnosis_2026-07-31.md``.
"""

from __future__ import annotations

import re
from collections import Counter
from dataclasses import dataclass, field
from statistics import median

from esg_page_geometry import (
    continues,
    persistent_regions,
    table_blocks,
    visual_lines,
)
from esg_reading_order import (
    HEADER_FOOTER_BAND_SHARE,
    _line_groups,
    _line_segments,
    _linear_words,
    _number,
    _usable_words,
)

TOKEN_RE = re.compile(r"[a-z0-9]+(?:[./:&'+-][a-z0-9]+)*|[%$€£]", re.IGNORECASE)
NUMBER_RE = re.compile(r"(?<![a-z0-9])[+-]?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?%?", re.IGNORECASE)

#: A segment this wide, relative to the body's content width, spans the page and
#: therefore orders everything below it.
FULL_WIDTH_SHARE = 0.60
#: Font size multiple over body median that marks a heading.
HEADING_SIZE_MULTIPLE = 1.15
#: A recovered page must carry at least this much real text to be worth indexing.
MIN_BLOCK_TOKENS = 25
#: Share of an unruled table's lines that must be complete rows before the
#: table's row relationships count as proven. Below it a visual line is no
#: longer a record -- cells wrap over several lines -- so reading across rows
#: cannot be shown to keep any label with its own values, and the page is held.
MIN_FULL_ROW_SHARE = 0.60
#: A row key repeated further right on this many of a table's lines, with this
#: many distinct keys, means two tables were printed side by side and read
#: across the gutter as if they were one.
MIN_PARALLEL_LABEL_LINES = 2
MIN_PARALLEL_LABELS = 2
# A wide report image can carry chart labels and values that never enter the
# native word stream. Without vision, a candidate made only from native text
# cannot prove that visible evidence survived.
CONTENT_IMAGE_MIN_WIDTH_SHARE = 0.75
CONTENT_IMAGE_MIN_HEIGHT_SHARE = 0.30
CONTENT_IMAGE_MIN_ASPECT_RATIO = 2.35
# A page with more drawn objects than native words is often a table whose
# visible labels and values are painted as shapes or an inaccessible layer.
# The native word stream cannot prove completeness on such a page.
DENSE_VISUAL_GRID_MIN_OBJECTS = 100
DENSE_VISUAL_GRID_MIN_OBJECT_WORD_RATIO = 1.0
# A prose-heavy four-or-more-column block is a set of cards or panels, not a numeric
# table. Reading it row by row can detach each panel heading from its body even
# when the row geometry itself is regular.
PANEL_GRID_MIN_ANCHORS = 4
PANEL_GRID_MAX_NUMBER_SEGMENT_SHARE = 0.40
# A full-page raster with many mixed-column lines means the native text layer
# cannot prove where the words sit in the visible page. This catches OCR/text
# overlays whose coordinates collapse a real table into one false column.
RASTER_ORDER_MIN_MIXED_LINES = 25
VISUAL_CONTENT_MIN_OBJECTS = 15
VISUAL_CONTENT_MIN_OBJECT_WORD_RATIO = 0.18
CHART_ORDER_MIN_MIXED_LINES = 30
CHART_ORDER_MIN_NUMBER_SEGMENT_SHARE = 0.50
FULL_PAGE_IMAGE_SHARE = 0.80
# Tolerances. Zero-tolerance on the structural checks passes almost nothing:
# splitting a page into line segments produces a little unavoidable noise, so a
# single stray segment would condemn an otherwise clean read. These are shares
# of the page's segment count, so a long page is not penalised for its length.
#
# Calibrated on the 36 development content pages only. That is a small sample
# with few non-table pages in it, so treat these two numbers as the weakest part
# of this module and re-check them before trusting a corpus-wide result.
MAX_REVISIT_SHARE = 0.02
MAX_INVERSION_SHARE = 0.02
#: Reuses the benchmark's own FAIL_HEADING_ATTACHMENT rather than inventing a
#: second, different definition of an acceptable heading.
MIN_HEADING_ATTACHMENT = 0.70
#: Two passing candidates whose orders agree at least this closely are treated as
#: the same answer; below it the page is ambiguous and stays held.
ORDER_AGREEMENT_MIN = 0.98


@dataclass
class Segment:
    """One gutter-separated piece of one visual line."""

    index: int
    top: float
    bottom: float
    x0: float
    x1: float
    size: float
    tokens: tuple[str, ...]
    #: Raw joined word text. Numbers must be read from this, not from ``tokens``:
    #: tokenising first splits "1,234" into "1" and "234" and then every
    #: thousands-separated figure looks both lost and newly invented.
    raw_text: str = ""
    #: Persistent spatial region: the panel, card or table column this segment
    #: belongs to. Assigned by ``esg_page_geometry.persistent_regions``.
    region: int = -1
    #: True when this segment sits inside a detected unruled table, where
    #: reading across a row is the correct order rather than an interleave.
    tabular: bool = False
    full_width: bool = False


@dataclass
class OrderSafetyResult:
    passed: bool
    failures: tuple[str, ...]
    metrics: dict[str, float | int] = field(default_factory=dict)
    #: Consumed segment indices in candidate order; empty when alignment failed.
    order: tuple[int, ...] = ()

    @property
    def reason(self) -> str:
        return "order_safe" if self.passed else ";".join(self.failures)


def has_full_page_image(
    images: list[dict], page_width: float, page_height: float
) -> bool:
    """Whether a raster covers most of the page in both dimensions."""

    if page_width <= 0 or page_height <= 0:
        return False
    for item in images or []:
        width = float(item.get("width") or 0)
        height = float(item.get("height") or 0)
        if (
            width >= page_width * FULL_PAGE_IMAGE_SHARE
            and height >= page_height * FULL_PAGE_IMAGE_SHARE
        ):
            return True
    return False


def has_wide_content_image(
    images: list[dict], page_width: float, page_height: float
) -> bool:
    """Whether a wide raster may contain chart labels missing from native text."""

    if page_width <= 0 or page_height <= 0:
        return False
    for item in images or []:
        width = float(item.get("width") or 0)
        height = float(item.get("height") or 0)
        if height <= 0:
            continue
        if (
            width >= page_width * CONTENT_IMAGE_MIN_WIDTH_SHARE
            and height >= page_height * CONTENT_IMAGE_MIN_HEIGHT_SHARE
            and width / height >= CONTENT_IMAGE_MIN_ASPECT_RATIO
        ):
            return True
    return False


def tokens_of(text: str) -> list[str]:
    return [t.lower() for t in TOKEN_RE.findall(text or "")]


def numbers_of(text: str) -> Counter[str]:
    return Counter(m.group(0).replace(",", "") for m in NUMBER_RE.finditer(text or ""))


def _word_tokens(word: dict) -> tuple[str, ...]:
    return tuple(tokens_of(str(word.get("text", ""))))


def _rotated_words(words: list[dict]) -> list[dict]:
    """Words the linear reader drops: vertical spine text and rotated labels."""
    linear = {id(w) for w in _linear_words(words)}
    return [w for w in _usable_words(words) if id(w) not in linear]


def build_segments(
    words: list[dict], page_width: float, page_height: float
) -> tuple[list[Segment], list[dict], list]:
    """Return body segments, the header/footer words they exclude, and tables."""

    linear = _linear_words(words)
    if not linear or page_height <= 0:
        return [], [], []

    band = page_height * HEADER_FOOTER_BAND_SHARE
    body_words, margin_words = [], []
    for word in linear:
        top = _number(word, "top")
        bottom = _number(word, "bottom", top)
        if top < band or bottom > page_height - band:
            margin_words.append(word)
        else:
            body_words.append(word)

    segments: list[Segment] = []
    for line in _line_groups(body_words):
        for piece in _line_segments(line, page_width):
            toks: list[str] = []
            for word in piece:
                toks.extend(_word_tokens(word))
            if not toks:
                continue
            segments.append(
                Segment(
                    index=len(segments),
                    top=min(_number(w, "top") for w in piece),
                    bottom=max(_number(w, "bottom", _number(w, "top")) for w in piece),
                    x0=min(_number(w, "x0") for w in piece),
                    x1=max(_number(w, "x1") for w in piece),
                    size=median([float(w.get("size", 0) or 0) for w in piece]) or 0.0,
                    tokens=tuple(toks),
                    raw_text=" ".join(str(w.get("text", "")) for w in piece),
                )
            )

    return segments, margin_words, _assign_structure(segments, page_width)


def _assign_structure(segments: list[Segment], page_width: float) -> list:
    """Label each segment with its region, table membership and width status.

    Regions are the page's own persistent spatial flows -- a card, a map
    callout, a paragraph, a table column -- rather than buckets derived from a
    prose-column detector that short content cannot satisfy. Table membership
    is separate because inside a table reading across a row is *correct*, so a
    table's columns must not be judged as interleaved panels.
    """

    if not segments:
        return []
    content_left = min(s.x0 for s in segments)
    content_right = max(s.x1 for s in segments)
    content_width = max(content_right - content_left, 1.0)
    for segment in segments:
        segment.full_width = (segment.x1 - segment.x0) >= FULL_WIDTH_SHARE * content_width

    lines = visual_lines(segments)
    for region in persistent_regions(segments, page_width, lines):
        for index in region.members:
            segments[index].region = region.index

    blocks = table_blocks(segments, page_width, lines)
    for block in blocks:
        for line in block.lines:
            for index in lines[line]:
                segments[index].tabular = True
    return blocks


def _align(candidate_tokens: list[str], segments: list[Segment]) -> tuple[list[int], list[str]]:
    """Greedily consume source segments along the candidate token stream.

    Returns the consumed segment indices in candidate order, plus the candidate
    tokens that matched no segment. Repeated identical segments are consumed in
    top-to-bottom order so a page with duplicate lines aligns stably.
    """

    by_first: dict[str, list[int]] = {}
    for segment in sorted(segments, key=lambda s: (s.top, s.x0)):
        if segment.tokens:
            by_first.setdefault(segment.tokens[0], []).append(segment.index)
    consumed: set[int] = set()
    order: list[int] = []
    extra: list[str] = []
    lookup = {s.index: s for s in segments}

    position = 0
    while position < len(candidate_tokens):
        token = candidate_tokens[position]
        best_index, best_length = None, 0
        for index in by_first.get(token, ()):
            if index in consumed:
                continue
            seg_tokens = lookup[index].tokens
            length = len(seg_tokens)
            if (
                length > best_length
                and candidate_tokens[position : position + length] == list(seg_tokens)
            ):
                best_index, best_length = index, length
        if best_index is None:
            extra.append(token)
            position += 1
            continue
        consumed.add(best_index)
        order.append(best_index)
        position += best_length

    return order, extra


def evaluate_order_safety(
    words: list[dict],
    page_width: float,
    page_height: float,
    candidate_text: str,
    *,
    table_like: bool = False,
    visual_object_count: int = 0,
    mixed_column_lines: int = 0,
    full_page_image: bool = False,
    wide_content_image: bool = False,
) -> OrderSafetyResult:
    """Decide whether ``candidate_text`` is a retrieval-safe reading of the page.

    ``table_like`` refuses table and grid pages outright. The caller derives it
    from the parser's own table handling -- extracted markdown rows or a table
    candidate on the page map -- not from the coarse visual grid-risk signal,
    which also fires on decorative card layouts that this rule should recover.
    """

    failures: list[str] = []
    metrics: dict[str, float | int] = {}

    if table_like:
        return OrderSafetyResult(False, ("not_grid",), {"table_like": 1})

    segments, margin_words, blocks = build_segments(words, page_width, page_height)
    if not segments:
        return OrderSafetyResult(False, ("no_body_segments",), {})

    candidate_tokens = tokens_of(candidate_text)
    metrics["source_segments"] = len(segments)
    metrics["candidate_tokens"] = len(candidate_tokens)
    metrics["visual_object_count"] = int(visual_object_count)
    metrics["mixed_column_lines"] = int(mixed_column_lines)
    metrics["full_page_image"] = int(bool(full_page_image))
    metrics["wide_content_image"] = int(bool(wide_content_image))
    if not candidate_tokens:
        return OrderSafetyResult(False, ("no_candidate_text",), metrics)

    object_word_ratio = visual_object_count / max(len(words), 1)
    metrics["visual_object_word_ratio"] = round(object_word_ratio, 4)
    if (
        visual_object_count >= DENSE_VISUAL_GRID_MIN_OBJECTS
        and object_word_ratio > DENSE_VISUAL_GRID_MIN_OBJECT_WORD_RATIO
    ):
        failures.append(
            f"visual_grid_unread(objects={visual_object_count},ratio={object_word_ratio:.2f})"
        )

    if wide_content_image:
        failures.append("wide_image_text_unproven")
    if full_page_image and mixed_column_lines >= RASTER_ORDER_MIN_MIXED_LINES:
        failures.append(
            f"raster_order_unproven(mixed_lines={mixed_column_lines})"
        )

    order, extra = _align(candidate_tokens, segments)
    consumed = set(order)
    missing = [s for s in segments if s.index not in consumed]
    metrics["segments_consumed"] = len(consumed)
    metrics["segments_missing"] = len(missing)
    metrics["tokens_unmatched"] = len(extra)

    # 1. preservation -- every body segment survives, numbers exactly.
    if missing:
        failures.append(f"preserved(missing_segments={len(missing)})")
    source_numbers = numbers_of(" ".join(s.raw_text for s in segments))
    candidate_numbers = numbers_of(candidate_text)
    lost = source_numbers - candidate_numbers
    if lost:
        failures.append(f"preserved(lost_numbers={sum(lost.values())})")
    metrics["source_numbers"] = sum(source_numbers.values())

    # 2. no invention -- unmatched tokens may only be margin or rotated text.
    tolerated: Counter[str] = Counter()
    for word in margin_words:
        tolerated.update(_word_tokens(word))
    invented = Counter(extra) - tolerated
    metrics["tokens_invented"] = sum(invented.values())
    if invented:
        failures.append(f"no_invention(invented={sum(invented.values())})")
    margin_numbers = numbers_of(" ".join(str(w.get("text", "")) for w in margin_words))
    gained = candidate_numbers - source_numbers - margin_numbers
    if gained:
        failures.append(f"no_invention(new_numbers={sum(gained.values())})")

    # 3. noise removed -- rotated spine text must not be in the output.
    rotated = Counter()
    for word in _rotated_words(words):
        rotated.update(_word_tokens(word))
    if rotated:
        leaked = sum(min(count, Counter(candidate_tokens)[tok]) for tok, count in rotated.items())
        metrics["rotated_tokens_leaked"] = leaked
        if leaked > len(rotated) * 0.5:
            failures.append(f"noise_removed(rotated_leaked={leaked})")

    lookup = {s.index: s for s in segments}
    ordered = [lookup[i] for i in order]

    # 4. within a region, lines stay top-to-bottom.
    inversions = 0
    seen: dict[int, float] = {}
    for segment in ordered:
        previous = seen.get(segment.region)
        if previous is not None and segment.top < previous - 1.0:
            inversions += 1
        seen[segment.region] = max(previous or 0.0, segment.top)
    metrics["column_inversions"] = inversions
    if inversions > max(1, int(len(segments) * MAX_INVERSION_SHARE)):
        failures.append(f"column_monotonic(inversions={inversions}/{len(segments)})")

    # 5. panels and cards are read in blocks, not interleaved. Segments inside
    # a detected table are exempt: a table's columns are revisited once per
    # row *by design*, and counting that as interleaving would hold every
    # table on the corpus. What proves a table safe instead is check 9.
    flow = [s for s in ordered if not s.tabular]
    runs: list[int] = []
    for segment in flow:
        if not runs or runs[-1] != segment.region:
            runs.append(segment.region)
    revisits = len(runs) - len(set(runs))
    metrics["flow_segments"] = len(flow)
    metrics["block_revisits"] = revisits
    if revisits > max(1, int(len(segments) * MAX_REVISIT_SHARE)):
        failures.append(f"regions_blocked(revisits={revisits}/{len(segments)})")

    # 5b. nothing is inserted into a sentence or into a lead-in's list. This is
    # the check that sees a heading dropped between "Our" and "work", or a
    # neighbouring panel's line landing between "By Category:" and the
    # categories, neither of which moves any segment out of top-to-bottom order
    # and so neither of which any ordering statistic can notice.
    flow_breaks = _flow_breaks(segments, order)
    metrics["flow_breaks"] = len(flow_breaks)
    if flow_breaks:
        failures.append(f"flow_broken(breaks={len(flow_breaks)})")

    # 6. a full-width heading precedes everything below it.
    position_of = {segment.index: i for i, segment in enumerate(ordered)}
    late_headings = 0
    for segment in segments:
        if not segment.full_width or segment.index not in position_of:
            continue
        for other in segments:
            if other.index not in position_of or other.index == segment.index:
                continue
            if other.top > segment.bottom and position_of[other.index] < position_of[segment.index]:
                late_headings += 1
                break
    metrics["late_full_width"] = late_headings
    if late_headings:
        failures.append(f"headings_lead(late={late_headings})")

    # 7. a heading is followed by the body it introduces.
    #
    # The old form of this check asked whether the heading and the next segment
    # shared a region. Under the collapsed single-bucket structure that was
    # always true, so it reported 1.00 on every page including the six the
    # first Terra sample found unsafe; under real regions it would be almost
    # always false, because a heading being its own spatial element is what
    # makes it a heading. Neither version measured anything. This one asks the
    # question the name claims: is the heading emitted before the body sitting
    # underneath it? A column-major reconstruction that hoists a heading past
    # its own section fails it, and ordinary top-to-bottom text does not.
    body_size = median([s.size for s in segments if s.size]) if any(s.size for s in segments) else 0.0
    detached = 0
    heading_count = 0
    if body_size:
        for segment in segments:
            if segment.size < body_size * HEADING_SIZE_MULTIPLE:
                continue
            if segment.index not in position_of:
                continue
            heading_count += 1
            for other in segments:
                if other.index == segment.index or other.index not in position_of:
                    continue
                overlap = min(segment.x1, other.x1) - max(segment.x0, other.x0)
                if (
                    other.top > segment.bottom
                    and overlap > 0
                    and position_of[other.index] < position_of[segment.index]
                ):
                    detached += 1
                    break
    metrics["headings"] = heading_count
    metrics["headings_detached"] = detached
    attachment = 1.0 - (detached / heading_count) if heading_count else 1.0
    metrics["heading_attachment"] = round(attachment, 4)
    if attachment < MIN_HEADING_ATTACHMENT:
        failures.append(f"heading_attached(attachment={attachment:.2f})")

    # A regular five-column panel layout can look like a table to the anchor
    # detector. Its rows are not records: each heading belongs to the prose
    # below it in the same panel. Refuse the row-major exemption when the block
    # is heading-heavy and has little numeric evidence of a real data table.
    max_table_anchors = max((len(block.left_anchors) for block in blocks), default=0)
    number_segment_share = sum(source_numbers.values()) / max(len(segments), 1)
    metrics["table_max_anchors"] = max_table_anchors
    metrics["number_segment_share"] = round(number_segment_share, 4)
    if (
        blocks
        and max_table_anchors >= PANEL_GRID_MIN_ANCHORS
        and number_segment_share < PANEL_GRID_MAX_NUMBER_SEGMENT_SHARE
    ):
        failures.append(
            "panel_links_unproven("
            f"anchors={max_table_anchors},headings={heading_count},"
            f"number_share={number_segment_share:.2f})"
        )

    # Visible charts, diagrams and brand panels may have no native text at all.
    # When a page has a meaningful amount of vector/image content but no table
    # structure linking it to the native words, completeness cannot be proven.
    if (
        not blocks
        and visual_object_count >= VISUAL_CONTENT_MIN_OBJECTS
        and object_word_ratio >= VISUAL_CONTENT_MIN_OBJECT_WORD_RATIO
    ):
        failures.append(
            "visual_content_unproven("
            f"objects={visual_object_count},ratio={object_word_ratio:.2f})"
        )

    # A data-dense multi-column area that does not form a proven table can emit
    # chart values as one number run and labels as another. The numbers survive,
    # but their meaning does not.
    if (
        not blocks
        and mixed_column_lines >= CHART_ORDER_MIN_MIXED_LINES
        and number_segment_share >= CHART_ORDER_MIN_NUMBER_SEGMENT_SHARE
    ):
        failures.append(
            "chart_links_unproven("
            f"mixed_lines={mixed_column_lines},number_share={number_segment_share:.2f})"
        )

    # 8. every unruled table's lines are whole records.
    worst_full_row = min((b.full_row_share for b in blocks), default=1.0)
    metrics["table_blocks"] = len(blocks)
    metrics["table_full_row_share"] = round(worst_full_row, 4)
    if blocks and worst_full_row < MIN_FULL_ROW_SHARE:
        failures.append(f"table_rows_unproven(full_rows={worst_full_row:.2f})")

    # 9. no two side-by-side tables were read across the gutter as one.
    parallel = [
        b
        for b in blocks
        if b.parallel_label_lines >= MIN_PARALLEL_LABEL_LINES
        and b.parallel_labels >= MIN_PARALLEL_LABELS
    ]
    metrics["table_parallel_labels"] = max((b.parallel_labels for b in blocks), default=0)
    if parallel:
        failures.append(f"table_parallel(labels={parallel[0].parallel_labels})")

    # 10. the result splits into usable blocks.
    block_tokens = sum(len(s.tokens) for s in ordered)
    metrics["block_tokens"] = block_tokens
    if block_tokens < MIN_BLOCK_TOKENS:
        failures.append(f"blocks_ok(tokens={block_tokens})")

    return OrderSafetyResult(not failures, tuple(failures), metrics, tuple(order))


def _flow_breaks(segments: list[Segment], order: tuple[int, ...] | list[int]) -> list[tuple[int, int]]:
    """Pairs of segments whose shared flow the candidate order splits open.

    A pair qualifies when both segments sit in one persistent region, follow
    each other within it, and read on as one sentence or as a lead-in and its
    list. Anything from another region emitted between them has been inserted
    into that sentence. Table segments are excluded: a wrapped cell's own
    continuation is legitimately separated by the rest of its row.
    """

    lookup = {s.index: s for s in segments}
    position = {index: i for i, index in enumerate(order)}
    by_region: dict[int, list[int]] = {}
    for segment in segments:
        if not segment.tabular and segment.index in position:
            by_region.setdefault(segment.region, []).append(segment.index)

    breaks: list[tuple[int, int]] = []
    for region, members in by_region.items():
        members.sort(key=lambda i: (lookup[i].top, lookup[i].x0))
        for first, second in zip(members, members[1:]):
            if not continues(lookup[first].raw_text, lookup[second].raw_text):
                continue
            low, high = sorted((position[first], position[second]))
            if any(lookup[order[k]].region != region for k in range(low + 1, high)):
                breaks.append((first, second))
    return breaks


def order_agreement(first: OrderSafetyResult, second: OrderSafetyResult) -> float:
    """How closely two passing candidates agree on segment order."""

    if not first.order or not second.order:
        return 0.0
    import difflib

    matcher = difflib.SequenceMatcher(None, first.order, second.order, autojunk=False)
    matched = sum(block.size for block in matcher.get_matching_blocks())
    return matched / max(len(first.order), len(second.order))
