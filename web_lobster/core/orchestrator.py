"""Orchestrator — the main agent loop that coordinates all components.

This is the brain of Web Lobster. It runs the Observe → Think → Act loop,
manages sub-goal progression, handles failures and re-planning, and
enforces safety rails.

Flow:
  0. Think first, before the browser opens (core/briefing.py): the planner writes
     a brief, code checks it against the mandate, and its questions are settled
     by the user, supplied answers, or defaults. A run that still needs answers
     stops here.
  1. Planner decomposes task → sub-goals, then code reviews the plan against the
     mandate and the planner gets one round to fix what's found
  2. For each sub-goal:
     a. Observer captures page state
     b. Executor picks an action
     c. Mandate (if any) and safety gate check the action
     d. Browser executes the action
     e. Repeat until executor says "done" or budget exhausted
  3. Verify the sub-goal: its evidence checks, run in code, or the validator
     model when it declared none. Either way, draft a receipt.
  4. Seal the sub-goal's receipt into the chain. If failed → retry or re-plan
  5. If succeeded → keep any values it read, then next sub-goal

Trust boundary: the planner never sees page content. It gets the user's task,
its own sub-goals, counts, the current origin, and type-checked values
(core/values.py). Everything that reads pages (executor, validator, extractor,
answer extraction) is quarantined: its free text never reaches the planner.
"""

from __future__ import annotations

import asyncio
import re
import time
from pathlib import Path
from collections import Counter
from typing import Optional

from web_lobster.core.schemas import (
    Action,
    ActionType,
    AgentState,
    SubGoal,
    SubGoalStatus,
    TaskPlan,
    ValidationResult,
)
from web_lobster.core.briefing import (
    MANDATE_QUESTION_ID,
    MAX_NOTES,
    Briefing,
    PlanningContext,
    Question,
    TaskBrief,
    mandate_gaps,
    mandate_lines,
    mandate_question,
    origin_strings,
    resolve_answers,
    review_plan,
)
from web_lobster.core.config import WebLobsterConfig
from web_lobster.core.values import ExtractedValue, ValueSpec, origin_of, render_value_refs
from web_lobster.models.planner import Planner
from web_lobster.models.executor import Executor
from web_lobster.models.validator import Validator
from web_lobster.models.extractor import PAGE_TEXT_LIMIT, Extractor
from web_lobster.models.ollama_backend import OllamaBackend
from web_lobster.models.llamacpp_backend import LlamaCppBackend
from web_lobster.models.anthropic_backend import AnthropicBackend
from web_lobster.browser.controller import BrowserController
from web_lobster.actions.safety import SafetyGate
from web_lobster.tools.mcp_manager import MCPManager
from web_lobster.memory.task_memory import TaskMemory, TaskRecord
from web_lobster.mandate.enforcer import MandateEnforcer, MandateExpiredError, Violation
from web_lobster.mandate.schema import Mandate
from web_lobster.verify.evidence import EvidenceContext, evaluate
from web_lobster.verify.receipts import (
    MAX_RECEIPT_WRITES,
    ModelVerdict,
    Receipt,
    ReceiptLog,
    page_label,
)
from web_lobster.utils.logging import get_logger, TaskDisplay
from web_lobster.utils.retry import StuckDetector, retry_async

logger = get_logger("orchestrator")


# Sub-goals that only take the browser to a page.
_NAVIGATION_GOAL = re.compile(
    r"^\s*(open|go to|navigate to|visit|load|return to|get to)\b", re.IGNORECASE
)


def collapse_violations(violations: list[Violation]) -> list[tuple[Violation, int]]:
    """Group repeats of the same blocked request, keeping the first of each and a count."""
    groups: dict[tuple, list] = {}
    for violation in violations:
        key = (violation.kind, violation.detail)
        if key in groups:
            groups[key][1] += 1
        else:
            groups[key] = [violation, 1]
    return [(violation, count) for violation, count in groups.values()]


