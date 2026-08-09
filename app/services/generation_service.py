"""Explicit-region Bedrock DeepSeek generation service."""
from __future__ import annotations

import time
from typing import Any

from app.services.citation_validator import validate_answer
from app.services.generation_contract import (
    INPUT_PRICE_PER_MILLION, MODEL_ID, MODEL_VARIANT,
    OUTPUT_PRICE_PER_MILLION, REGION, SYSTEM, complexity, request_prompt,
)
from app.services.semantic_guard import guarded_prompt


def response_text(response: dict[str, Any]) -> str:
    blocks = response.get("output", {}).get("message", {}).get("content", [])
    return "\n".join(block["text"] for block in blocks if "text" in block).strip()


class GenerationService:
    def __init__(self, client: Any | None = None, region: str = REGION):
        self.region = region
        if client is None:
            import boto3
            from botocore.config import Config
            client = boto3.client(
                "bedrock-runtime", region_name=region,
                config=Config(retries={"max_attempts": 1, "mode": "standard"}),
            )
        self.client = client

    def generate(self, row: dict[str, Any]) -> dict[str, Any]:
        max_tokens, word_budget = complexity(row)
        started = time.perf_counter()
        response = self.client.converse(
            modelId=MODEL_ID,
            system=[{"text": SYSTEM}],
            messages=[{"role": "user", "content": [{"text": guarded_prompt(request_prompt(row))}]}],
            inferenceConfig={"maxTokens": max_tokens, "temperature": 0},
        )
        latency_ms = round((time.perf_counter() - started) * 1000, 3)
        answer = response_text(response)
        usage = response.get("usage", {})
        input_tokens = int(usage.get("inputTokens", 0))
        output_tokens = int(usage.get("outputTokens", 0))
        validation = validate_answer(
            answer, row["evidence"], word_budget, response.get("stopReason")
        )
        cost = round(
            input_tokens / 1_000_000 * INPUT_PRICE_PER_MILLION
            + output_tokens / 1_000_000 * OUTPUT_PRICE_PER_MILLION,
            6,
        )
        return {
            "answer": answer, "model_id": MODEL_ID, "variant": MODEL_VARIANT,
            "region": self.region, "input_tokens": input_tokens,
            "output_tokens": output_tokens, "stop_reason": response.get("stopReason"),
            "generation_ms": latency_ms, "estimated_cost_usd": cost,
            "validation": validation,
        }
