"""The trials tool: live tasks run again and again, scored against known answers."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from web_lobster.bench.agents import ScriptedPlanner
from web_lobster.core.config import WebLobsterConfig
from web_lobster.core.schemas import Action, ActionType, SubGoal, ValidationResult
from web_lobster.trials.report import render, render_markdown, summarize
from web_lobster.trials.runner import TrialOutcome, TrialReport, run_trials
from web_lobster.trials.spec import Expectation, TrialFile, TrialTask

REPO = Path(__file__).resolve().parent.parent


class TestExpectations:
    def test_numbers_within_tolerance(self):
        assert Expectation(equals=8848.86, tolerance=0.5).check(8849)
        assert Expectation(equals=330).check(330.0)
        assert not Expectation(equals=330).check(300)

    def test_text_ignores_case_and_surrounding_space(self):
        assert Expectation(equals="3.14.7").check(" 3.14.7 ")
        assert not Expectation(equals="3.14.7").check("3.15")

    def test_patterns_and_missing_values(self):
        assert Expectation(pattern=r"3\.\d+\.\d+").check("3.14.7")
        assert not Expectation(pattern=r"3\.\d+\.\d+").check("3.15")
        assert not Expectation(equals=330).check(None)
        assert Expectation().check("anything") and not Expectation().check(None)

    def test_booleans_only_match_booleans(self):
        assert Expectation(equals=True).check(True)
        assert not Expectation(equals=True).check("true")
        assert not Expectation(equals=1).check(True)


class TestTrialFiles:
    def test_the_shipped_trial_files_load_and_are_read_only(self):
        files = sorted(path.name for path in (REPO / "trials").glob("*.yaml"))
        assert files == ["signed-in.yaml", "web.yaml"]
        for name in files:
            trial_file = TrialFile.from_yaml(REPO / "trials" / name)
            assert trial_file.tasks, name
            for task in trial_file.tasks:
                # Read-only: a trial may sign in, but never submits, buys, or sends.
                assert task.mandate.writes == [] and not task.mandate.allow_any_write
                assert task.expect and set(task.expect) <= {spec.name for spec in task.values}

    def test_the_default_trial_file_needs_nothing_from_the_environment(self):
        trial_file = TrialFile.from_yaml(REPO / "trials" / "web.yaml")
        assert [task.id for task in trial_file.tasks] == ["eiffel", "python-latest", "everest"]
        # Tasks that need credentials live in signed-in.yaml, so the default
        # suite runs for anyone with Ollama and nothing else set up.
        assert not any(task.mandate.data for task in trial_file.tasks)

    def test_expectations_must_name_requested_values(self, tmp_path):
        path = tmp_path / "bad.yaml"
        path.write_text(
            "tasks:\n  - id: t\n    task: Look\n    mandate: {origins: ['https://example.com']}\n"
            "    expect: {price: {equals: 1}}\n"
        )
        with pytest.raises(ValueError, match="doesn't request"):
            TrialFile.from_yaml(path)


def test_summary_counts_medians_and_lists_what_went_wrong():
    outcomes = [
        TrialOutcome(task="t", config="a", run=1, done=True, verified=True, correct=True, steps=9, seconds=160),
        TrialOutcome(task="t", config="a", run=2, done=True, correct=False, wrong=["v"], values={"v": "3.15"},
                     steps=21, seconds=470),
        TrialOutcome(task="t", config="a", run=3, error="boom", seconds=5),
    ]
    report = TrialReport(started_at=0, seconds=635, runs=3, memory="fresh", configs=["a"], tasks=["t"],
                         outcomes=outcomes)
    [row] = summarize(report)
    assert (row.runs, row.done, row.verified, row.correct, row.errors) == (3, 2, 1, 1, 1)
    assert row.median_steps == 9 and row.median_seconds == 160
    text = render(report)
    assert "t / a / run 2: v='3.15'" in text and "boom" in text
    assert "| t | `a` | 2/3 | 1/3 | 1/3 | 9 | 160 s |" in render_markdown(report)


def _scripted(orchestrator, request):
    orchestrator.planner = ScriptedPlanner([SubGoal(id=1, goal="Look over the article", success_criteria="Shown")])
    orchestrator.executor.decide = AsyncMock(return_value=Action(action=ActionType.DONE, reason="done"))
    orchestrator.validator.validate = AsyncMock(
        return_value=ValidationResult(achieved=True, confidence=0.9, observation="shown")
    )
    orchestrator.extractor.backend = SimpleNamespace(generate=AsyncMock(return_value=json.dumps({"height_m": 330})))
    orchestrator.planner_backend = SimpleNamespace(generate=AsyncMock(return_value="330 metres"))


def _config() -> WebLobsterConfig:
    config = WebLobsterConfig()
    config.browser.headless = True
    config.browser.default_timeout = 5.0
    config.agent.dom_mode = True
    return config


async def test_every_task_runs_under_every_config_and_is_scored(sites, tmp_path):
    a, _ = sites
    task = TrialTask(
        id="tower", task="How tall is the tower?", mandate={"origins": [a.origin]},
        start_url=f"{a.origin}/article",
        values=[{"name": "height_m", "type": "number", "min": 100, "max": 500}],
        expect={"height_m": {"equals": 330}},
    )
    seen: list[TrialOutcome] = []
    report = await run_trials(
        [task], [("first", _config()), ("second", _config())], runs=2, runs_dir=tmp_path,
        prepare=_scripted, on_outcome=seen.append,
    )
    if any(o.error and "Executable doesn't exist" in o.error for o in report.outcomes):
        pytest.skip("Playwright Chromium isn't installed")

    assert [(o.run, o.config) for o in report.outcomes] == [(1, "first"), (1, "second"), (2, "first"), (2, "second")]
    assert seen == report.outcomes
    assert all(o.done and o.correct and o.values == {"height_m": 330} for o in report.outcomes), \
        [o.verdict() for o in report.outcomes]
    assert [(r.config, r.done, r.correct) for r in summarize(report)] == [("first", 2, 2), ("second", 2, 2)]
    assert len(list((tmp_path / "memory").glob("*.jsonl"))) == 4  # fresh memory: one file per run


async def test_shared_memory_carries_across_runs(sites, tmp_path):
    a, _ = sites
    task = TrialTask(id="tower", task="How tall is the tower?", mandate={"origins": [a.origin]},
                     start_url=f"{a.origin}/article")
    report = await run_trials([task], [("only", _config())], runs=2, runs_dir=tmp_path,
                              memory="shared", prepare=_scripted)
    if any(o.error and "Executable doesn't exist" in o.error for o in report.outcomes):
        pytest.skip("Playwright Chromium isn't installed")
    [memory_file] = (tmp_path / "memory").glob("*.jsonl")
    assert len(memory_file.read_text().splitlines()) == 2
    assert [o.correct for o in report.outcomes] == [None, None]  # nothing expected, so not scored
