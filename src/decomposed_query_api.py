"""Application wrapper around the locked production retrieval API."""
from __future__ import annotations

import re
from typing import Any

from src.ai_query_planner import (
    PLANNER_VERSION, build_planner_context, clarification_response,
    planner_from_environment, validate_and_lock_plan,
)
from src.query_api import ProductionRetriever, SourceSpec
from src.query_decomposition import (
    CONTRACT_VERSION, ContractError, ProductionRetrieverAdapter, SourceResolver,
    aggregate, build_subqueries, normalize_for_matching,
)


def _aliases_from_connection(conn: Any) -> tuple[set[str], dict[str, str]]:
    sql = """
      SELECT DISTINCT c.ticker, c.name
      FROM public.companies c
      JOIN public.rag_eligible_10k_chunks r USING(company_id)
      ORDER BY c.ticker
    """
    with conn.cursor() as cursor:
        cursor.execute(sql)
        rows = [(str(a), str(b)) for a, b in cursor.fetchall()]
    tickers = {ticker for ticker, _ in rows}
    candidates: dict[str, set[str]] = {}
    suffixes = re.compile(r"\b(incorporated|inc|corporation|corp|company|co|plc|ltd|limited|holdings?|hldgs|industries|inds)\b", re.I)
    unsafe_single_words = {
        "brand", "digital", "group", "international", "retail", "stores",
        "worldwide", "advance", "american", "national", "superior", "global",
        "academy", "designer", "destination", "grocery", "natural", "village",
    }
    for ticker, name in rows:
        normalized = normalize_for_matching(name)
        stripped = " ".join(suffixes.sub(" ", normalized).split())
        variants = {normalized, stripped}
        words = normalized.split()
        if words and words[0] == "the":
            variants.add(" ".join(words[1:]))
        # Natural questions commonly use a distinctive leading brand word
        # while corpus metadata stores the full legal name.  Add that word
        # only when it is non-generic; the collision pass below still requires
        # it to identify exactly one eligible ticker.
        brand_words = stripped.split()
        if (
            brand_words
            and len(brand_words[0]) >= 6
            and brand_words[0] not in unsafe_single_words
        ):
            variants.add(brand_words[0])
        for variant in variants:
            if variant and not (len(variant.split()) == 1 and variant in unsafe_single_words):
                candidates.setdefault(variant, set()).add(ticker)
    aliases = {alias: next(iter(values)) for alias, values in candidates.items() if len(values) == 1}
    return tickers, aliases


def _simple_response(result: dict[str, Any]) -> dict[str, Any]:
    evidence = []
    for item in result["evidence"]:
        copied = dict(item)
        copied["aggregated_rank"] = int(item["final_rank"])
        copied["page_start"] = None
        copied["page_end"] = None
        copied["page_unavailable_reason"] = "html_source"
        evidence.append(copied)
    return {**result, "status": "success", "contract_version": CONTRACT_VERSION, "is_decomposed": False, "evidence": evidence}


def run_query(
    question: str,
    filters: dict[str, Any] | None = None,
    *,
    retriever: Any | None = None,
    conn: Any | None = None,
    request_id: str = "request",
    planner: Any | None = None,
) -> dict[str, Any]:
    """Run a simple hard-routed request or a decomposed comparison request."""
    owned = retriever is None
    retriever = retriever or ProductionRetriever(conn=conn)
    try:
        if filters:
            source = SourceSpec.from_mapping(filters)
            return _simple_response(retriever.retrieve(question, [source]))
        active_conn = conn or retriever.conn
        resolver = SourceResolver.from_connection(active_conn)
        tickers, aliases = _aliases_from_connection(active_conn)
        active_planner = planner if planner is not None else planner_from_environment()
        normalized_intent = None
        if active_planner is not None:
            raw_plan = active_planner.create_plan(
                question,
                context=build_planner_context(tickers, aliases, resolver),
            )
            plan = validate_and_lock_plan(
                raw_plan,
                question=question,
                known_tickers=tickers,
                aliases=aliases,
                resolver=resolver,
            )
            if plan.status == "clarification_required":
                return {
                    **clarification_response(plan),
                    "contract_version": CONTRACT_VERSION,
                    "original_question": question,
                }
            query_type, subqueries = plan.query_type, plan.subqueries
            normalized_intent = plan.normalized_intent
        else:
            query_type, subqueries = build_subqueries(
                request_id, question, tickers, aliases, resolver
            )
        response = aggregate(
            subqueries,
            ProductionRetrieverAdapter(
                retriever, SourceSpec, original_question=question,
            ),
            evidence_limit=max(5, len(subqueries)),
        )
        return {
            **response,
            "is_decomposed": True,
            "query_type": query_type.value,
            "original_question": question,
            "normalized_intent": normalized_intent,
            "planner_mode": "ai" if active_planner is not None else "deterministic",
            "planner_version": PLANNER_VERSION if active_planner is not None else None,
            "subqueries": [sq.__dict__ for sq in subqueries],
        }
    except ContractError as exc:
        ambiguous = {"ENTITY_UNRESOLVED", "YEAR_UNRESOLVED", "SECTION_UNRESOLVED", "AMBIGUOUS_COMPANY_YEAR_PAIRING", "AMBIGUOUS_CLAIM_SECTION_PAIRING"}
        source_errors = {"SOURCE_NOT_FOUND", "SOURCE_AMBIGUOUS"}
        status = "ambiguous_request" if exc.code in ambiguous else ("source_resolution_failed" if exc.code in source_errors else "retrieval_failed")
        return {"status": status, "error_code": exc.code, "message": str(exc), "contract_version": CONTRACT_VERSION, "is_decomposed": False, "evidence": []}
    except LookupError:
        return {"status": "insufficient_evidence", "error_code": "MISSING_COMPARISON_SIDE", "message": "An authorized source produced no candidates", "contract_version": CONTRACT_VERSION, "is_decomposed": bool(not filters), "evidence": []}
    except Exception:
        return {"status": "retrieval_failed", "error_code": "RETRIEVAL_EXCEPTION", "message": "Retrieval failed; inspect the protected server log", "contract_version": CONTRACT_VERSION, "is_decomposed": bool(not filters), "evidence": []}
    finally:
        if owned:
            retriever.close()
