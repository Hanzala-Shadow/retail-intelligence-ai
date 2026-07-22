"""Validated one-call AI planning for the production 10-K retriever.

The model may interpret language and propose search text.  It never receives
database credentials and it never controls source filters.  This module turns
the model response into locked :class:`SubQuery` objects only after every
source field has been checked against the approved corpus catalog.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
from difflib import SequenceMatcher
from dataclasses import asdict, dataclass
from typing import Any, Mapping, Protocol

from src.query_decomposition import (
    CLAIM_SECTION_TABLE,
    VALID_SECTIONS,
    ContractError,
    QueryType,
    SourceResolver,
    SubQuery,
    detect_claims,
    detect_entities,
    detect_filing_years,
    detect_sections,
    normalize_for_matching,
)

PLANNER_VERSION = "0.1.0"
MAX_REQUIREMENTS = 5
MAX_SEARCH_QUERIES = 2
MAX_SEARCH_QUERY_LENGTH = 240

CLARIFICATION_FIELDS = {
    "company",
    "filing_year",
    "document_type",
    "claim",
    "section",
    "company_year_pairing",
}

SECTION_DESCRIPTIONS = {
    "Item_1": "business, products, customers, stores, operations, and competition",
    "Item_1A": "risk factors and major business risks",
    "Item_7": "management discussion, performance drivers, margins, and liquidity",
    "Item_8": "financial statements, notes, accounting policies, and reported figures",
}


@dataclass(frozen=True)
class Clarification:
    field: str
    question: str
    reason: str
    options: tuple[str, ...]


@dataclass(frozen=True)
class ValidatedQueryPlan:
    status: str
    query_type: QueryType
    normalized_intent: str
    subqueries: tuple[SubQuery, ...]
    clarification: Clarification | None = None


class QueryPlannerClient(Protocol):
    """Small interface that keeps paid model calls mockable in tests."""

    def create_plan(
        self, question: str, *, context: Mapping[str, Any]
    ) -> Mapping[str, Any]: ...


def build_planner_context(
    known_tickers: set[str], aliases: Mapping[str, str], resolver: SourceResolver
) -> dict[str, Any]:
    """Return the safe catalog facts the model may use for planning."""
    grouped: dict[tuple[str, int, str], dict[str, Any]] = {}
    for record in resolver.records:
        key = (record.ticker, record.filing_year, record.doc_type)
        row = grouped.setdefault(
            key,
            {
                "ticker": record.ticker,
                "filing_year": record.filing_year,
                "document_type": record.doc_type,
                "source_count": 0,
                "sections": set(),
            },
        )
        row["source_count"] += 1
        row["sections"].update(resolver.sections_for(record))

    sources = []
    for row in grouped.values():
        sources.append(
            {
                **row,
                "sections": sorted(row["sections"]),
            }
        )
    sources.sort(key=lambda item: (item["ticker"], item["filing_year"]))
    return {
        "allowed_tickers": sorted(known_tickers),
        "company_aliases": [
            {"name": name, "ticker": ticker}
            for name, ticker in sorted(aliases.items())
        ],
        "eligible_sources": sources,
        "allowed_document_types": ["10-K"],
        "section_catalog": [
            {
                "section_code": section,
                "description": SECTION_DESCRIPTIONS.get(
                    section, "an allowed SEC 10-K section"
                ),
            }
            for section in sorted(VALID_SECTIONS)
        ],
        "known_claim_section_examples": [
            {"claim": claim, "section_code": section}
            for claim, section in sorted(CLAIM_SECTION_TABLE.items())
        ],
        "limits": {
            "maximum_requirements": MAX_REQUIREMENTS,
            "maximum_search_queries_per_requirement": MAX_SEARCH_QUERIES,
        },
    }


def _planner_schema() -> dict[str, Any]:
    query_types = [item.value for item in QueryType]
    requirement = {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "company_mention",
            "ticker",
            "year_mention",
            "filing_year",
            "document_type",
            "claim_mention",
            "claim_key",
            "section_code",
            "search_queries",
        ],
        "properties": {
            "company_mention": {
                "type": "string",
                "minLength": 1,
                "maxLength": 120,
            },
            "ticker": {"type": "string", "minLength": 1, "maxLength": 10},
            "year_mention": {"type": "string", "minLength": 1, "maxLength": 40},
            "filing_year": {"type": "integer", "minimum": 1900, "maximum": 2100},
            "document_type": {"type": "string", "enum": ["10-K"]},
            "claim_mention": {
                "type": "string",
                "minLength": 1,
                "maxLength": 160,
            },
            "claim_key": {"type": "string", "minLength": 1, "maxLength": 160},
            "section_code": {"type": "string", "enum": sorted(VALID_SECTIONS)},
            "search_queries": {
                "type": "array",
                "minItems": 1,
                "maxItems": MAX_SEARCH_QUERIES,
                "items": {
                    "type": "string",
                    "minLength": 1,
                    "maxLength": MAX_SEARCH_QUERY_LENGTH,
                },
            },
        },
    }
    clarification = {
        "type": "object",
        "additionalProperties": False,
        "required": ["field", "question", "reason", "options"],
        "properties": {
            "field": {"type": "string", "enum": sorted(CLARIFICATION_FIELDS)},
            "question": {"type": "string", "minLength": 1, "maxLength": 300},
            "reason": {"type": "string", "minLength": 1, "maxLength": 500},
            "options": {
                "type": "array",
                "maxItems": 8,
                "items": {"type": "string", "minLength": 1, "maxLength": 120},
            },
        },
    }
    return {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "status",
            "query_type",
            "normalized_intent",
            "requirements",
            "clarification",
        ],
        "properties": {
            "status": {
                "type": "string",
                "enum": ["ready", "clarification_required"],
            },
            "query_type": {"type": "string", "enum": query_types},
            "normalized_intent": {
                "type": "string",
                "minLength": 1,
                "maxLength": 500,
            },
            "requirements": {
                "type": "array",
                "maxItems": MAX_REQUIREMENTS,
                "items": requirement,
            },
            "clarification": {
                "anyOf": [clarification, {"type": "null"}],
            },
        },
    }


SYSTEM_PROMPT = """You plan searches for a fail-closed 10-K RAG system.
Return only the requested JSON object. Use only tickers, years, document types,
and sections present in the supplied catalog. Correct obvious spelling by
selecting a catalog company, but never invent or silently substitute a source.
If more than one source meaning is reasonable, or a detail would change the
evidence, return clarification_required.

