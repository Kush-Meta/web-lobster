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
from web_lobster.core.values import ExtractedValue, ValueSpec, ValueType
from web_lobster.verify.evidence import RequestCheck, TextCheck, UrlCheck, ValueCheck
from web_lobster.verify.receipts import verify_chain

TASK = "Book the trip"


def _orchestrator(tmp_path, sub_goal: SubGoal, mandate=None) -> tuple[Orchestrator, FakeUI]:
    config = WebLobsterConfig()
    config.browser.headless = True
    config.browser.default_timeout = 5.0
    config.agent.dom_mode = True
    # These tests script the plan; thinking first has its own tests.
    config.agent.briefing = False
    config.agent.plan_review = False

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


async def test_open_page_goal_already_met_takes_no_steps(sites, tmp_path):
    a, _ = sites
    goal = SubGoal(id=1, goal="Open the checkout page", success_criteria="Checkout is visible",
                   evidence=[UrlCheck(pattern=f"{a.origin}/checkout*")])
    orchestrator, _ = _orchestrator(tmp_path, goal)
    orchestrator.executor.decide = AsyncMock(return_value=Action(action=ActionType.DONE, reason="done"))

    result = await orchestrator.run("Open checkout", start_url=f"{a.origin}/checkout")
    _skip_without_chromium(result)

    assert result.success, result.error
    orchestrator.executor.decide.assert_not_called()
    assert result.steps_taken == 0
    assert result.answer == "Booked."  # answered even though no page was observed
    [receipt] = result.receipts
    assert receipt.achieved and receipt.basis == "evidence"


async def test_a_guessed_url_that_404s_does_not_prove_the_page(sites, tmp_path):
    a, _ = sites
    # Live, a 7B planner sent two runs to saucedemo.com/login.html, which doesn't
    # exist. The url check passed on the 404, and the executor then had an empty
    # page to work with for the rest of its step budget.
    goal = SubGoal(id=1, goal="Open the login page", success_criteria="The login page is open",
                   evidence=[UrlCheck(pattern=f"{a.origin}/login.html")])
    orchestrator, _ = _orchestrator(tmp_path, goal)
    orchestrator.executor.decide = AsyncMock(return_value=Action(action=ActionType.DONE, reason="done"))

    result = await orchestrator.run("Sign in", start_url=f"{a.origin}/login.html")
    _skip_without_chromium(result)

    assert not result.success
    [receipt] = result.receipts
    assert not receipt.achieved
    assert "answered 404" in receipt.checks[0].detail


async def test_a_goal_stops_the_moment_it_is_proven(sites, tmp_path):
    a, _ = sites
    # The executor kept acting after a sub-goal was met: on the practice site it
    # signed in, clicked on into the menu, and logged itself back out (run 28).
    goal = SubGoal(id=1, goal="Go to checkout", success_criteria="Checkout is open",
                   evidence=[UrlCheck(pattern=f"{a.origin}/checkout*")])
    orchestrator, _ = _orchestrator(tmp_path, goal)
    clicks = 0

    async def decide(**kwargs):
        nonlocal clicks
        clicks += 1
        return Action(action=ActionType.CLICK, element_id=element_id(kwargs["observation"], "Go to checkout"))

    orchestrator.executor.decide = AsyncMock(side_effect=decide)

    result = await orchestrator.run("Go to checkout", start_url=f"{a.origin}/links")
    _skip_without_chromium(result)

    assert result.success, result.error
    assert clicks == 1  # asked once; the link took it there, and nothing else was tried
    # One action, then a step that noticed the goal was met instead of acting.
    assert result.steps_taken == 2
    assert [line.split(". ", 1)[1] for line in result.actions] == ["click (el=1)"]
    [receipt] = result.receipts
    assert receipt.achieved and receipt.basis == "evidence"


