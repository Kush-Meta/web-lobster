"""Orchestrator — the main agent loop that coordinates all components.

This is the brain of Web Lobster. It runs the Observe → Think → Act loop,
manages sub-goal progression, handles failures and re-planning, and
enforces safety rails.

Flow:
  1. Planner decomposes task → sub-goals
  2. For each sub-goal:
     a. Observer captures page state
     b. Executor picks an action
     c. Mandate (if any) and safety gate check the action
     d. Browser executes the action
     e. Repeat until executor says "done" or budget exhausted
  3. Validator checks if sub-goal was achieved
  4. If failed → retry or re-plan
  5. If succeeded → next sub-goal
"""

from __future__ import annotations

import asyncio
import time
from typing import Optional

from web_lobster.core.schemas import (
    Action,
    ActionType,
    AgentState,
    SubGoalStatus,
    TaskPlan,
)
from web_lobster.core.config import WebLobsterConfig
from web_lobster.models.planner import Planner
from web_lobster.models.executor import Executor
from web_lobster.models.validator import Validator
from web_lobster.models.ollama_backend import OllamaBackend
from web_lobster.models.llamacpp_backend import LlamaCppBackend
from web_lobster.models.anthropic_backend import AnthropicBackend
from web_lobster.browser.controller import BrowserController
from web_lobster.actions.safety import SafetyGate
from web_lobster.tools.mcp_manager import MCPManager
from web_lobster.memory.task_memory import TaskMemory, TaskRecord
from web_lobster.mandate.enforcer import MandateEnforcer, MandateExpiredError, Violation
from web_lobster.mandate.schema import Mandate
from web_lobster.utils.logging import get_logger, TaskDisplay
from web_lobster.utils.retry import StuckDetector, retry_async

logger = get_logger("orchestrator")