When ready, create one requirement for each company-year-claim-section need.
Split comparisons into separate sides. Each search query must stay on the same
claim and must not introduce another company, year, document type, or section.
Use short retrieval phrases, not answers. Do not claim that evidence exists;
the backend will check the database and retrieval results.

For every requirement, copy company_mention, year_mention, and claim_mention
from exact wording in the user's question. company_mention may keep a spelling
mistake while ticker contains the corrected approved ticker. For "latest" or
"most recent", copy that phrase as year_mention and select the newest approved
year for that ticker. Do not create a ready plan when these fields cannot be
grounded in the user's words.

For clarification options about a company, use ticker values from the catalog.
For a filing year, use four-digit years from the catalog. Never output SQL.
"""


class OpenAIQueryPlanner:
    """OpenAI-compatible structured-output client used only when enabled."""

    def __init__(
        self,
        *,
        api_key: str,
        model: str = "gpt-5-mini",
        base_url: str = "https://api.openai.com/v1",
        timeout_seconds: float = 30.0,
        session: Any | None = None,
    ):
        if not api_key:
            raise ValueError("api_key must be non-empty")
        self.api_key = api_key
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.timeout_seconds = timeout_seconds
        self.session = session

    @classmethod
    def from_environment(cls) -> "OpenAIQueryPlanner":
        api_key = os.getenv("OPENAI_API_KEY", "").strip()
        if not api_key:
            raise ContractError(
                "PLANNER_CONFIG_ERROR",
                "AI query planning is enabled but OPENAI_API_KEY is missing",
            )
        return cls(
            api_key=api_key,
            model=os.getenv("QUERY_PLANNER_MODEL", "gpt-5-mini").strip()
            or "gpt-5-mini",
            base_url=os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1"),
            timeout_seconds=float(os.getenv("QUERY_PLANNER_TIMEOUT_SECONDS", "30")),
        )

    def create_plan(
        self, question: str, *, context: Mapping[str, Any]
    ) -> Mapping[str, Any]:
        import requests

        client = self.session or requests
        payload = {
            "model": self.model,
            "messages": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {
                    "role": "user",
                    "content": json.dumps(
                        {"question": question, "approved_catalog": context},
                        sort_keys=True,
                    ),
                },
            ],
            "response_format": {
                "type": "json_schema",
                "json_schema": {
                    "name": "uncertainty_aware_query_plan",
                    "strict": True,
                    "schema": _planner_schema(),
                },
            },
            "max_completion_tokens": 2000,
        }
        response = client.post(
            f"{self.base_url}/chat/completions",
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
            json=payload,
            timeout=self.timeout_seconds,
        )
        response.raise_for_status()
        body = response.json()
        try:
            content = body["choices"][0]["message"]["content"]
            result = json.loads(content)
        except (KeyError, IndexError, TypeError, json.JSONDecodeError) as exc:
            raise ContractError(
                "PLANNER_RESPONSE_INVALID",
                "AI planner did not return the required JSON object",
            ) from exc
        if not isinstance(result, dict):
            raise ContractError(
                "PLANNER_RESPONSE_INVALID", "AI planner response must be an object"
            )
        return result


def planner_from_environment() -> QueryPlannerClient | None:
    from dotenv import load_dotenv

    load_dotenv()
    enabled = os.getenv("QUERY_PLANNER_ENABLED", "").strip().casefold()
    if enabled not in {"1", "true", "yes", "on"}:
        return None
    return OpenAIQueryPlanner.from_environment()


def _require_exact_keys(
    value: Mapping[str, Any], expected: set[str], path: str
) -> None:
    actual = set(value)
    if actual != expected:
        missing = sorted(expected - actual)
        extra = sorted(actual - expected)
        details = []
        if missing:
            details.append(f"missing {', '.join(missing)}")
        if extra:
            details.append(f"unexpected {', '.join(extra)}")
        raise ContractError(
            "PLANNER_RESPONSE_INVALID", f"{path} has invalid fields: {'; '.join(details)}"
        )


def _bounded_text(value: Any, path: str, maximum: int) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ContractError("PLANNER_RESPONSE_INVALID", f"{path} must be non-empty text")
    cleaned = " ".join(value.split())
    if len(cleaned) > maximum:
        raise ContractError("PLANNER_RESPONSE_INVALID", f"{path} is too long")
    return cleaned


def _validate_clarification(
    value: Any, *, resolver: SourceResolver, known_tickers: set[str]
) -> Clarification:
    if not isinstance(value, Mapping):
        raise ContractError(
            "PLANNER_RESPONSE_INVALID", "clarification must be an object"
        )
    _require_exact_keys(
        value, {"field", "question", "reason", "options"}, "clarification"
    )
    field = str(value["field"])
    if field not in CLARIFICATION_FIELDS:
        raise ContractError(
            "PLANNER_RESPONSE_INVALID", "clarification field is not allowed"
        )
    question = _bounded_text(value["question"], "clarification.question", 300)
    reason = _bounded_text(value["reason"], "clarification.reason", 500)
    raw_options = value["options"]
    if not isinstance(raw_options, list) or len(raw_options) > 8:
        raise ContractError(
            "PLANNER_RESPONSE_INVALID", "clarification.options must contain at most 8 items"
        )
    options = tuple(
        _bounded_text(item, f"clarification.options[{index}]", 120)
        for index, item in enumerate(raw_options)
    )
    if len(set(options)) != len(options):
        raise ContractError(
            "PLANNER_RESPONSE_INVALID", "clarification options must be unique"
        )
    if field == "company" and any(option not in known_tickers for option in options):
        raise ContractError(
            "PLANNER_RESPONSE_INVALID",
            "company clarification options must be approved tickers",
        )
    if field == "filing_year":
        valid_years = {str(record.filing_year) for record in resolver.records}
        if any(option not in valid_years for option in options):
            raise ContractError(
                "PLANNER_RESPONSE_INVALID",
                "filing-year clarification options must exist in the approved corpus",
            )
    if field == "document_type" and any(option != "10-K" for option in options):
        raise ContractError(
            "PLANNER_RESPONSE_INVALID",
            "document-type clarification options must be approved",
        )
    if field == "section" and any(option not in VALID_SECTIONS for option in options):
        raise ContractError(
            "PLANNER_RESPONSE_INVALID",
            "section clarification options must be approved",
        )
    return Clarification(field=field, question=question, reason=reason, options=options)


def _derive_query_type(subqueries: tuple[SubQuery, ...]) -> QueryType:
    tickers = {item.ticker for item in subqueries}
    years = {item.filing_year for item in subqueries}
    if len(tickers) > 1 and len(years) > 1:
        return QueryType.MULTI_AXIS_COMPARISON
    if len(years) > 1:
        return QueryType.TEMPORAL_COMPARISON
    if len(tickers) > 1:
        return QueryType.CROSS_COMPANY_COMPARISON
    if len(subqueries) > 1:
        return QueryType.CROSS_SECTION_SYNTHESIS
    return QueryType.SINGLE_SOURCE


def _mention_is_in_question(mention: str, question: str) -> bool:
    normalized_mention = normalize_for_matching(mention)
    normalized_question = normalize_for_matching(question)
    if not normalized_mention:
        return False
    return bool(
        re.search(
            rf"(?<!\w){re.escape(normalized_mention)}(?!\w)", normalized_question
        )
    )


def _tickers_for_company_mention(
    mention: str, known_tickers: set[str], aliases: Mapping[str, str]
) -> set[str]:
    normalized = normalize_for_matching(mention)
    exact = {
        ticker
        for alias, ticker in aliases.items()
        if normalize_for_matching(alias) == normalized
    }
    exact.update(
        ticker for ticker in known_tickers if ticker.casefold() == normalized
    )
    if exact:
        return exact

    scores: dict[str, float] = {}
    for alias, ticker in aliases.items():
        normalized_alias = normalize_for_matching(alias)
        if not normalized_alias:
            continue
        score = SequenceMatcher(None, normalized, normalized_alias).ratio()
        scores[ticker] = max(score, scores.get(ticker, 0.0))
    if not scores:
        return set()
    ordered = sorted(scores.items(), key=lambda item: (-item[1], item[0]))
    best_ticker, best_score = ordered[0]
    second_score = ordered[1][1] if len(ordered) > 1 else 0.0
    if best_score >= 0.80 and best_score - second_score >= 0.08:
        return {best_ticker}
    return set()


def _validate_grounded_requirement(
    raw_requirement: Mapping[str, Any],
    *,
    question: str,
    ticker: str,
    filing_year: int,
    doc_type: str,
    claim_key: str,
    known_tickers: set[str],
    aliases: Mapping[str, str],
    resolver: SourceResolver,
) -> None:
    company_mention = _bounded_text(
        raw_requirement["company_mention"], "company_mention", 120
    )
    year_mention = _bounded_text(
        raw_requirement["year_mention"], "year_mention", 40
    )
    claim_mention = _bounded_text(
        raw_requirement["claim_mention"], "claim_mention", 160
    )
    for name, mention in (
        ("company_mention", company_mention),
        ("year_mention", year_mention),
        ("claim_mention", claim_mention),
    ):
        if not _mention_is_in_question(mention, question):
            raise ContractError(
                "PLANNER_GROUNDING_FAILED",
                f"{name} is not grounded in the user's question",
            )

    company_candidates = _tickers_for_company_mention(
        company_mention, known_tickers, aliases
    )
    if company_candidates != {ticker}:
        raise ContractError(
            "PLANNER_GROUNDING_FAILED",
            "company mention does not safely resolve to the planned ticker",
        )

    stated_years = set(detect_filing_years(year_mention))
    normalized_year_mention = normalize_for_matching(year_mention)
    latest_terms = {"latest", "most recent", "newest"}
    if stated_years:
        if stated_years != {filing_year}:
            raise ContractError(
                "PLANNER_GROUNDING_FAILED",
                "year mention does not match the planned filing year",
            )
    elif normalized_year_mention in latest_terms:
        available_years = {
            record.filing_year
            for record in resolver.records
            if record.ticker == ticker and record.doc_type == doc_type
        }
        if not available_years or filing_year != max(available_years):
            raise ContractError(
                "PLANNER_GROUNDING_FAILED",
                "latest must resolve to the newest approved filing year",
            )
    else:
        raise ContractError(
            "PLANNER_GROUNDING_FAILED",
            "year mention must state a year or an approved latest-year phrase",
        )

    normalized_claim = normalize_for_matching(claim_key)
    normalized_claim_mention = normalize_for_matching(claim_mention)
    if not normalized_claim or not normalized_claim_mention:
        raise ContractError(
            "PLANNER_GROUNDING_FAILED", "claim must be grounded in user wording"
        )
    for detected_claim in detect_claims(claim_mention):
        normalized_detected = normalize_for_matching(detected_claim)
        if (
            normalized_detected not in normalized_claim
            and normalized_claim not in normalized_detected
        ):
            raise ContractError(
                "PLANNER_GROUNDING_FAILED",
                "claim normalization changed the user's known claim",
            )


def _validate_search_queries(
    raw_queries: Any,
    *,
    ticker: str,
    filing_year: int,
    claim_key: str,
    section_code: str,
    known_tickers: set[str],
    aliases: Mapping[str, str],
) -> tuple[str, ...]:
    if not isinstance(raw_queries, list) or not 1 <= len(raw_queries) <= MAX_SEARCH_QUERIES:
        raise ContractError(
            "PLANNER_RESPONSE_INVALID",
            f"each requirement needs 1-{MAX_SEARCH_QUERIES} search queries",
        )
    queries: list[str] = []
    normalized_seen: set[str] = set()
    aliases_with_tickers = dict(aliases)
    aliases_with_tickers.update({item.casefold(): item for item in known_tickers})
    normalized_claim = normalize_for_matching(claim_key)
    for index, raw_query in enumerate(raw_queries):
        query = _bounded_text(
            raw_query, f"requirement.search_queries[{index}]", MAX_SEARCH_QUERY_LENGTH
        )
        normalized = normalize_for_matching(query)
        if normalized in normalized_seen:
            raise ContractError(
                "PLANNER_RESPONSE_INVALID", "search queries must be meaningfully different"
            )
        normalized_seen.add(normalized)
        mentioned_years = set(detect_filing_years(query))
        if mentioned_years - {filing_year}:
            raise ContractError(
                "PLANNER_SOURCE_DRIFT", "search query introduced another filing year"
            )
        mentioned_tickers = set(
            detect_entities(query, known_tickers, aliases_with_tickers)
        )
        if mentioned_tickers - {ticker}:
            raise ContractError(
                "PLANNER_SOURCE_DRIFT", "search query introduced another company"
            )
        mentioned_sections = set(detect_sections(query))
        if mentioned_sections - {section_code}:
            raise ContractError(
                "PLANNER_SOURCE_DRIFT", "search query introduced another SEC section"
            )
        for detected_claim in detect_claims(query):
            normalized_detected = normalize_for_matching(detected_claim)
            if (
                normalized_detected not in normalized_claim
                and normalized_claim not in normalized_detected
            ):
                raise ContractError(
                    "PLANNER_CLAIM_DRIFT", "search query introduced another known claim"
                )
        queries.append(query)
    return tuple(queries)


def validate_and_lock_plan(
    raw_plan: Mapping[str, Any],
    *,
    question: str,
    known_tickers: set[str],
    aliases: Mapping[str, str],
    resolver: SourceResolver,
) -> ValidatedQueryPlan:
    """Validate model JSON and compile it into hard-routed subqueries."""
    if not isinstance(raw_plan, Mapping):
        raise ContractError("PLANNER_RESPONSE_INVALID", "AI plan must be an object")
    _require_exact_keys(
        raw_plan,
        {"status", "query_type", "normalized_intent", "requirements", "clarification"},
        "plan",
    )
    status = str(raw_plan["status"])
    if status not in {"ready", "clarification_required"}:
        raise ContractError("PLANNER_RESPONSE_INVALID", "AI plan status is not allowed")
    normalized_intent = _bounded_text(
        raw_plan["normalized_intent"], "normalized_intent", 500
    )
    if not isinstance(question, str) or not question.strip():
        raise ContractError("PLANNER_RESPONSE_INVALID", "question must be non-empty")
    raw_requirements = raw_plan["requirements"]
    if not isinstance(raw_requirements, list):
        raise ContractError(
            "PLANNER_RESPONSE_INVALID", "requirements must be an array"
        )

    if status == "clarification_required":
        if raw_requirements:
            raise ContractError(
                "PLANNER_RESPONSE_INVALID",
                "a clarification plan must not authorize retrieval requirements",
            )
        if raw_plan["query_type"] != QueryType.UNSUPPORTED_OR_AMBIGUOUS.value:
            raise ContractError(
                "PLANNER_RESPONSE_INVALID",
                "clarification plans must use unsupported_or_ambiguous query type",
            )
        clarification = _validate_clarification(
            raw_plan["clarification"],
            resolver=resolver,
            known_tickers=known_tickers,
        )
        return ValidatedQueryPlan(
            status=status,
            query_type=QueryType.UNSUPPORTED_OR_AMBIGUOUS,
            normalized_intent=normalized_intent,
            subqueries=(),
            clarification=clarification,
        )

    if raw_plan["clarification"] is not None:
        raise ContractError(
            "PLANNER_RESPONSE_INVALID", "ready plans must not include clarification"
        )
    if not 1 <= len(raw_requirements) <= MAX_REQUIREMENTS:
        raise ContractError(
            "PLANNER_RESPONSE_INVALID",
            f"ready plans need 1-{MAX_REQUIREMENTS} requirements",
        )

    subqueries: list[SubQuery] = []
    for index, raw_requirement in enumerate(raw_requirements):
        if not isinstance(raw_requirement, Mapping):
            raise ContractError(
                "PLANNER_RESPONSE_INVALID", f"requirements[{index}] must be an object"
            )
        _require_exact_keys(
            raw_requirement,
            {
                "company_mention",
                "ticker",
                "year_mention",
                "filing_year",
                "document_type",
                "claim_mention",
                "claim_key",
                "section_code",
                "search_queries",
            },
            f"requirements[{index}]",
        )
        ticker = _bounded_text(raw_requirement["ticker"], "ticker", 10).upper()
        if ticker not in known_tickers:
            raise ContractError(
                "PLANNER_SOURCE_INVALID", f"ticker {ticker} is not in the approved corpus"
            )
        year_value = raw_requirement["filing_year"]
        if isinstance(year_value, bool) or not isinstance(year_value, int):
            raise ContractError(
                "PLANNER_RESPONSE_INVALID", "filing_year must be an integer"
            )
        doc_type = str(raw_requirement["document_type"])
        if doc_type != "10-K":
            raise ContractError(
                "PLANNER_SOURCE_INVALID", "only approved 10-K sources are allowed"
            )
        claim_key = _bounded_text(raw_requirement["claim_key"], "claim_key", 160)
        section_code = str(raw_requirement["section_code"])
        if section_code not in VALID_SECTIONS:
            raise ContractError(
                "PLANNER_SECTION_INVALID", f"section {section_code} is not allowed"
            )
        source = resolver.resolve(ticker, year_value, doc_type)
        _validate_grounded_requirement(
            raw_requirement,
            question=question,
            ticker=ticker,
            filing_year=year_value,
            doc_type=doc_type,
            claim_key=claim_key,
            known_tickers=known_tickers,
            aliases=aliases,
            resolver=resolver,
        )
        available_sections = resolver.sections_for(source)
        if available_sections and section_code not in available_sections:
            raise ContractError(
                "PLANNER_SECTION_INVALID",
                f"section {section_code} is not eligible for {ticker} {year_value}",
            )
        search_queries = _validate_search_queries(
            raw_requirement["search_queries"],
            ticker=ticker,
            filing_year=year_value,
            claim_key=claim_key,
            section_code=section_code,
            known_tickers=known_tickers,
            aliases=aliases,
        )
        side_slug = re.sub(r"[^a-z0-9]+", "-", claim_key.casefold()).strip("-")
        side = f"{ticker}-{year_value}-{section_code}-{side_slug}"
        identity = "|".join(
            (ticker, str(year_value), source.accession_number, section_code, claim_key)
        )
        stable_id = hashlib.sha256(identity.encode("utf-8")).hexdigest()[:16]
        subqueries.append(
            SubQuery(
                subquery_id=f"sq-{stable_id}",
                question=(
                    f"In the {year_value} 10-K filing for {ticker}, Section "
                    f"{section_code}, what are the key disclosures regarding {claim_key}?"
                ),
                claim_key=claim_key,
                comparison_side_id=side,
                ticker=ticker,
                filing_year=year_value,
                doc_type=doc_type,
                accession_number=source.accession_number,
                section_code=section_code,
                search_queries=search_queries,
            )
        )

    locked = tuple(subqueries)
    if len({item.comparison_side_id for item in locked}) != len(locked):
        raise ContractError(
            "DUPLICATE_REQUIREMENT", "AI plan produced duplicate requirements"
        )
    derived_type = _derive_query_type(locked)
    if raw_plan["query_type"] != derived_type.value:
        raise ContractError(
            "PLANNER_TYPE_MISMATCH",
            "AI query type does not match the validated requirements",
        )
    return ValidatedQueryPlan(
        status="ready",
        query_type=derived_type,
        normalized_intent=normalized_intent,
        subqueries=locked,
    )


def clarification_response(plan: ValidatedQueryPlan) -> dict[str, Any]:
    if plan.clarification is None:
        raise ValueError("plan does not require clarification")
    return {
        "status": "clarification_required",
        "error_code": "AI_CLARIFICATION_REQUIRED",
        "message": plan.clarification.question,
        "normalized_intent": plan.normalized_intent,
        "clarification": asdict(plan.clarification),
        "query_type": plan.query_type.value,
        "planner_version": PLANNER_VERSION,
        "planner_mode": "ai",
        "is_decomposed": True,
        "evidence": [],
    }
