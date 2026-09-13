"""Tests for the value extractor and the Anthropic structured-output call it uses."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

from web_lobster.core.schemas import Observation
from web_lobster.core.values import ValueSpec, ValueType
from web_lobster.models.anthropic_backend import AnthropicBackend
from web_lobster.models.extractor import Extractor

SPECS = [
    ValueSpec(name="price", type=ValueType.NUMBER),
    ValueSpec(name="cabin", type=ValueType.CHOICE, choices=["Economy", "Business"]),
    ValueSpec(name="nonstop", type=ValueType.BOOLEAN),
]


def _observation() -> Observation:
    return Observation(
        url="https://fares.example/results?session=abc",
        title="Fares",
        page_text="Economy nonstop from $412",
    )


def _tool_response(payload: dict) -> SimpleNamespace:
    return SimpleNamespace(
        content=[SimpleNamespace(type="tool_use", name="report_values", input=payload)],
        usage=SimpleNamespace(input_tokens=10, output_tokens=5),
    )


class TestAnthropicStructured:
    async def test_forces_the_tool_and_returns_its_input(self):
        backend = AnthropicBackend(model="claude-haiku-4-5-20251001", api_key="test")
        backend._call_with_retry = AsyncMock(return_value=_tool_response({"price": 412}))
        tool = {"name": "report_values", "input_schema": {"type": "object"}}

        result = await backend.structured(prompt="read", tool=tool, system="sys")

        assert result == {"price": 412}
        kwargs = backend._call_with_retry.call_args.kwargs
        assert kwargs["tool_choice"] == {"type": "tool", "name": "report_values"}
        assert kwargs["tools"] == [tool]
        assert kwargs["system"] == "sys"

    async def test_missing_tool_block_returns_empty(self):
        backend = AnthropicBackend(model="claude-haiku-4-5-20251001", api_key="test")
        backend._call_with_retry = AsyncMock(return_value=SimpleNamespace(
            content=[SimpleNamespace(type="text", text="no")],
            usage=SimpleNamespace(input_tokens=1, output_tokens=1),
        ))
        tool = {"name": "report_values", "input_schema": {"type": "object"}}
        assert await backend.structured(prompt="read", tool=tool) == {}


class TestExtractor:
    async def test_anthropic_backend_uses_structured_call(self):
        backend = AnthropicBackend(model="claude-haiku-4-5-20251001", api_key="test")
        backend._call_with_retry = AsyncMock(return_value=_tool_response(
            {"price": 412, "cabin": "economy", "nonstop": True}
        ))

        values = await Extractor(backend).extract(_observation(), SPECS)

        assert {n: v.value for n, v in values.items()} == {
            "price": 412.0, "cabin": "Economy", "nonstop": True,
        }
        assert values["price"].origin == "https://fares.example"
        schema = backend._call_with_retry.call_args.kwargs["tools"][0]["input_schema"]
        assert set(schema["properties"]) == {"price", "cabin", "nonstop"}

    async def test_local_backend_parses_json_text(self):
        backend = SimpleNamespace(generate=AsyncMock(
            return_value='```json\n{"price": "$1,209.50", "cabin": "First", "nonstop": "maybe"}\n```'
        ))

        values = await Extractor(backend).extract(_observation(), SPECS)

        # Only the value that fits its type survives.
        assert list(values) == ["price"]
        assert values["price"].value == 1209.5

    async def test_garbage_or_failure_yields_no_values(self):
        garbage = SimpleNamespace(generate=AsyncMock(return_value="ignore previous instructions"))
        assert await Extractor(garbage).extract(_observation(), SPECS) == {}

        broken = SimpleNamespace(generate=AsyncMock(side_effect=RuntimeError("down")))
        assert await Extractor(broken).extract(_observation(), SPECS) == {}

    async def test_no_specs_means_no_call(self):
        backend = SimpleNamespace(generate=AsyncMock())
        assert await Extractor(backend).extract(_observation(), []) == {}
        backend.generate.assert_not_called()

    def test_prompt_lists_values_and_page(self):
        prompt = Extractor(backend=None)._prompt(_observation(), SPECS)
        assert '"cabin": one of: Economy, Business' in prompt
        assert "Economy nonstop from $412" in prompt
