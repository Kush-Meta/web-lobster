"""Running one sub-goal: the loop that observes, acts, and decides when to stop.

Lifted out of the orchestrator unchanged. It had grown to seventeen labelled
concerns in a single `while`, because every new thing the agent learned about
the world had nowhere to live except another branch
([design/step-9-architecture.md](../../docs/design/step-9-architecture.md)).

The runner owns the step loop and the bookkeeping only the loop uses: what the
last action did, which URLs led nowhere, which proofs were given up on, and
which blocks the executor has already been told about.

Everything else — the browser, the models, the plan, the mandate, verification,
receipts, memory — belongs to the orchestrator, and is reached through it. That
delegation is deliberate and visible: anything resolved through `__getattr__`
is a dependency this layer still has on the one above it, and narrowing that
list is what the later phases are for.
"""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING, Optional

from web_lobster.core.values import origin_of
from web_lobster.core.schemas import (
    Action,
    ActionType,
    Observation,
    SubGoal,
    SubGoalStatus,
)
from web_lobster.mandate.enforcer import (
    MandateExpiredError,
    Violation,
    collapse_violations,
)
from web_lobster.utils.logging import get_logger
from web_lobster.verify.evidence import EvidenceCheck, EvidenceContext, evaluate
from web_lobster.mandate.schema import url_matches

if TYPE_CHECKING:
    from web_lobster.core.orchestrator import Orchestrator

logger = get_logger("orchestrator")


