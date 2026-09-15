"""Shape checks on requested values: patterns for text, bounds for numbers."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from web_lobster.core.schemas import Observation
from web_lobster.core.values import ValueSpec, ValueType, coerce_value
from web_lobster.models.extractor import Extractor


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


def test_the_reader_is_told_the_shape():
    observation = Observation(url="https://www.python.org/downloads/", title="Downloads", page_text="...")
    prompt = Extractor(backend=None)._prompt(observation, [
        ValueSpec(name="version", type=ValueType.TEXT, pattern=r"3\.\d+\.\d+"),
        ValueSpec(name="elevation", type=ValueType.NUMBER, min=8000, max=9000),
    ])
    assert '"version": short text, matching the regular expression 3\\.\\d+\\.\\d+' in prompt
    assert '"elevation": a number, between 8000 and 9000' in prompt
