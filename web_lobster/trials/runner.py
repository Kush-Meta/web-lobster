"""Runs trial tasks repeatedly and scores them against their known answers."""

from __future__ import annotations

import time
from pathlib import Path
from typing import Callable, Mapping, Optional

from pydantic import BaseModel, Field

from web_lobster.core.config import WebLobsterConfig
from web_lobster.core.orchestrator import Orchestrator
from web_lobster.core.values import Scalar
from web_lobster.mcp_server.models import WebTaskRequest
from web_lobster.mcp_server.service import WebTaskService
from web_lobster.memory.task_memory import TaskMemory
from web_lobster.trials.spec import TrialTask

# Called with the orchestrator before a run; tests use it to swap in scripted models.
PrepareFn = Callable[[Orchestrator, WebTaskRequest], None]


class TrialOutcome(BaseModel):
    """One run of a trial task under one config."""
    task: str
    config: str
    run: int
    done: bool = False
    verified: bool = False
    # Whether every expected value matched; None when the task expects none
    correct: Optional[bool] = None
    values: dict[str, Optional[Scalar]] = Field(default_factory=dict)
    wrong: list[str] = Field(default_factory=list)
    needs_input: bool = False
    steps: int = 0
    seconds: float = 0.0
    run_id: Optional[str] = None
    error: Optional[str] = None

    def verdict(self) -> str:
        if self.error:
            return "error"
        if self.needs_input:
            return "needs input"
        if not self.done:
            return "not done"
        if self.correct is False:
            return "WRONG: " + ", ".join(self.wrong)
        label = "done" if self.correct is None else "right"
        return f"{label}, verified" if self.verified else label


class TrialReport(BaseModel):
    started_at: float
    seconds: float
    runs: int
    memory: str
    configs: list[str]
    tasks: list[str]
    outcomes: list[TrialOutcome]


async def run_trial(
    task: TrialTask,
    label: str,
    config: WebLobsterConfig,
    run: int,
    *,
    runs_dir: Path,
    memory_file: Path,
    prepare: Optional[PrepareFn] = None,
    environ: Optional[Mapping[str, str]] = None,
) -> TrialOutcome:
    """Run a task once, as a calling agent would over MCP, and score its values."""
    def setup(orchestrator: Orchestrator, request: WebTaskRequest) -> None:
        orchestrator.memory = TaskMemory(memory_file)
        if prepare:
            prepare(orchestrator, request)

    service = WebTaskService(config, runs_dir=runs_dir, prepare=setup, environ=environ)
    request = WebTaskRequest(
        task=task.task, mandate=task.mandate, start_url=task.start_url, values=task.values,
        include_page_text=True, max_steps=task.max_steps, notes=task.notes, answers=task.answers,
        on_questions="assume",  # nobody is there to answer
    )
    started = time.monotonic()
    try:
        result = await service.run(request)
    except Exception as e:
        return TrialOutcome(
            task=task.id, config=label, run=run, seconds=round(time.monotonic() - started, 1),
            error=f"{type(e).__name__}: {e}",
        )
    values = {name: value.value for name, value in result.values.items()}
    wrong = [name for name, expected in task.expect.items() if not expected.check(values.get(name))]
    return TrialOutcome(
        task=task.id, config=label, run=run, done=result.done, verified=result.verified,
        correct=(not wrong) if task.expect else None, values=values, wrong=wrong,
        needs_input=result.needs_input, steps=result.steps, seconds=result.seconds,
        run_id=result.run_id, error=result.error,
    )


async def run_trials(
    tasks: list[TrialTask],
    configs: list[tuple[str, WebLobsterConfig]],
    runs: int,
    *,
    runs_dir: Path,
    memory: str = "fresh",
    prepare: Optional[PrepareFn] = None,
    environ: Optional[Mapping[str, str]] = None,
    on_outcome: Optional[Callable[[TrialOutcome], None]] = None,
) -> TrialReport:
    """Run every task under every config, runs times each.

    Runs go one at a time, since local models share one machine, and are
    interleaved by run, so a slow minute on a site doesn't land on one config.
    With memory "fresh" every run starts from empty memory. With "shared", runs
    of a task under one config share a memory file, so later runs can reuse what
    earlier ones learned.
    """
    if memory not in ("fresh", "shared"):
        raise ValueError(f"memory must be 'fresh' or 'shared', not {memory!r}")
    runs_dir = Path(runs_dir)
    memory_dir = runs_dir / "memory"
    memory_dir.mkdir(parents=True, exist_ok=True)
    started_at, started = time.time(), time.monotonic()

    outcomes: list[TrialOutcome] = []
    for run in range(1, runs + 1):
        for task in tasks:
            for label, config in configs:
                name = f"{label}--{task.id}" + ("" if memory == "shared" else f"--{run}")
                outcome = await run_trial(
                    task, label, config, run, runs_dir=runs_dir,
                    memory_file=memory_dir / f"{name}.jsonl", prepare=prepare, environ=environ,
                )
                outcomes.append(outcome)
                if on_outcome:
                    on_outcome(outcome)

    return TrialReport(
        started_at=started_at, seconds=round(time.monotonic() - started, 1), runs=runs, memory=memory,
        configs=[label for label, _ in configs], tasks=[task.id for task in tasks], outcomes=outcomes,
    )
