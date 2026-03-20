"""Anthropic Claude backend — uses the Anthropic Python SDK.

Advantages over local backends:
- Claude tool_use for the executor: guaranteed-valid Action object, zero parse failures.
- Reliable vision via image content blocks (for validator if needed).
- Sonnet for deep reasoning (planner), Haiku for speed (executor).
"""

from __future__ import annotations

import asyncio
import os
from typing import Any, Optional

import anthropic

from web_lobster.core.schemas import Action, ActionType, ValidationResult
from web_lobster.models.base import ModelBackend
from web_lobster.utils.logging import get_logger

logger = get_logger(__name__)


# ---------------------------------------------------------------------------
# Tool schema — mirrors the Action Pydantic model exactly.
# Claude tool_use with tool_choice forced means Claude MUST call this tool,
# returning a structurally valid dict that maps 1:1 onto Action fields.
# ---------------------------------------------------------------------------

DECIDE_ACTION_TOOL = {
    "name": "decide_action",
    "description": (
        "Choose the single next browser action to take toward the current sub-goal. "
        "You MUST call this tool — do not respond with plain text."
    ),
    "input_schema": {
        "type": "object",
        "required": ["action"],
        "properties": {
            "action": {
                "type": "string",
                "enum": [
                    "click",
                    "type",
                    "scroll",
                    "navigate",
                    "wait",
                    "done",
                    "select",
                    "hover",
                    "go_back",
                ],
                "description": "The type of browser action to perform.",
            },
            "element_id": {
                "type": "integer",
                "description": (
                    "The numeric ID of the interactive element to act on. "
                    "Required for: click, type, select, hover."
                ),
            },
            "text": {
                "type": "string",
                "description": (
                    "Text to type (for 'type' action) or option label "
                    "(for 'select' action)."
                ),
            },
            "direction": {
                "type": "string",
                "enum": ["up", "down"],
                "description": "Scroll direction. Required for 'scroll' action.",
            },
            "url": {
                "type": "string",
                "description": "Destination URL. Required for 'navigate' action.",
            },
            "reason": {
                "type": "string",
                "description": (
                    "Human-readable reason. Required for 'done'; optional elsewhere."
                ),
            },
            "seconds": {
                "type": "number",
                "description": "Seconds to wait. Required for 'wait' action.",
            },
        },
        "additionalProperties": False,
    },
}


VALIDATE_RESULT_TOOL = {
    "name": "validate_result",
    "description": (
        "Report whether the sub-goal success criteria has been met. "
        "You MUST call this tool — do not respond with plain text."
    ),
    "input_schema": {
        "type": "object",
        "required": ["achieved", "confidence", "observation"],
        "properties": {
            "achieved": {
                "type": "boolean",
                "description": "True only if the success criteria is clearly met.",
            },
            "confidence": {
                "type": "number",
                "description": "Confidence score between 0.0 and 1.0.",
            },
            "observation": {
                "type": "string",
                "description": "Brief description of what you see on the page.",
            },
        },
        "additionalProperties": False,
    },
}


