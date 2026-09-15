"""The agent thinks a task through before the browser opens.

A scripted planner writes a brief with questions. Depending on who can answer,
the run stops for input, takes the user's answers into planning and into every
executor step, falls back on defaults, or stops because the user won't run past
the mandate. Plan review then gives the planner one round to fix its plan.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from web_lobster.core.briefing import MANDATE_QUESTION_ID
from web_lobster.core.config import WebLobsterConfig
from web_lobster.core.orchestrator import Orchestrator
from web_lobster.core.schemas import Action, ActionType, ValidationResult
from web_lobster.mandate.schema import Mandate
from web_lobster.memory.task_memory import TaskMemory
from web_lobster.models.planner import BRIEF_SYSTEM, PLANNER_SYSTEM, REVISE_SYSTEM

TASK = "Book a table for two"
TIME_QUESTION = "What time should the table be for?"
BRIEF = {
    "goal": "Book a table for 2", "thinking": "No time is given.", "assumptions": ["A party of 2"],
    "questions": [{"id": "time", "question": TIME_QUESTION, "type": "text"}],
    "success": "A confirmation shows",
}
LOOK = [{"id": 1, "goal": "Look over the checkout page", "success_criteria": "Checkout is visible"}]


class RecordingPlanner:
    """A planner backend that records every prompt, keyed by the kind of request."""

    def __init__(self, brief=BRIEF, plan=LOOK, revised=None):
        self.replies = {
            BRIEF_SYSTEM: json.dumps(brief),
            PLANNER_SYSTEM: json.dumps(plan),
            REVISE_SYSTEM: json.dumps(revised or plan),
        }
        self.prompts: dict[str, list[str]] = {"brief": [], "plan": [], "revise": [], "other": []}

    async def generate(self, prompt, system=None, **kwargs):
        kind = {BRIEF_SYSTEM: "brief", PLANNER_SYSTEM: "plan", REVISE_SYSTEM: "revise"}.get(system, "other")
        self.prompts[kind].append(prompt)
        return self.replies.get(system, "")


def _orchestrator(tmp_path, planner: RecordingPlanner, *, mandate=None, asker=None, questions="ask"):
    config = WebLobsterConfig()
    config.browser.headless = True
    config.browser.default_timeout = 5.0
    config.agent.dom_mode = True
    config.agent.questions = questions

    orchestrator = Orchestrator(config, mandate=mandate, asker=asker)
    orchestrator.memory = TaskMemory(tmp_path / "tasks.jsonl")
    orchestrator.planner.backend = planner
    orchestrator.planner_backend = SimpleNamespace(generate=AsyncMock(return_value="Booked for 20:00."))
    orchestrator.validator.validate = AsyncMock(
        return_value=ValidationResult(achieved=True, confidence=0.9, observation="looks right")
    )
    orchestrator.executor.decide = AsyncMock(return_value=Action(action=ActionType.DONE, reason="done"))
    return orchestrator


def _skip_without_chromium(result):
    if result.error and "Executable doesn't exist" in result.error:
        pytest.skip("Playwright Chromium isn't installed")


async def test_questions_nobody_can_answer_stop_the_run_before_the_browser_opens(tmp_path):
    planner = RecordingPlanner()
    orchestrator = _orchestrator(tmp_path, planner)

    result = await orchestrator.run(TASK, start_url="https://www.united.com/")

    assert result.needs_input and not result.success
    assert [q.id for q in result.questions] == ["time"]
    assert result.steps_taken == 0 and result.receipts == []
    assert orchestrator.browser._page is None  # never started
    assert len(planner.prompts["brief"]) == 1 and planner.prompts["plan"] == []
    summary = result.summary()
    assert "NEEDS INPUT" in summary and f"time: {TIME_QUESTION} (text)" in summary


async def test_answers_reach_the_planner_every_executor_step_and_memory(sites, tmp_path):
    a, _ = sites
    asked = []

    async def asker(questions, brief):
        asked.append(([q.id for q in questions], brief.goal))
        return {"time": "20:00"}

    planner = RecordingPlanner()
    orchestrator = _orchestrator(tmp_path, planner, asker=asker)

    result = await orchestrator.run(TASK, start_url=f"{a.origin}/checkout")
    _skip_without_chromium(result)
    assert result.success, result.error

    assert asked == [(["time"], "Book a table for 2")]
    assert result.answers == {"time": "20:00"} and result.brief.goal == "Book a table for 2"

    [plan_prompt] = planner.prompts["plan"]
    for part in ["NOW:", "TASK BRIEF", "Goal: Book a table for 2", f"{TIME_QUESTION} → 20:00"]:
        assert part in plan_prompt, part
    for call in orchestrator.executor.decide.call_args_list:
        assert f'The user answered "{TIME_QUESTION}": 20:00' in call.kwargs["briefing"]

    # Memory keeps per-sub-goal steps and the sites used, and shows them for other tasks there.
    [record] = orchestrator.memory._records
    assert a.origin in record.origins
    assert record.goal_stats == [
        {"goal": "Look over the checkout page", "done": True, "basis": "model", "steps": 1},
    ]
    assert record.plan == [
        {"goal": "Look over the checkout page", "success_criteria": "Checkout is visible", "extract": [], "evidence": []},
    ]
    experience = orchestrator.memory.format_for_prompt("Something else entirely", origins=[a.origin])
    assert f"[Same site: {a.origin}]" in experience
    assert "Look over the checkout page (1 step, judged by a model)" in experience


async def test_assume_uses_defaults_and_leaves_the_rest_to_the_planner(sites, tmp_path):
    a, _ = sites
    brief = {**BRIEF, "questions": [
        {"id": "time", "question": TIME_QUESTION, "type": "text"},
        {"id": "seats", "question": "How many seats?", "type": "integer", "default": 2},
    ]}
    planner = RecordingPlanner(brief=brief)
    orchestrator = _orchestrator(tmp_path, planner, questions="assume")

    result = await orchestrator.run(TASK, start_url=f"{a.origin}/checkout")
    _skip_without_chromium(result)

    assert result.success, result.error
    assert result.answers == {"seats": 2}
    [plan_prompt] = planner.prompts["plan"]
    assert "How many seats? → 2" in plan_prompt
    assert f"NOT ANSWERED (decide sensibly" in plan_prompt and f"- {TIME_QUESTION}" in plan_prompt


async def test_the_user_can_refuse_a_task_that_needs_more_than_the_mandate(tmp_path):
    mandate = Mandate(task=TASK, origins=["https://www.nopa.example"], writes=[])
    brief = {**BRIEF, "questions": [], "changes_something": True}
    seen = []

    async def asker(questions, brief):
        seen.extend(questions)
        return {MANDATE_QUESTION_ID: "no"}

    orchestrator = _orchestrator(tmp_path, RecordingPlanner(brief=brief), mandate=mandate, asker=asker)
    result = await orchestrator.run(TASK, start_url="https://www.nopa.example/")

    assert [q.id for q in seen] == [MANDATE_QUESTION_ID]
    assert "the mandate is read-only" in seen[0].question
    assert not result.success and not result.needs_input
    assert "chose not to run" in result.error
    assert result.gaps == ["it expects to submit or change something, but the mandate is read-only"]
    assert orchestrator.browser._page is None


async def test_plan_review_gives_the_planner_one_round_to_fix_the_plan(sites, tmp_path):
    a, _ = sites
    planner = RecordingPlanner(
        brief={**BRIEF, "questions": []},
        plan=[{"id": 1, "goal": "Book the table", "success_criteria": "Booked"}],
        revised=LOOK,
    )
    orchestrator = _orchestrator(tmp_path, planner)

    result = await orchestrator.run(TASK, start_url=f"{a.origin}/checkout")
    _skip_without_chromium(result)

    assert result.success, result.error
    [revise_prompt] = planner.prompts["revise"]
    assert "PROBLEMS FOUND" in revise_prompt
    assert 'Sub-goal 1 ("Book the table") changes something' in revise_prompt
    assert [g.goal for g in result.plan.sub_goals] == ["Look over the checkout page"]
    assert result.plan_issues == []


async def test_answers_given_up_front_reach_the_brief_and_the_plan(sites, tmp_path):
    a, _ = sites
    planner = RecordingPlanner()
    orchestrator = _orchestrator(tmp_path, planner)

    result = await orchestrator.run(
        TASK, start_url=f"{a.origin}/checkout", answers={"time": "20:00", "dates": "2026-12-15"},
    )
    _skip_without_chromium(result)

    assert result.success and not result.needs_input, result.error
    [brief_prompt] = planner.prompts["brief"]
    assert "- time: 20:00" in brief_prompt and "- dates: 2026-12-15" in brief_prompt
    [plan_prompt] = planner.prompts["plan"]
    assert f"{TIME_QUESTION} → 20:00" in plan_prompt  # matched a question, so shown with it
    assert "- dates: 2026-12-15" in plan_prompt and "- time: 20:00" not in plan_prompt
    assert result.answers == {"dates": "2026-12-15", "time": "20:00"}
