"""Pure response-format helpers shared by the Streamlit page and tests."""
from __future__ import annotations

from typing import Any


def status_message(result: dict[str, Any]) -> tuple[str, str]:
    status = result.get("status")
    if status == "success":
        return "success", "Answer generated from indexed annual filings."
    if status == "partial_answer":
        return "warning", "Partial answer: at least one requirement lacks direct evidence."
    if status == "insufficient_evidence":
        return "warning", "The indexed filings do not provide sufficient evidence."
    if status == "ambiguous_request":
        return "warning", "Clarification required: identify the companies, filing years, and topic."
    return "error", "The request could not be completed. Use the request ID when reporting it."


def citation_rows(result: dict[str, Any]) -> list[dict[str, str]]:
    rows = []
    for item in result.get("citations") or []:
        rows.append({
            "label": str(item.get("label") or "Citation"),
            "source": " · ".join(filter(None, (
                str(item.get("ticker") or ""),
                str(item.get("filing_year") or ""),
                str(item.get("section_code") or ""),
            ))),
            "accession": str(item.get("accession_number") or "Unavailable"),
            "chunk": str(item.get("source_chunk_id") or "Unavailable"),
            "excerpt": str(item.get("excerpt") or "Excerpt unavailable").strip(),
        })
    return rows


def coverage_rows(result: dict[str, Any]) -> list[dict[str, Any]]:
    rows = []
    for item in result.get("requirements") or []:
        requirement = str(item.get("claim_key") or "Requested filing analysis")
        scope = " · ".join(filter(None, (
            str(item.get("ticker") or ""),
            str(item.get("filing_year") or ""),
            str(item.get("required_section_code") or ""),
        )))
        status = str(item.get("status", "unknown")).replace("_", " ").title()
        rows.append({
            "requirement": requirement, "scope": scope, "status": status,
            "supported": item.get("status") == "supported",
            # Preserve the established helper contract for downstream tests.
            "Requirement": requirement, "Ticker": item.get("ticker"),
            "Filing year": item.get("filing_year"),
            "Section": item.get("required_section_code"), "Coverage": status,
        })
    return rows


def technical_metrics(result: dict[str, Any]) -> dict[str, Any]:
    telemetry = result.get("telemetry") or {}
    metrics = dict([
        ("Request ID", result.get("request_id")),
        ("Retrieval policy", telemetry.get("policy_id")),
        ("Generator", telemetry.get("model_id")),
        ("Evidence passages", telemetry.get("evidence_count")),
        ("Database connection", _seconds(telemetry.get("database_connect_ms"))),
        ("Routing orchestration", _seconds(telemetry.get("routing_orchestration_ms"))),
        ("Retrieval core", _seconds(telemetry.get("retrieval_core_ms"))),
        ("Retrieval total", _seconds(telemetry.get("retrieval_ms"))),
        ("Generation", _seconds(telemetry.get("generation_ms"))),
        ("End-to-end total", _seconds(telemetry.get("total_ms"))),
        ("Routing catalog preload (startup only)",
         _seconds(telemetry.get("routing_catalog_load_ms"))),
        ("Input tokens", telemetry.get("input_tokens")),
        ("Output tokens", telemetry.get("output_tokens")),
        ("Estimated Bedrock cost (USD)", telemetry.get("estimated_cost_usd")),
    ])
    # Backward-compatible keys used by the original UI helper contract.
    metrics["Retrieval seconds"] = _seconds_number(telemetry.get("retrieval_ms"))
    metrics["Generation seconds"] = _seconds_number(telemetry.get("generation_ms"))
    metrics["Total seconds"] = _seconds_number(telemetry.get("total_ms"))
    return metrics


def _seconds(value: Any) -> str | None:
    return f"{float(value) / 1000:.2f} s" if value is not None else None


def _seconds_number(value: Any) -> float | None:
    return round(float(value) / 1000, 2) if value is not None else None