class Orchestrator:
    """Main agent loop coordinator."""

    def __init__(
        self,
        config: WebLobsterConfig,
        shared_state=None,
        mandate: Optional[Mandate] = None,
        asker=None,
    ):
        """asker answers the brief's questions: an async callable taking the open
        questions and the brief, returning answers by id (or None to skip). Without
        one, the UI's answer_questions is used when there is a UI."""
        self.config = config
        self.asker = asker
        # Trusted context for the planner, built before the browser opens (think())
        self.context: Optional[PlanningContext] = None
        self._plan_issues: list[str] = []
        # Steps each sub-goal took and the origins the run visited, for memory
        self._goal_steps: dict[int, int] = {}
        self._visited_origins: set[str] = set()

        # Mandate enforcement — None keeps the unscoped behaviour
        self.enforcer = MandateEnforcer(mandate) if mandate else None

        # Initialize model backends
        self.planner_backend = self._create_backend(config.planner)
        self.executor_backend = self._create_backend(config.executor)
        self.validator_backend = self._create_backend(config.validator)

        # Initialize models
        self.planner = Planner(self.planner_backend, config.planner.temperature)
        self.executor = Executor(
            self.executor_backend,
            config.executor.temperature,
            use_grammar=(config.executor.backend == "llamacpp"),
        )
        self.validator = Validator(
            self.validator_backend,
            config.validator.temperature,
            use_vision=config.validator.vision,
        )

        # Reads declared values off pages for the planner. It sees page text, so
        # it runs on the executor's quarantined side; see core/values.py.
        self.extractor = Extractor(self.executor_backend, config.executor.temperature)
        self._goal_violations_start = 0

        # One receipt per finished sub-goal, chained so later edits are detectable
        self.receipts = ReceiptLog()
        self._last_receipt: Optional[Receipt] = None
        self._goal_network_start = 0
        self._goal_start_url = ""
        # A later sub-goal the browser reached while an earlier one failed
        self._proven_by_skip: Optional[SubGoal] = None

        # Initialize browser and safety
        self.browser = BrowserController(config.browser, enforcer=self.enforcer)
        self.safety = SafetyGate(config.safety)
        self.stuck_detector = StuckDetector()

        # MCP server manager
        self.mcp = MCPManager(config.mcp_servers)

        # Agent state
        self.state = AgentState(max_steps=config.agent.max_steps)

        # Episodic memory — persists across runs, improves planning over time
        self.memory = TaskMemory()

        # Live terminal display (CLI only, not used when UI is attached)
        self.display = TaskDisplay()

        # UI shared state (optional — None when running from CLI)
        self.ui = shared_state

    def _create_backend(self, model_config):
        """Create the appropriate backend based on config."""
        if model_config.backend == "llamacpp":
            return LlamaCppBackend(
                model_path=model_config.model,
                timeout=model_config.timeout,
            )
        elif model_config.backend == "anthropic":
            return AnthropicBackend(
                model=model_config.model,
                api_key=model_config.api_key,  # None = fall back to ANTHROPIC_API_KEY env var
                timeout=model_config.timeout,
            )
        else:
            return OllamaBackend(
                model=model_config.model,
                base_url=model_config.base_url,
                timeout=model_config.timeout,
                context_window=model_config.context_window,
            )

    async def _ui_emit(self, method_name: str, *args, **kwargs) -> None:
        """Call a shared state method if UI is connected."""
        if self.ui:
            method = getattr(self.ui, method_name, None)
            if method:
                await method(*args, **kwargs)

    async def _ui_wait_if_paused(self) -> None:
        """Block if the user paused from the UI."""
        if self.ui:
            await self.ui.wait_if_paused()

    def recall(self, task: str, start_url: str) -> tuple[Optional[str], int]:
        """Past experience for the planner: similar tasks, and runs on the same sites."""
        origins = [origin_of(start_url)]
        if self.enforcer:
            origins += origin_strings(self.enforcer.mandate.origins)
        memory_context = self.memory.format_for_prompt(task, origins=origins)
        memory_hits = len(self.memory.find_similar(task)) if memory_context else 0
        if memory_context:
            logger.info("memory_context", hits=memory_hits)
        return memory_context, memory_hits

    async def run(
        self,
        task: str,
        start_url: str = "https://www.google.com",
        *,
        notes: Optional[str] = None,
        requested_values: Optional[list[ValueSpec]] = None,
        brief: Optional[TaskBrief] = None,
        answers: Optional[dict] = None,
    ) -> AgentResult:
        """Execute a complete task from start to finish.

        Args:
            task: Natural language task description
            start_url: URL to start the browser at
            notes: The user's standing notes for the planner (trusted)
            requested_values: Values the caller wants back, so the plan reaches them
            brief: A brief written earlier, to run without rethinking the task
            answers: Answers to the brief's questions, by question id

        Returns:
            AgentResult with success status and details
        """
        logger.info("task_start", task=task)
        start_time = time.time()
        memory_context, memory_hits = self.recall(task, start_url)

        await self._ui_emit("on_task_start", task, memory_hits=memory_hits)

        # Think first. This runs before the time limit starts: a person may be answering.
        briefing = await self.think(
            task, start_url, notes=notes, requested_values=requested_values,
            brief=brief, answers=answers, memory_context=memory_context,
        )
        if briefing.needs_input or briefing.declined:
            return self._stopped_before_start(task, start_time, briefing)
        context = briefing.context

        # Start live display (CLI mode only)
        if not self.ui:
            self.display.start(task, self.config.agent.max_steps, memory_hits)

        # Optionally cap total wall-clock time
        max_seconds = self.config.agent.max_seconds
        if max_seconds > 0:
            try:
                return await asyncio.wait_for(
                    self._run_inner(task, start_url, start_time, context, memory_hits),
                    timeout=max_seconds,
                )
            except asyncio.TimeoutError:
                logger.error("task_timeout", seconds=max_seconds)
                if not self.ui:
                    self.display.stop()
                await self.browser.close()
                return AgentResult(
                    success=False,
                    task=task,
                    plan=self.state.plan,
                    steps_taken=self.state.step_count,
                    elapsed_seconds=time.time() - start_time,
                    error=f"Timed out after {max_seconds}s",
                    violations=self._violations(),
                    values=dict(self.state.values),
                    receipts=list(self.receipts.receipts),
                    **self._briefing_fields(),
                )
        else:
            return await self._run_inner(task, start_url, start_time, context, memory_hits)

    async def think(
        self,
        task: str,
        start_url: str,
        *,
        notes: Optional[str] = None,
        requested_values: Optional[list[ValueSpec]] = None,
        brief: Optional[TaskBrief] = None,
        answers: Optional[dict] = None,
        memory_context: Optional[str] = None,
    ) -> Briefing:
        """Think the task through before the browser opens: write a brief, check it
        against the mandate, and settle the questions it raises.

        Supplied answers count first. If questions remain and someone can be asked
        (the dashboard, a terminal), they're asked, and defaults and the planner's
        judgement cover whatever they skip. If nobody can be asked, defaults apply,
        and a question without one blocks the run unless agent.questions is "assume".
        Nothing here reads a page: the browser hasn't started.
        """
        mandate = self.enforcer.mandate if self.enforcer else None
        context = PlanningContext(
            task=task,
            mandate=mandate_lines(mandate) if mandate else [],
            requested_values=list(requested_values or []),
            notes=notes if notes is not None else self._notes_from_config(),
            memory=memory_context,
            given=self._given(answers),
        )
        if brief is None and self.config.agent.briefing and hasattr(self.planner, "brief"):
            try:
                brief = await self.planner.brief(task, context.render(), start_url)
            except Exception as e:
                logger.warning("brief_failed", error=str(e)[:160])
        context.brief = brief

        questions = list(brief.questions) if brief else []
        if brief and mandate:
            context.gaps = mandate_gaps(brief, mandate)
            gap_question = mandate_question(context.gaps)
            if gap_question:
                questions.append(gap_question)
        context.questions = questions
        # Answers that match a question are shown with it; the rest stay as given.
        context.given = {k: v for k, v in context.given.items() if k not in {q.id for q in questions}}
        if brief:
            logger.info("brief_ready", questions=len(questions), gaps=len(context.gaps))
            await self._ui_emit("on_brief_ready", brief, context.gaps)

        assume = self.config.agent.questions == "assume"
        resolution = resolve_answers(questions, answers, use_defaults=False)
        asker = self._asker()
        blocking: list[Question] = []
        if resolution.unanswered and asker and not assume:
            try:
                given = await asker(resolution.unanswered, brief)
            except Exception as e:
                logger.warning("asking_failed", error=str(e)[:160])
                given = None
            resolution = resolve_answers(questions, {**(answers or {}), **(given or {})}, use_defaults=True)
            # They were asked: whatever is still open, they chose to leave to the planner.
        else:
            resolution = resolve_answers(questions, answers, use_defaults=True)
            if not assume:
                blocking = resolution.unanswered

        context.answers = resolution.answers
        self.context = context
        return Briefing(
            context=context,
            unanswered=blocking,
            problems=resolution.problems,
            declined=context.answers.get(MANDATE_QUESTION_ID) is False,
        )

    @staticmethod
    def _given(answers: Optional[dict]) -> dict:
        """Answers supplied before any question was asked, under the user's own labels."""
        return {
            str(key): value if isinstance(value, (bool, int, float, str)) else str(value)
            for key, value in list((answers or {}).items())[:10]
            if value is not None
        }

    def _asker(self):
        if self.asker:
            return self.asker
        return getattr(self.ui, "answer_questions", None) if self.ui else None

    def _notes_from_config(self) -> Optional[str]:
        path = self.config.agent.notes_file
        if not path:
            return None
        try:
            return Path(path).expanduser().read_text()[:MAX_NOTES]
        except OSError as e:
            logger.warning("notes_unreadable", error=str(e)[:120])
            return None

    def _stopped_before_start(self, task: str, start_time: float, briefing: Briefing) -> AgentResult:
        """The result of a run that ended before the browser opened."""
        error = None
        if briefing.declined:
            error = "Not run: the user chose not to run a task that needs more than the mandate allows."
        logger.info("stopped_before_start", needs_input=briefing.needs_input, declined=briefing.declined)
        context = briefing.context
        return AgentResult(
            success=False,
            task=task,
            elapsed_seconds=time.time() - start_time,
            error=error,
            memory_hits=0,
            brief=context.brief,
            questions=briefing.unanswered,
            needs_input=briefing.needs_input,
            answers={**context.given, **context.answers},
            gaps=context.gaps,
            problems=briefing.problems,
        )

    async def _review_plan(self, plan: TaskPlan, context: PlanningContext, start_url: str) -> TaskPlan:
        """Check the plan in code, and give the planner one round to fix what's found.

        The revision is kept unless it leaves more problems than it fixed.
        """
        if not self.config.agent.plan_review:
            return plan
        mandate = self.enforcer.mandate if self.enforcer else None
        issues = review_plan(plan, mandate, self.config.agent.max_sub_goals)
        if issues and hasattr(self.planner, "revise"):
            logger.info("plan_review_issues", count=len(issues))
            await self._ui_emit("on_plan_review", issues)
            try:
                revised = await self.planner.revise(plan.task, plan, issues, context.render(), start_url)
            except Exception as e:
                logger.warning("plan_revision_failed", error=str(e)[:160])
                revised = []
            if revised:
                candidate = TaskPlan(task=plan.task, sub_goals=revised)
                remaining = review_plan(candidate, mandate, self.config.agent.max_sub_goals)
                if len(remaining) <= len(issues):
                    plan, issues = candidate, remaining
        self._plan_issues = issues
        return plan

    async def _run_inner(
        self,
        task: str,
        start_url: str,
        start_time: float,
        context: PlanningContext,
        memory_hits: int,
    ) -> AgentResult:
        try:
            # 1. Launch browser
            await self.browser.start(start_url)

            # 1b. Connect to MCP servers (if any configured). MCP tools act outside
            # the browser, where a mandate can't be enforced, so a mandate turns them off.
            if not self.enforcer:
                await self.mcp.start()
            elif self.config.mcp_servers:
                logger.warning("mcp_disabled_under_mandate")
            if self.mcp.has_tools:
                logger.info("mcp_ready", tool_count=len(self.mcp.tools))

            # 2. Plan the task (informed by episodic memory)
            plan = await retry_async(
                self.planner.plan, task,
                context.render(),
                start_url,
                max_retries=2,
                base_delay=2.0,
            )
            plan = await self._review_plan(plan, context, start_url)
            self.state.plan = plan
            logger.info("plan_ready", sub_goals=len(plan.sub_goals))
            await self._ui_emit("on_plan_ready", plan)
            if not self.ui:
                self.display.update_plan([
                    ("pending", sg.goal) for sg in plan.sub_goals
                ])

            # 3. Execute sub-goals via while loop so replanning works correctly.
            # A for loop holds a reference to the original list and misses new goals
            # appended by _replan. plan.current_goal always finds the first pending goal.
            while not self.state.is_over_budget:
                sub_goal = plan.current_goal
                if sub_goal is None:
                    break  # all goals completed or the only remaining ones are failed

                sub_goal.status = SubGoalStatus.ACTIVE
                logger.info("subgoal_start", id=sub_goal.id, goal=sub_goal.goal)
                await self._ui_emit("on_subgoal_start", sub_goal)

                steps_before = self.state.step_count
                success = await self._execute_subgoal(sub_goal)
                self._goal_steps[id(sub_goal)] = self.state.step_count - steps_before
                await self._seal_receipt()

                if success:
                    sub_goal.status = SubGoalStatus.COMPLETED
                    logger.success("subgoal_complete", id=sub_goal.id)
                    await self._ui_emit("on_subgoal_complete", sub_goal)
                    if not self.ui:
                        self._refresh_display()
                elif await self._skip_to_later_goal(plan, sub_goal):
                    continue  # the loop resumes at the later sub-goal, already met
                else:
                    if self.state.replan_count < self.state.max_replans:
                        logger.warning("subgoal_failed_replanning", id=sub_goal.id)
                        # Mark as skipped so current_goal doesn't return it again
                        sub_goal.status = SubGoalStatus.SKIPPED
                        replanned = await self._replan(sub_goal)
                        if not replanned:
                            sub_goal.status = SubGoalStatus.FAILED
                            logger.error("replan_failed", id=sub_goal.id)
                            break
                        # New goals were appended — while loop picks them up automatically
                    else:
                        sub_goal.status = SubGoalStatus.FAILED
                        logger.error("subgoal_failed", id=sub_goal.id)
                        break

            if self.state.is_over_budget:
                logger.warning("step_budget_exhausted", steps=self.state.step_count)

            # 4. Extract the answer from the final page. A run whose sub-goals were all
            # met before any action never observed a page, so don't require one.
            answer = await self._extract_answer(task, self.state.current_observation)

            # 5. Learn from this run and save to episodic memory
            elapsed = time.time() - start_time
            await self._save_to_memory(task, plan, answer, elapsed)

            # 6. Determine final result
            result = AgentResult(
                success=plan.is_complete,
                task=task,
                plan=plan,
                steps_taken=self.state.step_count,
                elapsed_seconds=elapsed,
                final_url=self.browser.current_url,
                answer=answer,
                memory_hits=memory_hits,
                violations=self._violations(),
                values=dict(self.state.values),
                receipts=list(self.receipts.receipts),
                **self._briefing_fields(),
            )

            if result.success:
                logger.success("task_complete", steps=result.steps_taken, time=f"{elapsed:.1f}s")
            else:
                logger.error("task_failed", steps=result.steps_taken, time=f"{elapsed:.1f}s")

            return result

        except Exception as e:
            error = self.enforcer.redact(str(e)) if self.enforcer else str(e)
            logger.error("task_error", error=error)
            return AgentResult(
                success=False,
                task=task,
                plan=self.state.plan,
                steps_taken=self.state.step_count,
                elapsed_seconds=time.time() - start_time,
                error=error,
                violations=self._violations(),
                values=dict(self.state.values),
                receipts=list(self.receipts.receipts),
                **self._briefing_fields(),
            )
        finally:
            if not self.ui:
                self.display.stop()
            await self.mcp.stop()
            await self.browser.close()

    async def _execute_subgoal(self, sub_goal) -> bool:
        """Run the Observe → Reflect → Act loop for a single sub-goal."""
        # Models that read pages see {{$name}} references filled in; the plan keeps
        # the references, so filled-in text never flows back to the planner.
        goal_view = self._render_goal(sub_goal)
        self._goal_violations_start = len(self.enforcer.violations) if self.enforcer else 0
        self._goal_network_start = self.browser.network.mark()
        self._goal_start_url = self.browser.current_url
        self._last_receipt = None
        self.safety.reset_counter()
        self.stuck_detector.reset()
        # Accumulated reflections: persist across multiple stuck-then-retry cycles
        # so the model never loses the diagnostic history of this sub-goal.
        reflections: list[str] = []

        proven, self._proven_by_skip = self._proven_by_skip is sub_goal, None
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
                    action_history_text=self.state.action_history_summary(),
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

    async def _already_there(self, sub_goal: SubGoal) -> bool:
        """Whether a sub-goal that only opens a page is already met, before any action.

        Plans often start with "Open the downloads page" when the browser starts
        there, and the executor then clicks around a page it should stay on. Only
        goals that open a page qualify: "Book the trip" can share its url check with
        the page the booking starts from.
        """
        return bool(_NAVIGATION_GOAL.match(sub_goal.goal)) and await self._page_proves(sub_goal)

    async def _page_proves(self, sub_goal: SubGoal) -> bool:
        """Whether the live browser already meets a sub-goal's evidence.

        Only evidence pinned to a page by a url check counts, and never a request
        check: a write has to happen during its sub-goal.
        """
        checks = sub_goal.evidence
        if not any(check.type == "url" for check in checks) or any(
            check.type == "request" for check in checks
        ):
            return False
        context = EvidenceContext(
            page_url=self.browser.current_url,
            page_text=await self.browser.page_text(),
            events=[],
            values=dict(self.state.values),
        )
        return all(evaluate(check, context).passed for check in checks)

    async def _skip_to_later_goal(self, plan: TaskPlan, failed: SubGoal) -> bool:
        """After a sub-goal fails, jump ahead if its actions reached a later one.

        Small planners split one search into "type the query", "click search" and
        "open the result". Typing and pressing Enter lands on the result, and the
        typing step then fails its own check. When the failed step took the browser
        to a page that proves a later sub-goal, the steps before that one are
        skipped instead of replanned. Never across a sub-goal with a request check:
        skipped sub-goals count toward completion, and a write it required would
        never have been proven.
        """
        pending = [goal for goal in plan.sub_goals if goal.status == SubGoalStatus.PENDING]
        for index, later in enumerate(pending):
            skipped = [failed, *pending[:index]]
            if any(check.type == "request" for goal in skipped for check in goal.evidence):
                return False
            if self._matched_at_goal_start(later) or not await self._page_proves(later):
                continue
            for goal in skipped:
                goal.status = SubGoalStatus.SKIPPED
            self._proven_by_skip = later
            logger.info("subgoals_superseded", failed=failed.id, resume_at=later.id)
            return True
        return False

    def _matched_at_goal_start(self, sub_goal: SubGoal) -> bool:
        """Whether the browser was already on a sub-goal's page when the current one began."""
        context = EvidenceContext(page_url=self._goal_start_url, page_text="", events=[], values={})
        url_checks = [check for check in sub_goal.evidence if check.type == "url"]
        return all(evaluate(check, context).passed for check in url_checks)

    def _mandate_approves(self, action: Action, page_url: str) -> bool:
        """Whether the mandate already approved this action: typing only granted data where allowed."""
        if not self.enforcer or action.action != ActionType.TYPE or not action.text:
            return False
        return self.enforcer.approves_entry(action.text, page_url)

    def _violations(self) -> list[Violation]:
        return list(self.enforcer.violations) if self.enforcer else []

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
            await self._report_violation(action, violation, count)
        if any(v.main_frame for v in blocked):
            # A blocked navigation leaves Chromium's error page behind; put the
            # agent back on the page it was working on.
            await self.browser.recover_from_blocked_navigation()
        violation = self.enforcer.check_page(self.browser.current_url)
        if violation:
            await self._report_violation(action, violation)
            await self.browser.leave_page()

    def _render_goal(self, sub_goal: SubGoal) -> SubGoal:
        """Copy of a sub-goal with {{$name}} references filled in for the executor."""
        return sub_goal.model_copy(update={
            "goal": render_value_refs(sub_goal.goal, self.state.values),
            "success_criteria": render_value_refs(sub_goal.success_criteria, self.state.values),
        })

    async def _read_values(self, sub_goal: SubGoal) -> dict[str, ExtractedValue]:
        """Read the values a sub-goal declared off the page it reached."""
        try:
            observation = await self.browser.observer.observe(
                include_screenshot=False, extract_dom=True
            )
        except Exception as e:
            logger.warning("value_observation_failed", error=str(e)[:120])
            return {}
        if self.enforcer:
            observation = self.enforcer.redact_observation(observation)
        # Read the page's main content: an observation's truncated text often stops
        # inside the site's menus. (The controller redacts it under a mandate.)
        main_text = await self.browser.page_text(main_only=True, limit=PAGE_TEXT_LIMIT)
        if main_text:
            observation = observation.model_copy(update={"page_text": main_text})
        values = await self.extractor.extract(observation, sub_goal.extract)
        missing = [spec.name for spec in sub_goal.extract if spec.name not in values]
        logger.info("values_read", read=sorted(values), missing=missing)
        return values

    async def _verify(self, sub_goal: SubGoal, goal_view: SubGoal, observation) -> bool:
        """Decide whether a sub-goal is done, and draft its receipt either way.

        With evidence declared, code decides: every check must pass against the
        live page, the network log, and typed values, and the validator model is
        not consulted. Without evidence the validator judges the page, and the
        receipt records that the result is a judgement, not a verification.
        """
        read = await self._read_values(sub_goal) if sub_goal.extract else {}

        if sub_goal.evidence:
            # Evidence can trail "done" by a moment: a redirect still landing, a
            # response not in yet. Re-check while requests are in flight. Waiting
            # can't make a false check pass, only let a true one arrive.
            loop = asyncio.get_running_loop()
            deadline = loop.time() + self.config.agent.evidence_wait_seconds
            while True:
                context = EvidenceContext(
                    page_url=self.browser.current_url,
                    page_text=await self.browser.page_text(),
                    events=self.browser.network.since(self._goal_network_start),
                    values={**self.state.values, **read},
                )
                checks = [evaluate(check, context) for check in sub_goal.evidence]
                achieved = all(check.passed for check in checks)
                if achieved or self.browser.network.settled() or loop.time() >= deadline:
                    break
                await asyncio.sleep(0.1)
            writes, page = self._receipt_facts()
            passed = sum(check.passed for check in checks)
            verdict = ValidationResult(
                achieved=achieved,
                confidence=1.0 if achieved else 0.0,
                observation=f"{passed}/{len(checks)} evidence checks passed",
            )
            receipt = Receipt(
                sub_goal_id=sub_goal.id, goal=sub_goal.goal, achieved=achieved,
                basis="evidence", checks=checks, page=page, writes=writes,
            )
            if not achieved:
                unmet = "; ".join(check.description for check in checks if not check.passed)
                # Tell the executor what's missing so it doesn't just claim done again.
                self.state.action_history.append(
                    Action(action=ActionType.WAIT, reason=f"Not done yet. Missing evidence: {unmet}")
                )
        else:
            verdict = await self.validator.validate(observation, goal_view)
            achieved = (
                verdict.achieved
                and verdict.confidence >= self.config.agent.validation_confidence_threshold
            )
            writes, page = self._receipt_facts()
            receipt = Receipt(
                sub_goal_id=sub_goal.id, goal=sub_goal.goal, achieved=achieved,
                basis="model", page=page, writes=writes,
                model_verdict=ModelVerdict(achieved=verdict.achieved, confidence=verdict.confidence),
            )
            if not achieved:
                logger.warning("validation_failed", confidence=verdict.confidence,
                               observation=verdict.observation[:80])

        await self._ui_emit("on_validation", verdict, sub_goal)
        if achieved:
            self.state.values.update(read)
        self._last_receipt = receipt
        return achieved

    def _receipt_facts(self) -> tuple[list, str]:
        """The write requests sent during the sub-goal, and the page it ended on."""
        events = self.browser.network.since(self._goal_network_start)
        writes = [event for event in events if event.is_write][-MAX_RECEIPT_WRITES:]
        return writes, page_label(self.browser.current_url)

    async def _seal_receipt(self) -> None:
        """Add the finished sub-goal's last verification to the receipt chain."""
        if self._last_receipt is None:
            return
        receipt = self.receipts.append(self._last_receipt)
        self._last_receipt = None
        logger.info("receipt_sealed", sub_goal=receipt.sub_goal_id,
                    achieved=receipt.achieved, basis=receipt.basis)
        await self._ui_emit("on_receipt", receipt)

    def _describe_failure(self, failed_goal: SubGoal) -> str:
        """Why a sub-goal failed, from counts and enums only: safe for the planner."""
        parts = [f"success criteria not confirmed after {failed_goal.attempts} attempt(s)"]
        receipt = self.receipts.last_for(failed_goal.id)
        if receipt and receipt.basis == "evidence":
            unmet = sorted({check.type for check in receipt.checks if not check.passed})
            if unmet:
                parts.append(f"evidence not met: {', '.join(unmet)} check(s)")
        if self.enforcer:
            kinds = Counter(
                v.kind.value for v in self.enforcer.violations[self._goal_violations_start:]
            )
            if kinds:
                blocked = ", ".join(f"{count}x {kind}" for kind, count in sorted(kinds.items()))
                parts.append(f"mandate blocked {blocked}")
        return "; ".join(parts)

    async def _extract_answer(self, task: str, observation=None) -> Optional[str]:
        """After task completion, ask the model to extract a direct answer from the page."""
        page_content = await self.browser.page_text(main_only=True, limit=PAGE_TEXT_LIMIT)
        if not page_content and observation:
            page_content = (observation.page_text or observation.accessibility_tree or "")[:3000]
        if not page_content:
            return None
        url = self.browser.current_url
        if self.enforcer:
            url = self.enforcer.redact(url)
        title = observation.title if observation else ""

        prompt = f"""The user asked: "{task}"

The agent has finished. Here is the final page content:

URL: {url}
Title: {title}

PAGE TEXT:
{page_content}

Based on what is on this page, provide a direct, concise answer to the user's question.
If the page contains a specific fact, date, name, or value they asked for, state it clearly.
If the task was an action (e.g. "search for X") rather than a question, summarise what was accomplished and what the page shows."""

        try:
            answer = await self.planner_backend.generate(
                prompt=prompt,
                system="You extract direct answers from web page content. Be concise and factual.",
                temperature=0.1,
                max_tokens=512,
            )
            logger.info("answer_extracted", length=len(answer))
            return answer.strip()
        except Exception as e:
            logger.warning("answer_extraction_failed", error=str(e))
            return None

    async def _replan(self, failed_goal) -> bool:
        """Request a revised plan from the planner after a failure."""
        self.state.replan_count += 1

        completed = [
            sg for sg in self.state.plan.sub_goals
            if sg.status == SubGoalStatus.COMPLETED
        ]

        try:
            # Only trusted facts go to the planner: its own sub-goals, counts, the
            # current origin, and type-checked values. Never page text or URLs.
            new_goals = await self.planner.replan(
                task=self.state.plan.task,
                completed_goals=completed,
                failed_goal=failed_goal,
                current_origin=origin_of(self.browser.current_url),
                failure=self._describe_failure(failed_goal),
                values=list(self.state.values.values()),
                context=self.context.render(include_memory=False) if self.context else None,
            )

            if not new_goals:
                return False

            # Append new goals to the END of the existing list (do not replace it).
            # The while loop in run() uses plan.current_goal which finds the first
            # PENDING goal — appended goals will be picked up automatically.
            self.state.plan.sub_goals.extend(new_goals)
            logger.info("replan_success", new_goals=len(new_goals))
            return True

        except Exception as e:
            logger.error("replan_error", error=str(e))
            return False

    def _refresh_display(self) -> None:
        """Sync the Rich live display with current plan state."""
        status_map = {
            SubGoalStatus.COMPLETED: "completed",
            SubGoalStatus.FAILED: "failed",
            SubGoalStatus.ACTIVE: "active",
            SubGoalStatus.PENDING: "pending",
            SubGoalStatus.SKIPPED: "skipped",
        }
        if self.state.plan:
            self.display.update_plan([
                (status_map.get(sg.status, "pending"), sg.goal)
                for sg in self.state.plan.sub_goals
            ])

    async def _save_to_memory(
        self,
        task: str,
        plan: TaskPlan,
        answer: Optional[str],
        elapsed: float,
    ) -> None:
        """Extract learnings and persist this run to episodic memory."""
        completed = [sg for sg in plan.sub_goals if sg.status == SubGoalStatus.COMPLETED]
        failed = [sg for sg in plan.sub_goals if sg.status == SubGoalStatus.FAILED]

        learnings = None
        try:
            learnings = await self.planner.extract_learnings(
                task=task,
                completed_goals=completed,
                failed_goals=failed,
            )
        except Exception as e:
            logger.warning("learnings_extraction_failed", error=str(e))

        record = TaskRecord(
            task=task,
            success=plan.is_complete,
            final_url=self.browser.current_url,
            steps_taken=self.state.step_count,
            elapsed_seconds=elapsed,
            timestamp=time.time(),
            sub_goals=[sg.goal for sg in plan.sub_goals],
            completed_goals=[sg.goal for sg in completed],
            replanned_goals=[
                sg.goal for sg in plan.sub_goals if sg.status == SubGoalStatus.SKIPPED
            ],
            answer=answer,
            learnings=learnings,
            trusted=True,
            origins=sorted(self._visited_origins),
            goal_stats=[self._goal_stat(sg) for sg in plan.sub_goals],
        )
        self.memory.save(record)

    def _goal_stat(self, sub_goal: SubGoal) -> dict:
        """Counts and planner-written text about one sub-goal, for memory."""
        receipt = self.receipts.last_for(sub_goal.id)
        return {
            "goal": sub_goal.goal,
            "done": sub_goal.status == SubGoalStatus.COMPLETED,
            "basis": receipt.basis if receipt else None,
            "steps": self._goal_steps.get(id(sub_goal), 0),
        }

    def _briefing_fields(self) -> dict:
        context = self.context
        return {
            "brief": context.brief if context else None,
            "answers": {**context.given, **context.answers} if context else {},
            "gaps": context.gaps if context else [],
            "plan_issues": list(self._plan_issues),
        }

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