class AnthropicBackend(ModelBackend):
    """Backend using the Anthropic API (Claude models).

    Requires: pip install anthropic>=0.40.0
    And either ANTHROPIC_API_KEY env var or api_key= constructor arg.
    """

    def __init__(
        self,
        model: str = "claude-haiku-4-5-20251001",
        api_key: Optional[str] = None,
        timeout: float = 120.0,
        max_retries: int = 2,
    ):
        self.model = model
        self._api_key = api_key or os.environ.get("ANTHROPIC_API_KEY")
        self.timeout = timeout
        self.max_retries = max_retries
        self._client: Optional[anthropic.AsyncAnthropic] = None

    def _get_client(self) -> anthropic.AsyncAnthropic:
        if self._client is None:
            if not self._api_key:
                raise RuntimeError(
                    "Anthropic API key required. Set ANTHROPIC_API_KEY env var "
                    "or pass api_key= to AnthropicBackend."
                )
            self._client = anthropic.AsyncAnthropic(
                api_key=self._api_key,
                timeout=self.timeout,
                max_retries=self.max_retries,
            )
        return self._client

    async def generate(
        self,
        prompt: str,
        system: Optional[str] = None,
        images: Optional[list[str]] = None,  # base64-encoded PNG
        temperature: float = 0.1,
        max_tokens: int = 2048,
        grammar: Optional[str] = None,  # ignored — Claude has no GBNF
    ) -> str:
        """Text or vision generation for planner and validator roles."""
        client = self._get_client()

        # Build user content: text only, or text + image blocks for vision
        if images:
            content: list = []
            for b64 in images:
                # Detect actual format from base64 header bytes
                # JPEG starts with /9j/, PNG starts with iVBORw0KGgo
                media_type = "image/jpeg" if b64.startswith("/9j/") else "image/png"
                content.append({
                    "type": "image",
                    "source": {
                        "type": "base64",
                        "media_type": media_type,
                        "data": b64,
                    },
                })
            content.append({"type": "text", "text": prompt})
        else:
            content = prompt  # simple string shorthand

        kwargs: dict = {
            "model": self.model,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "messages": [{"role": "user", "content": content}],
        }
        if system:
            kwargs["system"] = system

        logger.debug(
            "anthropic_generate",
            model=self.model,
            prompt_len=len(prompt),
            has_images=bool(images),
        )

        response = await self._call_with_retry(**kwargs)
        text = response.content[0].text

        logger.debug(
            "anthropic_response",
            model=self.model,
            response_len=len(text),
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
        )
        return text

    async def decide_action(
        self,
        prompt: str,
        system: Optional[str] = None,
        images: Optional[list[str]] = None,
        temperature: float = 0.0,
        max_tokens: int = 256,
    ) -> Action:
        """Executor-specific: uses tool_use to return a guaranteed-valid Action.

        By forcing tool_choice to this specific tool, Claude MUST call it —
        it cannot produce plain text. The tool's input_schema validates all
        fields, so we get a structurally valid dict that maps 1:1 onto Action.
        No JSON parsing, no fallbacks needed.
        """
        client = self._get_client()

        # Build content: annotated screenshot first (if available), then text prompt
        if images:
            content: list = []
            for b64 in images:
                media_type = "image/jpeg" if b64.startswith("/9j/") else "image/png"
                content.append({
                    "type": "image",
                    "source": {"type": "base64", "media_type": media_type, "data": b64},
                })
            content.append({"type": "text", "text": prompt})
        else:
            content = prompt

        kwargs: dict = {
            "model": self.model,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "tools": [DECIDE_ACTION_TOOL],
            "tool_choice": {"type": "tool", "name": "decide_action"},
            "messages": [{"role": "user", "content": content}],
        }
        if system:
            kwargs["system"] = system

        logger.debug("anthropic_decide_action", model=self.model, prompt_len=len(prompt), has_image=bool(images))

        response = await self._call_with_retry(**kwargs)

        # Find the tool_use block — guaranteed present due to forced tool_choice
        tool_block = next(
            (b for b in response.content if b.type == "tool_use"),
            None,
        )
        if tool_block is None:
            logger.error("anthropic_no_tool_block", content=str(response.content))
            return Action(action=ActionType.WAIT, seconds=2, reason="No tool_use block")

        raw = tool_block.input  # already a dict, not a string

        logger.debug(
            "anthropic_action",
            action=raw.get("action"),
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
        )

        return Action(**raw)

    async def _call_with_retry(self, **kwargs: Any):
        """Call client.messages.create with exponential backoff for rate limits.

        The Anthropic SDK has built-in retries (max_retries=2) but they use
        short delays. For free-tier rate limits (30K tokens/min) we need up to
        4 retries with delays that respect the Retry-After header.
        """
        client = self._get_client()
        delays = [5, 10, 20, 40]
        last_exc: Exception = RuntimeError("no attempts made")
        for attempt, delay in enumerate([0] + delays, start=1):
            if delay:
                await asyncio.sleep(delay)
            try:
                return await client.messages.create(**kwargs)
            except anthropic.RateLimitError as e:
                last_exc = e
                retry_after = int(getattr(e, "response", None) and
                                  e.response.headers.get("retry-after", 0) or 0)
                wait = max(delay, retry_after or delay)
                logger.warning("rate_limit", attempt=attempt, wait=wait)
                if attempt > len(delays):
                    raise
                await asyncio.sleep(wait)
            except anthropic.APIStatusError as e:
                last_exc = e
                if e.status_code and e.status_code < 500:
                    raise  # 4xx errors (other than 429) are not retryable
                logger.warning("api_server_error", attempt=attempt, status=e.status_code)
                if attempt > len(delays):
                    raise
            except anthropic.APIConnectionError as e:
                last_exc = e
                logger.warning("api_connection_error", attempt=attempt)
                if attempt > len(delays):
                    raise
        raise last_exc

    async def decide_validation(
        self,
        prompt: str,
        system: Optional[str] = None,
        images: Optional[list[str]] = None,
        temperature: float = 0.1,
        max_tokens: int = 256,
    ) -> ValidationResult:
        """Validator-specific: uses tool_use to return a guaranteed-valid ValidationResult.

        Mirrors decide_action — by forcing Claude to call validate_result, we
        eliminate all JSON parsing fragility from the validator.
        """
        if images:
            content: list = []
            for b64 in images:
                media_type = "image/jpeg" if b64.startswith("/9j/") else "image/png"
                content.append({
                    "type": "image",
                    "source": {"type": "base64", "media_type": media_type, "data": b64},
                })
            content.append({"type": "text", "text": prompt})
        else:
            content = prompt

        kwargs: dict = {
            "model": self.model,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "tools": [VALIDATE_RESULT_TOOL],
            "tool_choice": {"type": "tool", "name": "validate_result"},
            "messages": [{"role": "user", "content": content}],
        }
        if system:
            kwargs["system"] = system

        logger.debug("anthropic_decide_validation", model=self.model, has_image=bool(images))

        response = await self._call_with_retry(**kwargs)

        tool_block = next(
            (b for b in response.content if b.type == "tool_use"),
            None,
        )
        if tool_block is None:
            logger.error("anthropic_no_validate_block", content=str(response.content))
            return ValidationResult(achieved=False, confidence=0.0, observation="No tool_use block")

        raw = tool_block.input
        logger.debug(
            "anthropic_validation",
            achieved=raw.get("achieved"),
            confidence=raw.get("confidence"),
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
        )
        return ValidationResult(
            achieved=bool(raw.get("achieved", False)),
            confidence=float(max(0.0, min(1.0, raw.get("confidence", 0.0)))),
            observation=str(raw.get("observation", "")),
        )

    async def is_available(self) -> bool:
        if not self._api_key and not os.environ.get("ANTHROPIC_API_KEY"):
            return False
        try:
            client = self._get_client()
            await client.messages.create(
                model=self.model,
                max_tokens=1,
                messages=[{"role": "user", "content": "hi"}],
            )
            return True
        except Exception as e:
            logger.warning("anthropic_unavailable", error=str(e))
            return False
