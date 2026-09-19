"""Typed values — the only channel from web pages back to the planner.

The planner decides what the agent does, so it must never read page text: a
page could write instructions into it. When the plan needs something a page
shows (a price, a date, a yes/no), the planner declares a typed value on a
sub-goal. A quarantined model reads the value off the page, and code, not a
model, checks it against the declared type.

Numbers, booleans, ISO dates, and choices from the planner's own list can't
carry instructions, so the planner sees them. Text can, so the planner only
learns that a text value exists; the executor and the user see its content.
"""

from __future__ import annotations

import json
import math
import re
from datetime import date
from enum import Enum
from typing import Literal, Optional, Union
from urllib.parse import urlsplit

from pydantic import BaseModel, Field, field_validator, model_validator

MAX_TEXT_LENGTH = 500
MAX_PATTERN_LENGTH = 200
# A group that repeats and contains a repeat, like (a+)+, can take exponential time to match.
_NESTED_QUANTIFIER = re.compile(r"\([^()]*[+*][^()]*\)[+*{]")
VALUE_REF_RE = re.compile(r"\{\{\s*\$([A-Za-z_][A-Za-z0-9_]*)\s*\}\}")

_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
# "$1,209.50", "412", "-3.5", "USD 99", "99 EUR". US separators only: "1.209,50"
# doesn't match, and "1.209" reads as one-point-two-oh-nine.
_NUMBER_RE = re.compile(
    r"^[^\d\-]{0,3}\s*(-?(?:\d{1,3}(?:,\d{3})+|\d+)(?:\.\d+)?)\s*[^\d]{0,3}$"
)

Scalar = Union[bool, int, float, str]


class ValueType(str, Enum):
    NUMBER = "number"
    INTEGER = "integer"
    BOOLEAN = "boolean"
    DATE = "date"      # ISO YYYY-MM-DD
    CHOICE = "choice"  # one of the planner's own choices
    TEXT = "text"      # free text; never shown to the planner


class ValueSpec(BaseModel):
    """A value the planner wants read off the page a sub-goal reaches.

    pattern, min, and max are shape checks enforced in code: a text value must
    match pattern in full, and a number must fall within min and max. A value
    that fails them counts as not read, however sure the reader was.
    """
    name: str
    type: ValueType
    description: str = ""
    choices: list[str] = Field(default_factory=list)
    pattern: Optional[str] = Field(default=None, max_length=MAX_PATTERN_LENGTH)
    min: Optional[float] = None
    max: Optional[float] = None
    # Which one, when a page shows a list of them. A 7B model asked for the
    # newest commit on a page of commits returned the last one in the text every
    # time, at any window size (live run 29). Asked instead to list them in the
    # order they appear, it gets the order right, so code takes the end it wants.
    pick: Optional[Literal["first", "last"]] = None

    @field_validator("name")
    @classmethod
    def _check_name(cls, v: str) -> str:
        if not _NAME_RE.match(v):
            raise ValueError(f"value name must be an identifier: {v!r}")
        return v

    @model_validator(mode="after")
    def _check_shape(self) -> ValueSpec:
        if self.type == ValueType.CHOICE and not self.choices:
            raise ValueError(f"choice value {self.name!r} needs choices")
        if self.pattern is not None:
            if self.type != ValueType.TEXT:
                raise ValueError(f"pattern only applies to text values, not {self.name!r}")
            try:
                re.compile(self.pattern)
            except re.error as e:
                raise ValueError(f"pattern for {self.name!r} isn't a valid regular expression: {e}") from None
            if _NESTED_QUANTIFIER.search(self.pattern):
                raise ValueError(f"pattern for {self.name!r} repeats a group that repeats, which can hang matching")
        numeric = self.type in (ValueType.NUMBER, ValueType.INTEGER)
        if (self.min is not None or self.max is not None) and not numeric:
            raise ValueError(f"min and max only apply to number and integer values, not {self.name!r}")
        if self.min is not None and self.max is not None and self.min > self.max:
            raise ValueError(f"min is above max for {self.name!r}")
        return self


class ExtractedValue(BaseModel):
    """A value read off a page that passed its type check."""
    name: str
    type: ValueType
    value: Scalar
    origin: str  # where it was read, e.g. "https://www.google.com"

    def planner_view(self) -> str:
        """How the value appears in a planner prompt. Text content is withheld."""
        ref = "{{$" + self.name + "}}"
        if self.type == ValueType.TEXT:
            return (
                f"{ref}: text read from {self.origin} "
                f"({len(str(self.value))} chars; content withheld)"
            )
        return f"{ref} = {json.dumps(self.value)} ({self.type.value}, read from {self.origin})"


def coerce_value(spec: ValueSpec, raw: object) -> Optional[Scalar]:
    """Check a model-reported value against its declared type. None if it doesn't fit."""
    if raw is None:
        return None

    if spec.type in (ValueType.NUMBER, ValueType.INTEGER):
        number = _to_number(raw)
        if number is None or not _within(spec, number):
            return None
        if spec.type == ValueType.INTEGER:
            return int(number) if number.is_integer() else None
        return number

    if spec.type == ValueType.BOOLEAN:
        if isinstance(raw, bool):
            return raw
        if isinstance(raw, str):
            return {"true": True, "yes": True, "false": False, "no": False}.get(raw.strip().lower())
        return None

    if spec.type == ValueType.DATE:
        if not isinstance(raw, str):
            return None
        try:
            return date.fromisoformat(raw.strip()).isoformat()
        except ValueError:
            return None

    if spec.type == ValueType.CHOICE:
        if not isinstance(raw, str):
            return None
        wanted = raw.strip().lower()
        return next((c for c in spec.choices if c.lower() == wanted), None)

    # TEXT
    if isinstance(raw, bool) or not isinstance(raw, (str, int, float)):
        return None
    text = str(raw).strip()[:MAX_TEXT_LENGTH]
    if not text or (spec.pattern and not re.fullmatch(spec.pattern, text)):
        return None
    return text


def _within(spec: ValueSpec, number: float) -> bool:
    return (spec.min is None or number >= spec.min) and (spec.max is None or number <= spec.max)


def _to_number(raw: object) -> Optional[float]:
    if isinstance(raw, bool):
        return None
    if isinstance(raw, (int, float)):
        number = float(raw)
    elif isinstance(raw, str):
        match = _NUMBER_RE.match(raw.strip())
        if not match:
            return None
        number = float(match.group(1).replace(",", ""))
    else:
        return None
    return number if math.isfinite(number) else None


def render_value_refs(text: str, values: dict[str, ExtractedValue]) -> str:
    """Fill {{$name}} references with value content, for the executor's eyes only."""
    def fill(match: re.Match) -> str:
        value = values.get(match.group(1))
        return str(value.value) if value is not None else f"(unknown value {match.group(1)})"
    return VALUE_REF_RE.sub(fill, text)


def origin_of(url: str) -> str:
    """scheme://host[:port] of a URL, without any path, query, or credentials."""
    parts = urlsplit(url)
    if not parts.hostname:
        return url.split(":", 1)[0] + ":"
    port = f":{parts.port}" if parts.port else ""
    return f"{parts.scheme}://{parts.hostname}{port}"
