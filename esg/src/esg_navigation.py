"""Separate repeated rotated navigation from real page content.

Rotation is taken from PDF character transform matrices.  The ``upright``
word flag is intentionally ignored because pdfminer can set it to false for
ordinary horizontal text.
"""

from __future__ import annotations

from dataclasses import dataclass
import math
import re


VERTICAL_ANGLE_TOLERANCE = 5.0
RUN_CROSS_TOLERANCE = 4.0
RUN_GAP_MULTIPLIER = 2.5
RUN_GAP_FLOOR = 18.0
INFERRED_SPACE_FLOOR = 8.0
AREA_TOLERANCE_SHARE = 0.025
MIN_REPEAT_PAGES = 3
HEADER_FOOTER_BAND_SHARE = 0.12
HORIZONTAL_LINE_TOLERANCE = 3.0
HORIZONTAL_RUN_GAP = 28.0


@dataclass(frozen=True)
class RotatedRun:
    text: str
    x0: float
    top: float
    x1: float
    bottom: float
    rotation: float


@dataclass(frozen=True)
class NavigationProfileItem:
    text_key: str
    item_type: str
    position_share: float
    page_count: int


@dataclass(frozen=True)
class NavigationCleanResult:
    body_words: list[dict]
    navigation_items: list[dict]
    rotated_content_items: list[dict]


def character_angle(char: dict) -> float:
    """Return the real text-baseline angle from the character matrix."""

    matrix = char.get("matrix") or (1, 0, 0, 1, 0, 0)
    if len(matrix) < 2:
        return 0.0
    a, b = float(matrix[0]), float(matrix[1])
    if a == 0.0 and b == 0.0:
        return 0.0
    return math.degrees(math.atan2(b, a)) % 360.0


def _is_vertical_angle(angle: float) -> bool:
    return min(abs(angle - 90.0), abs(angle - 270.0)) <= VERTICAL_ANGLE_TOLERANCE


def _number(item: dict, key: str) -> float:
    try:
        return float(item.get(key, 0.0))
    except (TypeError, ValueError):
        return 0.0


def _text_key(text: str) -> str:
    return re.sub(r"\W+", "", text, flags=re.UNICODE).casefold()


def join_rotated_characters(chars: list[dict]) -> list[RotatedRun]:
    """Join adjacent matrix-rotated characters in extraction order."""

    groups: list[dict] = []
    for char in chars:
        angle = character_angle(char)
        if not _is_vertical_angle(angle) or not str(char.get("text", "")):
            continue
        cross = (_number(char, "x0") + _number(char, "x1")) / 2
        along = (_number(char, "top") + _number(char, "bottom")) / 2
        size = max(
            _number(char, "x1") - _number(char, "x0"),
            _number(char, "bottom") - _number(char, "top"),
        )
        previous = groups[-1] if groups else None
        continues = bool(
            previous
            and abs(previous["rotation"] - angle) <= VERTICAL_ANGLE_TOLERANCE
            and abs(cross - previous["cross"]) <= max(RUN_CROSS_TOLERANCE, size * 0.75)
            and abs(along - previous["last_along"]) <= max(RUN_GAP_FLOOR, size * RUN_GAP_MULTIPLIER)
        )
        if not continues:
            groups.append(
                {
                    "text": str(char.get("text", "")),
                    "rotation": angle,
                    "cross": cross,
                    "last_along": along,
                    "x0": _number(char, "x0"),
                    "x1": _number(char, "x1"),
                    "top": _number(char, "top"),
                    "bottom": _number(char, "bottom"),
                }
            )
            continue
        char_text = str(char.get("text", ""))
        if (
            abs(along - previous["last_along"]) > max(INFERRED_SPACE_FLOOR, size * 1.25)
            and not previous["text"].endswith(" ")
            and not char_text.startswith(" ")
        ):
            previous["text"] += " "
        previous["text"] += char_text
        previous["last_along"] = along
        previous["x0"] = min(previous["x0"], _number(char, "x0"))
        previous["x1"] = max(previous["x1"], _number(char, "x1"))
        previous["top"] = min(previous["top"], _number(char, "top"))
        previous["bottom"] = max(previous["bottom"], _number(char, "bottom"))

    return [
        RotatedRun(
            text=re.sub(r"\s+", " ", group["text"]).strip(),
            x0=group["x0"],
            top=group["top"],
            x1=group["x1"],
            bottom=group["bottom"],
            rotation=group["rotation"],
        )
        for group in groups
        if _text_key(group["text"])
    ]


