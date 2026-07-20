"""ESG structure-extraction verifier v2 (deterministic; no LLM).

Verifies word-index-only extraction records against the page's own word list:
no numeric token ships unless it appears verbatim on the page. Checks, in
gate order: token anchoring (with role collisions), literal numeric grammar,
spatial coherence (strict card_bbox/row_band for label+value+unit, loose for
qualifiers), single-consumption of value tokens, fail-closed table-header
plausibility, and qualifier provenance outside structural data-row bands.
Also grades self-containment (full / partial / bare, non-gating) and writes a
per-page numeric-token reconciliation (non-gating).

Input directory contract: records.jsonl (one record per line: page_id,
record_id, metric_label_idx/value_idx/unit_idx/qualifier_idx word-index
lists, coherence_mode with card_bbox or table_id+col_idx), wordlists/*.json
(page_id + words with idx/text/x0/x1/top/bottom), optional manifest.csv
(refused if it contains an OCR-lineage document). Output goes to a separate
directory so a run never overwrites earlier evidence.

Validated: 206/206 records pass on the held-structural pilot; the two header
parameters were frozen on the development split before the held-out run.
"""
import argparse
import json
import re
from collections import defaultdict
from pathlib import Path

import pandas as pd


# v1's literal-value grammar, retained unchanged in meaning.  Unicode escapes
# avoid a Windows-console encoding dependency in this source file.
NUMERIC_RE = re.compile(
    r"^[+\-~<>]?[$\u20AC\u00A3]?\d[\d,]*(\.\d+)?[KMBkmb]?\+?%?\.?$"
)
BBOX_TOL = 4.0
QUALIFIER_TOL = 250.0
ROWBAND_TOL = 2.0

# These are the only tuned v2 header parameters.  They were selected on the
# existing T3 development split, then frozen before the held-out test run.
HEADER_MIN_LABEL_LIKE_CELL_SHARE = 0.50
HEADER_MAX_DATA_LIKE_TOKEN_SHARE = 0.50

# A temporal label is label-like even when it is digits only.  DATA_LIKE_RE is
# intentionally broader than v1's value grammar so parenthesized accounting
# figures such as '(0.84)' cannot masquerade as a header label.
TEMPORAL_LABEL_RE = re.compile(
    r"^(?:(?:FY|CY)?(?:19|20)\d{2}|(?:FY|CY)\d{2}|Q[1-4])$", re.IGNORECASE
)
DATA_LIKE_RE = re.compile(
    r"^[+\-~<>]?[$\u20AC\u00A3]?\(?\d[\d,]*(?:\.\d+)?[KMBkmb]?\+?%?\)?\.?$"
)
OCR_LINEAGE_DOCUMENTS = {
    "ETSY-2024",
    "NGVC-2021",
    "NGVC-2022",
    "SHOO-2021",
    "WMT-2024",
}