class AgentResult:
    """Final result of a task execution."""

    def __init__(
        self,
        success: bool,
        task: str,
        plan: Optional[TaskPlan] = None,
        steps_taken: int = 0,
        elapsed_seconds: float = 0.0,
        final_url: str = "",
        error: Optional[str] = None,
        answer: Optional[str] = None,
        memory_hits: int = 0,
        violations: Optional[list[Violation]] = None,
        values: Optional[dict[str, ExtractedValue]] = None,
        receipts: Optional[list[Receipt]] = None,
        brief: Optional[TaskBrief] = None,
        questions: Optional[list[Question]] = None,
        needs_input: bool = False,
        answers: Optional[dict] = None,
        gaps: Optional[list[str]] = None,
        plan_issues: Optional[list[str]] = None,
        problems: Optional[list[str]] = None,
    ):
        self.success = success
        self.task = task
        self.plan = plan
        self.steps_taken = steps_taken
        self.elapsed_seconds = elapsed_seconds
        self.final_url = final_url
        self.error = error
        self.answer = answer
        self.memory_hits = memory_hits
        self.violations = violations or []
        self.values = values or {}
        self.receipts = receipts or []
        # Thinking before acting (core/briefing.py)
        self.brief = brief
        self.questions = questions or []   # still unanswered when needs_input
        self.needs_input = needs_input
        self.answers = answers or {}
        self.gaps = gaps or []
        self.plan_issues = plan_issues or []
        self.problems = problems or []

    def summary(self) -> str:
        status = "? NEEDS INPUT" if self.needs_input else "✓ SUCCESS" if self.success else "✗ FAILED"
        lines = [
            f"\n{'='*50}",
            f"  {status}",
            f"  Task: {self.task}",
            f"  Steps: {self.steps_taken}",
            f"  Time: {self.elapsed_seconds:.1f}s",
            f"  Final URL: {self.final_url}",
        ]
        if self.memory_hits:
            lines.append(f"  Memory: {self.memory_hits} similar past task(s) used")
        if self.error:
            lines.append(f"  Error: {self.error}")
        if self.brief and self.brief.goal:
            lines.append(f"  Brief: {self.brief.goal}")
            for assumption in self.brief.assumptions:
                lines.append(f"    assumed: {assumption}")
        for gap in self.gaps:
            lines.append(f"    ⚠ mandate: {gap}")
        if self.needs_input:
            lines.append("  Answer these, then run again with --answer ID=VALUE (or --assume):")
            for question in self.questions:
                lines.append(f"    {question.id}: {question.question} ({question.expected()})")
            for problem in self.problems:
                lines.append(f"    didn't fit: {problem}")
        if self.plan_issues:
            lines.append(f"  Plan review: {len(self.plan_issues)} issue(s) left after revision")
        if self.violations:
            lines.append(f"  Mandate blocked {len(self.violations)} action(s):")
            for v, count in collapse_violations(self.violations)[:10]:
                repeats = f" (x{count})" if count > 1 else ""
                lines.append(f"    ⛔ {v.kind.value}: {v.detail}{repeats}")
        if self.plan:
            lines.append(f"  Sub-goals: {len(self.plan.sub_goals)}")
            for sg in self.plan.sub_goals:
                icon = {"completed": "✓", "failed": "✗", "pending": "○", "active": "◉", "skipped": "⊘"}
                lines.append(f"    {icon.get(sg.status.value, '?')} {sg.goal}")
        if self.answer:
            lines.append(f"\n  ANSWER:\n  {self.answer}")
        if self.values:
            lines.append("\n  VALUES:")
            for v in self.values.values():
                shown = str(v.value) if len(str(v.value)) <= 80 else str(v.value)[:77] + "..."
                lines.append(f"    {v.name} = {shown}  ({v.type.value}, {v.origin})")
        if self.receipts:
            verified = sum(r.achieved and r.basis == "evidence" for r in self.receipts)
            judged = sum(r.achieved and r.basis == "model" for r in self.receipts)
            lines.append(f"\n  RECEIPTS: {verified} verified by evidence, {judged} judged by model")
            for r in self.receipts:
                lines.append(f"    {r.summary_line()}")
            lines.append(f"    chain head: {self.receipts[-1].digest[:16]}")
        lines.append(f"{'='*50}\n")
        return "\n".join(lines)
