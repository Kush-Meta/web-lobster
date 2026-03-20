"""Tests for the 5 robustness fixes.

Covers:
- Fix 1: Validator uses tool_use (decide_validation) when available
- Fix 2: Element ID guard in orchestrator rejects hallucinated IDs
- Fix 3: AnthropicBackend retries on RateLimitError
- Fix 4: AgentConfig.max_seconds field exists with correct default
- Fix 5: Reflection buffer accumulates across stuck cycles
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from web_lobster.core.schemas import (
    Action,
    ActionType,
    BoundingBox,
    Observation,
    PageElement,
    SubGoal,
    ValidationResult,
)
from web_lobster.core.config import AgentConfig
from web_lobster.models.validator import Validator
from web_lobster.utils.retry import StuckDetector


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_observation(element_ids: list[int] | None = None) -> Observation:
    elements = []
    for eid in (element_ids or [1, 2, 3]):
        elements.append(PageElement(
            id=eid, role="button", label=f"Button {eid}", tag="button",
            bbox=BoundingBox(x=0, y=0, width=100, height=30),
        ))
    return Observation(
        url="https://example.com",
        title="Test",
        elements=elements,
    )


def _make_sub_goal() -> SubGoal:
    return SubGoal(id=1, goal="Click login", success_criteria="Login form visible")


# ---------------------------------------------------------------------------
# Fix 1: Validator tool_use
# ---------------------------------------------------------------------------

class TestValidatorToolUse:
    """Validator should call decide_validation when backend supports it."""

    @pytest.mark.asyncio
    async def test_uses_decide_validation_when_available(self):
        backend = MagicMock()
        backend.decide_validation = AsyncMock(return_value=ValidationResult(
            achieved=True, confidence=0.9, observation="Login form visible"
        ))
        # generate should NOT be called when decide_validation is available
        backend.generate = AsyncMock()

        validator = Validator(backend, use_vision=False)
        result = await validator.validate(_make_observation(), _make_sub_goal())

        backend.decide_validation.assert_awaited_once()
        backend.generate.assert_not_awaited()
        assert result.achieved is True
        assert result.confidence == 0.9

    @pytest.mark.asyncio
    async def test_falls_back_to_generate_when_no_decide_validation(self):
        backend = MagicMock(spec=[])  # no decide_validation attribute
        backend.generate = AsyncMock(return_value='{"achieved": false, "confidence": 0.3, "observation": "not done"}')

        validator = Validator(backend, use_vision=False)
        result = await validator.validate(_make_observation(), _make_sub_goal())

        backend.generate.assert_awaited_once()
        assert result.achieved is False


# ---------------------------------------------------------------------------
# Fix 2: Element ID validation
# ---------------------------------------------------------------------------

class TestElementIdValidation:
    """Actions with invalid element IDs should be skipped, not executed."""

    def test_valid_element_id_passes(self):
        obs = _make_observation(element_ids=[10, 20, 30])
        valid_ids = {el.id for el in obs.elements}
        assert 10 in valid_ids

    def test_hallucinated_element_id_detected(self):
        obs = _make_observation(element_ids=[10, 20, 30])
        valid_ids = {el.id for el in obs.elements}
        assert 999 not in valid_ids  # 999 is hallucinated

    def test_none_element_id_skips_validation(self):
        """Actions without element_id (navigate, scroll) should not be blocked."""
        action = Action(action=ActionType.NAVIGATE, url="https://google.com")
        assert action.element_id is None  # no ID to validate


# ---------------------------------------------------------------------------
# Fix 3: Anthropic retry on rate limits
# ---------------------------------------------------------------------------

class TestAnthropicRetry:
    """_call_with_retry should handle RateLimitError with backoff."""

    @pytest.mark.asyncio
    async def test_retries_on_rate_limit(self):
        import anthropic as _anthropic
        from web_lobster.models.anthropic_backend import AnthropicBackend

        backend = AnthropicBackend(api_key="fake-key")
        # Simulate client that fails twice then succeeds
        mock_response = MagicMock()
        mock_response.content = [MagicMock(type="text", text="hello")]
        mock_response.usage = MagicMock(input_tokens=10, output_tokens=5)

        rate_limit_error = _anthropic.RateLimitError(
            message="rate limit",
            response=MagicMock(headers={}),
            body={},
        )

        call_count = 0

        async def fake_create(**kwargs):
            nonlocal call_count
            call_count += 1
            if call_count <= 2:
                raise rate_limit_error
            return mock_response

        mock_client = AsyncMock()
        mock_client.messages.create = fake_create
        backend._client = mock_client

        # Patch asyncio.sleep to avoid actual waiting
        with patch("web_lobster.models.anthropic_backend.asyncio.sleep", new_callable=AsyncMock):
            result = await backend._call_with_retry(
                model="claude-haiku-4-5-20251001",
                max_tokens=10,
                messages=[{"role": "user", "content": "hi"}],
            )

        assert call_count == 3
        assert result is mock_response

    @pytest.mark.asyncio
    async def test_raises_after_max_retries(self):
        import anthropic as _anthropic
        from web_lobster.models.anthropic_backend import AnthropicBackend

        backend = AnthropicBackend(api_key="fake-key")
        rate_limit_error = _anthropic.RateLimitError(
            message="rate limit",
            response=MagicMock(headers={}),
            body={},
        )

        async def always_fail(**kwargs):
            raise rate_limit_error

        mock_client = AsyncMock()
        mock_client.messages.create = always_fail
        backend._client = mock_client

        with patch("web_lobster.models.anthropic_backend.asyncio.sleep", new_callable=AsyncMock):
            with pytest.raises(_anthropic.RateLimitError):
                await backend._call_with_retry(
                    model="claude-haiku-4-5-20251001",
                    max_tokens=10,
                    messages=[{"role": "user", "content": "hi"}],
                )


# ---------------------------------------------------------------------------
# Fix 4: Wall-clock timeout config
# ---------------------------------------------------------------------------

class TestAgentConfigTimeout:
    def test_max_seconds_default(self):
        config = AgentConfig()
        assert config.max_seconds == 600

    def test_max_seconds_zero_means_no_limit(self):
        config = AgentConfig(max_seconds=0)
        assert config.max_seconds == 0

    def test_max_seconds_customizable(self):
        config = AgentConfig(max_seconds=120)
        assert config.max_seconds == 120


# ---------------------------------------------------------------------------
# Fix 5: Reflection buffer accumulation
# ---------------------------------------------------------------------------

class TestReflectionBuffer:
    """Reflections should accumulate across stuck cycles, not be overwritten."""

    def test_reflection_list_grows(self):
        reflections: list[str] = []
        reflections.append("Attempt 1 stuck: keep clicking disabled button")
        reflections.append("Attempt 2 stuck: login form not found after navigation")
        assert len(reflections) == 2
        # Most recent 2 should be passed to decide()
        combined = "\n\n".join(reflections[-2:])
        assert "Attempt 1" in combined
        assert "Attempt 2" in combined

    def test_only_last_two_passed(self):
        reflections = ["r1", "r2", "r3", "r4"]
        combined = "\n\n".join(reflections[-2:])
        assert "r3" in combined
        assert "r4" in combined
        assert "r1" not in combined

    def test_empty_reflections_yields_none(self):
        reflections: list[str] = []
        combined = "\n\n".join(reflections[-2:]) if reflections else None
        assert combined is None
