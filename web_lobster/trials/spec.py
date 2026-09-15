"""Trial files: live web tasks with known answers, run again and again."""

from __future__ import annotations

import math
import re
from pathlib import Path
from typing import Optional

import yaml
from pydantic import BaseModel, Field, field_validator, model_validator

from web_lobster.core.values import Scalar, ValueSpec
from web_lobster.mcp_server.models import MandateInput

_TASK_ID_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,40}$")


class Expectation(BaseModel):
    """What a requested value should be, checked in code."""
    equals: Optional[Scalar] = None
    # How far a number may be from equals
    tolerance: float = Field(default=0.0, ge=0)
    # A regular expression a text value must fully match
    pattern: Optional[str] = None

    @field_validator("pattern")
    @classmethod
    def _compiles(cls, v: Optional[str]) -> Optional[str]:
        if v is not None:
            try:
                re.compile(v)
            except re.error as e:
                raise ValueError(f"invalid pattern: {e}") from None
        return v

    def check(self, value: Optional[Scalar]) -> bool:
        if value is None:
            return False
        if self.pattern is not None and not (isinstance(value, str) and re.fullmatch(self.pattern, value)):
            return False
        expected = self.equals
        if expected is None:
            return True
        if isinstance(expected, bool) or isinstance(value, bool):
            return isinstance(expected, bool) and isinstance(value, bool) and value == expected
        if isinstance(expected, (int, float)):
            return isinstance(value, (int, float)) and math.isclose(
                float(value), float(expected), rel_tol=0.0, abs_tol=self.tolerance,
            )
        return str(value).strip().casefold() == str(expected).strip().casefold()


class TrialTask(BaseModel):
    """A live task to run repeatedly, with the answers it should give."""
    id: str
    task: str = Field(min_length=1)
    mandate: MandateInput
    start_url: Optional[str] = None
    values: list[ValueSpec] = Field(default_factory=list)
    expect: dict[str, Expectation] = Field(default_factory=dict)
    notes: Optional[str] = None
    answers: dict[str, Scalar] = Field(default_factory=dict)
    max_steps: Optional[int] = Field(default=None, ge=1, le=300)

    @field_validator("id")
    @classmethod
    def _check_id(cls, v: str) -> str:
        if not _TASK_ID_RE.match(v):
            raise ValueError(f"task id must be lowercase letters, digits, - or _: {v!r}")
        return v

    @model_validator(mode="after")
    def _expect_requested_values(self) -> TrialTask:
        unknown = sorted(set(self.expect) - {spec.name for spec in self.values})
        if unknown:
            raise ValueError(f"task {self.id!r} expects values it doesn't request: {unknown}")
        return self


class TrialFile(BaseModel):
    tasks: list[TrialTask] = Field(min_length=1)

    @model_validator(mode="after")
    def _unique_ids(self) -> TrialFile:
        ids = [task.id for task in self.tasks]
        duplicates = sorted({task_id for task_id in ids if ids.count(task_id) > 1})
        if duplicates:
            raise ValueError(f"duplicate task ids: {duplicates}")
        return self

    @classmethod
    def from_yaml(cls, path: str | Path) -> TrialFile:
        return cls.model_validate(yaml.safe_load(Path(path).read_text()) or {})