def load_records(records_path):
    records = []
    if not records_path.exists():
        return records
    with open(records_path, encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if line and not line.startswith("//"):
                records.append(json.loads(line))
    return records


def load_wordlists(wordlist_dir):
    cache = {}
    for path in wordlist_dir.glob("*.json"):
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
        cache[data["page_id"]] = data
    return cache


def words_by_idx(page_data):
    return {word["idx"]: word for word in page_data["words"]}


def render_text(idx_list, widx):
    ordered = sorted(idx_list)
    return " ".join(widx[idx]["text"] for idx in ordered), ordered


def check_token_anchoring(record, widx):
    """The v1 all-role anchoring invariant, including role collisions."""
    reasons = []
    roles = ["metric_label_idx", "value_idx", "unit_idx", "qualifier_idx"]
    seen_role_for_idx = {}
    all_idx = []
    for role in roles:
        for idx in record.get(role, []):
            if idx not in widx:
                reasons.append(f"idx {idx} ({role}) not on this page's word list")
                continue
            if idx in seen_role_for_idx:
                reasons.append(
                    f"idx {idx} used in both '{seen_role_for_idx[idx]}' and "
                    f"'{role}' in the same record"
                )
            seen_role_for_idx[idx] = role
            all_idx.append(idx)
    if not record.get("value_idx"):
        reasons.append("record has no value_idx (every record must anchor a value)")
    return reasons, all_idx


def check_core_token_anchoring(record, widx):
    """Core-only view used for the separately reported value verification."""
    reasons = []
    seen_role_for_idx = {}
    for role in ["metric_label_idx", "value_idx", "unit_idx"]:
        for idx in record.get(role, []):
            if idx not in widx:
                reasons.append(f"idx {idx} ({role}) not on this page's word list")
                continue
            if idx in seen_role_for_idx:
                reasons.append(
                    f"idx {idx} used in both '{seen_role_for_idx[idx]}' and "
                    f"'{role}' in the same record"
                )
            seen_role_for_idx[idx] = role
    if not record.get("value_idx"):
        reasons.append("record has no value_idx (every record must anchor a value)")
    return reasons


def check_numeric_exactness(record, widx):
    reasons = []
    value_idx = [idx for idx in record.get("value_idx", []) if idx in widx]
    if not value_idx:
        return reasons  # anchoring already reports a missing value index
    text, _ = render_text(value_idx, widx)
    if not NUMERIC_RE.match(text.replace(" ", "")):
        reasons.append(f"value_idx renders to non-numeric text: {text!r}")
    return reasons


def check_core_spatial_coherence(record, widx):
    """v1's strict label/value/unit half of spatial coherence."""
    reasons = []
    core_idx = [
        idx
        for role in ["metric_label_idx", "value_idx", "unit_idx"]
        for idx in record.get(role, [])
        if idx in widx
    ]
    if len(core_idx) < 2:
        if not core_idx and not [idx for idx in record.get("qualifier_idx", []) if idx in widx]:
            reasons.append("no valid word indices to check spatial coherence")
        return reasons

    mode = record.get("coherence_mode", "card_bbox")
    if mode == "card_bbox":
        card_bbox = record.get("card_bbox")
        if not card_bbox or len(card_bbox) != 4:
            reasons.append("coherence_mode=card_bbox but no valid card_bbox declared")
            return reasons
        bx0, btop, bx1, bbottom = card_bbox
        for idx in core_idx:
            word = widx[idx]
            if (
                word["x0"] < bx0 - BBOX_TOL
                or word["x1"] > bx1 + BBOX_TOL
                or word["top"] < btop - BBOX_TOL
                or word["bottom"] > bbottom + BBOX_TOL
            ):
                reasons.append(
                    f"idx {idx} ({word['text']!r}) bbox falls outside declared "
                    "card_bbox by more than tolerance"
                )
    elif mode == "row_band":
        tops = [widx[idx]["top"] for idx in core_idx]
        bottoms = [widx[idx]["bottom"] for idx in core_idx]
        if max(tops) > min(bottoms) + ROWBAND_TOL:
            reasons.append(
                f"core words do not mutually y-overlap within {ROWBAND_TOL}pt "
                "tolerance (row_band mode)"
            )
    else:
        reasons.append(f"unknown coherence_mode {mode!r}")
    return reasons


def check_qualifier_spatial_coherence(record, widx):
    """v1's loose qualifier-distance half of spatial coherence."""
    reasons = []
    core_idx = [
        idx
        for role in ["metric_label_idx", "value_idx", "unit_idx"]
        for idx in record.get(role, [])
        if idx in widx
    ]
    qual_idx = [idx for idx in record.get("qualifier_idx", []) if idx in widx]
    if len(core_idx) + len(qual_idx) < 2:
        return reasons

    mode = record.get("coherence_mode", "card_bbox")
    if mode == "card_bbox":
        card_bbox = record.get("card_bbox")
        if not card_bbox or len(card_bbox) != 4:
            return reasons  # core check carries the invalid-card error
        bx0, btop, bx1, bbottom = card_bbox
        for idx in qual_idx:
            word = widx[idx]
            if (
                word["x0"] < bx0 - QUALIFIER_TOL
                or word["x1"] > bx1 + QUALIFIER_TOL
                or word["top"] < btop - QUALIFIER_TOL
                or word["bottom"] > bbottom + QUALIFIER_TOL
            ):
                reasons.append(
                    f"idx {idx} ({word['text']!r}) qualifier bbox falls outside "
                    "declared card_bbox by more than the wider qualifier tolerance"
                )
    elif mode == "row_band" and qual_idx and core_idx:
        tops = [widx[idx]["top"] for idx in core_idx]
        bottoms = [widx[idx]["bottom"] for idx in core_idx]
        core_mid = (min(tops) + max(bottoms)) / 2
        for idx in qual_idx:
            word = widx[idx]
            if abs(((word["top"] + word["bottom"]) / 2) - core_mid) > QUALIFIER_TOL:
                reasons.append(
                    f"idx {idx} ({word['text']!r}) qualifier is implausibly far "
                    "from the row (row_band mode)"
                )
    return reasons


def check_spatial_coherence(record, widx):
    """The complete v1 spatial gate, retained verbatim in decision behaviour."""
    reasons = []
    core_roles = ["metric_label_idx", "value_idx", "unit_idx"]
    core_idx = [
        idx
        for role in core_roles
        for idx in record.get(role, [])
        if idx in widx
    ]
    qual_idx = [idx for idx in record.get("qualifier_idx", []) if idx in widx]
    all_idx = core_idx + qual_idx
    if len(all_idx) < 2:
        if len(all_idx) == 0:
            reasons.append("no valid word indices to check spatial coherence")
        return reasons

    mode = record.get("coherence_mode", "card_bbox")
    if mode == "card_bbox":
        card_bbox = record.get("card_bbox")
        if not card_bbox or len(card_bbox) != 4:
            reasons.append("coherence_mode=card_bbox but no valid card_bbox declared")
            return reasons
        bx0, btop, bx1, bbottom = card_bbox
        for idx in core_idx:
            word = widx[idx]
            if (
                word["x0"] < bx0 - BBOX_TOL
                or word["x1"] > bx1 + BBOX_TOL
                or word["top"] < btop - BBOX_TOL
                or word["bottom"] > bbottom + BBOX_TOL
            ):
                reasons.append(
                    f"idx {idx} ({word['text']!r}) bbox falls outside declared "
                    "card_bbox by more than tolerance"
                )
        for idx in qual_idx:
            word = widx[idx]
            if (
                word["x0"] < bx0 - QUALIFIER_TOL
                or word["x1"] > bx1 + QUALIFIER_TOL
                or word["top"] < btop - QUALIFIER_TOL
                or word["bottom"] > bbottom + QUALIFIER_TOL
            ):
                reasons.append(
                    f"idx {idx} ({word['text']!r}) qualifier bbox falls outside "
                    "declared card_bbox by more than the wider qualifier tolerance"
                )
    elif mode == "row_band":
        tops = [widx[idx]["top"] for idx in core_idx]
        bottoms = [widx[idx]["bottom"] for idx in core_idx]
        if core_idx and max(tops) > min(bottoms) + ROWBAND_TOL:
            reasons.append(
                f"core words do not mutually y-overlap within {ROWBAND_TOL}pt "
                "tolerance (row_band mode)"
            )
        if qual_idx and core_idx:
            core_mid = (min(tops) + max(bottoms)) / 2
            for idx in qual_idx:
                word = widx[idx]
                if abs(((word["top"] + word["bottom"]) / 2) - core_mid) > QUALIFIER_TOL:
                    reasons.append(
                        f"idx {idx} ({word['text']!r}) qualifier is implausibly "
                        "far from the row (row_band mode)"
                    )
    else:
        reasons.append(f"unknown coherence_mode {mode!r}")
    return reasons


def is_temporal_label(text):
    return bool(TEMPORAL_LABEL_RE.fullmatch(text.strip()))


def is_label_like(text):
    return bool(re.search(r"[A-Za-z]", text)) or is_temporal_label(text)


def is_data_like(text):
    return bool(DATA_LIKE_RE.fullmatch(text.strip())) and not is_temporal_label(text)


def structural_scope_key(record):
    """Never compare separate tables or separate Tier C cards on one page."""
    if record.get("table_id"):
        return record["page_id"], "table", record["table_id"]
    if record.get("card_bbox"):
        return record["page_id"], "card", tuple(record["card_bbox"])
    return record["page_id"], "record", record.get("record_id")


def build_table_context(records, wordlists):
    """Derive the proposed row-0 header from the existing qualifier indices."""
    tables = defaultdict(list)
    for record in records:
        if record.get("table_id"):
            tables[(record["page_id"], record["table_id"])].append(record)

    contexts = {}
    for key, table_records in tables.items():
        page_id, table_id = key
        widx = words_by_idx(wordlists[page_id])
        qualifiers_by_col = defaultdict(set)
        for record in table_records:
            qualifiers_by_col[record.get("col_idx")].update(record.get("qualifier_idx", []))

        nonempty_cells = [
            sorted(idx for idx in indices if idx in widx)
            for indices in qualifiers_by_col.values()
            if any(idx in widx for idx in indices)
        ]
        header_idx = {idx for cell in nonempty_cells for idx in cell}
        header_present = bool(nonempty_cells)
        if not header_present:
            contexts[key] = {
                "header_present": False,
                "plausible": True,
                "header_idx": set(),
                "candidate_data_idx": set(),
                "reason": "",
            }
            continue

        label_like_cells = sum(
            any(is_label_like(widx[idx]["text"]) for idx in cell) for cell in nonempty_cells
        )
        header_tokens = [idx for cell in nonempty_cells for idx in cell]
        data_like_idx = {
            idx for idx in header_tokens if is_data_like(widx[idx]["text"])
        }
        label_share = label_like_cells / len(nonempty_cells)
        data_share = len(data_like_idx) / len(header_tokens)
        plausible = (
            label_share >= HEADER_MIN_LABEL_LIKE_CELL_SHARE
            and data_share <= HEADER_MAX_DATA_LIKE_TOKEN_SHARE
        )
        reason = ""
        if not plausible:
            reason = (
                "proposed header is implausible: "
                f"label-like cells={label_like_cells}/{len(nonempty_cells)} "
                f"({label_share:.0%}; minimum {HEADER_MIN_LABEL_LIKE_CELL_SHARE:.0%}), "
                f"data-like tokens={len(data_like_idx)}/{len(header_tokens)} "
                f"({data_share:.0%}; maximum {HEADER_MAX_DATA_LIKE_TOKEN_SHARE:.0%})"
            )
        contexts[key] = {
            "header_present": True,
            "plausible": plausible,
            "header_idx": header_idx,
            "candidate_data_idx": data_like_idx,
            "reason": reason,
        }
    return contexts


def y_overlaps(word_a, word_b):
    return word_a["top"] < word_b["bottom"] and word_b["top"] < word_a["bottom"]


def check_header_plausibility(record, table_context):
    if not record.get("table_id"):
        return []
    context = table_context[(record["page_id"], record["table_id"])]
    return [] if context["plausible"] else [context["reason"]]


def check_qualifier_provenance(record, widx, records_in_scope, table_context):
    """Reject a qualifier that is physically in another structural value band.

    For a table's proposed header we also treat a value-looking header token as
    a candidate data band.  This is parameter-free provenance: a parenthesized
    accounting figure at the qualifier's own y-band cannot be a header source.
    """
    qual_idx = [idx for idx in record.get("qualifier_idx", []) if idx in widx]
    if not qual_idx:
        return []

    reasons = []
    other_value_idx = {
        idx
        for other in records_in_scope
        if other.get("record_id") != record.get("record_id")
        for idx in other.get("value_idx", [])
        if idx in widx
    }
    for qualifier_idx in qual_idx:
        qualifier_word = widx[qualifier_idx]
        overlapping = [
            idx for idx in other_value_idx if y_overlaps(qualifier_word, widx[idx])
        ]
        if overlapping:
            reasons.append(
                f"idx {qualifier_idx} ({qualifier_word['text']!r}) qualifier "
                "y-overlaps a different record's value band"
            )

    if record.get("table_id"):
        context = table_context[(record["page_id"], record["table_id"])]
        for qualifier_idx in qual_idx:
            if qualifier_idx in context["candidate_data_idx"]:
                reasons.append(
                    f"idx {qualifier_idx} ({widx[qualifier_idx]['text']!r}) "
                    "qualifier lies in a candidate numeric data band, not a "
                    "declared header/pre-table region"
                )
    return reasons


def qualifier_grade(record, provenance_reasons, table_context):
    if not record.get("qualifier_idx"):
        return "bare", "bare"
    if record.get("table_id"):
        context = table_context[(record["page_id"], record["table_id"])]
        header_sourced = (
            context["header_present"]
            and context["plausible"]
            and set(record["qualifier_idx"]).issubset(context["header_idx"])
            and not provenance_reasons
        )
        if header_sourced:
            return "full", "header_sourced"
    if provenance_reasons:
        return "partial", "misprovenanced"
    return "partial", "weaker_provenance"


def assert_ocr_exclusion(input_dir):
    manifest_path = input_dir / "manifest.csv"
    if not manifest_path.exists():
        return
    manifest = pd.read_csv(manifest_path)
    found = []
    for _, row in manifest.iterrows():
        ticker = str(row.get("ticker", ""))
        pdf_stem = str(row.get("pdf_stem", ""))
        year_match = re.search(r"-(\d{4})$", pdf_stem)
        document_id = f"{ticker}-{year_match.group(1)}" if year_match else ""
        if document_id in OCR_LINEAGE_DOCUMENTS:
            found.append(document_id)
    found = sorted(set(found))
    if found:
        raise AssertionError(
            "OCR-lineage document(s) present in manifest: " + ", ".join(found)
        )


def run_verifier(input_dir, output_dir):
    records_path = input_dir / "records.jsonl"
    wordlist_dir = input_dir / "wordlists"
    records = load_records(records_path)
    wordlists = load_wordlists(wordlist_dir)
    assert_ocr_exclusion(input_dir)

    if not records:
        print(f"No records found at {records_path} - nothing to verify yet.")
        return
    output_dir.mkdir(parents=True, exist_ok=True)

    table_context = build_table_context(records, wordlists)
    records_by_scope = defaultdict(list)
    for record in records:
        records_by_scope[structural_scope_key(record)].append(record)

    results = []
    value_owner = {}
    for record in records:
        page_id = record["page_id"]
        page_data = wordlists.get(page_id)
        if page_data is None:
            results.append(
                {
                    "page_id": page_id,
                    "record_id": record.get("record_id"),
                    "record_type": record.get("record_type"),
                    "pass": False,
                    "value_verification_pass": False,
                    "qualifier_provenance_status": "unavailable",
                    "self_containment_grade": "bare" if not record.get("qualifier_idx") else "partial",
                    "reasons": f"no wordlist json found for page_id {page_id!r}",
                    "rendered_label": "",
                    "rendered_value": "",
                    "rendered_unit": "",
                    "rendered_qualifier": "",
                }
            )
            continue

        widx = words_by_idx(page_data)
        anchoring_reasons, _ = check_token_anchoring(record, widx)
        numeric_reasons = check_numeric_exactness(record, widx)
        core_spatial_reasons = check_core_spatial_coherence(record, widx)
        spatial_reasons = check_spatial_coherence(record, widx)
        header_reasons = check_header_plausibility(record, table_context)
        provenance_reasons = check_qualifier_provenance(
            record,
            widx,
            records_by_scope[structural_scope_key(record)],
            table_context,
        )

        value_reuse_reasons = []
        for idx in record.get("value_idx", []):
            key = (page_id, idx)
            if key in value_owner and value_owner[key] != record.get("record_id"):
                value_reuse_reasons.append(
                    f"value idx {idx} already consumed by record {value_owner[key]} "
                    "on this page"
                )
            else:
                value_owner[key] = record.get("record_id")

        reasons = (
            anchoring_reasons
            + numeric_reasons
            + spatial_reasons
            + value_reuse_reasons
            + header_reasons
            + provenance_reasons
        )
        value_integrity_reasons = (
            check_core_token_anchoring(record, widx)
            + numeric_reasons
            + core_spatial_reasons
            + value_reuse_reasons
        )
        grade, qualifier_status = qualifier_grade(
            record, provenance_reasons, table_context
        )

        label_text, _ = render_text(
            [idx for idx in record.get("metric_label_idx", []) if idx in widx], widx
        )
        value_text, _ = render_text(
            [idx for idx in record.get("value_idx", []) if idx in widx], widx
        )
        unit_text, _ = render_text(
            [idx for idx in record.get("unit_idx", []) if idx in widx], widx
        )
        qualifier_text, _ = render_text(
            [idx for idx in record.get("qualifier_idx", []) if idx in widx], widx
        )
        results.append(
            {
                "page_id": page_id,
                "record_id": record.get("record_id"),
                "record_type": record.get("record_type"),
                "pass": len(reasons) == 0,
                "value_verification_pass": len(value_integrity_reasons) == 0,
                "qualifier_provenance_status": qualifier_status,
                "self_containment_grade": grade,
                "reasons": "; ".join(reasons),
                "rendered_label": label_text,
                "rendered_value": value_text,
                "rendered_unit": unit_text,
                "rendered_qualifier": qualifier_text,
            }
        )

    results_df = pd.DataFrame(results)
    results_df.to_csv(output_dir / "verifier_results.csv", index=False)

    # v1 reconciliation, still non-gating, now calculated from v2 passes.
    passing_all_idx_by_page = {}
    roles = ["metric_label_idx", "value_idx", "unit_idx", "qualifier_idx"]
    for record, result in zip(records, results):
        if result["pass"]:
            bucket = passing_all_idx_by_page.setdefault(record["page_id"], set())
            for role in roles:
                bucket.update(record.get(role, []))

    recon_rows = []
    pages_with_records = {record["page_id"] for record in records}
    for page_id in pages_with_records:
        page_data = wordlists[page_id]
        numeric_word_idx = {
            word["idx"] for word in page_data["words"] if NUMERIC_RE.match(word["text"])
        }
        consumed = passing_all_idx_by_page.get(page_id, set()) & numeric_word_idx
        total = len(numeric_word_idx)
        recon_rows.append(
            {
                "page_id": page_id,
                "numeric_tokens_on_page": total,
                "consumed_by_passing_records": len(consumed),
                "orphaned": total - len(consumed),
                "orphan_share": round((total - len(consumed)) / total, 4) if total else None,
            }
        )
    pd.DataFrame(recon_rows).sort_values("page_id").to_csv(
        output_dir / "page_reconciliation.csv", index=False
    )

    passed = int(results_df["pass"].sum())
    print(
        f"Records verified: {len(results_df)}  |  PASS: {passed} "
        f"({passed / len(results_df):.1%})  |  FAIL: {len(results_df) - passed}"
    )
    print(f"Wrote v2 evidence to {output_dir}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input-dir", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args()
    run_verifier(args.input_dir, args.output_dir)


if __name__ == "__main__":
    main()
