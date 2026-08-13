"""Authenticated private GPU query-embedding client."""
from __future__ import annotations

from dataclasses import dataclass
import ipaddress
import json
import math
import os
from urllib.parse import urlparse
from urllib.request import Request, urlopen

import numpy as np

MODEL_ID = "BAAI/bge-base-en-v1.5"
REVISION = "a5beb1e3e68b9ab74eb54cfd186867f64f240e1a"
DIMENSION = 768


@dataclass(frozen=True)
class RemoteEmbedder:
    base_url: str
    token: str
    timeout_seconds: float = 120.0

    @classmethod
    def from_env(cls) -> "RemoteEmbedder":
        return cls(
            os.environ["RAG_GPU_RERANKER_URL"].strip(),
            os.environ["RAG_GPU_RERANKER_TOKEN"].strip(),
            float(os.getenv("RAG_GPU_EMBEDDER_TIMEOUT_SECONDS", "120")),
        )

    def __post_init__(self) -> None:
        parsed = urlparse(self.base_url)
        if parsed.scheme != "http" or not parsed.hostname:
            raise ValueError("GPU embedder must use private HTTP")
        if not ipaddress.ip_address(parsed.hostname).is_private:
            raise ValueError("GPU embedder must use a private IP")
        if len(self.token) < 32 or not 1 <= self.timeout_seconds <= 600:
            raise ValueError("invalid GPU embedder configuration")

    def get_embedding_dimension(self) -> int:
        return DIMENSION

    def encode(self, sentences, **kwargs):
        if kwargs.get("normalize_embeddings") is not True:
            raise ValueError("remote embeddings must remain normalized")
        texts = [str(value) for value in sentences]
        payload = {
            "schema_version": 1,
            "model_id": MODEL_ID,
            "revision": REVISION,
            "texts": texts,
        }
        request = Request(
            self.base_url.rstrip("/") + "/v1/embed",
            data=json.dumps(payload, separators=(",", ":")).encode(),
            method="POST",
            headers={
                "Authorization": f"Bearer {self.token}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            },
        )
        with urlopen(request, timeout=self.timeout_seconds) as response:
            result = json.loads(response.read().decode())
        if (
            result.get("schema_version") != 1
            or result.get("model_id") != MODEL_ID
            or result.get("revision") != REVISION
            or result.get("dimension") != DIMENSION
        ):
            raise RuntimeError("remote embedding identity mismatch")
        vectors = np.asarray(result.get("vectors"), dtype=np.float32)
        if vectors.shape != (len(texts), DIMENSION) or not np.isfinite(vectors).all():
            raise RuntimeError("remote embedding payload is invalid")
        return vectors
