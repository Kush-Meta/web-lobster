"""Validator model — checks if a sub-goal was achieved using vision.

Takes a screenshot and the success criteria, returns a confidence-scored
assessment. This is the component that decides whether to advance,
retry, or escalate to re-planning.
"""

from __future__ import annotations

import json
from typing import Optional

from web_lobster.core.schemas import Observation, SubGoal, ValidationResult
from web_lobster.models.base import ModelBackend
from web_lobster.utils.logging import get_logger

logger = get_logger(__name__)

VALIDATOR_SYSTEM = """You are a web page validator. You will be shown a screenshot
of a web page and a success criteria. Your job is to determine whether
the criteria has been met based on what you see.

Be precise. Only mark as achieved if you can clearly see evidence of success.

Respond ONLY with a JSON object:
{
  "achieved": true or false,
  "confidence": 0.0 to 1.0,
  "observation": "brief description of what you see on the page"
}"""

VALIDATOR_TEXT_SYSTEM = """You are a web page validator. You will be given information
about the current state of a web page and a success criteria. Your job is to
determine whether the criteria has been met.

Be precise. Only mark as achieved if the evidence clearly shows success.

Respond ONLY with a JSON object:
{
  "achieved": true or false,
  "confidence": 0.0 to 1.0,
  "observation": "brief description of what the page shows"
}"""


class Validator:
    """Vision-based sub-goal achievement checker."""

    def __init__(
        self,
        backend: ModelBackend,
        temperature: float = 0.1,
        use_vision: bool = True,
    ):
        self.backend = backend
        self.temperature = temperature
        self.use_vision = use_vision

    async def validate(
        self,
        observation: Observation,
        sub_goal: SubGoal,
    ) -> ValidationResult:
        """Check if the sub-goal's success criteria is met."""

        prompt = f"""SUB-GOAL: {sub_goal.goal}
SUCCESS CRITERIA: {sub_goal.success_criteria}

Current URL: {observation.url}
Page title: {observation.title}

Based on what you see, has the success criteria been met?"""

        # Use vision if available and screenshot exists
        images = None
        system = VALIDATOR_TEXT_SYSTEM
        if self.use_vision and observation.screenshot_base64:
            images = [observation.screenshot_base64]
            system = VALIDATOR_SYSTEM
        else:
            # Fall back to text-only with page content
            if observation.page_text:
                prompt += f"\n\nVISIBLE PAGE TEXT (truncated):\n{observation.page_text[:2000]}"
            prompt += f"\n\nINTERACTIVE ELEMENTS:\n{observation.elements_summary(20)}"

        # Use structured tool_use when available (e.g. AnthropicBackend) to
        # eliminate JSON parse failures. Fall back to text generation + parsing
        # for Ollama / llama.cpp backends that don't implement this method.
        if hasattr(self.backend, "decide_validation"):
            result = await self.backend.decide_validation(
                prompt=prompt,
                system=system,
                images=images,
                temperature=self.temperature,
                max_tokens=256,
            )
        else:
            response = await self.backend.generate(
                prompt=prompt,
                system=system,
                images=images,
                temperature=self.temperature,
                max_tokens=512,
            )
            result = self._parse_result(response)
        logger.info(
            "validation",
            goal=sub_goal.goal[:50],
            achieved=result.achieved,
            confidence=result.confidence,
        )
        return result

    def _parse_result(self, response: str) -> ValidationResult:
        """Parse the validator's JSON response."""
        text = response.strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[1] if "\n" in text else text[3:]
        if text.endswith("```"):
            text = text[:-3]
        text = text.strip()

        start = text.find("{")
        end = text.rfind("}") + 1

        if start == -1 or end == 0:
            logger.warning("validator_parse_fallback", response=text[:100])
            return ValidationResult(
                achieved=False,
                confidence=0.0,
                observation="Could not parse validator response",
            )

        try:
            raw = json.loads(text[start:end])
            return ValidationResult(
                achieved=raw.get("achieved", False),
                confidence=float(raw.get("confidence", 0.0)),
                observation=raw.get("observation", ""),
            )
        except (json.JSONDecodeError, Exception) as e:
            logger.warning("validator_parse_error", error=str(e))
            return ValidationResult(
                achieved=False,
                confidence=0.0,
                observation=f"Parse error: {e}",
            )
