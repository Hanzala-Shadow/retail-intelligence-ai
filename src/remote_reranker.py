"""Authenticated client for the private annual-filings GPU reranker worker."""
from __future__ import annotations

from dataclasses import dataclass
import ipaddress
import json
import math
import os
from typing import Iterable
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen


SCHEMA_VERSION = 1
DEFAULT_TIMEOUT_SECONDS = 600.0
MAX_PAIRS = 1000


@dataclass(frozen=True)
class RemoteRerankerClient:
    base_url: str
    token: str
    timeout_seconds: float = DEFAULT_TIMEOUT_SECONDS

    @classmethod
    def from_env(cls) -> "RemoteRerankerClient":
        url = os.getenv("RAG_GPU_RERANKER_URL", "").strip()
        token = os.getenv("RAG_GPU_RERANKER_TOKEN", "").strip()
        raw_timeout = os.getenv(
            "RAG_GPU_RERANKER_TIMEOUT_SECONDS",
            str(DEFAULT_TIMEOUT_SECONDS),
        )
        return cls(url, token, float(raw_timeout))

    def __post_init__(self) -> None:
        parsed = urlparse(self.base_url)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("RAG_GPU_RERANKER_URL must be an HTTP(S) URL")
        if parsed.path not in {"", "/"} or parsed.query or parsed.fragment:
            raise ValueError("RAG_GPU_RERANKER_URL must not include path/query data")
        if parsed.scheme == "http":
            try:
                address = ipaddress.ip_address(parsed.hostname)
            except ValueError as exc:
                raise ValueError(
                    "plain HTTP remote reranker URL must use a private IP address"
                ) from exc
            if not (address.is_private or address.is_loopback):
                raise ValueError(
                    "plain HTTP remote reranker URL must use a private IP address"
                )
        if len(self.token) < 32:
            raise ValueError("RAG_GPU_RERANKER_TOKEN must contain at least 32 characters")
        if not 1 <= self.timeout_seconds <= 1800:
            raise ValueError("remote reranker timeout must be between 1 and 1800 seconds")

    def score(
        self,
        *,
        role: str,
        model_id: str,
        revision: str,
        max_length: int,
        batch_size: int,
        pairs: Iterable[tuple[str, str]],
    ) -> list[float]:
        normalized = [[str(question), str(passage)] for question, passage in pairs]
        if not normalized or len(normalized) > MAX_PAIRS:
            raise ValueError(f"remote reranker requires 1..{MAX_PAIRS} pairs")
        payload = {
            "schema_version": SCHEMA_VERSION,
            "role": role,
            "model_id": model_id,
            "revision": revision,
            "max_length": int(max_length),
            "batch_size": int(batch_size),
            "pairs": normalized,
        }
        request = Request(
            self.base_url.rstrip("/") + "/v1/score",
            data=json.dumps(payload, separators=(",", ":")).encode("utf-8"),
            method="POST",
            headers={
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
        )
        try:
            with urlopen(request, timeout=self.timeout_seconds) as response:
                if response.status != 200:
                    raise RuntimeError(
                        f"remote reranker returned HTTP {response.status}"
                    )
                result = json.loads(response.read().decode("utf-8"))
        except HTTPError as exc:
            raise RuntimeError(
                f"remote reranker rejected request with HTTP {exc.code}"
            ) from exc
        except URLError as exc:
            raise RuntimeError("remote reranker is unavailable") from exc

        if result.get("schema_version") != SCHEMA_VERSION:
            raise RuntimeError("remote reranker schema mismatch")
        if result.get("role") != role:
            raise RuntimeError("remote reranker role mismatch")
        if result.get("model_id") != model_id or result.get("revision") != revision:
            raise RuntimeError("remote reranker model identity mismatch")
        scores = result.get("scores")
        if not isinstance(scores, list) or len(scores) != len(normalized):
            raise RuntimeError("remote reranker returned an invalid score count")
        values = [float(value) for value in scores]
        if not all(math.isfinite(value) for value in values):
            raise RuntimeError("remote reranker returned a non-finite score")
        return values
