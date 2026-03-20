"""Ollama backend — talks to a running Ollama server.

Handles both text-only and vision (multimodal) models.
"""

from __future__ import annotations

import json
from typing import Optional

import httpx

from web_lobster.models.base import ModelBackend
from web_lobster.utils.logging import get_logger

logger = get_logger(__name__)


class OllamaBackend(ModelBackend):
    """Client for the Ollama REST API."""

    def __init__(
        self,
        model: str = "qwen2.5:7b",
        base_url: str = "http://localhost:11434",
        timeout: float = 120.0,
    ):
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._client: Optional[httpx.AsyncClient] = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=self.timeout)
        return self._client

    async def generate(
        self,
        prompt: str,
        system: Optional[str] = None,
        images: Optional[list[str]] = None,
        temperature: float = 0.1,
        max_tokens: int = 2048,
        grammar: Optional[str] = None,  # ignored for Ollama
    ) -> str:
        client = await self._get_client()

        # Build the messages array (chat endpoint for better control)
        messages = []
        if system:
            messages.append({"role": "system", "content": system})

        user_message: dict = {"role": "user", "content": prompt}
        if images:
            user_message["images"] = images  # Ollama expects base64 strings
        messages.append(user_message)

        payload = {
            "model": self.model,
            "messages": messages,
            "stream": False,
            "options": {
                "temperature": temperature,
                "num_predict": max_tokens,
            },
        }

        logger.debug(
            "ollama_request",
            model=self.model,
            prompt_len=len(prompt),
            has_images=bool(images),
        )

        response = await client.post(
            f"{self.base_url}/api/chat",
            json=payload,
        )
        response.raise_for_status()
        data = response.json()

        content = data.get("message", {}).get("content", "")
        logger.debug(
            "ollama_response",
            model=self.model,
            response_len=len(content),
            eval_duration_ms=data.get("eval_duration", 0) / 1_000_000,
        )
        return content

    async def is_available(self) -> bool:
        try:
            client = await self._get_client()
            resp = await client.get(f"{self.base_url}/api/tags")
            if resp.status_code != 200:
                return False
            tags = resp.json()
            model_names = [m["name"] for m in tags.get("models", [])]
            # Check if our model (or a prefix of it) is available
            base_name = self.model.split(":")[0]
            return any(base_name in name for name in model_names)
        except Exception:
            return False

    async def close(self):
        if self._client and not self._client.is_closed:
            await self._client.aclose()