async def test_a_goal_already_proven_when_it_starts_still_runs(sites, tmp_path):
    a, _ = sites
    # Stopping early must need a check that went from failing to passing, or a
    # goal whose url already matched would finish without doing its work.
    goal = SubGoal(id=1, goal=TASK, success_criteria="Booking is confirmed",
                   evidence=[UrlCheck(pattern=f"{a.origin}/checkout*")])
    orchestrator, _ = _orchestrator(tmp_path, goal)
    seen = 0

    async def decide(**kwargs):
        nonlocal seen
        seen += 1
        if seen == 1:
            return Action(action=ActionType.SCROLL)
        return Action(action=ActionType.DONE, reason="done")

    orchestrator.executor.decide = AsyncMock(side_effect=decide)

    result = await orchestrator.run(TASK, start_url=f"{a.origin}/checkout")
    _skip_without_chromium(result)

    assert result.success, result.error
    assert seen == 2  # it kept working until it said so itself


async def test_typing_with_nothing_to_type_is_refused(sites, tmp_path):
    a, _ = sites
    # Five of Google Flights' forty steps were type actions with no text, each
    # one clearing the field it had just filled (live run 27).
    goal = SubGoal(id=1, goal="Enter the email", success_criteria="Filled",
                   evidence=[TextCheck(contains="kush.test@example.com")])
    orchestrator, _ = _orchestrator(tmp_path, goal)
    texts = ["", "kush.test@example.com"]

    async def decide(**kwargs):
        if not texts:
            return Action(action=ActionType.DONE, reason="entered")
        return Action(
            action=ActionType.TYPE,
            element_id=element_id(kwargs["observation"], "Email"),
            text=texts.pop(0),
        )

    orchestrator.executor.decide = AsyncMock(side_effect=decide)

    result = await orchestrator.run("Enter the email", start_url=f"{a.origin}/")
    _skip_without_chromium(result)

    assert result.success, result.error
    assert any("needs text to enter" in line for line in result.actions)
    # The field check passed on what the field holds, not on page text.
    assert result.receipts[0].checks[0].detail == "found in a field on the page"


async def test_a_slow_page_is_waited_for_not_abandoned(sites, tmp_path):
    a, _ = sites
    # A live run called jaipurliteraturefestival.org dead after two seconds and
    # went back; the page renders at three and a half.
    goal = SubGoal(id=1, goal="Read the speakers", success_criteria="Speakers are listed",
                   evidence=[TextCheck(contains="Speakers")])
    orchestrator, _ = _orchestrator(tmp_path, goal)
    orchestrator.executor.decide = AsyncMock(return_value=Action(action=ActionType.DONE, reason="read"))

    result = await orchestrator.run("Read the speakers", start_url=f"{a.origin}/slow")
    _skip_without_chromium(result)

    assert result.success, result.error
    assert not any("go_back" in line for line in result.actions)  # it waited instead


async def test_a_page_with_nothing_on_it_ends_the_attempt(sites, tmp_path):
    a, _ = sites
    # Live run 28 landed on an empty page and spent 35 of its 40 steps choosing
    # elements that weren't there. One step back, then let the plan change.
    goal = SubGoal(id=1, goal="Sign in", success_criteria="Signed in",
                   evidence=[UrlCheck(pattern=f"{a.origin}/confirmation/*")])
    orchestrator, _ = _orchestrator(tmp_path, goal)
    orchestrator.executor.decide = AsyncMock(
        return_value=Action(action=ActionType.CLICK, element_id=1, reason="the login button")
    )

    result = await orchestrator.run("Sign in", start_url=f"{a.origin}/nothing")
    _skip_without_chromium(result)

    assert not result.success
    # Well short of max_actions_per_subgoal (30) for even one attempt.
    assert result.steps_taken < 10, result.actions
    assert any("go_back" in line for line in result.actions)
    orchestrator.executor.decide.assert_not_called()


async def test_a_value_check_does_not_block_the_already_there_path(sites, tmp_path):
    a, _ = sites
    # The step that reads proves it read — but the reading happens in _verify, a
    # moment after this check. Waiting for it here cost the Eiffel task its
    # 0-step path: 7 steps to reach a page the browser had started on.
    goal = SubGoal(
        id=1, goal="Open the checkout page", success_criteria="Checkout is visible",
        evidence=[UrlCheck(pattern=f"{a.origin}/checkout*"),
                  ValueCheck(name="reference", op="!=", value=None)],
        extract=[ValueSpec(name="reference", type=ValueType.TEXT)],
    )
    orchestrator, _ = _orchestrator(tmp_path, goal)
    orchestrator.executor.decide = AsyncMock(return_value=Action(action=ActionType.DONE, reason="done"))
    orchestrator.extractor.extract = AsyncMock(return_value={
        "reference": ExtractedValue(name="reference", type=ValueType.TEXT, value="ABC123", origin=a.origin),
    })

    result = await orchestrator.run("Open checkout", start_url=f"{a.origin}/checkout")
    _skip_without_chromium(result)

    assert result.success, result.error
    assert result.steps_taken == 0
    orchestrator.executor.decide.assert_not_called()


