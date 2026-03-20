"""Tests for model response parsing (planner and executor)."""

import pytest
from web_lobster.models.planner import Planner
from web_lobster.models.executor import Executor
from web_lobster.core.schemas import ActionType


class TestPlannerParsing:
    """Test the planner's JSON response parsing."""

    def _parse(self, response: str):
        """Helper: create a planner with a dummy backend and parse."""
        planner = Planner(backend=None)  # backend not needed for parsing
        return planner._parse_subgoals(response)

    def test_clean_json(self):
        response = '''[
            {"id": 1, "goal": "Navigate to site", "success_criteria": "Page loaded"},
            {"id": 2, "goal": "Search for item", "success_criteria": "Results shown"}
        ]'''
        goals = self._parse(response)
        assert len(goals) == 2
        assert goals[0].goal == "Navigate to site"
        assert goals[1].success_criteria == "Results shown"

    def test_json_with_markdown_fences(self):
        response = '''```json
[
    {"id": 1, "goal": "Open browser", "success_criteria": "Browser open"}
]
```'''
        goals = self._parse(response)
        assert len(goals) == 1
        assert goals[0].goal == "Open browser"

    def test_json_with_preamble(self):
        response = '''Here is the plan:
[
    {"id": 1, "goal": "Step one", "success_criteria": "Done"}
]
That should work!'''
        goals = self._parse(response)
        assert len(goals) == 1

    def test_missing_success_criteria_uses_goal(self):
        response = '[{"id": 1, "goal": "Do the thing"}]'
        goals = self._parse(response)
        assert goals[0].success_criteria == "Do the thing"

    def test_invalid_json_raises(self):
        with pytest.raises(ValueError):
            self._parse("This is not JSON at all")

    def test_no_array_raises(self):
        with pytest.raises(ValueError):
            self._parse('{"id": 1, "goal": "Not an array"}')


class TestExecutorParsing:
    """Test the executor's action response parsing."""

    def _parse(self, response: str):
        executor = Executor(backend=None)
        return executor._parse_action(response)

    def test_click_action(self):
        action = self._parse('{"action": "click", "element_id": 5}')
        assert action.action == ActionType.CLICK
        assert action.element_id == 5

    def test_type_action(self):
        action = self._parse('{"action": "type", "element_id": 3, "text": "hello"}')
        assert action.action == ActionType.TYPE
        assert action.text == "hello"

    def test_done_action(self):
        action = self._parse('{"action": "done", "reason": "Goal reached"}')
        assert action.action == ActionType.DONE

    def test_with_markdown_fences(self):
        action = self._parse('```json\n{"action": "scroll", "direction": "down"}\n```')
        assert action.action == ActionType.SCROLL

    def test_with_preamble_text(self):
        action = self._parse('I should click the button.\n{"action": "click", "element_id": 1}')
        assert action.action == ActionType.CLICK

    def test_invalid_returns_wait(self):
        action = self._parse("I don't know what to do")
        assert action.action == ActionType.WAIT
