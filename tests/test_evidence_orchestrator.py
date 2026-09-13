"""Sub-goals are done when the browser can prove it, not when a model says so.

These run the agent loop against a real browser with scripted models. The
executor claims success before doing anything, and a page displays "Booking
confirmed" without anything being booked; neither may count as done.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from tests.agent_fakes import FakeUI, element_id
from web_lobster.core.config import WebLobsterConfig
from web_lobster.core.orchestrator import Orchestrator
from web_lobster.core.schemas import Action, ActionType, SubGoal, TaskPlan, ValidationResult
from web_lobster.mandate.schema import Mandate
from web_lobster.memory.task_memory import TaskMemory
from web_lobster.verify.evidence import RequestCheck, TextCheck, UrlCheck
from web_lobster.verify.receipts import verify_chain

TASK = "Book the trip"


def _orchestrator(tmp_path, sub_goal: SubGoal, mandate=None) -> tuple[Orchestrator, FakeUI]:
    config = WebLobsterConfig()
    config.browser.headless = True
    config.browser.default_timeout = 5.0
    config.agent.dom_mode = True

    ui = FakeUI()
    orchestrator = Orchestrator(config, shared_state=ui, mandate=mandate)
    orchestrator.memory = TaskMemory(tmp_path / "tasks.jsonl")
    orchestrator.planner.plan = AsyncMock(return_value=TaskPlan(task=TASK, sub_goals=[sub_goal]))
    orchestrator.planner.replan = AsyncMock(return_value=[])
    orchestrator.planner.extract_learnings = AsyncMock(return_value="")
    orchestrator.planner_backend = SimpleNamespace(generate=AsyncMock(return_value="Booked."))
    orchestrator.validator.validate = AsyncMock()
    return orchestrator, ui


def _skip_without_chromium(result):
    if result.error and "Executable doesn't exist" in result.error:
        pytest.skip("Playwright Chromium isn't installed")


@pytest.mark.parametrize("with_mandate", [False, True])
async def test_claimed_success_is_refused_until_evidence_exists(sites, tmp_path, with_mandate):
    a, _ = sites
    goal = SubGoal(id=1, goal=TASK, success_criteria="Booking is confirmed", evidence=[
        RequestCheck(method="POST", url=f"{a.origin}/api/book"),
        UrlCheck(pattern=f"{a.origin}/confirmation/*"),
        TextCheck(contains="booking   CONFIRMED"),
    ])
    mandate = Mandate(task=TASK, origins=[a.origin]) if with_mandate else None
    orchestrator, ui = _orchestrator(tmp_path, goal, mandate)

    calls: list[dict] = []

    async def decide(**kwargs):
        calls.append(kwargs)
        step = len(calls)
        if step == 1:  # claims success before doing anything
            return Action(action=ActionType.DONE, reason="Looks booked")
        if step == 2:
            return Action(action=ActionType.CLICK, element_id=element_id(kwargs["observation"], "Book now"))
        return Action(action=ActionType.DONE, reason="Booked")

    orchestrator.executor.decide = AsyncMock(side_effect=decide)

    result = await orchestrator.run(TASK, start_url=f"{a.origin}/checkout")
    _skip_without_chromium(result)
    assert result.success, result.error

    # Code decided; the validator model was never asked.
    orchestrator.validator.validate.assert_not_called()
    assert "Not done yet. Missing evidence" in calls[1]["action_history_text"]
    assert ("POST", "/api/book", b"") in a.requests

    [receipt] = result.receipts
    assert receipt.achieved and receipt.basis == "evidence"
    assert [check.passed for check in receipt.checks] == [True, True, True]
    assert any(w.method == "POST" and w.url == f"{a.origin}/api/book" for w in receipt.writes)
    assert receipt.page == f"{a.origin}/confirmation/ABC123"
    assert verify_chain(result.receipts)

    assert any(name == "on_receipt" for name, _ in ui.events)
    assert "1 verified by evidence, 0 judged by model" in result.summary()


async def test_page_displaying_success_is_not_enough(sites, tmp_path):
    a, _ = sites
    goal = SubGoal(id=1, goal=TASK, success_criteria="Booking is confirmed", evidence=[
        RequestCheck(method="POST", url=f"{a.origin}/api/book"),
        TextCheck(contains="Booking confirmed"),
    ])
    orchestrator, _ = _orchestrator(tmp_path, goal)
    orchestrator.executor.decide = AsyncMock(
        return_value=Action(action=ActionType.DONE, reason="The page says booking confirmed")
    )

    result = await orchestrator.run(TASK, start_url=f"{a.origin}/fake-success")
    _skip_without_chromium(result)

    assert not result.success
    orchestrator.validator.validate.assert_not_called()

    [receipt] = result.receipts
    assert not receipt.achieved
    checks = {check.type: check for check in receipt.checks}
    assert checks["text"].passed  # the page really does say it
    assert not checks["request"].passed
    assert checks["request"].detail == "no matching request was sent"
    assert receipt.writes == []
    assert verify_chain(result.receipts)

    # The planner hears which kind of check failed, and nothing from the page.
    failure = orchestrator.planner.replan.call_args.kwargs["failure"]
    assert failure.endswith("evidence not met: request check(s)")


async def test_goal_without_evidence_is_marked_as_judged(sites, tmp_path):
    a, _ = sites
    goal = SubGoal(id=1, goal="Open checkout", success_criteria="Checkout is visible")
    orchestrator, _ = _orchestrator(tmp_path, goal)
    orchestrator.validator.validate = AsyncMock(
        return_value=ValidationResult(achieved=True, confidence=0.9, observation="checkout page")
    )
    orchestrator.executor.decide = AsyncMock(return_value=Action(action=ActionType.DONE, reason="done"))

    result = await orchestrator.run("Open checkout", start_url=f"{a.origin}/checkout")
    _skip_without_chromium(result)

    assert result.success, result.error
    [receipt] = result.receipts
    assert receipt.basis == "model" and receipt.model_verdict.confidence == 0.9
    assert "0 verified by evidence, 1 judged by model" in result.summary()
    assert "judged by model (0.90), not verified" in result.summary()
