"""Planner model — decomposes a user task into ordered sub-goals.

Called once at the start, and again if the validator triggers a re-plan.
Uses the largest available model for best reasoning quality.
"""

from __future__ import annotations

import json
from typing import Optional

from web_lobster.core.schemas import SubGoal, TaskPlan
from web_lobster.models.base import ModelBackend
from web_lobster.utils.logging import get_logger

logger = get_logger(__name__)

PLANNER_SYSTEM = """You are a web task planner for an autonomous browser agent.

Given a user's task, decompose it into a sequence of sub-goals that a browser
agent can execute. Each sub-goal should be:

1. Achievable in 1-15 browser actions (click, type, scroll, navigate)
2. Verifiable — you can tell if it succeeded by looking at the page
3. Ordered — later goals may depend on earlier ones
4. Specific — avoid vague goals like "find information"

Think step by step about what a human would do to complete this task in a browser.

Respond ONLY with a JSON array of sub-goals. No other text.

Example:
[
  {"id": 1, "goal": "Navigate to Google Flights", "success_criteria": "Google Flights search page is visible with departure/arrival fields"},
  {"id": 2, "goal": "Enter LAX as departure airport", "success_criteria": "LAX is selected in the departure field"},
  {"id": 3, "goal": "Enter JFK as arrival airport", "success_criteria": "JFK is selected in the arrival field"}
]"""

REPLAN_SYSTEM = """You are a web task planner. A previous plan partially failed.
Review what was accomplished, what failed, and create a revised plan to complete
the remaining work. Only include sub-goals that still need to be done.

Respond ONLY with a JSON array of sub-goals. No other text."""


class Planner:
    """Task decomposition using a large language model."""

    def __init__(self, backend: ModelBackend, temperature: float = 0.2):
        self.backend = backend
        self.temperature = temperature

    async def plan(self, task: str) -> TaskPlan:
        """Decompose a task into sub-goals."""
        logger.info("planning", task=task)

        response = await self.backend.generate(
            prompt=f"USER TASK: {task}",
            system=PLANNER_SYSTEM,
            temperature=self.temperature,
            max_tokens=4096,
        )

        sub_goals = self._parse_subgoals(response)
        plan = TaskPlan(task=task, sub_goals=sub_goals)

        logger.info("plan_created", num_subgoals=len(sub_goals))
        for sg in sub_goals:
            logger.debug("subgoal", id=sg.id, goal=sg.goal)

        return plan

    async def replan(
        self,
        task: str,
        completed_goals: list[SubGoal],
        failed_goal: SubGoal,
        current_url: str,
        error_context: str = "",
    ) -> list[SubGoal]:
        """Create a revised plan after a failure."""
        completed_summary = "\n".join(
            f"  ✓ {sg.id}. {sg.goal}" for sg in completed_goals
        )
        prompt = f"""USER TASK: {task}

COMPLETED SUB-GOALS:
{completed_summary or "  (none)"}

FAILED SUB-GOAL:
  ✗ {failed_goal.id}. {failed_goal.goal}
  Failure reason: {error_context}

CURRENT PAGE URL: {current_url}

Create a revised plan to complete the remaining work from the current state."""

        logger.info("replanning", failed_goal=failed_goal.goal)
        response = await self.backend.generate(
            prompt=prompt,
            system=REPLAN_SYSTEM,
            temperature=self.temperature,
            max_tokens=4096,
        )

        return self._parse_subgoals(response)

    def _parse_subgoals(self, response: str) -> list[SubGoal]:
        """Parse the model's JSON response into SubGoal objects."""
        # Strip markdown code fences if present
        text = response.strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[1] if "\n" in text else text[3:]
        if text.endswith("```"):
            text = text[:-3]
        text = text.strip()

        # Find JSON array in the response
        start = text.find("[")
        end = text.rfind("]") + 1
        if start == -1 or end == 0:
            logger.error("planner_parse_failed", response=text[:200])
            raise ValueError("Planner did not return a valid JSON array")

        try:
            raw = json.loads(text[start:end])
        except json.JSONDecodeError as e:
            logger.error("planner_json_error", error=str(e), response=text[:200])
            raise ValueError(f"Planner returned invalid JSON: {e}")

        sub_goals = []
        for i, item in enumerate(raw):
            # Model may return plain strings instead of dicts
            if isinstance(item, str):
                item = {"goal": item}
            sub_goals.append(SubGoal(
                id=item.get("id", i + 1),
                goal=item.get("goal", f"Step {i + 1}"),
                success_criteria=item.get("success_criteria", item.get("goal", f"Step {i + 1} complete")),
            ))
        return sub_goals