class StepRunner:
    """One sub-goal at a time, against the live browser."""

    def __init__(self, agent: "Orchestrator"):
        self.agent = agent
        # URLs that led nowhere, so the executor isn't allowed to try them twice.
        self._dead_urls: dict[str, str] = {}
        # Block signatures the executor has already been told about, once per run.
        self._reported_blocks: set[str] = set()
        # The action whose outcome is worked out on the next step.
        self._pending_outcome: Optional[tuple[Action, str, bool]] = None
        # Checks dropped because the page they name turned out not to exist.
        self._dropped_checks: list[str] = []

    def start_run(self) -> None:
        """Forget what the last run learned about the world."""
        self._dead_urls.clear()
        self._reported_blocks.clear()
        self._dropped_checks.clear()
        self._pending_outcome = None

    def __getattr__(self, name: str):
        """Anything this layer doesn't own belongs to the orchestrator.

        Keeps the extraction a move rather than a rewrite, and leaves the
        remaining coupling in plain sight.
        """
        if name.startswith("__") or name == "agent":
            raise AttributeError(name)
        return getattr(self.agent, name)

    async def run(self, sub_goal) -> bool:
        """Run the Observe → Reflect → Act loop for a single sub-goal."""
        # Models that read pages see {{$name}} references filled in; the plan keeps
        # the references, so filled-in text never flows back to the planner.
        goal_view = self._render_goal(sub_goal)
        # Per-goal state the layer above reads too — verification, skip-ahead and
        # receipts all need it — so it is set there, not here. Writing it on self
        # would leave the orchestrator reading a stale value.
        self.agent._goal_violations_start = len(self.enforcer.violations) if self.enforcer else 0
        self.agent._goal_network_start = self.browser.network.mark()
        self.agent._goal_start_url = self.browser.current_url
        self.agent._last_receipt = None
        self.safety.reset_counter()
        self.stuck_detector.reset()
        # Accumulated reflections: persist across multiple stuck-then-retry cycles
        # so the model never loses the diagnostic history of this sub-goal.
        reflections: list[str] = []
        went_back_from_nothing = False
        proven_at_start = await self._cheap_checks(sub_goal)

        proven, self.agent._proven_by_skip = self._proven_by_skip is sub_goal, None
        if (proven or await self._already_there(sub_goal)) and await self._verify(
            sub_goal, goal_view, self.state.current_observation
        ):
            logger.info("subgoal_already_met", id=sub_goal.id)
            return True

        for attempt in range(sub_goal.max_attempts):
            sub_goal.attempts = attempt + 1
            action_count_this_attempt = 0
            max_actions = self.config.safety.max_actions_per_subgoal

            while action_count_this_attempt < max_actions and not self.state.is_over_budget:
                self.state.step_count += 1
                action_count_this_attempt += 1

                await self._ui_wait_if_paused()

                if self.enforcer and self.enforcer.mandate.is_expired():
                    raise MandateExpiredError("Mandate expired before the task finished")

                # --- OBSERVE ---
                dom_mode = self.config.agent.dom_mode or \
                           self.config.agent.screenshot_mode == "dom_only"
                include_screenshot = not dom_mode
                observation = await self.browser.observer.observe(
                    include_screenshot=include_screenshot,
                    extract_dom=dom_mode,
                )
                if self.enforcer:
                    observation = self.enforcer.redact_observation(observation)
                self.state.current_observation = observation
                if observation.url.startswith(("http://", "https://")):
                    self._visited_origins.add(origin_of(observation.url))
                await self._ui_emit("on_observation", observation)

                # --- WHAT DID THE LAST ACTION DO? ---
                # The executor used to see only what it had tried, never what came
                # of it, so nothing stopped it making the same guess three times.
                if self._pending_outcome:
                    last, before_url, ok = self._pending_outcome
                    last.outcome = await self._describe_outcome(last, before_url, ok, observation)
                    self._pending_outcome = None

                # --- EVIDENCE THAT POINTS AT A PAGE THAT ISN'T THERE ---
                # The planner can't see pages, so a url it writes is either from
                # the task, the start page, or invented — and an invented one is
                # sometimes right (/wiki/Mount_Everest) and sometimes not
                # (/speakers/2026). Code can't tell in advance, but the moment the
                # browser proves the page doesn't exist, the check can never pass.
                # Drop it and carry on with the page that does exist, rather than
                # bouncing between a check it can't meet and a URL it's refused.
                for check, dead in self._unreachable_checks(sub_goal):
                    why = self._dead_urls.get(dead.rstrip("/"), "doesn't exist")
                    logger.warning("evidence_url_is_dead", id=sub_goal.id, url=dead)
                    sub_goal.evidence = [c for c in sub_goal.evidence if c is not check]
                    goal_view.evidence = [c for c in goal_view.evidence if c.describe() != check.describe()]
                    self._dropped_checks.append(f"{check.describe()} ({dead} {why})")
                    reflections.append(
                        f"Ignore {dead}: it {why}. That page is not where this step gets "
                        "finished — work with the page you're on."
                    )

                # --- A PAGE WITH NOTHING ON IT ---
                # A guessed URL can land on a page with no elements and no text,
                # and the executor then picks elements that aren't there for the
                # rest of its budget: 35 steps of it in live run 28. Go back once,
                # and if that isn't better, end the attempt so the plan can change.
                # Only without a screenshot: a vision model can work a page that
                # is all canvas or image, where there is nothing to read or click.
                if dom_mode and observation.is_empty:
                    logger.warning("empty_page", url=observation.url)
                    if went_back_from_nothing:
                        reflections.append(
                            f"{observation.url} has nothing on it, and going back didn't help. "
                            "The URL is probably wrong."
                        )
                        break
                    went_back_from_nothing = True
                    await self.browser.execute(Action(action=ActionType.GO_BACK))
                    self.state.action_history.append(
                        Action(action=ActionType.GO_BACK, reason="Nothing on the page")
                    )
                    continue

                # --- ALREADY PROVEN? ---
                # The executor kept acting after a sub-goal was met: on the
                # practice site it signed in, clicked on into the menu, and
                # logged itself back out (live run 28). Checked here, on a page
                # that has settled, and only when a check that was failing when
                # this goal began now passes — otherwise a goal whose url
                # already matched would finish without doing its work.
                if proven_at_start is not None and action_count_this_attempt > 1:
                    now = await self._cheap_checks(sub_goal)
                    if now and all(now.values()) and any(
                        not was for name, was in proven_at_start.items() if name in now
                    ):
                        logger.info("subgoal_proven_mid_step", id=sub_goal.id)
                        if await self._verify(sub_goal, goal_view, observation):
                            return True
                        proven_at_start = None  # a value is missing; let it work on

                # --- LOGIN DETECTION ---
                if observation.login_detected and not getattr(self, "_login_alerted", False):
                    self._login_alerted = True
                    logger.info("login_required", url=observation.url)
                    if self.ui:
                        await self.ui.on_login_required(observation.url)
                    else:
                        logger.warning("login_page_detected_no_ui")
                elif not observation.login_detected:
                    self._login_alerted = False

                # --- THINK: executor with visual grounding + reflection context ---
                # Pass at most the last 2 reflections so the model has full context
                # of why previous attempts failed, even across multiple stuck cycles.
                combined_reflection = "\n\n".join(reflections[-2:]) if reflections else None
                action = await self.executor.decide(
                    observation=observation,
                    sub_goal=goal_view,
                    # Long enough to still show a page that led nowhere: at five
                    # lines, three refusals pushed the reason off the end.
                    action_history_text=self.state.action_history_summary(n=10),
                    actions_taken_this_subgoal=action_count_this_attempt - 1,
                    reflection=combined_reflection,
                    mcp_tools=self.mcp.tools if self.mcp.has_tools else None,
                    data_placeholders=self.enforcer.placeholder_names() if self.enforcer else None,
                    briefing=self.context.for_executor() if self.context else None,
                )

                # --- MCP TOOL CALL ---
                if action.action == ActionType.MCP_TOOL:
                    tool_name = action.mcp_tool_name or ""
                    tool_args = action.mcp_tool_args or {}
                    logger.info("mcp_tool_call", tool=tool_name, args=str(tool_args)[:80])
                    result = await self.mcp.call_tool(tool_name, tool_args)
                    await self._ui_emit("on_action", action, self.state.step_count)
                    # Record in action history so the executor can see the result
                    self.state.action_history.append(
                        Action(
                            action=ActionType.MCP_TOOL,
                            mcp_tool_name=tool_name,
                            mcp_tool_args=tool_args,
                            reason=result[:500],
                        )
                    )
                    continue

                # --- TERMINAL: done ---
                if action.action == ActionType.DONE:
                    self.state.action_history.append(action)
                    await self._ui_emit("on_action", action, self.state.step_count)
                    if await self._verify(sub_goal, goal_view, observation):
                        return True
                    break

                # --- ELEMENT ID VALIDATION ---
                # Catch hallucinated or stale element IDs before they cause a
                # 30-second Playwright timeout that wastes the step budget.
                actions_needing_element = {
                    ActionType.CLICK, ActionType.TYPE,
                    ActionType.SELECT, ActionType.HOVER,
                }
                if action.action in actions_needing_element and action.element_id is not None:
                    valid_ids = {el.id for el in observation.elements}
                    if action.element_id not in valid_ids:
                        logger.warning(
                            "invalid_element_id",
                            element_id=action.element_id,
                            valid_count=len(valid_ids),
                        )
                        self.state.action_history.append(
                            Action(
                                action=ActionType.WAIT,
                                seconds=1,
                                reason=f"element_id {action.element_id} not on page (valid: {sorted(valid_ids)[:10]})",
                            )
                        )
                        continue

                # --- A URL THAT ALREADY LED NOWHERE ---
                # Live, the executor navigated to a made-up /2027 page three
                # times in one run. It gets told once; after that it's refused,
                # so the budget goes on the page it actually has.
                if action.action == ActionType.NAVIGATE and action.url:
                    why = self._dead_urls.get(action.url.rstrip("/"))
                    if why:
                        logger.warning("dead_url_refused", url=action.url)
                        self.state.action_history.append(Action(
                            action=ActionType.WAIT,
                            reason=f"{action.url} was already tried and {why}. Use the page you're on.",
                        ))
                        continue

                # --- NOTHING TO ENTER ---
                # Typing with no text clears the field and costs a step. Five of
                # Google Flights' forty steps went that way in live run 27.
                if action.action in (ActionType.TYPE, ActionType.SELECT) and not (action.text or "").strip():
                    logger.warning("type_without_text", element_id=action.element_id)
                    self.state.action_history.append(Action(
                        action=ActionType.WAIT,
                        reason=f"{action.action.value} needs text to enter; nothing was given",
                    ))
                    continue

                # --- MANDATE CHECK (deterministic; the model can't argue past it) ---
                if self.enforcer:
                    violation = self.enforcer.check_action(action, observation.url)
                    if violation:
                        await self._report_violation(action, violation)
                        continue

                # --- SAFETY CHECK ---
                verdict = self.safety.check(action, observation)
                if not verdict.allowed:
                    logger.warning("action_blocked", reason=verdict.reason)
                    await self._ui_emit("on_safety_flag", action, verdict.reason)
                    self.state.action_history.append(
                        Action(action=ActionType.WAIT, reason=f"Blocked: {verdict.reason}")
                    )
                    continue

                if verdict.needs_confirmation and self._mandate_approves(action, observation.url):
                    # The user already approved entering this data on this site, in the mandate.
                    logger.info("confirmation_covered_by_mandate", reason=verdict.reason)
                elif verdict.needs_confirmation:
                    logger.warning("action_needs_confirmation", reason=verdict.reason)
                    await self._ui_emit("on_safety_flag", action, verdict.reason)
                    confirmed = await self._request_confirmation(action, verdict.reason)
                    if not confirmed:
                        self.state.action_history.append(
                            Action(action=ActionType.WAIT, reason="User declined action")
                        )
                        continue

                # --- ACT ---
                action_desc = action.action.value
                if action.element_id:
                    action_desc += f"(el={action.element_id})"
                if action.text:
                    action_desc += f' "{action.text[:25]}"'
                logger.step(self.state.step_count, self.state.max_steps, action_desc)
                if not self.ui:
                    self.display.update_action(
                        self.state.step_count, action_desc, observation.url
                    )
                await self._ui_emit("on_action", action, self.state.step_count)
                # Give the controller the current observation so it can use
                # bbox coordinates and stable selectors instead of data-wl-id.
                self.browser.last_observation = observation
                success = await self.browser.execute(action)
                self.state.action_history.append(action)
                await self._ui_emit("on_action_result", success)

                if self.enforcer:
                    await self._enforce_after_action(action)

                if not success:
                    logger.warning("action_execution_failed", action=action.action.value)

                # What it did is worked out at the top of the next step, once the
                # page has landed: read here, a click's navigation hasn't
                # committed yet and every one of them looks like "no change".
                self._pending_outcome = (action, observation.url, success)

                # --- STUCK DETECTION with smarter tracking ---
                self.stuck_detector.record(
                    action.action.value,
                    action.element_id,
                    observation.url,
                )
                if self.stuck_detector.is_stuck():
                    logger.warning("stuck_detected", attempt=attempt + 1)
                    # Reflect on why we're stuck and accumulate the diagnosis.
                    # We keep reflections across attempts so the model always has
                    # the full history of what went wrong in this sub-goal.
                    try:
                        new_reflection = await self.executor.reflect(
                            observation=observation,
                            sub_goal=goal_view,
                            failed_actions=self.stuck_detector.get_failed_actions(),
                        )
                        reflections.append(new_reflection)
                        logger.info("reflection_ready", total=len(reflections), text=new_reflection[:80])
                    except Exception:
                        pass  # reflection is optional; continue without it
                    self.stuck_detector.reset()
                    break

            # After action budget exhausted, check if sub-goal is actually done
            observation = await self.browser.observer.observe(include_screenshot=True)
            if self.enforcer:
                observation = self.enforcer.redact_observation(observation)
            self.state.current_observation = observation
            if await self._verify(sub_goal, goal_view, observation):
                return True

        return False

    def _mandate_approves(self, action: Action, page_url: str) -> bool:
        """Whether the mandate already approved this action: entering only granted
        data where allowed. select counts too, since it types into comboboxes."""
        if not self.enforcer or action.action not in (ActionType.TYPE, ActionType.SELECT) or not action.text:
            return False
        return self.enforcer.approves_entry(action.text, page_url)

    async def _report_violation(self, action: Action, violation: Violation, count: int = 1) -> None:
        """Surface a blocked action in the UI and in the executor's history."""
        detail = violation.detail + (f" ({count} times)" if count > 1 else "")
        await self._ui_emit("on_safety_flag", action, f"Mandate: {detail}")
        self.state.action_history.append(
            Action(action=ActionType.WAIT, reason=f"Blocked by mandate: {detail}")
        )

    async def _enforce_after_action(self, action: Action) -> None:
        """Report what the network layer blocked during the action, and step off
        any page that ended up outside the mandate anyway."""
        if action.action in (ActionType.CLICK, ActionType.NAVIGATE, ActionType.SELECT, ActionType.GO_BACK):
            # A click's navigation reaches the network layer a few ms after the
            # click returns; wait for the verdict so a block is handled on this step.
            await self.enforcer.wait_for_pending(timeout=0.25)
        blocked = self.enforcer.drain()
        # A page can repeat the same blocked request many times; tell the executor once.
        for violation, count in collapse_violations(blocked):
            # And a single-page app repeats it on every step: Google Flights' own
            # telemetry filled half the executor's history in live run 27. Every
            # block is still recorded and counted; the executor hears each one once.
            signature = f"{violation.kind.value} {violation.method or ''} {violation.url.split('?')[0]}"
            if signature in self._reported_blocks:
                continue
            self._reported_blocks.add(signature)
            await self._report_violation(action, violation, count)
        if any(v.main_frame for v in blocked):
            # A blocked navigation leaves Chromium's error page behind; put the
            # agent back on the page it was working on.
            await self.browser.recover_from_blocked_navigation()
        violation = self.enforcer.check_page(self.browser.current_url)
        if violation:
            await self._report_violation(action, violation)
            await self.browser.leave_page()

    async def _describe_outcome(
        self, action: Action, before_url: str, success: bool, now: Observation
    ) -> str:
        """One line on what an action did, for the executor's own history.

        Only what code can see: where the page went, what it answered, and
        whether there is anything on it. A navigation that led nowhere is
        remembered, so the same guess can't be made twice.
        """
        if not success:
            return "the browser couldn't do it"

        url, status = now.url, self.browser.page_status
        moved = url != before_url
        if action.action in (ActionType.NAVIGATE, ActionType.CLICK, ActionType.GO_BACK):
            if status is not None and status >= 400:
                self._remember_dead(action, url, f"answered {status}")
                return f"{url} answered {status}"
            if now.is_empty:
                self._remember_dead(action, url, "had nothing on it")
                return f"went to {url}, which has nothing on it"
            if not moved:
                return "the page didn't change"
            return f"went to {url}"

        if action.action in (ActionType.TYPE, ActionType.SELECT) and action.text:
            held = await self.browser.field_values()
            entered = any(_normalise(action.text) in _normalise(value) for value in held)
            if not entered:
                return "the field didn't keep it — it may need choosing from a list"
            return "the field holds it" + (f", and the page moved to {url}" if moved else "")

        return f"the page moved to {url}" if moved else ""

    def _unreachable_checks(self, sub_goal: SubGoal) -> list[tuple[EvidenceCheck, str]]:
        """Url checks pointing at pages the browser has proved don't exist."""
        found = []
        for check in sub_goal.evidence:
            if check.type != "url":
                continue
            for dead in self._dead_urls:
                if url_matches(dead, check.pattern):
                    found.append((check, dead))
                    break
        return found

    def _remember_dead(self, action: Action, landed: str, why: str) -> None:
        """A URL that led nowhere, by what was asked for and where it ended up."""
        for url in {action.url, landed}:
            if url:
                self._dead_urls[url.rstrip("/")] = why

    async def _cheap_checks(self, sub_goal: SubGoal) -> Optional[dict[str, bool]]:
        """How a sub-goal's code-checkable evidence stands, without a model call.

        Value checks are left out: reading a value costs a model call, so those
        wait for the executor to say it's done. None when there's nothing here
        that code can settle.
        """
        checks = [check for check in sub_goal.evidence if check.type in ("url", "text")]
        if not checks:
            return None
        context = EvidenceContext(
            page_url=self.browser.current_url,
            page_text=await self.browser.page_text(),
            events=self.browser.network.since(self._goal_network_start),
            values=dict(self.state.values),
            page_status=self.browser.page_status,
            page_fields=await self.browser.field_values(),
        )
        return {check.describe(): evaluate(check, context).passed for check in checks}

    def action_trail(self) -> list[str]:
        """One line per action taken, for reading a failed run afterwards.

        Blocked and declined actions are in here too, as the WAIT entries the
        executor was given, which is usually what a stuck run needs explaining.
        Text is redacted, since a typed action can carry a granted value.
        """
        return [
            f"{step}. {self._action_line(act)}"
            for step, act in enumerate(self.state.action_history, start=1)
        ]

    def _safe(self, value: str) -> str:
        return (self.enforcer.redact(value) if self.enforcer else value)[:120]

    def _action_line(self, act: Action) -> str:
        parts = [act.action.value]
        if act.element_id is not None:
            parts.append(f"(el={act.element_id})")
        if act.mcp_tool_name:
            parts.append(act.mcp_tool_name)
        if act.text:
            parts.append(f'"{self._safe(act.text)}"')
        if act.url:
            parts.append(self._safe(act.url))
        if act.reason:
            parts.append(f"({self._safe(act.reason)})")
        if act.outcome:
            parts.append(f"→ {self._safe(act.outcome)}")
        return " ".join(parts)

    async def _request_confirmation(self, action: Action, reason: str) -> bool:
        """Request user confirmation for a high-risk action.

        When the UI is connected, shows an interactive modal.
        In CLI/headless mode, auto-declines for safety.
        """
        if self.ui:
            return await self.ui.request_confirmation(action, reason)
        # No UI — auto-decline in safety-first mode
        logger.info("auto_declining_risky_action", reason=reason)
        return False


def _normalise(text: str) -> str:
    return " ".join(text.split()).casefold()
