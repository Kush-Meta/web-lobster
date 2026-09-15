"""Summaries of a trial run: how often each task was done, right, and verified, and what it cost."""

from __future__ import annotations

import statistics
from typing import Optional

from pydantic import BaseModel

from web_lobster.trials.runner import TrialReport


class TrialSummary(BaseModel):
    task: str
    config: str
    runs: int
    done: int
    verified: int
    # How many runs had every expected value right; None when the task expects none
    correct: Optional[int]
    needs_input: int
    errors: int
    median_steps: float
    median_seconds: float


def summarize(report: TrialReport) -> list[TrialSummary]:
    rows = []
    for task in report.tasks:
        for config in report.configs:
            group = [o for o in report.outcomes if o.task == task and o.config == config]
            if not group:
                continue
            expects = any(o.correct is not None for o in group)
            rows.append(TrialSummary(
                task=task, config=config, runs=len(group),
                done=sum(o.done for o in group),
                verified=sum(o.verified for o in group),
                correct=sum(bool(o.correct) for o in group) if expects else None,
                needs_input=sum(o.needs_input for o in group),
                errors=sum(o.error is not None for o in group),
                median_steps=statistics.median(o.steps for o in group),
                median_seconds=statistics.median(o.seconds for o in group),
            ))
    return rows


def _table(headers: list[str], rows: list[list[object]]) -> list[str]:
    cells = [headers, *[[str(c) for c in row] for row in rows]]
    widths = [max(len(row[i]) for row in cells) for i in range(len(headers))]

    def line(row: list[str]) -> str:
        return "    " + "   ".join(cell.ljust(width) for cell, width in zip(row, widths)).rstrip()

    return [line(headers), line(["-" * w for w in widths]), *(line(row) for row in cells[1:])]


def render(report: TrialReport) -> str:
    lines = [
        f"\nTrials: {len(report.outcomes)} runs in {report.seconds:.0f}s "
        f"({report.runs} per task and config, {report.memory} memory)",
        "",
    ]
    lines += _table(
        ["task", "config", "done", "right", "verified", "median steps", "median time"],
        [
            [r.task, r.config, f"{r.done}/{r.runs}", "-" if r.correct is None else f"{r.correct}/{r.runs}",
             f"{r.verified}/{r.runs}", f"{r.median_steps:g}", f"{r.median_seconds:.0f}s"]
            for r in summarize(report)
        ],
    )
    wrong = [o for o in report.outcomes if o.correct is False]
    if wrong:
        # repr() keeps page-derived text from carrying terminal control characters.
        lines += ["", "  Wrong values"] + [
            f"    {o.task} / {o.config} / run {o.run}: " + ", ".join(f"{name}={o.values.get(name)!r}" for name in o.wrong)
            for o in wrong
        ]
    errors = [o for o in report.outcomes if o.error]
    if errors:
        lines += ["", "  Errors"] + [
            f"    {o.task} / {o.config} / run {o.run}: {o.error.splitlines()[0][:160]!r}" for o in errors
        ]
    lines.append("")
    return "\n".join(lines)


def render_markdown(report: TrialReport) -> str:
    lines = [
        "| Task | Config | Done | Right | Verified | Median steps | Median time |",
        "|---|---|---|---|---|---|---|",
    ]
    for r in summarize(report):
        right = "—" if r.correct is None else f"{r.correct}/{r.runs}"
        lines.append(
            f"| {r.task} | `{r.config}` | {r.done}/{r.runs} | {right} | {r.verified}/{r.runs} "
            f"| {r.median_steps:g} | {r.median_seconds:.0f} s |"
        )
    return "\n".join(lines) + "\n"
