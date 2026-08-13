"""Deterministic, no-model detector coverage auditing.

Expected routing metadata is parsed only after question-only detection has
completed. Gold answers, chunks, and passages are never accepted by this
module.
"""
from __future__ import annotations

from collections import Counter
from dataclasses import asdict, dataclass
from typing import Any, Iterable

from src.query_decomposition import (
    CONTRACT_VERSION,
    ContractError,
    SourceResolver,
    build_subqueries,
    detect_claims,
    detect_entities,
    detect_filing_years,
    detect_sections,
)

AUDIT_VERSION = "1.0.0"
ALLOWED_QUESTION_FIELDS = (
    "question_id",
    "question_group",
    "question",
    "expected_tickers",
    "expected_years",
    "required_doc_type",
    "required_sections",
    "supporting_accession_numbers",
    "refusal_expected",
)
POST_DETECTION_SCORING_FIELDS = (
    "expected_tickers",
    "expected_years",
    "required_doc_type",
    "required_sections",
    "supporting_accession_numbers",
)
PROHIBITED_GOLD_FIELDS = (
    "expected_answer",
    "supporting_chunk_ids",
    "supporting_passages",
    "supporting_chunk_indexes",
    "supporting_source_files",
    "supporting_file_sha256",
    "supporting_token_counts",
)


@dataclass(frozen=True, order=True)
class Route:
    ticker: str
    filing_year: int
    doc_type: str
    accession_number: str
    section_code: str


def _parts(value: str) -> list[str]:
    return [part.strip() for part in str(value or "").split("|") if part.strip()]


def _broadcast(
    values: list[str], count: int, field: str, question_id: str
) -> list[str]:
    if len(values) == 1 and count > 1:
        values = values * count
    if len(values) != count:
        raise ValueError(f"{question_id}: positional {field} mismatch")
    return values


def expected_routes(row: dict[str, str]) -> tuple[Route, ...]:
    """Parse declared non-gold routing metadata for post-detection scoring."""
    question_id = row["question_id"]
    tickers = _parts(row["expected_tickers"])
    if not tickers:
        raise ValueError(f"{question_id}: expected_tickers is empty")
    count = len(tickers)
    years = _broadcast(
        _parts(row["expected_years"]), count, "expected_years", question_id
    )
    doc_types = _broadcast(
        _parts(row["required_doc_type"]),
        count,
        "required_doc_type",
        question_id,
    )
    accessions = _broadcast(
        _parts(row["supporting_accession_numbers"]),
        count,
        "supporting_accession_numbers",
        question_id,
    )
    sections = _broadcast(
        _parts(row["required_sections"]),
        count,
        "required_sections",
        question_id,
    )
    return tuple(
        Route(ticker, int(year), doc_type, accession, section)
        for ticker, year, doc_type, accession, section in zip(
            tickers, years, doc_types, accessions, sections, strict=True
        )
    )


def _route_from_subquery(subquery: Any) -> Route:
    return Route(
        ticker=subquery.ticker,
        filing_year=int(subquery.filing_year),
        doc_type=subquery.doc_type,
        accession_number=subquery.accession_number,
        section_code=subquery.section_code,
    )


def _multiset_equal(left: Iterable[Any], right: Iterable[Any]) -> bool:
    return Counter(left) == Counter(right)


def audit_question(
    row: dict[str, str],
    known_tickers: set[str],
    aliases: dict[str, str],
    resolver: SourceResolver,
) -> dict[str, Any]:
    """Audit one question without providing its expected route to detection."""
    question_id = row["question_id"]
    question = row["question"]

    # Question-only detection happens before expected routing is parsed.
    entities = detect_entities(question, known_tickers, aliases)
    years = detect_filing_years(question)
    claims = detect_claims(question)
    explicit_sections = detect_sections(question)

    query_type: str | None = None
    error_code: str | None = None
    error_message: str | None = None
    subqueries: tuple[Any, ...] = ()
    try:
        detected_type, subqueries = build_subqueries(
            question_id,
            question,
            known_tickers,
            aliases,
            resolver,
        )
        query_type = detected_type.value
    except ContractError as exc:
        error_code = exc.code
        error_message = str(exc)

    # Declared routing fields enter only here, after detection is complete.
    expected = expected_routes(row)
    detected_routes = tuple(_route_from_subquery(item) for item in subqueries)
    expected_tickers = tuple(route.ticker for route in expected)
    expected_years = tuple(route.filing_year for route in expected)
    expected_sections = tuple(route.section_code for route in expected)
    detected_route_tickers = tuple(route.ticker for route in detected_routes)
    detected_route_years = tuple(route.filing_year for route in detected_routes)
    detected_route_sections = tuple(route.section_code for route in detected_routes)

    entity_exact = set(entities) == set(expected_tickers)
    year_exact = bool(years) and set(years) == set(expected_years)
    route_ticker_exact = bool(detected_routes) and _multiset_equal(
        detected_route_tickers, expected_tickers
    )
    route_year_exact = bool(detected_routes) and _multiset_equal(
        detected_route_years, expected_years
    )
    route_section_exact = bool(detected_routes) and _multiset_equal(
        detected_route_sections, expected_sections
    )
    routing_exact = bool(detected_routes) and _multiset_equal(
        detected_routes, expected
    )

    if not years:
        year_interpretation = "filing_year_omitted"
    elif set(years) == set(expected_years):
        year_interpretation = "matches_declared_filing_year"
    else:
        year_interpretation = "content_year_filing_year_mismatch"

    return {
        "question_id": question_id,
        "question_group": row["question_group"],
        "question": question,
        "detector_input_fields": ["question"],
        "detected": {
            "entities": list(entities),
            "years": list(years),
            "claims": list(claims),
            "explicit_sections": list(explicit_sections),
            "query_type": query_type,
            "subqueries": [
                {
                    "subquery_id": item.subquery_id,
                    "ticker": item.ticker,
                    "filing_year": item.filing_year,
                    "doc_type": item.doc_type,
                    "accession_number": item.accession_number,
                    "section_code": item.section_code,
                    "claim_key": item.claim_key,
                }
                for item in subqueries
            ],
            "resolution_status": "resolved" if error_code is None else "unresolved",
            "error_code": error_code,
            "error_message": error_message,
        },
        "declared_route_for_post_detection_scoring": [
            asdict(route) for route in expected
        ],
        "scoring": {
            "entity_exact": entity_exact,
            "raw_year_set_exact": year_exact,
            "route_ticker_exact": route_ticker_exact,
            "route_year_exact": route_year_exact,
            "route_section_exact": route_section_exact,
            "routing_exact": routing_exact,
            "year_interpretation": year_interpretation,
        },
    }


