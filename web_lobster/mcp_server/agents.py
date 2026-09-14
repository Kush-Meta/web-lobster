"""Pieces the MCP service plugs into the orchestrator for a tool call."""

from __future__ import annotations

from typing import Awaitable, Callable, Optional

from web_lobster.core.schemas import Action, SubGoal, TaskPlan
from web_lobster.core.values import ValueSpec

ProgressFn = Callable[[float, Optional[float], str], Awaitable[None]]


class ServerUI:
    """Stands in for the dashboard during an MCP call.

    Progress messages are built only from trusted parts: step counts, action
    types, and the planner's own sub-goal text. No one is there to answer the
    safety gate's confirmation prompts, so they're declined unless the operator
    started the server with --approve-confirmations.
    """

    def __init__(self, progress: Optional[ProgressFn], approve_confirmations: bool, max_steps: int):
        self._progress = progress
        self._approve = approve_confirmations
        self._max_steps = float(max_steps)
        self._step = 0

    async def _report(self, message: str) -> None:
        if self._progress is None:
            return
        try:
            await self._progress(float(self._step), self._max_steps, message)
        except Exception:
            pass  # a client that stopped listening mustn't fail the task

    async def on_plan_ready(self, plan: TaskPlan) -> None:
        await self._report(f"Plan ready: {len(plan.sub_goals)} sub-goal(s)")

    async def on_subgoal_start(self, sub_goal: SubGoal) -> None:
        await self._report(f"Working on: {sub_goal.goal}")

    async def on_action(self, action: Action, step: int) -> None:
        self._step = step
        await self._report(f"Step {step}: {action.action.value}")

    async def on_receipt(self, receipt) -> None:
        state = "done" if receipt.achieved else "not done"
        basis = "verified by evidence" if receipt.basis == "evidence" else "judged by a model"
        await self._report(f"Sub-goal {receipt.sub_goal_id} {state} ({basis})")

    async def on_safety_flag(self, action: Action, reason: str) -> None:
        # The reason can quote page labels, so it stays out of the message.
        await self._report(f"Blocked or flagged a {action.action.value} action")

    async def on_login_required(self, url: str) -> None:
        await self._report("The page asks for a login, which web-lobster can't do for you")

    async def request_confirmation(self, action: Action, reason: str) -> bool:
        return self._approve

    async def wait_if_paused(self) -> None:
        pass

    def __getattr__(self, name: str):
        if name.startswith("on_"):
            async def ignore(*args, **kwargs) -> None:
                pass
            return ignore
        raise AttributeError(name)


class ValueRequestingPlanner:
    """Wraps a planner so the caller's value requests are read on the last sub-goal of every plan."""

    def __init__(self, planner, specs: list[ValueSpec]):
        self._planner = planner
        self._specs = specs

    async def plan(self, task: str, memory_context: Optional[str] = None) -> TaskPlan:
        plan = await self._planner.plan(task, memory_context)
        if plan.sub_goals:
            plan.sub_goals[-1] = self._with_values(plan.sub_goals[-1])
        return plan

    async def replan(self, **kwargs) -> list[SubGoal]:
        goals = await self._planner.replan(**kwargs)
        if goals:
            goals[-1] = self._with_values(goals[-1])
        return goals

    async def extract_learnings(self, **kwargs) -> str:
        return await self._planner.extract_learnings(**kwargs)

    def _with_values(self, goal: SubGoal) -> SubGoal:
        declared = {spec.name for spec in goal.extract}
        extra = [spec for spec in self._specs if spec.name not in declared]
        return goal.model_copy(update={"extract": [*goal.extract, *extra]})
