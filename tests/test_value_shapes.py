"""Shape checks on requested values: patterns for text, bounds for numbers."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from web_lobster.core.schemas import Observation
from web_lobster.core.values import ValueSpec, ValueType, coerce_value
from web_lobster.models.extractor import Extractor, _picked


def test_text_must_match_its_pattern_in_full():
    spec = ValueSpec(name="version", type=ValueType.TEXT, pattern=r"3\.\d+\.\d+")
    assert coerce_value(spec, "3.14.7") == "3.14.7"
    assert coerce_value(spec, "3.15") is None  # live run 15's misread
    assert coerce_value(spec, "Python 3.14.7") is None  # the whole text must match


def test_numbers_must_fall_within_min_and_max():
    spec = ValueSpec(name="elevation", type=ValueType.NUMBER, min=8000, max=9000)
    assert coerce_value(spec, "8,848.86") == 8848.86
    assert coerce_value(spec, "29,032") is None  # feet, not metres
    adults = ValueSpec(name="adults", type=ValueType.INTEGER, min=1)
    assert coerce_value(adults, 0) is None and coerce_value(adults, 2) == 2


@pytest.mark.parametrize("fields", [
    {"type": "text", "pattern": "(unclosed"},
    {"type": "number", "pattern": "[0-9]+"},
    {"type": "text", "min": 1},
    {"type": "number", "min": 10, "max": 1},
    {"type": "text", "pattern": "(a+)+$"},
])
def test_shape_checks_that_cannot_work_are_rejected(fields):
    with pytest.raises(ValidationError):
        ValueSpec(name="v", **fields)


def test_a_picked_value_is_asked_for_as_a_list_and_taken_in_code():
    # A 7B model asked for "the newest commit" on a page of commits returned the
    # last one in the text every time, at every window size (live run 29). Asked
    # to list them in page order it gets the order right, so code picks the end.
    observation = Observation(url="https://github.com/o/r/commits/main", title="Commits", page_text="...")
    spec = ValueSpec(name="latest_commit", type=ValueType.TEXT, pick="first",
                     description="the newest commit's subject line")
    prompt = Extractor(backend=None)._prompt(observation, [spec])
    assert "in the order they appear, top of the page first" in prompt

    assert _picked(spec, ["newest", "older", "oldest"]) == "newest"
    assert _picked(spec.model_copy(update={"pick": "last"}), ["newest", "older", "oldest"]) == "oldest"
    assert _picked(spec, []) is None
    assert _picked(spec, "a bare string") == "a bare string"  # a model that ignored the ask
    assert _picked(spec.model_copy(update={"pick": None}), ["a", "b"]) == ["a", "b"]


def test_the_reader_is_told_the_shape():
    observation = Observation(url="https://www.python.org/downloads/", title="Downloads", page_text="...")
    prompt = Extractor(backend=None)._prompt(observation, [
        ValueSpec(name="version", type=ValueType.TEXT, pattern=r"3\.\d+\.\d+"),
        ValueSpec(name="elevation", type=ValueType.NUMBER, min=8000, max=9000),
    ])
    assert '"version": short text, matching the regular expression 3\\.\\d+\\.\\d+' in prompt
    assert '"elevation": a number, between 8000 and 9000' in prompt


def test_the_callers_value_spec_beats_the_planners():
    from unittest.mock import AsyncMock

    from web_lobster.core.briefing import PlanningContext
    from web_lobster.core.config import WebLobsterConfig
    from web_lobster.core.orchestrator import Orchestrator
    from web_lobster.core.schemas import SubGoal, TaskPlan

    # The planner is told which values the caller wants and writes its own specs
    # for them, dropping the shape: two of three python.org trials read an
    # unshaped version, and a GitHub lookup ignored "the first one on the page".
    caller = [
        ValueSpec(name="version", type=ValueType.TEXT, pattern=r"3\.\d+\.\d+"),
        ValueSpec(name="downloads", type=ValueType.NUMBER, min=1),
    ]
    plan = TaskPlan(task="Read the version", sub_goals=[SubGoal(
        id=1, goal="Open the downloads page", success_criteria="Open",
        extract=[ValueSpec(name="version", type=ValueType.TEXT)],  # no pattern
    )])
    orchestrator = Orchestrator(WebLobsterConfig())
    orchestrator.planner.revise = AsyncMock(return_value=[])
    context = PlanningContext(task="Read the version", requested_values=caller)

    plan = orchestrator._apply_requested_values(plan, context)
    [version, downloads] = plan.sub_goals[0].extract
    assert version.pattern == r"3\.\d+\.\d+"        # the caller's shape, not the planner's
    assert downloads.name == "downloads"              # never declared, read on the last step
    # And the step that now reads can prove it read, rather than being judged.
    assert [(c.name, c.op, c.value) for c in plan.sub_goals[0].evidence] == [
        ("version", "!=", None), ("downloads", "!=", None),
    ]

