"""Bounded localhost client that preserves degraded readiness responses."""
from __future__ import annotations

import os
from typing import Any

DEFAULT_API_URL = "http://127.0.0.1:8000"


class ChatbotApiError(RuntimeError):
    pass


class ChatbotApiClient:
    def __init__(self, base_url=None, timeout_seconds=None, session=None):
        self.base_url = (base_url or os.getenv("CHATBOT_API_URL", DEFAULT_API_URL)).rstrip("/")
        self.timeout_seconds = float(timeout_seconds or os.getenv("CHATBOT_UI_TIMEOUT_SECONDS", "600"))
        if session is None:
            import requests
            session = requests.Session()
        self.session = session

    def health(self) -> dict[str, Any]:
        try:
            response = self.session.get(f"{self.base_url}/api/health", timeout=10)
            data = response.json()
        except Exception as error:
            raise ChatbotApiError("The chatbot service is currently unavailable") from error
        if response.status_code in {200, 503} and isinstance(data, dict):
            return data
        return self._decode(response)

    def models(self):
        return self._get("/api/models")

    def chat(self, question, conversation_id=None):
        try:
            response = self.session.post(
                f"{self.base_url}/api/chat",
                json={"question": question, "conversation_id": conversation_id,
                      "show_debug": False},
                timeout=self.timeout_seconds,
            )
        except Exception as error:
            raise ChatbotApiError("The chatbot API is unavailable") from error
        return self._decode(response)

    def _get(self, path):
        try:
            response = self.session.get(f"{self.base_url}{path}", timeout=10)
        except Exception as error:
            raise ChatbotApiError("The chatbot API is unavailable") from error
        return self._decode(response)

    @staticmethod
    def _decode(response):
        try:
            data = response.json()
        except ValueError as error:
            raise ChatbotApiError("The chatbot API returned invalid JSON") from error
        if response.status_code >= 400:
            request_id = data.get("request_id") or "unavailable"
            message = data.get("message") or data.get("detail") or "Request failed"
            raise ChatbotApiError(f"{message} (request ID: {request_id})")
        if not isinstance(data, dict):
            raise ChatbotApiError("The chatbot API returned an invalid response")
        return data
