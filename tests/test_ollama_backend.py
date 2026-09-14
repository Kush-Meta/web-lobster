"""Tests for the Ollama backend against a fake Ollama server."""

from __future__ import annotations

import json

import httpx
import pytest

from web_lobster.core.config import WebLobsterConfig
from web_lobster.core.orchestrator import Orchestrator
from web_lobster.models.ollama_backend import OllamaBackend


def _backend(handler) -> tuple[OllamaBackend, list[dict]]:
    requests: list[dict] = []

    def record(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        requests.append(body)
        return handler(body)

    backend = OllamaBackend(model="qwen2.5-coder:7b")
    backend._client = httpx.AsyncClient(transport=httpx.MockTransport(record))
    return backend, requests


def _reply(text: str) -> httpx.Response:
    return httpx.Response(200, json={"message": {"content": text}})


async def test_text_only_model_retries_without_images_and_remembers():
    def handler(body):
        if "images" in body["messages"][-1]:
            return httpx.Response(400, json={"error": {"message": (
                "Multimodal data provided, but model does not support multimodal requests."
            )}})
        return _reply("ok")

    backend, requests = _backend(handler)
    assert await backend.generate("describe", images=["aGVsbG8="]) == "ok"
    assert await backend.generate("again", images=["aGVsbG8="]) == "ok"
    # First call: one rejected request and a retry; second call skips images up front.
    assert ["images" in r["messages"][-1] for r in requests] == [True, False, False]


async def test_context_window_is_sent_as_num_ctx():
    backend, requests = _backend(lambda body: _reply("ok"))
    await backend.generate("hi")
    backend.context_window = 16384
    await backend.generate("hi")
    assert "num_ctx" not in requests[0]["options"]
    assert requests[1]["options"]["num_ctx"] == 16384


async def test_other_errors_carry_the_server_message():
    backend, _ = _backend(lambda body: httpx.Response(404, json={"error": "model 'nope' not found"}))
    with pytest.raises(RuntimeError, match="404.*model 'nope' not found"):
        await backend.generate("hi")


def test_validator_vision_follows_config():
    config = WebLobsterConfig()
    config.validator.vision = False
    assert Orchestrator(config).validator.use_vision is False
    assert Orchestrator(WebLobsterConfig()).validator.use_vision is True
