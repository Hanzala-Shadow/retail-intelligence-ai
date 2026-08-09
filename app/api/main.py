"""FastAPI surface with startup preloading and a bounded daily request budget."""
from __future__ import annotations

import datetime as dt
import logging
import os
import threading
import uuid
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

from app.api.schemas import ChatRequest
from app.services.chat_service import ChatService
from app.services.generation_contract import MODEL_ID, MODEL_VARIANT, REGION, SYSTEM_SHA256
from app.services.generation_service import GenerationService
from app.services.readiness_service import readiness
from app.services.retrieval_service import (
    preload_routing_metadata, resident_bi_encoder, retrieve, routing_catalog_stats,
)

LOGGER = logging.getLogger("annual_filings_chatbot")
CHAT_SLOTS = threading.BoundedSemaphore(
    value=int(os.getenv("CHATBOT_MAX_CONCURRENT_REQUESTS", "1"))
)
_BUDGET_LOCK = threading.Lock()
_BUDGET_DAY = dt.datetime.now(dt.timezone.utc).date()
_BUDGET_USED = 0


def reserve_daily_request() -> tuple[bool, int, int]:
    """Reserve one generation request against the process-local UTC budget."""
    global _BUDGET_DAY, _BUDGET_USED
    limit = max(1, int(os.getenv("CHATBOT_DAILY_REQUEST_LIMIT", "100")))
    today = dt.datetime.now(dt.timezone.utc).date()
    with _BUDGET_LOCK:
        if today != _BUDGET_DAY:
            _BUDGET_DAY, _BUDGET_USED = today, 0
        if _BUDGET_USED >= limit:
            return False, _BUDGET_USED, limit
        _BUDGET_USED += 1
        return True, _BUDGET_USED, limit


def create_app(chat_service: ChatService | None = None) -> FastAPI:
    service = chat_service

    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        if chat_service is None:
            LOGGER.info("preloading remote query encoder and routing catalog")
            resident_bi_encoder()
            preload_routing_metadata()
            LOGGER.info("chatbot startup dependencies ready")
        yield

    app = FastAPI(title="Annual Filings Chatbot API", version="1.3.0", lifespan=lifespan)

    def active_service() -> ChatService:
        nonlocal service
        if service is None:
            service = ChatService(retrieve, GenerationService(region=REGION))
        return service

    @app.exception_handler(Exception)
    async def safe_error(_request: Request, error: Exception):
        request_id = str(uuid.uuid4())
        LOGGER.exception("request_id=%s status=internal_error", request_id)
        return JSONResponse(status_code=500, content={
            "request_id": request_id, "status": "generation_failed",
            "message": "Request failed; inspect the protected server log",
        })

    @app.get("/api/health/live")
    def live():
        return {"status": "live", "schema_version": 1}

    @app.get("/api/health")
    def health():
        result = readiness()
        result["environment"] = os.getenv("CHATBOT_ENV", "staging")
        result["routing_catalog"] = routing_catalog_stats()
        if result["status"] != "ready" or not result["routing_catalog"]["ready"]:
            result["status"] = "degraded"
            return JSONResponse(status_code=503, content=result)
        return result

    @app.get("/api/models")
    def models():
        return {
            "generator": {"model_id": MODEL_ID, "variant": MODEL_VARIANT,
                          "region": REGION, "system_sha256": SYSTEM_SHA256},
            "retrieval": {"policy_id": "balanced_anchored_round_robin_k16",
                          "evidence_limit": 16, "reranker_batch_size": 32},
        }

    @app.post("/api/chat")
    def chat(payload: ChatRequest):
        request_id = str(uuid.uuid4())
        if not CHAT_SLOTS.acquire(blocking=False):
            return JSONResponse(status_code=429, content={
                "request_id": request_id, "status": "service_busy",
                "message": "Another analysis is currently running; retry shortly",
            })
        try:
            allowed, used, limit = reserve_daily_request()
            if not allowed:
                return JSONResponse(status_code=429, content={
                    "request_id": request_id, "status": "daily_limit_reached",
                    "message": "The daily testing limit has been reached",
                })
            LOGGER.info("request_id=%s daily_budget=%s/%s", request_id, used, limit)
            result = active_service().answer(payload.question, request_id)
        except TimeoutError as error:
            raise HTTPException(status_code=504, detail="Request timed out") from error
        finally:
            CHAT_SLOTS.release()
        LOGGER.info(
            "request_id=%s status=%s total_ms=%s retrieval_ms=%s routing_ms=%s",
            request_id, result.get("status"),
            (result.get("telemetry") or {}).get("total_ms"),
            (result.get("telemetry") or {}).get("retrieval_ms"),
            (result.get("telemetry") or {}).get("routing_orchestration_ms"),
        )
        return result

    return app


app = create_app()
