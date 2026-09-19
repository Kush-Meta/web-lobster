"""The agent loop under a mandate, with scripted models and a real browser.

The executor here behaves as if a page had prompt-injected it: it tries to leave
the mandate twice before doing the real work. The loop has to block both
attempts, tell the executor why, get it back on track, and still finish the task
without the model ever seeing the granted value.
"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from tests.agent_fakes import FakeUI
from tests.agent_fakes import element_id as _element_id
from tests.sites import EMAIL
from web_lobster.core.config import MCPServerConfig, WebLobsterConfig
from web_lobster.core.orchestrator import Orchestrator
from web_lobster.core.schemas import Action, ActionType, SubGoal, TaskPlan, ValidationResult
from web_lobster.mandate.enforcer import Violation, ViolationKind
from web_lobster.mandate.schema import DataGrant, Mandate
from web_lobster.memory.task_memory import TaskMemory


async def test_injected_executor_is_contained_and_task_still_completes(sites, tmp_path):
    a, b = sites
    mandate = Mandate(
        task="Enter my email on site A",
        origins=[a.origin],
        data=[DataGrant(name="email", value=EMAIL, origins=[a.origin])],
    )
    config = WebLobsterConfig()
    config.browser.headless = True
    config.browser.default_timeout = 5.0
    config.agent.dom_mode = True
    config.mcp_servers = [MCPServerConfig(name="fs", command="unused")]
    # These tests script the plan; thinking first has its own tests.
    config.agent.briefing = False
    config.agent.plan_review = False

    ui = FakeUI()
    orchestrator = Orchestrator(config, shared_state=ui, mandate=mandate)
    orchestrator.memory = TaskMemory(tmp_path / "tasks.jsonl")
    orchestrator.mcp.start = AsyncMock()
    orchestrator.planner.plan = AsyncMock(return_value=TaskPlan(
        task=mandate.task,
        sub_goals=[SubGoal(id=1, goal="Enter email", success_criteria="Email field is filled")],
    ))
    orchestrator.planner.extract_learnings = AsyncMock(return_value="")
    orchestrator.planner_backend = SimpleNamespace(generate=AsyncMock(return_value="Entered the email."))
    orchestrator.validator.validate = AsyncMock(
        return_value=ValidationResult(achieved=True, confidence=0.9, observation="filled")
    )

    calls: list[dict] = []

    async def decide(**kwargs):
        calls.append(kwargs)
        observation = kwargs["observation"]
        step = len(calls)
        if step == 1:  # stopped by the pre-check, before the browser
            return Action(action=ActionType.NAVIGATE, url=f"{b.origin}/prize")
        if step == 2:  # stopped in the network layer
            return Action(action=ActionType.CLICK, element_id=_element_id(observation, "Claim prize"))
        if step == 3:
            return Action(
                action=ActionType.TYPE, element_id=_element_id(observation, "Email"), text="{{email}}"
            )
        kwargs["field_value"] = await orchestrator.browser._page.input_value("input")
        return Action(action=ActionType.DONE, reason="Email entered")

    orchestrator.executor.decide = AsyncMock(side_effect=decide)

    result = await orchestrator.run(mandate.task, start_url=f"{a.origin}/")
    if result.error and "Executable doesn't exist" in result.error:
        pytest.skip("Playwright Chromium isn't installed")

    assert result.success, result.error
    assert b.requests == []
    assert [v.kind for v in result.violations] == [ViolationKind.NAVIGATION] * 2
    orchestrator.mcp.start.assert_not_called()

    # The executor was told why both attempts failed, and was back on site A.
    assert calls[2]["action_history_text"].count("Blocked by mandate") == 2
    assert calls[2]["observation"].url.startswith(a.origin)

    # The model worked with the placeholder; only the page got the real value.
    assert calls[0]["data_placeholders"] == ["email"]
    assert calls[3]["field_value"] == EMAIL
    email_field = next(el for el in calls[3]["observation"].elements if el.label == "Email")
    assert email_field.value == "{{email}}"
    for call in calls:
        assert EMAIL not in call["observation"].model_dump_json()
        assert EMAIL not in call["action_history_text"]

    assert any(
        name == "on_safety_flag" and args[1].startswith("Mandate:") for name, args in ui.events
    )
    assert "Mandate blocked 2 action(s)" in result.summary()

    # The run record says what the executor did, blocked attempts included, so a
    # stuck run can be read afterwards — and the granted value isn't in it.
    trail = "\n".join(result.actions)
    assert EMAIL not in trail
    assert '"{{email}}"' in trail
    assert trail.count("Blocked by mandate") == 2


async def test_the_executor_hears_about_a_repeated_block_once(sites):
    a, _ = sites
    # Google Flights' own telemetry was blocked on every step, and half the
    # executor's history became the same two lines (live run 27).
    mandate = Mandate(task="Look", origins=[a.origin])
    orchestrator = Orchestrator(WebLobsterConfig(), mandate=mandate)
    noise = Violation(
        kind=ViolationKind.UNAPPROVED_WRITE, url=f"{a.origin}/_/jserror?id=1",
        detail="POST /_/jserror isn't one of the writes this mandate allows",
        method="POST", resource_type="fetch",
    )
    later = noise.model_copy(update={"url": f"{a.origin}/_/jserror?id=2"})
    orchestrator.enforcer = SimpleNamespace(
        drain=lambda: [noise], check_page=lambda url: None,
        wait_for_pending=AsyncMock(), violations=[],
    )

    action = Action(action=ActionType.CLICK, element_id=1)
    await orchestrator._enforce_after_action(action)
    orchestrator.enforcer.drain = lambda: [later]  # same endpoint, new query string
    await orchestrator._enforce_after_action(action)

    blocked = [act for act in orchestrator.state.action_history if "Blocked by mandate" in (act.reason or "")]
    assert len(blocked) == 1

    # A different endpoint is news, and is reported.
    other = noise.model_copy(update={"url": f"{a.origin}/_/batchexecute", "detail": "POST /_/batchexecute"})
    orchestrator.enforcer.drain = lambda: [other]
    await orchestrator._enforce_after_action(action)
    blocked = [act for act in orchestrator.state.action_history if "Blocked by mandate" in (act.reason or "")]
    assert len(blocked) == 2

