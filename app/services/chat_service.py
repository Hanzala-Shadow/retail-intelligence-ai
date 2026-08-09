"""Question-to-citations orchestration with complete latency telemetry."""
from __future__ import annotations

import time
from typing import Any, Callable


def _requirements(result: dict[str, Any]) -> list[dict[str, Any]]:
    if result.get("subqueries"):
        return [{
            "subquery_id": item["subquery_id"], "claim_key": item["claim_key"],
            "ticker": item["ticker"], "filing_year": int(item["filing_year"]),
            "required_section_code": item["section_code"],
        } for item in result["subqueries"]]
    raise ValueError("retrieval response lacks decomposed requirements")


def _evidence(result: dict[str, Any]) -> list[dict[str, Any]]:
    rows = []
    for index, item in enumerate(result.get("evidence") or [], 1):
        rows.append({
            "label": f"C{index}", "rank": index, "ticker": item["ticker"],
            "filing_year": int(item["filing_year"]),
            "section_code": item["section_code"],
            "accession_number": item.get("accession_number"),
            "source_chunk_id": item.get("source_chunk_id"),
            "text": str(item.get("chunk_text") or item.get("text") or ""),
        })
    if len(rows) != 16 or any(not row["text"] for row in rows):
        raise ValueError("frozen retrieval must return 16 non-empty evidence rows")
    return rows


class ChatService:
    def __init__(self, retrieval: Callable[[str, str], dict[str, Any]], generator: Any):
        self.retrieval = retrieval
        self.generator = generator

    def answer(self, question: str, request_id: str) -> dict[str, Any]:
        total_started = time.perf_counter()
        retrieval_started = time.perf_counter()
        retrieval = self.retrieval(question, request_id)
        retrieval_wall_ms = round((time.perf_counter() - retrieval_started) * 1000, 3)
        if retrieval.get("status") != "success":
            return {
                "request_id": request_id,
                "status": retrieval.get("status", "retrieval_failed"),
                "answer": "", "message": retrieval.get("message", "Retrieval failed"),
                "requirements": [], "citations": [], "limitations": [],
                "telemetry": {"retrieval_ms": retrieval_wall_ms},
            }
        requirements = _requirements(retrieval)
        evidence = _evidence(retrieval)
        generated = self.generator.generate({
            "question": question, "query_type": retrieval.get("query_type"),
            "requirements": requirements, "evidence": evidence,
        })
        validation = generated["validation"]
        status = (
            "validation_failed" if not validation["valid"] else
            "partial_answer" if validation["insufficient_evidence"] else "success"
        )
        cited = set(validation["citations"])
        citations = [{
            "label": item["label"], "ticker": item["ticker"],
            "filing_year": item["filing_year"], "section_code": item["section_code"],
            "accession_number": item["accession_number"],
            "source_chunk_id": item["source_chunk_id"], "excerpt": item["text"][:500],
        } for item in evidence if f"[{item['label']}]" in cited]
        coverage_by_id = {
            item["subquery_id"]: item.get("retrieval_status")
            for item in retrieval.get("requirement_coverage") or []
        }
        requirement_output = [{
            **item,
            "status": "supported" if coverage_by_id.get(item["subquery_id"]) == "represented"
            else "insufficient_evidence",
        } for item in requirements]
        adapter = retrieval.get("chatbot_retrieval_timings_ms") or {}
        return {
            "request_id": request_id, "status": status, "answer": generated["answer"],
            "requirements": requirement_output, "citations": citations,
            "limitations": (["One or more requirements lack direct support in the supplied evidence."]
                            if validation["insufficient_evidence"] else []),
            "telemetry": {
                "total_ms": round((time.perf_counter() - total_started) * 1000, 3),
                "retrieval_ms": retrieval_wall_ms,
                "retrieval_core_ms": adapter.get("retrieval_core"),
                "routing_orchestration_ms": adapter.get("routing_orchestration"),
                "database_connect_ms": adapter.get("database_connect"),
                "routing_catalog_load_ms": adapter.get("routing_catalog_load"),
                "generation_ms": generated["generation_ms"], "evidence_count": len(evidence),
                "input_tokens": generated["input_tokens"],
                "output_tokens": generated["output_tokens"],
                "estimated_cost_usd": generated["estimated_cost_usd"],
                "model_id": generated["model_id"],
                "policy_id": "balanced_anchored_round_robin_k16",
            },
            "validation": validation,
        }
