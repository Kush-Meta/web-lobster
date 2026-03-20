"""Executor model — decides the next single browser action.

Called every step of the agent loop. Optimized for speed.
Uses grammar constraints (via llama.cpp) when available to guarantee valid output.
"""

from __future__ import annotations

import json
from typing import Optional

from web_lobster.core.schemas import Action, Observation, SubGoal
from web_lobster.models.base import ModelBackend
from web_lobster.models.llamacpp_backend import LlamaCppBackend
from web_lobster.models.anthropic_backend import AnthropicBackend
from web_lobster.utils.logging import get_logger

logger = get_logger(__name__)

EXECUTOR_SYSTEM = """You are a browser automation executor. You see the current page state
and must choose exactly ONE action to progress toward the goal.

Rules:
- Pick the single most useful next action
- Prefer clicking labeled buttons/links over typing URLs
- If a form field needs text, use "type" with the element_id
- Use "scroll" if the target element might be below the fold
- Use "wait" if the page is loading (e.g. after navigation)
- Never repeat the same failed action — try an alternative element or approach
- CRITICAL: Call "done" ONLY after you have personally taken at least one meaningful
  action (click, type, navigate, etc.) toward this sub-goal in this attempt, AND the
  success criteria is now clearly met. The ONE exception: if the page already satisfies
  the success criteria exactly as-is when you first see it (e.g. you are already on the
  correct page), you may call "done" immediately.

Available actions:
  {"action": "click", "element_id": N}
  {"action": "type", "element_id": N, "text": "..."}
  {"action": "scroll", "direction": "down"|"up"}
  {"action": "navigate", "url": "https://..."}
  {"action": "wait", "seconds": N}
  {"action": "select", "element_id": N, "text": "option text"}
  {"action": "hover", "element_id": N}
  {"action": "go_back"}
  {"action": "done", "reason": "brief description of what was accomplished"}

Respond with ONLY a single JSON action object. No explanation."""


class Executor:
    """Single-action decision model for the agent loop."""

    def __init__(
        self,
        backend: ModelBackend,
        temperature: float = 0.0,
        use_grammar: bool = True,
    ):
        self.backend = backend
        self.temperature = temperature
        self.use_grammar = use_grammar

    async def decide(
        self,
        observation: Observation,
        sub_goal: SubGoal,
        action_history_text: str = "",
        actions_taken_this_subgoal: int = 0,
    ) -> Action:
        """Given the current page state and goal, pick the next action."""

        done_warning = (
            "\nWARNING: You have taken 0 actions toward this sub-goal. "
            "You MUST perform the required action before calling 'done', "
            "unless the success criteria is already fully met on this page right now."
            if actions_taken_this_subgoal == 0
            else ""
        )

        prompt = f"""CURRENT SUB-GOAL: {sub_goal.goal}
SUCCESS CRITERIA: {sub_goal.success_criteria}
ACTIONS TAKEN THIS SUB-GOAL SO FAR: {actions_taken_this_subgoal}{done_warning}

PAGE STATE:
- URL: {observation.url}
- Title: {observation.title}

INTERACTIVE ELEMENTS:
{observation.elements_summary(max_elements=40)}

ACTION HISTORY (recent):
{action_history_text}

Choose your next action:"""

        # AnthropicBackend: use tool_use for guaranteed-valid Action (zero parse failures)
        if isinstance(self.backend, AnthropicBackend):
            action = await self.backend.decide_action(
                prompt=prompt,
                system=EXECUTOR_SYSTEM,
                temperature=self.temperature,
                max_tokens=256,
            )
        else:
            # Use GBNF grammar constraint if backend supports it
            grammar = None
            if self.use_grammar and isinstance(self.backend, LlamaCppBackend):
                grammar = LlamaCppBackend.get_action_grammar()

            response = await self.backend.generate(
                prompt=prompt,
                system=EXECUTOR_SYSTEM,
                temperature=self.temperature,
                max_tokens=256,
                grammar=grammar,
            )
            action = self._parse_action(response)
        logger.info(
            "executor_action",
            action=action.action.value,
            element_id=action.element_id,
            text=action.text[:50] if action.text else None,
        )
        return action

    def _parse_action(self, response: str) -> Action:
        """Parse the model's response into an Action."""
        text = response.strip()

        # Strip markdown if present
        if text.startswith("```"):
            text = text.split("\n", 1)[1] if "\n" in text else text[3:]
        if text.endswith("```"):
            text = text[:-3]
        text = text.strip()

        # Find JSON object
        start = text.find("{")
        end = text.rfind("}") + 1
        if start == -1 or end == 0:
            logger.warning("executor_parse_fallback", response=text[:100])
            # Fallback: if we can't parse, wait and let the loop retry
            return Action(action="wait", seconds=2, reason="Failed to parse action")

        try:
            raw = json.loads(text[start:end])
            return Action(**raw)
        except (json.JSONDecodeError, Exception) as e:
            logger.warning("executor_parse_error", error=str(e), response=text[:100])
            return Action(action="wait", seconds=2, reason=f"Parse error: {e}")
