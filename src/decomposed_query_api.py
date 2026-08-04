"""Application wrapper around the locked production retrieval API."""
from __future__ import annotations

import os
import re
from types import SimpleNamespace
from typing import Any

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
    suffixes = re.compile(
        r"\b(incorporated|inc|corporation|corp|company|co|plc|ltd|limited|"
        r"holdings?|hldgs|industries|inds)\b",
        re.I,
    )
    unsafe_single_words = {
        "brand", "digital", "group", "international", "retail", "stores",
        "worldwide", "advance", "american", "national", "superior", "global",
        "academy", "designer", "destination", "grocery", "natural", "village",
        "america", "americas", "north", "south", "east", "west",
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
        # Some corpus legal names omit an apostrophe before a possessive s
        # (for example, "retailers"), while natural questions retain it and
        # normalize to two tokens ("retailer s"). Add that corpus-derived
        # variant only when the leading token is long enough; the collision
        # pass still requires a unique ticker.
        if (
            brand_words
            and len(brand_words[0]) >= 5
            and brand_words[0].endswith("s")
        ):
            possessive_words = [
                brand_words[0][:-1],
                "s",
                *brand_words[1:],
            ]
            for length in range(2, min(4, len(possessive_words)) + 1):
                variants.add(" ".join(possessive_words[:length]))
        if (
            len(brand_words) >= 2
            and brand_words[1] == "s"
            and len(brand_words[0]) >= 4
        ):
            merged_words = [
                brand_words[0] + "s",
                *brand_words[2:],
            ]
            for length in range(1, min(3, len(merged_words)) + 1):
                variant = " ".join(merged_words[:length])
                if not (
                    len(merged_words[:length]) == 1
                    and variant in unsafe_single_words
                ):
                    variants.add(variant)
        # Add unique multiword leading forms from corpus company names. This
        # handles natural shortened legal names while the collision pass below
        # prevents ambiguous aliases from being activated.
        for length in range(2, min(4, len(brand_words)) + 1):
            variants.add(" ".join(brand_words[:length]))
        if (
            brand_words
            and len(brand_words[0]) >= 4
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
    sufficiency_enabled: bool = False,
    direct_support_labels: dict[tuple[int, str], str] | None = None,
) -> dict[str, Any]:
    """Run a simple hard-routed request or a decomposed comparison request."""
    owned = retriever is None
    retriever = retriever or ProductionRetriever(conn=conn)
    try:
        retrieval_policy = os.getenv(
            "RAG_RETRIEVAL_POLICY",
            "legacy_v1",
        ).strip()
        if filters:
            source = SourceSpec.from_mapping(filters)
            if retrieval_policy == "balanced_anchored_round_robin_k16":
                requirement = SimpleNamespace(
                    subquery_id="sq-direct-request",
                    question=question,
                    claim_key=f"direct request for {source.section_code}",
                    comparison_side_id=(
                        f"{source.ticker}-{source.filing_year}-{source.section_code}"
                    ),
                    ticker=source.ticker,
                    filing_year=source.filing_year,
                    accession_number=source.accession_number,
                    section_code=source.section_code,
                    doc_type=source.doc_type,
                )
                result = retriever.retrieve_anchored(question, [requirement])
                return {
                    **result,
                    "status": "success",
                    "contract_version": CONTRACT_VERSION,
                    "is_decomposed": False,
                }
            return _simple_response(retriever.retrieve(question, [source]))
        active_conn = conn or retriever.conn
        routing_metadata = getattr(
            retriever,
            "_routing_metadata_cache",
            None,
        )
        if routing_metadata is None:
            resolver = SourceResolver.from_connection(active_conn)
            tickers, aliases = _aliases_from_connection(active_conn)
            routing_metadata = (resolver, tickers, aliases)
            setattr(retriever, "_routing_metadata_cache", routing_metadata)
        else:
            resolver, tickers, aliases = routing_metadata
        query_type, subqueries = build_subqueries(request_id, question, tickers, aliases, resolver)
        if retrieval_policy == "balanced_anchored_round_robin_k16":
            result = retriever.retrieve_anchored(question, subqueries)
            return {
                **result,
                "status": "success",
                "contract_version": CONTRACT_VERSION,
                "is_decomposed": True,
                "query_type": query_type.value,
                "original_question": question,
                "subqueries": [sq.__dict__ for sq in subqueries],
            }
        if retrieval_policy != "legacy_v1":
            raise ValueError(
                "RAG_RETRIEVAL_POLICY must be legacy_v1 or "
                "balanced_anchored_round_robin_k16"
            )
        response = aggregate(
            subqueries,
            ProductionRetrieverAdapter(
                retriever, SourceSpec, original_question=question,
            ),
            evidence_limit=None if sufficiency_enabled else 5,
            sufficiency_enabled=sufficiency_enabled,
            direct_support_labels=direct_support_labels,
        )
        return {**response, "is_decomposed": True, "query_type": query_type.value, "original_question": question, "subqueries": [sq.__dict__ for sq in subqueries]}
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