async def test_goal_that_acts_on_the_page_still_runs_when_its_url_already_matches(sites, tmp_path):
    a, _ = sites
    goal = SubGoal(id=1, goal=TASK, success_criteria="Booking is confirmed",
                   evidence=[UrlCheck(pattern=f"{a.origin}/checkout*")])
    orchestrator, _ = _orchestrator(tmp_path, goal)
    orchestrator.executor.decide = AsyncMock(return_value=Action(action=ActionType.DONE, reason="done"))

    result = await orchestrator.run(TASK, start_url=f"{a.origin}/checkout")
    _skip_without_chromium(result)

    assert result.success, result.error
    orchestrator.executor.decide.assert_called()


def _search_then_open(a, search_evidence=()) -> TaskPlan:
    return TaskPlan(task="Open checkout", sub_goals=[
        SubGoal(id=1, goal="Search for checkout", success_criteria="The search box holds 'checkout'",
                evidence=list(search_evidence)),
        SubGoal(id=2, goal="Click the checkout result", success_criteria="Checkout is visible",
                evidence=[UrlCheck(pattern=f"{a.origin}/checkout*")]),
    ])


def _clicks_link_then_claims_done(calls: list[dict]):
    async def decide(**kwargs):
        calls.append(kwargs)
        if len(calls) == 1:
            return Action(action=ActionType.CLICK, element_id=element_id(kwargs["observation"], "Go to checkout"))
        return Action(action=ActionType.DONE, reason="done")
    return decide


def _plan_run(tmp_path, plan: TaskPlan) -> Orchestrator:
    orchestrator, _ = _orchestrator(tmp_path, plan.sub_goals[0])
    orchestrator.planner.plan = AsyncMock(return_value=plan)
    orchestrator.validator.validate = AsyncMock(
        return_value=ValidationResult(achieved=False, confidence=0.9, observation="the box is empty")
    )
    return orchestrator


async def test_failed_step_that_reached_a_later_one_is_skipped(sites, tmp_path):
    a, _ = sites
    orchestrator = _plan_run(tmp_path, _search_then_open(a))
    calls: list[dict] = []
    orchestrator.executor.decide = AsyncMock(side_effect=_clicks_link_then_claims_done(calls))

    result = await orchestrator.run("Open checkout", start_url=f"{a.origin}/links")
    _skip_without_chromium(result)

    assert result.success, result.error
    orchestrator.planner.replan.assert_not_called()
    assert {call["sub_goal"].id for call in calls} == {1}  # step 2 was proven without acting
    assert [g.status.value for g in result.plan.sub_goals] == ["skipped", "completed"]
    assert [(r.sub_goal_id, r.achieved, r.basis) for r in result.receipts] == [
        (1, False, "model"), (2, True, "evidence"),
    ]


async def test_no_skip_to_a_page_the_browser_was_already_on(sites, tmp_path):
    a, _ = sites
    orchestrator = _plan_run(tmp_path, _search_then_open(a))
    orchestrator.executor.decide = AsyncMock(return_value=Action(action=ActionType.DONE, reason="done"))

    result = await orchestrator.run("Open checkout", start_url=f"{a.origin}/checkout")
    _skip_without_chromium(result)

    assert not result.success
    orchestrator.planner.replan.assert_called_once()


async def test_never_skips_past_a_step_that_had_to_send_a_write(sites, tmp_path):
    a, _ = sites
    plan = _search_then_open(a, search_evidence=[RequestCheck(method="POST", url=f"{a.origin}/api/book")])
    orchestrator = _plan_run(tmp_path, plan)
    orchestrator.executor.decide = AsyncMock(side_effect=_clicks_link_then_claims_done([]))

    result = await orchestrator.run("Open checkout", start_url=f"{a.origin}/links")
    _skip_without_chromium(result)

    assert not result.success
    orchestrator.planner.replan.assert_called_once()


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