class Orchestrator:
    """Main agent loop coordinator."""

    def __init__(
        self,
        config: WebLobsterConfig,
        shared_state=None,
        mandate: Optional[Mandate] = None,
    ):
        self.config = config

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
            use_vision=True,
        )

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

    async def run(self, task: str, start_url: str = "https://www.google.com") -> AgentResult:
        """Execute a complete task from start to finish.

        Args:
            task: Natural language task description
            start_url: URL to start the browser at

        Returns:
            AgentResult with success status and details
        """
        logger.info("task_start", task=task)
        start_time = time.time()
        # Retrieve relevant past experiences before planning (before UI start
        # so we can pass memory_hits to both the display and the UI event)
        memory_context = self.memory.format_for_prompt(task)
        memory_hits = len(self.memory.find_similar(task)) if memory_context else 0
        if memory_context:
            logger.info("memory_context", hits=memory_hits)

        await self._ui_emit("on_task_start", task, memory_hits=memory_hits)

        # Start live display (CLI mode only)
        if not self.ui:
            self.display.start(task, self.config.agent.max_steps, memory_hits)

        # Optionally cap total wall-clock time
        max_seconds = self.config.agent.max_seconds
        if max_seconds > 0:
            try:
                return await asyncio.wait_for(
                    self._run_inner(task, start_url, start_time, memory_context, memory_hits),
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
                )
        else:
            return await self._run_inner(task, start_url, start_time, memory_context, memory_hits)

    async def _run_inner(
        self,
        task: str,
        start_url: str,
        start_time: float,
        memory_context: Optional[str],
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
                memory_context,
                max_retries=2,
                base_delay=2.0,
            )
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

                success = await self._execute_subgoal(sub_goal)

                if success:
                    sub_goal.status = SubGoalStatus.COMPLETED
                    logger.success("subgoal_complete", id=sub_goal.id)
                    await self._ui_emit("on_subgoal_complete", sub_goal)
                    if not self.ui:
                        self._refresh_display()
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

            # 4. Extract the answer from the final page state.
            answer = None
            if self.state.current_observation:
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
            )
        finally:
            if not self.ui:
                self.display.stop()
            await self.mcp.stop()
            await self.browser.close()

    async def _execute_subgoal(self, sub_goal) -> bool:
        """Run the Observe → Reflect → Act loop for a single sub-goal."""
        self.safety.reset_counter()
        self.stuck_detector.reset()
        # Accumulated reflections: persist across multiple stuck-then-retry cycles
        # so the model never loses the diagnostic history of this sub-goal.
        reflections: list[str] = []

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
                    sub_goal=sub_goal,
                    action_history_text=self.state.action_history_summary(),
                    actions_taken_this_subgoal=action_count_this_attempt - 1,
                    reflection=combined_reflection,
                    mcp_tools=self.mcp.tools if self.mcp.has_tools else None,
                    data_placeholders=self.enforcer.placeholder_names() if self.enforcer else None,
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
                    result = await self.validator.validate(observation, sub_goal)
                    await self._ui_emit("on_validation", result, sub_goal)
                    if result.achieved and result.confidence >= self.config.agent.validation_confidence_threshold:
                        return True
                    else:
                        logger.warning("validation_failed", confidence=result.confidence,
                                       observation=result.observation[:80])
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

                if verdict.needs_confirmation:
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
                            sub_goal=sub_goal,
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
            result = await self.validator.validate(observation, sub_goal)
            await self._ui_emit("on_validation", result, sub_goal)
            if result.achieved and result.confidence >= self.config.agent.validation_confidence_threshold:
                return True

        return False

    def _violations(self) -> list[Violation]:
        return list(self.enforcer.violations) if self.enforcer else []

    async def _report_violation(self, action: Action, violation: Violation) -> None:
        """Surface a blocked action in the UI and in the executor's history."""
        await self._ui_emit("on_safety_flag", action, f"Mandate: {violation.detail}")
        self.state.action_history.append(
            Action(action=ActionType.WAIT, reason=f"Blocked by mandate: {violation.detail}")
        )

    async def _enforce_after_action(self, action: Action) -> None:
        """Report what the network layer blocked during the action, and step off
        any page that ended up outside the mandate anyway."""
        if action.action in (ActionType.CLICK, ActionType.NAVIGATE, ActionType.SELECT, ActionType.GO_BACK):
            # A click's navigation reaches the network layer a few ms after the
            # click returns; wait for the verdict so a block is handled on this step.
            await self.enforcer.wait_for_pending(timeout=0.25)
        blocked = self.enforcer.drain()
        for violation in blocked:
            await self._report_violation(action, violation)
        if any(v.main_frame for v in blocked):
            # A blocked navigation leaves Chromium's error page behind; put the
            # agent back on the page it was working on.
            await self.browser.recover_from_blocked_navigation()
        violation = self.enforcer.check_page(self.browser.current_url)
        if violation:
            await self._report_violation(action, violation)
            await self.browser.leave_page()

    async def _extract_answer(self, task: str, observation) -> Optional[str]:
        """After task completion, ask the model to extract a direct answer from the page."""
        page_content = ""
        if observation.page_text:
            page_content = observation.page_text[:3000]
        elif observation.accessibility_tree:
            page_content = observation.accessibility_tree[:3000]

        prompt = f"""The user asked: "{task}"

The agent has finished. Here is the final page content:

URL: {observation.url}
Title: {observation.title}

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
            new_goals = await self.planner.replan(
                task=self.state.plan.task,
                completed_goals=completed,
                failed_goal=failed_goal,
                current_url=self.browser.current_url,
                error_context=f"Failed after {failed_goal.attempts} attempts",
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
                answer=answer,
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
        )
        self.memory.save(record)

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

    def summary(self) -> str:
        status = "✓ SUCCESS" if self.success else "✗ FAILED"
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
        if self.violations:
            lines.append(f"  Mandate blocked {len(self.violations)} action(s):")
            for v in self.violations[:10]:
                lines.append(f"    ⛔ {v.kind.value}: {v.detail}")
        if self.plan:
            lines.append(f"  Sub-goals: {len(self.plan.sub_goals)}")
            for sg in self.plan.sub_goals:
                icon = {"completed": "✓", "failed": "✗", "pending": "○", "active": "◉", "skipped": "⊘"}
                lines.append(f"    {icon.get(sg.status.value, '?')} {sg.goal}")
        if self.answer:
            lines.append(f"\n  ANSWER:\n  {self.answer}")
        lines.append(f"{'='*50}\n")
        return "\n".join(lines)
