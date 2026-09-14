"""Scripted stand-ins for the models, and helpers the benchmark runs agents with."""

from __future__ import annotations

from collections import deque
from typing import Optional

from web_lobster.bench.scenarios import Step
from web_lobster.core.schemas import Action, ActionType, Observation, SubGoal, TaskPlan, ValidationResult


class ScriptedPlanner:
    """Returns a fixed plan; never replans."""

    def __init__(self, sub_goals: list[SubGoal]):
        self._sub_goals = sub_goals

    async def plan(self, task: str, memory_context: Optional[str] = None) -> TaskPlan:
        return TaskPlan(task=task, sub_goals=[g.model_copy(deep=True) for g in self._sub_goals])

    async def replan(self, **kwargs) -> list[SubGoal]:
        return []

    async def extract_learnings(self, **kwargs) -> str:
        return ""


class ScriptedExecutor:
    """Plays a fixed list of steps instead of choosing actions, then says done."""

    def __init__(self, steps: list[Step], fill: dict[str, str]):
        self._steps = deque(steps)
        self._fill_values = fill

    async def decide(self, observation: Observation, sub_goal: Optional[SubGoal] = None, **kwargs) -> Action:
        if not self._steps:
            return Action(action=ActionType.DONE, reason="scripted: nothing left to do")
        step = self._steps.popleft()

        if step.action == ActionType.DONE:
            return Action(action=ActionType.DONE, reason="scripted: claiming success")
        if step.action == ActionType.NAVIGATE:
            return Action(action=ActionType.NAVIGATE, url=self._fill(step.url))

        element_id = self._find(observation, step.target)
        if element_id is None:
            return Action(action=ActionType.WAIT, seconds=0.2, reason=f"scripted: '{step.target}' not on page")
        if step.action == ActionType.TYPE:
            return Action(action=ActionType.TYPE, element_id=element_id, text=self._fill(step.text))
        return Action(action=step.action, element_id=element_id)

    async def reflect(self, **kwargs) -> str:
        return ""

    def _fill(self, text: str) -> str:
        for token, value in self._fill_values.items():
            text = text.replace("{" + token + "}", value)
        return text

    @staticmethod
    def _find(observation: Observation, label: str) -> Optional[int]:
        wanted = label.strip().lower()
        exact = [el.id for el in observation.elements if el.label.strip().lower() == wanted]
        if exact:
            return exact[0]
        partial = [el.id for el in observation.elements if wanted in el.label.lower()]
        return partial[0] if partial else None


class GullibleValidator:
    """Calls a page done when it displays the success text, like a model taken in by a banner."""

    def __init__(self, success_text: str):
        self.success_text = success_text.lower()

    async def validate(self, observation: Observation, sub_goal: SubGoal) -> ValidationResult:
        shown = self.success_text in (observation.page_text or "").lower()
        return ValidationResult(
            achieved=shown, confidence=0.9 if shown else 0.1,
            observation="scripted validator: success text shown" if shown else "scripted validator: not shown",
        )


class SilentBackend:
    """A model backend that answers every prompt with nothing."""

    async def generate(self, prompt: str, system: Optional[str] = None, **kwargs) -> str:
        return ""


class ApprovingUI:
    """Stands in for a person who approves every confirmation: the worst case."""

    def __init__(self):
        self.events: list[str] = []

    def __getattr__(self, name: str):
        async def record(*args, **kwargs):
            self.events.append(name)
        return record

    async def wait_if_paused(self) -> None:
        pass

    async def request_confirmation(self, action: Action, reason: str) -> bool:
        self.events.append("request_confirmation")
        return True


class EvidenceStrippingPlanner:
    """Wraps a planner and removes evidence from its sub-goals, to run without that defense."""

    def __init__(self, planner):
        self._planner = planner

    async def plan(self, task: str, memory_context: Optional[str] = None) -> TaskPlan:
        plan = await self._planner.plan(task, memory_context)
        plan.sub_goals = [_without_evidence(goal) for goal in plan.sub_goals]
        return plan

    async def replan(self, **kwargs) -> list[SubGoal]:
        return [_without_evidence(goal) for goal in await self._planner.replan(**kwargs)]

    async def extract_learnings(self, **kwargs) -> str:
        return await self._planner.extract_learnings(**kwargs)


def _without_evidence(goal: SubGoal) -> SubGoal:
    return goal.model_copy(update={"evidence": []})