def _summary(rows: list[dict[str, Any]]) -> dict[str, Any]:
    def summarize(items: list[dict[str, Any]]) -> dict[str, Any]:
        error_counts = Counter(
            item["detected"]["error_code"]
            for item in items
            if item["detected"]["error_code"] is not None
        )
        year_counts = Counter(
            item["scoring"]["year_interpretation"] for item in items
        )
        return {
            "questions": len(items),
            "entity_exact": sum(item["scoring"]["entity_exact"] for item in items),
            "raw_year_set_exact": sum(
                item["scoring"]["raw_year_set_exact"] for item in items
            ),
            "resolved": sum(
                item["detected"]["resolution_status"] == "resolved"
                for item in items
            ),
            "route_ticker_exact": sum(
                item["scoring"]["route_ticker_exact"] for item in items
            ),
            "route_year_exact": sum(
                item["scoring"]["route_year_exact"] for item in items
            ),
            "route_section_exact": sum(
                item["scoring"]["route_section_exact"] for item in items
            ),
            "routing_exact": sum(
                item["scoring"]["routing_exact"] for item in items
            ),
            "error_codes": dict(sorted(error_counts.items())),
            "year_interpretations": dict(sorted(year_counts.items())),
        }

    groups = sorted({item["question_group"] for item in rows})
    return {
        "overall": summarize(rows),
        "by_group": {
            group: summarize(
                [item for item in rows if item["question_group"] == group]
            )
            for group in groups
        },
        "questions_requiring_metadata_or_clarification": [
            item["question_id"]
            for item in rows
            if not item["scoring"]["routing_exact"]
        ],
    }


def run_audit(
    question_rows: Iterable[dict[str, str]],
    known_tickers: set[str],
    aliases: dict[str, str],
    resolver: SourceResolver,
    *,
    questions_sha256: str,
    detector_code_sha256: str,
    audit_code_sha256: str,
    corpus_metadata_sha256: str,
    expected_supported: int | None = None,
    refusals_excluded: int = 0,
) -> dict[str, Any]:
    rows = list(question_rows)
    if not rows:
        raise RuntimeError("question set contains no supported questions")
    if expected_supported is not None and len(rows) != expected_supported:
        raise RuntimeError(
            f"expected {expected_supported} supported questions, "
            f"found {len(rows)}"
        )
    audited = [
        audit_question(row, known_tickers, aliases, resolver) for row in rows
    ]
    return {
        "audit_version": AUDIT_VERSION,
        "decomposition_contract_version": CONTRACT_VERSION,
        "in_sample": None,
        "mode": "question_only_detector_coverage",
        "no_model": True,
        "database_access": "read_only_corpus_metadata_only",
        "supported_questions": len(audited),
        "refusals_excluded": refusals_excluded,
        "detector_input_fields": ["question"],
        "approved_corpus_metadata_inputs": [
            "eligible ticker",
            "unambiguous company-name aliases",
            "eligible filing year",
            "document type",
            "accession number",
        ],
        "post_detection_scoring_fields": list(POST_DETECTION_SCORING_FIELDS),
        "prohibited_gold_fields": list(PROHIBITED_GOLD_FIELDS),
        "gold_chunk_ids_used_for_detection": False,
        "gold_passages_used_for_detection": False,
        "expected_answers_used_for_detection": False,
        "hashes": {
            "questions_sha256": questions_sha256,
            "query_decomposition_sha256": detector_code_sha256,
            "detector_coverage_audit_sha256": audit_code_sha256,
            "corpus_metadata_sha256": corpus_metadata_sha256,
        },
        "summary": _summary(audited),
        "questions": audited,
    }
