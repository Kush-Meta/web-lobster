"""Orchestrator — the main agent loop that coordinates all components.

This is the brain of Web Lobster. It runs the Observe → Think → Act loop,
manages sub-goal progression, handles failures and re-planning, and
enforces safety rails.

Flow:
  1. Planner decomposes task → sub-goals
  2. For each sub-goal:
     a. Observer captures page state
     b. Executor picks an action
     c. Safety gate checks the action
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
from web_lobster.utils.logging import get_logger
from web_lobster.utils.retry import StuckDetector, retry_async

logger = get_logger("orchestrator")


class Orchestrator:
    """Main agent loop coordinator."""

    def __init__(self, config: WebLobsterConfig, shared_state=None):
        self.config = config

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
        self.browser = BrowserController(config.browser)
        self.safety = SafetyGate(config.safety)
        self.stuck_detector = StuckDetector()

        # Agent state
        self.state = AgentState(max_steps=config.agent.max_steps)

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
        await self._ui_emit("on_task_start", task)

        try:
            # 1. Launch browser
            await self.browser.start(start_url)

            # 2. Plan the task
            plan = await retry_async(
                self.planner.plan, task,
                max_retries=2,
                base_delay=2.0,
            )
            self.state.plan = plan
            logger.info("plan_ready", sub_goals=len(plan.sub_goals))
            await self._ui_emit("on_plan_ready", plan)

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
            # Run even on partial success — if the agent navigated to the right page,
            # the answer is there regardless of whether every sub-goal was ticked off.
            answer = None
            if self.state.current_observation:
                answer = await self._extract_answer(task, self.state.current_observation)

            # 5. Determine final result
            elapsed = time.time() - start_time
            result = AgentResult(
                success=plan.is_complete,
                task=task,
                plan=plan,
                steps_taken=self.state.step_count,
                elapsed_seconds=elapsed,
                final_url=self.browser.current_url,
                answer=answer,
            )

            if result.success:
                logger.success("task_complete", steps=result.steps_taken, time=f"{elapsed:.1f}s")
            else:
                logger.error("task_failed", steps=result.steps_taken, time=f"{elapsed:.1f}s")

            return result

        except Exception as e:
            logger.error("task_error", error=str(e))
            return AgentResult(
                success=False,
                task=task,
                plan=self.state.plan,
                steps_taken=self.state.step_count,
                elapsed_seconds=time.time() - start_time,
                error=str(e),
            )
        finally:
            await self.browser.close()

    async def _execute_subgoal(self, sub_goal) -> bool:
        """Run the agent loop for a single sub-goal.

        Returns True if the sub-goal was achieved.
        """
        self.safety.reset_counter()
        self.stuck_detector.reset()

        for attempt in range(sub_goal.max_attempts):
            sub_goal.attempts = attempt + 1

            action_count_this_attempt = 0
            max_actions = self.config.safety.max_actions_per_subgoal

            while action_count_this_attempt < max_actions and not self.state.is_over_budget:
                self.state.step_count += 1
                action_count_this_attempt += 1

                # --- PAUSE CHECK ---
                await self._ui_wait_if_paused()

                # --- OBSERVE ---
                include_screenshot = (
                    self.config.agent.screenshot_mode != "dom_only"
                )
                observation = await self.browser.observer.observe(
                    include_screenshot=include_screenshot
                )
                self.state.current_observation = observation
                await self._ui_emit("on_observation", observation)

                # --- THINK (executor decides action) ---
                action = await self.executor.decide(
                    observation=observation,
                    sub_goal=sub_goal,
                    action_history_text=self.state.action_history_summary(),
                    actions_taken_this_subgoal=action_count_this_attempt - 1,
                )

                # --- CHECK for terminal action ---
                if action.action == ActionType.DONE:
                    self.state.action_history.append(action)
                    await self._ui_emit("on_action", action, self.state.step_count)
                    # Validate the sub-goal
                    result = await self.validator.validate(observation, sub_goal)
                    await self._ui_emit("on_validation", result, sub_goal)
                    if result.achieved and result.confidence >= self.config.agent.validation_confidence_threshold:
                        return True
                    else:
                        logger.warning(
                            "validation_failed",
                            confidence=result.confidence,
                            observation=result.observation[:80],
                        )
                        break  # retry the sub-goal

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
                logger.step(
                    self.state.step_count,
                    self.state.max_steps,
                    f"{action.action.value}",
                    element=action.element_id,
                    text=action.text[:30] if action.text else None,
                )
                await self._ui_emit("on_action", action, self.state.step_count)
                success = await self.browser.execute(action)
                self.state.action_history.append(action)
                await self._ui_emit("on_action_result", success)

                if not success:
                    logger.warning("action_execution_failed", action=action.action.value)

                # --- STUCK DETECTION ---
                self.stuck_detector.record(
                    observation.url,
                    len(observation.elements),
                    action.action.value,
                )
                if self.stuck_detector.is_stuck():
                    logger.warning("stuck_detected", steps=action_count_this_attempt)
                    break  # break to retry the sub-goal

            # After exhausting actions, check if we're actually done
            observation = await self.browser.observer.observe(include_screenshot=True)
            result = await self.validator.validate(observation, sub_goal)
            await self._ui_emit("on_validation", result, sub_goal)
            if result.achieved and result.confidence >= self.config.agent.validation_confidence_threshold:
                return True

        return False

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
    ):
        self.success = success
        self.task = task
        self.plan = plan
        self.steps_taken = steps_taken
        self.elapsed_seconds = elapsed_seconds
        self.final_url = final_url
        self.error = error
        self.answer = answer

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
        if self.error:
            lines.append(f"  Error: {self.error}")
        if self.plan:
            lines.append(f"  Sub-goals: {len(self.plan.sub_goals)}")
            for sg in self.plan.sub_goals:
                icon = {"completed": "✓", "failed": "✗", "pending": "○", "active": "◉", "skipped": "⊘"}
                lines.append(f"    {icon.get(sg.status.value, '?')} {sg.goal}")
        if self.answer:
            lines.append(f"\n  ANSWER:\n  {self.answer}")
        lines.append(f"{'='*50}\n")
        return "\n".join(lines)
