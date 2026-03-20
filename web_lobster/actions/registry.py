"""Action registry — maps action types to descriptions and metadata.

Provides a central registry for all available actions, making it easy
to extend with custom actions and generate documentation for model prompts.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from web_lobster.core.schemas import ActionType


@dataclass
class ActionSpec:
    """Specification for a single action type."""
    action_type: ActionType
    description: str
    requires_element: bool = False
    requires_text: bool = False
    requires_url: bool = False
    is_terminal: bool = False  # True for "done"
    risk_level: str = "low"    # low, medium, high
    example: str = ""


# Default action registry
_REGISTRY: dict[ActionType, ActionSpec] = {}


def register_action(spec: ActionSpec) -> None:
    _REGISTRY[spec.action_type] = spec


def get_action_spec(action_type: ActionType) -> Optional[ActionSpec]:
    return _REGISTRY.get(action_type)


def get_all_specs() -> list[ActionSpec]:
    return list(_REGISTRY.values())


def action_prompt_block() -> str:
    """Generate the action documentation block for model prompts."""
    lines = ["Available actions:"]
    for spec in _REGISTRY.values():
        lines.append(f"  {spec.example}")
    return "\n".join(lines)


# Register all built-in actions
_BUILTINS = [
    ActionSpec(
        action_type=ActionType.CLICK,
        description="Click an interactive element",
        requires_element=True,
        example='{"action": "click", "element_id": N}',
    ),
    ActionSpec(
        action_type=ActionType.TYPE,
        description="Type text into an input field",
        requires_element=True,
        requires_text=True,
        example='{"action": "type", "element_id": N, "text": "..."}',
    ),
    ActionSpec(
        action_type=ActionType.SCROLL,
        description="Scroll the page up or down",
        example='{"action": "scroll", "direction": "down"|"up"}',
    ),
    ActionSpec(
        action_type=ActionType.NAVIGATE,
        description="Navigate to a URL",
        requires_url=True,
        risk_level="medium",
        example='{"action": "navigate", "url": "https://..."}',
    ),
    ActionSpec(
        action_type=ActionType.WAIT,
        description="Wait for the page to load or settle",
        example='{"action": "wait", "seconds": N}',
    ),
    ActionSpec(
        action_type=ActionType.SELECT,
        description="Select an option from a dropdown",
        requires_element=True,
        requires_text=True,
        example='{"action": "select", "element_id": N, "text": "option text"}',
    ),
    ActionSpec(
        action_type=ActionType.HOVER,
        description="Hover over an element to reveal tooltips/menus",
        requires_element=True,
        example='{"action": "hover", "element_id": N}',
    ),
    ActionSpec(
        action_type=ActionType.GO_BACK,
        description="Go back to the previous page",
        example='{"action": "go_back"}',
    ),
    ActionSpec(
        action_type=ActionType.DONE,
        description="Signal that the current sub-goal is complete",
        is_terminal=True,
        example='{"action": "done", "reason": "..."}',
    ),
]

for _spec in _BUILTINS:
    register_action(_spec)
