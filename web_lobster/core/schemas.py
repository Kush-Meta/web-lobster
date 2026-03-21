"""Data contracts shared across all Web Lobster components.

Every piece of data that flows between the planner, executor, validator,
browser, and observer is defined here as a Pydantic model. This is the
single source of truth for the system's internal API.
"""

from __future__ import annotations

import time
from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field, field_validator


# ---------------------------------------------------------------------------
# Actions — what the executor can tell the browser to do
# ---------------------------------------------------------------------------

class ActionType(str, Enum):
    CLICK = "click"
    TYPE = "type"
    SCROLL = "scroll"
    NAVIGATE = "navigate"
    WAIT = "wait"
    DONE = "done"
    SELECT = "select"
    HOVER = "hover"
    GO_BACK = "go_back"
    SCREENSHOT = "screenshot"
    MCP_TOOL = "mcp_tool"


class ScrollDirection(str, Enum):
    UP = "up"
    DOWN = "down"


class Action(BaseModel):
    """A single browser action produced by the executor model."""
    action: ActionType
    element_id: Optional[int] = None
    text: Optional[str] = None
    direction: Optional[ScrollDirection] = None
    url: Optional[str] = None
    reason: Optional[str] = None
    seconds: Optional[float] = None
    mcp_tool_name: Optional[str] = None
    mcp_tool_args: Optional[dict] = None

    @field_validator("element_id", mode="before")
    @classmethod
    def coerce_element_id(cls, v):
        """Models sometimes return element_id as a single-element list, e.g. [14]."""
        if isinstance(v, list):
            return v[0] if v else None
        return v


# ---------------------------------------------------------------------------
# Page observation — what the observer extracts from the browser
# ---------------------------------------------------------------------------

class BoundingBox(BaseModel):
    x: float
    y: float
    width: float
    height: float


class PageElement(BaseModel):
    """An interactive element on the page, extracted from the a11y tree."""
    id: int
    role: str                          # button, input, link, select, etc.
    label: str                         # visible or aria label
    value: Optional[str] = None        # current value for inputs
    bbox: Optional[BoundingBox] = None # screen coordinates
    tag: Optional[str] = None          # HTML tag name
    href: Optional[str] = None         # for links
    stable_selector: Optional[str] = None  # CSS selector that survives React re-renders
    is_visible: bool = True
    is_enabled: bool = True


class Observation(BaseModel):
    """Complete snapshot of the current page state.

    This is what gets fed to the executor and validator models.
    """
    url: str
    title: str
    elements: list[PageElement] = Field(default_factory=list)
    screenshot_base64: Optional[str] = None    # PNG as base64
    annotated_screenshot_base64: Optional[str] = None  # with element labels overlaid
    accessibility_tree: Optional[str] = None   # simplified text representation
    page_text: Optional[str] = None            # visible text content (truncated)
    dom_structured: Optional[str] = None       # rich DOM summary for DOM mode
    login_detected: bool = False               # page requires authentication
    timestamp: float = Field(default_factory=time.time)

    def elements_summary(self, max_elements: int = 40) -> str:
        """Format elements as a compact text list for the executor prompt."""
        lines = []
        for el in self.elements[:max_elements]:
            parts = [f"[{el.id}]", el.role, f'"{el.label}"']
            if el.value:
                parts.append(f'value="{el.value}"')
            if el.bbox:
                parts.append(f"at ({int(el.bbox.x)},{int(el.bbox.y)})")
            if not el.is_enabled:
                parts.append("(disabled)")
            lines.append(" ".join(parts))
        summary = "\n".join(lines)
        if len(self.elements) > max_elements:
            summary += f"\n... and {len(self.elements) - max_elements} more elements"
        return summary


# ---------------------------------------------------------------------------
# Planning — task decomposition
# ---------------------------------------------------------------------------

class SubGoalStatus(str, Enum):
    PENDING = "pending"
    ACTIVE = "active"
    COMPLETED = "completed"
    FAILED = "failed"
    SKIPPED = "skipped"


class SubGoal(BaseModel):
    """A single sub-goal produced by the planner."""
    id: int
    goal: str
    success_criteria: str
    status: SubGoalStatus = SubGoalStatus.PENDING
    attempts: int = 0
    max_attempts: int = 3


class TaskPlan(BaseModel):
    """The planner's full output for a user task."""
    task: str                                # original user request
    sub_goals: list[SubGoal] = Field(default_factory=list)
    created_at: float = Field(default_factory=time.time)

    @property
    def current_goal(self) -> Optional[SubGoal]:
        """Return the first non-completed, non-failed sub-goal."""
        for sg in self.sub_goals:
            if sg.status in (SubGoalStatus.PENDING, SubGoalStatus.ACTIVE):
                return sg
        return None

    @property
    def is_complete(self) -> bool:
        return all(
            sg.status in (SubGoalStatus.COMPLETED, SubGoalStatus.SKIPPED)
            for sg in self.sub_goals
        )

    @property
    def is_failed(self) -> bool:
        return any(sg.status == SubGoalStatus.FAILED for sg in self.sub_goals)


# ---------------------------------------------------------------------------
# Validation — goal achievement checking
# ---------------------------------------------------------------------------

class ValidationResult(BaseModel):
    """The validator's assessment of whether a sub-goal was achieved."""
    achieved: bool
    confidence: float = Field(ge=0.0, le=1.0)
    observation: str
    should_replan: bool = False


# ---------------------------------------------------------------------------
# Agent state — tracks the full execution
# ---------------------------------------------------------------------------

class AgentState(BaseModel):
    """Mutable state of the agent across the entire task execution."""
    plan: Optional[TaskPlan] = None
    current_observation: Optional[Observation] = None
    action_history: list[Action] = Field(default_factory=list)
    step_count: int = 0
    max_steps: int = 100
    replan_count: int = 0
    max_replans: int = 3
    start_time: float = Field(default_factory=time.time)

    @property
    def is_over_budget(self) -> bool:
        return self.step_count >= self.max_steps

    def recent_actions(self, n: int = 5) -> list[Action]:
        return self.action_history[-n:]

    def action_history_summary(self, n: int = 5) -> str:
        """Format recent actions as text for the executor prompt."""
        lines = []
        for act in self.recent_actions(n):
            parts = [act.action.value]
            if act.action == ActionType.MCP_TOOL:
                if act.mcp_tool_name:
                    parts.append(act.mcp_tool_name)
                if act.reason:
                    parts.append(f"→ {act.reason}")
            else:
                if act.element_id is not None:
                    parts.append(f"element [{act.element_id}]")
                if act.text:
                    parts.append(f'"{act.text}"')
                if act.url:
                    parts.append(act.url)
                if act.reason:
                    parts.append(f"({act.reason})")
            lines.append("- " + " ".join(parts))
        return "\n".join(lines) if lines else "(no actions yet)"
