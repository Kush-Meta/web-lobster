"""Shared state bridge between the orchestrator and the web UI.

The orchestrator pushes events here; the WebSocket server streams them
to connected dashboard clients in real time. Also holds the mutable
config that the UI can edit while the agent is idle.
"""

from __future__ import annotations

import asyncio
import json
import time
from enum import Enum
from typing import Any, Optional

from pydantic import BaseModel, Field

from web_lobster.core.schemas import Action, Observation, SubGoal, TaskPlan, ValidationResult
from web_lobster.core.config import WebLobsterConfig


class AgentPhase(str, Enum):
    IDLE = "idle"
    PLANNING = "planning"
    OBSERVING = "observing"
    THINKING = "thinking"       # executor deciding
    ACTING = "acting"
    VALIDATING = "validating"
    REPLANNING = "replanning"
    PAUSED = "paused"
    COMPLETED = "completed"
    FAILED = "failed"


class UIEvent(BaseModel):
    """An event pushed from the orchestrator to the UI."""
    type: str
    timestamp: float = Field(default_factory=time.time)
    data: dict = Field(default_factory=dict)

    def to_json(self) -> str:
        return self.model_dump_json()


class SharedState:
    """Thread-safe shared state between orchestrator and UI.

    The orchestrator writes events; the WebSocket server reads them.
    """

    def __init__(self):
        self.phase: AgentPhase = AgentPhase.IDLE
        self.config: WebLobsterConfig = WebLobsterConfig.default()
        self.current_task: Optional[str] = None
        self.plan: Optional[TaskPlan] = None
        self.current_subgoal: Optional[SubGoal] = None
        self.current_observation: Optional[Observation] = None
        self.last_action: Optional[Action] = None
        self.last_validation: Optional[ValidationResult] = None
        self.step_count: int = 0
        self.max_steps: int = 100
        self.action_log: list[dict] = []
        self.start_time: Optional[float] = None

        # Event queue for WebSocket streaming
        self._event_queue: asyncio.Queue[UIEvent] = asyncio.Queue(maxsize=500)
        self._subscribers: list[asyncio.Queue] = []

        # Pause/resume control
        self._pause_event = asyncio.Event()
        self._pause_event.set()  # not paused initially

        # Confirmation flow
        self._confirmation_future: Optional[asyncio.Future] = None

        # Login-required flow
        self._login_future: Optional[asyncio.Future] = None

    # ── Event publishing ──────────────────────────────────

    async def emit(self, event_type: str, **data) -> None:
        """Publish an event to all connected UI clients."""
        event = UIEvent(type=event_type, data=data)
        for sub in self._subscribers:
            try:
                sub.put_nowait(event)
            except asyncio.QueueFull:
                pass  # drop oldest if client is slow

    def subscribe(self) -> asyncio.Queue:
        """Subscribe to the event stream. Returns a queue to read from."""
        q: asyncio.Queue = asyncio.Queue(maxsize=200)
        self._subscribers.append(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self._subscribers.remove(q)

    # ── Orchestrator hooks (called from orchestrator) ─────

    async def on_task_start(self, task: str, memory_hits: int = 0) -> None:
        self.current_task = task
        self.phase = AgentPhase.PLANNING
        self.step_count = 0
        self.action_log = []
        self.start_time = time.time()
        await self.emit("task_start", task=task, memory_hits=memory_hits)

    async def on_plan_ready(self, plan: TaskPlan) -> None:
        self.plan = plan
        goals = [{"id": sg.id, "goal": sg.goal, "status": sg.status.value}
                 for sg in plan.sub_goals]
        await self.emit("plan_ready", sub_goals=goals)

    async def on_subgoal_start(self, subgoal: SubGoal) -> None:
        self.current_subgoal = subgoal
        self.phase = AgentPhase.OBSERVING
        await self.emit("subgoal_start", id=subgoal.id, goal=subgoal.goal,
                        criteria=subgoal.success_criteria)

    async def on_observation(self, obs: Observation) -> None:
        self.current_observation = obs
        self.phase = AgentPhase.THINKING
        await self.emit("observation",
                        url=obs.url,
                        title=obs.title,
                        num_elements=len(obs.elements),
                        screenshot=obs.annotated_screenshot_base64 or obs.screenshot_base64)

    async def on_action(self, action: Action, step: int) -> None:
        self.last_action = action
        self.step_count = step
        self.phase = AgentPhase.ACTING
        entry = {
            "step": step,
            "action": action.action.value,
            "element_id": action.element_id,
            "text": action.text,
            "url": action.url,
            "reason": action.reason,
            "time": time.time(),
        }
        self.action_log.append(entry)
        await self.emit("action", **entry)

    async def on_action_result(self, success: bool) -> None:
        self.phase = AgentPhase.OBSERVING
        await self.emit("action_result", success=success)

    async def on_validation(self, result: ValidationResult, subgoal: SubGoal) -> None:
        self.last_validation = result
        self.phase = AgentPhase.VALIDATING
        await self.emit("validation",
                        achieved=result.achieved,
                        confidence=result.confidence,
                        observation=result.observation,
                        subgoal_id=subgoal.id)

    async def on_subgoal_complete(self, subgoal: SubGoal) -> None:
        await self.emit("subgoal_complete", id=subgoal.id, goal=subgoal.goal)

    async def on_subgoal_failed(self, subgoal: SubGoal, reason: str = "") -> None:
        await self.emit("subgoal_failed", id=subgoal.id, goal=subgoal.goal, reason=reason)

    async def on_replan(self, new_goals: list[SubGoal]) -> None:
        self.phase = AgentPhase.REPLANNING
        goals = [{"id": sg.id, "goal": sg.goal} for sg in new_goals]
        await self.emit("replan", new_goals=goals)

    async def on_task_complete(
        self,
        success: bool,
        summary: str = "",
        answer: Optional[str] = None,
        memory_hits: int = 0,
    ) -> None:
        self.phase = AgentPhase.COMPLETED if success else AgentPhase.FAILED
        elapsed = time.time() - (self.start_time or time.time())
        await self.emit("task_complete",
                        success=success,
                        summary=summary,
                        answer=answer,
                        memory_hits=memory_hits,
                        steps=self.step_count,
                        elapsed=round(elapsed, 1))

    async def on_safety_flag(self, action: Action, reason: str) -> None:
        await self.emit("safety_flag",
                        action=action.action.value,
                        element_id=action.element_id,
                        reason=reason)

    async def on_receipt(self, receipt) -> None:
        await self.emit("receipt", **receipt.model_dump(mode="json"))

    # ── Pause / resume / confirmation ─────────────────────

    async def wait_if_paused(self) -> None:
        """Block until unpaused. Called by orchestrator at each step."""
        await self._pause_event.wait()

    def pause(self) -> None:
        self._pause_event.clear()
        self.phase = AgentPhase.PAUSED

    def resume(self) -> None:
        self._pause_event.set()

    async def request_confirmation(self, action: Action, reason: str) -> bool:
        """Request user confirmation via the UI. Blocks until response."""
        await self.emit("confirm_request",
                        action=action.action.value,
                        element_id=action.element_id,
                        text=action.text,
                        reason=reason)
        loop = asyncio.get_event_loop()
        self._confirmation_future = loop.create_future()
        try:
            result = await asyncio.wait_for(self._confirmation_future, timeout=120.0)
            return result
        except asyncio.TimeoutError:
            await self.emit("confirm_timeout")
            return False

    def resolve_confirmation(self, approved: bool) -> None:
        """Called by the UI when user approves/declines."""
        if self._confirmation_future and not self._confirmation_future.done():
            self._confirmation_future.set_result(approved)

    async def on_login_required(self, url: str) -> None:
        """Pause and alert the UI that a login page was detected.

        Blocks until the user clicks 'Done' in the UI (or times out after 5 min).
        """
        self.pause()
        await self.emit("login_required", url=url)
        loop = asyncio.get_event_loop()
        self._login_future = loop.create_future()
        try:
            await asyncio.wait_for(self._login_future, timeout=300.0)
        except asyncio.TimeoutError:
            await self.emit("login_timeout")
        finally:
            self.resume()

    def resolve_login(self) -> None:
        """Called when the user signals they have finished logging in."""
        if self._login_future and not self._login_future.done():
            self._login_future.set_result(True)

    # ── Snapshot for REST API ─────────────────────────────

    def snapshot(self) -> dict:
        """Current state as a JSON-safe dict for the REST API."""
        return {
            "phase": self.phase.value,
            "task": self.current_task,
            "step_count": self.step_count,
            "max_steps": self.max_steps,
            "current_subgoal": {
                "id": self.current_subgoal.id,
                "goal": self.current_subgoal.goal,
                "criteria": self.current_subgoal.success_criteria,
            } if self.current_subgoal else None,
            "plan": {
                "task": self.plan.task,
                "sub_goals": [
                    {"id": sg.id, "goal": sg.goal, "status": sg.status.value}
                    for sg in self.plan.sub_goals
                ],
            } if self.plan else None,
            "last_action": {
                "action": self.last_action.action.value,
                "element_id": self.last_action.element_id,
                "text": self.last_action.text,
            } if self.last_action else None,
            "action_log": self.action_log[-20:],
            "elapsed": round(time.time() - self.start_time, 1) if self.start_time else 0,
        }
