"""Executor model — decides the next single browser action.

Called every step of the agent loop. Uses visual grounding when available:
the annotated screenshot shows numbered badges on every interactive element,
letting Claude correlate visual position with element IDs for much higher accuracy.
"""

from __future__ import annotations

import json
from typing import Optional

from web_lobster.core.schemas import Action, ActionType, Observation, SubGoal
from web_lobster.models.base import ModelBackend
from web_lobster.models.llamacpp_backend import LlamaCppBackend
from web_lobster.models.anthropic_backend import AnthropicBackend
from web_lobster.utils.logging import get_logger

logger = get_logger(__name__)

EXECUTOR_SYSTEM = """You are a browser automation executor with vision capabilities.

VISUAL GROUNDING: The screenshot you receive shows the live browser page with
NUMBERED ORANGE BADGES overlaid on every interactive element. Each badge shows
the element's ID. Use these visual markers to identify which element to target —
cross-reference the badge number with the INTERACTIVE ELEMENTS list.

Example: Badge "10" next to a search box → use element_id 10 to type in it.
         Badge "5" on a blue "Search" button → use element_id 5 to click it.

Rules:
- Pick the single most useful next action
- Prefer the visually-obvious interactive element for the task
- If a form field needs text, use "type" with that element's badge number
- Use "scroll" if the target element's badge is not visible in the screenshot
- Use "wait" if the page is still loading
- Never repeat the same (action + element_id) pair that already failed — try another element
- CRITICAL: Call "done" ONLY after you have personally taken at least one meaningful
  action (click, type, navigate, etc.) toward this sub-goal in this attempt AND the
  success criteria is now clearly met. Exception: call "done" immediately if the page
  already satisfies the success criteria without any action needed.

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

REFLECT_SYSTEM = """You are a browser automation debugger. The agent is stuck — it has
repeated the same actions without making progress. Diagnose the root cause and
prescribe a concrete alternative approach.

Be specific: name the exact element ID or URL that should be tried instead.
Output 2-3 sentences maximum."""


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
        reflection: Optional[str] = None,
    ) -> Action:
        """Given the current page state and goal, pick the next action."""

        done_warning = (
            "\nWARNING: You have taken 0 actions toward this sub-goal. "
            "You MUST perform the required action before calling 'done', "
            "unless the success criteria is already fully met on this page right now."
            if actions_taken_this_subgoal == 0
            else ""
        )

        reflection_block = (
            f"\nREFLECTION FROM PREVIOUS ATTEMPT:\n{reflection}\n"
            if reflection
            else ""
        )

        prompt = f"""CURRENT SUB-GOAL: {sub_goal.goal}
SUCCESS CRITERIA: {sub_goal.success_criteria}
ACTIONS TAKEN THIS SUB-GOAL SO FAR: {actions_taken_this_subgoal}{done_warning}{reflection_block}

PAGE STATE:
- URL: {observation.url}
- Title: {observation.title}

INTERACTIVE ELEMENTS (match badge numbers in image):
{observation.elements_summary(max_elements=50)}

ACTION HISTORY (recent):
{action_history_text}

Choose your next action:"""

        # Prefer annotated screenshot (has element badge overlays) over raw
        images = None
        if observation.annotated_screenshot_base64:
            images = [observation.annotated_screenshot_base64]
        elif observation.screenshot_base64:
            images = [observation.screenshot_base64]

        if isinstance(self.backend, AnthropicBackend):
            action = await self.backend.decide_action(
                prompt=prompt,
                system=EXECUTOR_SYSTEM,
                images=images,
                temperature=self.temperature,
                max_tokens=256,
            )
        else:
            grammar = None
            if self.use_grammar and isinstance(self.backend, LlamaCppBackend):
                grammar = LlamaCppBackend.get_action_grammar()
            response = await self.backend.generate(
                prompt=prompt,
                system=EXECUTOR_SYSTEM,
                images=images,
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

    async def reflect(
        self,
        observation: Observation,
        sub_goal: SubGoal,
        failed_actions: list[str],
    ) -> str:
        """Diagnose why the agent is stuck and suggest a different approach."""
        prompt = f"""SUB-GOAL: {sub_goal.goal}
SUCCESS CRITERIA: {sub_goal.success_criteria}

CURRENT URL: {observation.url}
PAGE TITLE: {observation.title}

FAILED ATTEMPTS (in order):
{chr(10).join(f"- {a}" for a in failed_actions[-8:])}

AVAILABLE ELEMENTS:
{observation.elements_summary(max_elements=20)}

Why is the agent stuck? What specific alternative action should it try?"""

        images = None
        if observation.annotated_screenshot_base64:
            images = [observation.annotated_screenshot_base64]

        if isinstance(self.backend, AnthropicBackend):
            reflection = await self.backend.generate(
                prompt=prompt,
                system=REFLECT_SYSTEM,
                images=images,
                temperature=0.3,
                max_tokens=150,
            )
        else:
            reflection = await self.backend.generate(
                prompt=prompt,
                system=REFLECT_SYSTEM,
                temperature=0.3,
                max_tokens=150,
            )

        logger.info("reflection", text=reflection[:100])
        return reflection.strip()

    def _parse_action(self, response: str) -> Action:
        """Parse the model's response into an Action (fallback for non-Anthropic backends)."""
        text = response.strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[1] if "\n" in text else text[3:]
        if text.endswith("```"):
            text = text[:-3]
        text = text.strip()

        start = text.find("{")
        end = text.rfind("}") + 1
        if start == -1 or end == 0:
            logger.warning("executor_parse_fallback", response=text[:100])
            return Action(action=ActionType.WAIT, seconds=2, reason="Failed to parse action")

        try:
            raw = json.loads(text[start:end])
            return Action(**raw)
        except (json.JSONDecodeError, Exception) as e:
            logger.warning("executor_parse_error", error=str(e), response=text[:100])
            return Action(action=ActionType.WAIT, seconds=2, reason=f"Parse error: {e}")
