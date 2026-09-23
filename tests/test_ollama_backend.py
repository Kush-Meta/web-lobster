"""Tests for the Ollama backend against a fake Ollama server."""

from __future__ import annotations

import json

import httpx
import pytest

from web_lobster.core.config import WebLobsterConfig
from web_lobster.core.orchestrator import Orchestrator
from web_lobster.models.ollama_backend import ModelNotInstalled, OllamaBackend


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
    backend, _ = _backend(lambda body: httpx.Response(500, json={"error": "out of memory"}))
    with pytest.raises(RuntimeError, match="500.*out of memory"):
        await backend.generate("hi")


async def test_a_model_that_was_never_pulled_says_so_and_names_what_is_there():
    """A dashboard run picked a 72B model nobody had pulled and died on a raw 404."""
    def route(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/tags":
            return httpx.Response(200, json={"models": [{"name": "qwen2.5-coder:7b"}, {"name": "llama3:latest"}]})
        return httpx.Response(404, json={"error": "model 'qwen2.5:72b' not found"})

    backend = OllamaBackend(model="qwen2.5:72b")
    backend._client = httpx.AsyncClient(transport=httpx.MockTransport(route))
    with pytest.raises(ModelNotInstalled) as raised:
        await backend.generate("hi")
    message = str(raised.value)
    assert "hasn't pulled 'qwen2.5:72b'" in message
    assert "llama3:latest, qwen2.5-coder:7b" in message      # what this machine has, in order
    assert "ollama pull qwen2.5:72b" in message              # and how to fix it


async def test_an_ollama_that_is_not_running_says_that_instead():
    def route(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/tags":
            raise httpx.ConnectError("connection refused")
        return httpx.Response(404, json={"error": "model 'x' not found"})

    backend = OllamaBackend(model="x")
    backend._client = httpx.AsyncClient(transport=httpx.MockTransport(route))
    with pytest.raises(ModelNotInstalled, match="isn't answering"):
        await backend.generate("hi")


def test_validator_vision_follows_config():
    config = WebLobsterConfig()
    config.validator.vision = False
    assert Orchestrator(config).validator.use_vision is False
    assert Orchestrator(WebLobsterConfig()).validator.use_vision is True


def test_the_dashboard_offers_the_models_this_machine_has(monkeypatch):
    """It offered a 72B model nobody had pulled, and the run died once the
    browser was already open."""
    from fastapi.testclient import TestClient

    from web_lobster.ui.server import app

    async def installed(self):
        return ["qwen2.5-coder:7b"]

    monkeypatch.setattr(OllamaBackend, "installed_models", installed)
    assert TestClient(app).get("/api/models/installed").json() == {"models": ["qwen2.5-coder:7b"]}

