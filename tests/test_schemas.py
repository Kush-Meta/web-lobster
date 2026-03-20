"""Tests for core data schemas."""

import pytest
from web_lobster.core.schemas import (
    Action,
    ActionType,
    AgentState,
    BoundingBox,
    Observation,
    PageElement,
    ScrollDirection,
    SubGoal,
    SubGoalStatus,
    TaskPlan,
    ValidationResult,
)


class TestAction:
    def test_click_action(self):
        action = Action(action=ActionType.CLICK, element_id=5)
        assert action.action == ActionType.CLICK
        assert action.element_id == 5

    def test_type_action(self):
        action = Action(action=ActionType.TYPE, element_id=3, text="hello")
        assert action.text == "hello"

    def test_done_action(self):
        action = Action(action=ActionType.DONE, reason="Goal achieved")
        assert action.reason == "Goal achieved"

    def test_scroll_action(self):
        action = Action(action=ActionType.SCROLL, direction=ScrollDirection.DOWN)
        assert action.direction == ScrollDirection.DOWN


class TestObservation:
    def test_elements_summary(self):
        obs = Observation(
            url="https://example.com",
            title="Test Page",
            elements=[
                PageElement(
                    id=1,
                    role="button",
                    label="Submit",
                    bbox=BoundingBox(x=100, y=200, width=80, height=30),
                ),
                PageElement(
                    id=2,
                    role="input",
                    label="Email",
                    value="test@example.com",
                    bbox=BoundingBox(x=100, y=150, width=200, height=30),
                ),
            ],
        )
        summary = obs.elements_summary()
        assert "[1]" in summary
        assert "Submit" in summary
        assert "[2]" in summary
        assert "Email" in summary
        assert "test@example.com" in summary

    def test_elements_summary_truncation(self):
        elements = [
            PageElement(id=i, role="button", label=f"Button {i}")
            for i in range(50)
        ]
        obs = Observation(url="https://example.com", title="Test", elements=elements)
        summary = obs.elements_summary(max_elements=10)
        assert "40 more elements" in summary


class TestTaskPlan:
    def test_current_goal(self):
        plan = TaskPlan(
            task="Test task",
            sub_goals=[
                SubGoal(id=1, goal="First", success_criteria="Done 1", status=SubGoalStatus.COMPLETED),
                SubGoal(id=2, goal="Second", success_criteria="Done 2", status=SubGoalStatus.PENDING),
                SubGoal(id=3, goal="Third", success_criteria="Done 3", status=SubGoalStatus.PENDING),
            ],
        )
        current = plan.current_goal
        assert current is not None
        assert current.id == 2

    def test_is_complete(self):
        plan = TaskPlan(
            task="Test",
            sub_goals=[
                SubGoal(id=1, goal="A", success_criteria="A", status=SubGoalStatus.COMPLETED),
                SubGoal(id=2, goal="B", success_criteria="B", status=SubGoalStatus.COMPLETED),
            ],
        )
        assert plan.is_complete is True

    def test_is_failed(self):
        plan = TaskPlan(
            task="Test",
            sub_goals=[
                SubGoal(id=1, goal="A", success_criteria="A", status=SubGoalStatus.COMPLETED),
                SubGoal(id=2, goal="B", success_criteria="B", status=SubGoalStatus.FAILED),
            ],
        )
        assert plan.is_failed is True
        assert plan.is_complete is False


class TestAgentState:
    def test_over_budget(self):
        state = AgentState(max_steps=5, step_count=5)
        assert state.is_over_budget is True

    def test_recent_actions(self):
        state = AgentState()
        for i in range(10):
            state.action_history.append(
                Action(action=ActionType.CLICK, element_id=i)
            )
        recent = state.recent_actions(3)
        assert len(recent) == 3
        assert recent[0].element_id == 7

    def test_action_history_summary_empty(self):
        state = AgentState()
        assert "(no actions yet)" in state.action_history_summary()


class TestValidationResult:
    def test_valid_result(self):
        result = ValidationResult(
            achieved=True,
            confidence=0.95,
            observation="Search results visible with prices",
        )
        assert result.achieved is True
        assert result.confidence == 0.95

    def test_confidence_bounds(self):
        with pytest.raises(Exception):
            ValidationResult(achieved=True, confidence=1.5, observation="")
