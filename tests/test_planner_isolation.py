"""The planner never sees page content.

A page plants instructions (the canary) in its text and its URL. The agent
reads a price and an airline off it, a later sub-goal fails and gets replanned,
and the run is saved to memory. Every prompt the planner receives (the brief, the
plan, the replan, the learnings, and the next run's memory) has to be free of the
canary, while the typed price still reaches the planner and the airline text
still reaches the executor and the user.
"""

from __future__ import annotations

import json
from unittest.mock import AsyncMock
from urllib.parse import quote_plus

import pytest

from tests.sites import CANARY
from web_lobster.core.config import WebLobsterConfig
from web_lobster.core.orchestrator import Orchestrator
from web_lobster.core.schemas import Action, ActionType, ValidationResult
from web_lobster.memory.task_memory import TaskMemory
from web_lobster.models.planner import BRIEF_SYSTEM, PLANNER_SYSTEM, REPLAN_SYSTEM

TASK = "Find the cheapest fare"
REPLANNED_GOAL = "Look up {{$airline}} fares under {{$price}}"


class RecordingBackend:
    """Records every prompt and replies from a script."""

    def __init__(self, reply):
        self.reply = reply
        self.prompts: list[str] = []

    async def generate(self, prompt, system=None, **kwargs):
        self.prompts.append(f"{system or ''}\n{prompt}")
        return self.reply(system or "", prompt)


def _planner_reply(system: str, prompt: str) -> str:
    if system == BRIEF_SYSTEM:
        return json.dumps({
            "goal": "Book the cheapest fare", "assumptions": ["One adult"],
            "questions": [{"id": "cabin", "question": "Which cabin?", "type": "choice",
                           "choices": ["Economy", "Business"], "default": "Economy"}],
        })
    if system == PLANNER_SYSTEM:
        return json.dumps([
            {"id": 1, "goal": "Open the fare results", "success_criteria": "Fares are listed",
             "extract": [{"name": "price", "type": "number"}, {"name": "airline", "type": "text"}]},
            {"id": 2, "goal": "Open the booking page", "success_criteria": "Booking form is visible"},
        ])
    if system == REPLAN_SYSTEM:
        return json.dumps([{"id": 3, "goal": REPLANNED_GOAL, "success_criteria": "Fares are shown"}])
    return "Open the results page before booking."  # learnings


async def test_planner_never_sees_page_content(sites, tmp_path):
    a, _ = sites
    config = WebLobsterConfig()
    config.browser.headless = True
    config.browser.default_timeout = 5.0
    config.agent.dom_mode = True

    # No mandate: isolation doesn't depend on one.
    orchestrator = Orchestrator(config)
    memory = TaskMemory(tmp_path / "tasks.jsonl")
    orchestrator.memory = memory

    planner = RecordingBackend(_planner_reply)
    orchestrator.planner.backend = planner
    # Quarantined readers: free to see the canary, never allowed to pass it on.
    orchestrator.planner_backend = RecordingBackend(lambda s, p: f"Cheapest is Evil Air. {CANARY}")
    orchestrator.extractor.backend = RecordingBackend(
        lambda s, p: json.dumps({"price": "$1,209.50", "airline": f"Evil Air. {CANARY}"})
    )

    goals_seen = []
    briefings = []

    async def decide(**kwargs):
        goals_seen.append(kwargs["sub_goal"])
        briefings.append(kwargs["briefing"])
        return Action(action=ActionType.DONE, reason="done")

    orchestrator.executor.decide = AsyncMock(side_effect=decide)
    orchestrator.validator.validate = AsyncMock(side_effect=lambda observation, goal: ValidationResult(
        achieved=goal.id != 2, confidence=0.9, observation=f"The page says {CANARY}",
    ))

    result = await orchestrator.run(TASK, start_url=f"{a.origin}/fare?note={quote_plus(CANARY)}")
    if result.error and "Executable doesn't exist" in result.error:
        pytest.skip("Playwright Chromium isn't installed")
    assert result.success, result.error

    # The planner was asked four times: brief, plan, replan, learnings. None saw the page.
    assert len(planner.prompts) == 4
    for prompt in planner.prompts:
        assert "CANARY" not in prompt
        assert "Evil Air" not in prompt
    # The brief and the plan hear where the browser starts (the caller's URL, without
    # its query); later prompts get no paths at all.
    for prompt in planner.prompts[:2]:
        assert f"THE BROWSER STARTS AT: {a.origin}/fare\n" in prompt + "\n"
    assert all("/fare" not in prompt for prompt in planner.prompts[2:])

    # The brief's default answer and the planner's own brief carry into planning.
    assert "Which cabin? → Economy" in planner.prompts[1]
    replan_prompt = planner.prompts[2]
    assert "Goal: Book the cheapest fare" in replan_prompt
    assert all(b and "Overall goal: Book the cheapest fare" in b for b in briefings)
    assert "{{$price}} = 1209.5 (number" in replan_prompt
    assert "{{$airline}}: text read from" in replan_prompt and "withheld" in replan_prompt
    assert f"CURRENT SITE: {a.origin}" in replan_prompt

    # The executor got the replanned goal with values filled in; the plan kept references.
    assert any(g.id == 3 and "Evil Air" in g.goal and "1209.5" in g.goal for g in goals_seen)
    assert result.plan.sub_goals[-1].goal == REPLANNED_GOAL

    # The user still gets the values and the answer.
    assert result.values["price"].value == 1209.5
    assert CANARY in result.values["airline"].value
    assert CANARY in result.answer

    # And the next run's planner starts from clean memory.
    next_context = memory.format_for_prompt(TASK)
    assert "Open the results page before booking." in next_context
    assert "CANARY" not in next_context and "Evil Air" not in next_context
