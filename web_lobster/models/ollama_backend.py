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


class ModelNotInstalled(RuntimeError):
    """The model this run needs hasn't been pulled. Retrying won't help."""


class OllamaBackend(ModelBackend):
    """Client for the Ollama REST API."""

    def __init__(
        self,
        model: str = "qwen2.5:7b",
        base_url: str = "http://localhost:11434",
        timeout: float = 120.0,
        context_window: Optional[int] = None,
    ):
        self.model = model
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        # Ollama silently truncates prompts longer than its context window.
        self.context_window = context_window
        self._client: Optional[httpx.AsyncClient] = None
        # Set once the model turns out to reject images; they're dropped from then on.
        self._text_only = False

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
        if images and not self._text_only:
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

        if self.context_window:
            payload["options"]["num_ctx"] = self.context_window

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
        if "images" in user_message and response.status_code == 400 and "multimodal" in response.text:
            # A text-only model: remember that and send the prompt without images.
            logger.warning("ollama_model_is_text_only", model=self.model)
            self._text_only = True
            user_message.pop("images")
            response = await client.post(f"{self.base_url}/api/chat", json=payload)
        if response.status_code == 404 and "not found" in response.text:
            raise ModelNotInstalled(await self._missing_model_message())
        if response.is_error:
            raise RuntimeError(f"Ollama returned {response.status_code} for {self.model}: {response.text[:200]}")
        data = response.json()

        content = data.get("message", {}).get("content", "")
        logger.debug(
            "ollama_response",
            model=self.model,
            response_len=len(content),
            eval_duration_ms=data.get("eval_duration", 0) / 1_000_000,
        )
        return content

    async def _missing_model_message(self) -> str:
        """What to tell someone whose model isn't there.

        Ollama answers a request for a model it hasn't pulled with a 404, and
        that used to surface as a raw error after the brief, the browser, and
        two retries. The dashboard offers models nobody has pulled, so this is
        easy to hit; naming the ones that are here is most of the fix.
        """
        installed = await self.installed_models()
        if not installed:
            return f"Ollama isn't answering at {self.base_url}. Is it running?"
        return (
            f"Ollama hasn't pulled {self.model!r}. It has: {', '.join(installed)}. "
            f"Run `ollama pull {self.model}`, or choose one of those."
        )

    async def installed_models(self) -> list[str]:
        """Every model this Ollama has pulled, or [] if it can't be reached."""
        try:
            client = await self._get_client()
            resp = await client.get(f"{self.base_url}/api/tags")
            if resp.status_code != 200:
                return []
            return sorted(model["name"] for model in resp.json().get("models", []))
        except Exception:
            return []

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