def join_horizontal_edge_characters(
    chars: list[dict], page_height: float
) -> list[tuple[str, RotatedRun]]:
    """Join horizontal character runs found only in header/footer bands."""

    groups: list[dict] = []
    for char in chars:
        text = str(char.get("text", ""))
        if not text or _is_vertical_angle(character_angle(char)):
            continue
        top, bottom = _number(char, "top"), _number(char, "bottom")
        if top <= page_height * HEADER_FOOTER_BAND_SHARE:
            item_type = "header"
        elif bottom >= page_height * (1.0 - HEADER_FOOTER_BAND_SHARE):
            item_type = "footer"
        else:
            continue
        previous = groups[-1] if groups else None
        continues = bool(
            previous
            and previous["item_type"] == item_type
            and abs(previous["top"] - top) <= HORIZONTAL_LINE_TOLERANCE
            and _number(char, "x0") >= previous["last_x1"] - 2.0
            and _number(char, "x0") - previous["last_x1"] <= HORIZONTAL_RUN_GAP
        )
        if not continues:
            groups.append(
                {
                    "item_type": item_type,
                    "text": text,
                    "x0": _number(char, "x0"),
                    "x1": _number(char, "x1"),
                    "top": top,
                    "bottom": bottom,
                    "last_x1": _number(char, "x1"),
                }
            )
            continue
        previous["text"] += text
        previous["x1"] = max(previous["x1"], _number(char, "x1"))
        previous["top"] = min(previous["top"], top)
        previous["bottom"] = max(previous["bottom"], bottom)
        previous["last_x1"] = _number(char, "x1")

    return [
        (
            group["item_type"],
            RotatedRun(
                re.sub(r"\s+", " ", group["text"]).strip(),
                group["x0"], group["top"], group["x1"], group["bottom"], 0.0,
            ),
        )
        for group in groups
        if _text_key(group["text"])
    ]


def build_navigation_profile(pages: list[tuple[list[dict], float, float]]) -> tuple[NavigationProfileItem, ...]:
    """Find repeated vertical and horizontal edge navigation."""

    clusters: list[dict] = []
    for page_index, (chars, width, height) in enumerate(pages):
        if width <= 0 or height <= 0:
            continue
        candidates = [("vertical", run) for run in join_rotated_characters(chars)]
        candidates.extend(join_horizontal_edge_characters(chars, height))
        for item_type, run in candidates:
            key = _text_key(run.text)
            position_share = (
                run.bottom / height if item_type == "footer" else run.top / height
            )
            match = next(
                (
                    cluster
                    for cluster in clusters
                    if cluster["text_key"] == key
                    and cluster["item_type"] == item_type
                    and abs(cluster["position_share"] - position_share) <= AREA_TOLERANCE_SHARE
                ),
                None,
            )
            if match is None:
                match = {
                    "text_key": key,
                    "item_type": item_type,
                    "position_share": position_share,
                    "pages": set(),
                }
                clusters.append(match)
            match["pages"].add(page_index)
    return tuple(
        NavigationProfileItem(
            c["text_key"], c["item_type"], c["position_share"], len(c["pages"])
        )
        for c in clusters
        if len(c["pages"]) >= MIN_REPEAT_PAGES
    )


def _overlaps(word: dict, run: RotatedRun, tolerance: float = 0.75) -> bool:
    return not (
        _number(word, "x1") < run.x0 - tolerance
        or _number(word, "x0") > run.x1 + tolerance
        or _number(word, "bottom") < run.top - tolerance
        or _number(word, "top") > run.bottom + tolerance
    )


def clean_navigation(
    words: list[dict],
    chars: list[dict],
    page_width: float,
    page_height: float,
    profile: tuple[NavigationProfileItem, ...],
) -> NavigationCleanResult:
    """Place every input word in body, navigation, or rotated content once."""

    classified_runs = [("vertical", run) for run in join_rotated_characters(chars)]
    classified_runs.extend(join_horizontal_edge_characters(chars, page_height))
    runs = [run for _, run in classified_runs]
    run_word_indices = [
        [index for index, word in enumerate(words) if _overlaps(word, run)]
        for run in runs
    ]
    navigation_indices: set[int] = set()
    rotated_indices: set[int] = set()
    navigation_items: list[dict] = []
    rotated_items: list[dict] = []

    for (item_type, run), indices in zip(classified_runs, run_word_indices):
        if not indices:
            continue
        match = next(
            (
                item
                for item in profile
                if item.text_key == _text_key(run.text)
                and item.item_type == item_type
                and page_width > 0
                and page_height > 0
                and abs(
                    item.position_share
                    - (run.bottom / page_height if item_type == "footer" else run.top / page_height)
                ) <= AREA_TOLERANCE_SHARE
            ),
            None,
        )
        item = {
            "text": run.text,
            "x0": run.x0,
            "top": run.top,
            "x1": run.x1,
            "bottom": run.bottom,
            "rotation": run.rotation,
            "item_type": item_type,
            "source_indices": tuple(indices),
        }
        if match:
            item["repeated_page_count"] = match.page_count
            navigation_items.append(item)
            navigation_indices.update(indices)
        elif item_type == "vertical":
            rotated_items.append(item)
            rotated_indices.update(indices)

    # Navigation wins only if malformed overlapping runs ever occur.
    rotated_indices.difference_update(navigation_indices)
    for item in rotated_items:
        item["source_indices"] = tuple(i for i in item["source_indices"] if i in rotated_indices)
    rotated_items = [item for item in rotated_items if item["source_indices"]]
    body_words = [
        word for index, word in enumerate(words)
        if index not in navigation_indices and index not in rotated_indices
    ]
    assert len(body_words) + len(navigation_indices) + len(rotated_indices) == len(words)
    return NavigationCleanResult(body_words, navigation_items, rotated_items)
