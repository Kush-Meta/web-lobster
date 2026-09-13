"""Evidence checks — how a sub-goal proves it's done without a model's opinion.

The planner declares checks on a sub-goal, and code evaluates them against the
live browser: the page URL, the network log of what the browser sent and got
back, the page text, and type-checked values. A page can display "Booking
confirmed", but it can't make the browser have sent a POST that got a 2xx.

Checks are planner-authored, so their descriptions are safe to show the planner
and the executor. Results carry page and network detail, so they go to receipts
and the user, never back to the planner.
"""

from __future__ import annotations

import json
import operator
import re
from dataclasses import dataclass, field
from fnmatch import fnmatchcase
from typing import Annotated, Literal, Union
from urllib.parse import urlsplit

from pydantic import BaseModel, Field, field_validator, model_validator

from web_lobster.core.values import ExtractedValue, Scalar
from web_lobster.mandate.schema import Origin, origin_matches, parse_origin_pattern, url_origin
from web_lobster.verify.network import WRITE_METHODS, NetworkEvent

HTTP_METHODS = frozenset({"GET", "HEAD", "OPTIONS"}) | WRITE_METHODS
_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_OPS = {
    "==": operator.eq, "!=": operator.ne,
    "<": operator.lt, "<=": operator.le,
    ">": operator.gt, ">=": operator.ge,
}


def parse_url_pattern(pattern: str) -> tuple[Origin, str]:
    """Split "https://www.united.com/confirmation/*" into an origin and a path glob.

    The origin follows mandate rules (at most a leading "*." label), so a
    pattern can't match a lookalike host. In the path, "*" matches anything,
    including "/" and the query string. No path means any path.
    """
    if "://" not in pattern:
        raise ValueError(f"URL pattern needs a scheme: {pattern!r}")
    scheme, rest = pattern.split("://", 1)
    host, slash, path = rest.partition("/")
    origin = parse_origin_pattern(f"{scheme}://{host}")
    return origin, ("/" + path) if slash else "*"


def url_matches(url: str, pattern: str) -> bool:
    origin_pattern, path_glob = parse_url_pattern(pattern)
    origin = url_origin(url)
    if origin is None or not origin_matches(origin, origin_pattern):
        return False
    parts = urlsplit(url)
    target = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
    return fnmatchcase(target, path_glob)


class UrlCheck(BaseModel):
    """The page ended up at a URL matching the pattern."""
    type: Literal["url"] = "url"
    pattern: str

    @field_validator("pattern")
    @classmethod
    def _check_pattern(cls, v: str) -> str:
        parse_url_pattern(v)
        return v

    def describe(self) -> str:
        return f"page URL matches {self.pattern}"


class RequestCheck(BaseModel):
    """During the sub-goal, the browser sent a matching request that got a non-error response."""
    type: Literal["request"] = "request"
    method: str = "POST"
    url: str
    # 3xx counts: form posts usually answer with a redirect to a confirmation page.
    status_min: int = 200
    status_max: int = 399

    @field_validator("method", mode="before")
    @classmethod
    def _check_method(cls, v: object) -> str:
        method = str(v).upper()
        if method not in HTTP_METHODS:
            raise ValueError(f"unsupported HTTP method: {v!r}")
        return method

    @field_validator("url")
    @classmethod
    def _check_url(cls, v: str) -> str:
        parse_url_pattern(v)
        return v

    @model_validator(mode="after")
    def _check_statuses(self) -> RequestCheck:
        if not 100 <= self.status_min <= self.status_max <= 599:
            raise ValueError("status range must be ordered and within 100-599")
        return self

    def describe(self) -> str:
        return f"{self.method} {self.url} answered {self.status_min}-{self.status_max}"


class TextCheck(BaseModel):
    """The page's text contains a phrase (case and whitespace ignored)."""
    type: Literal["text"] = "text"
    contains: str = Field(min_length=1, max_length=200)

    def describe(self) -> str:
        return f'page text contains "{self.contains}"'


class ValueCheck(BaseModel):
    """A typed value read on this or an earlier sub-goal compares as stated."""
    type: Literal["value"] = "value"
    name: str
    op: Literal["==", "!=", "<", "<=", ">", ">="]
    value: Scalar

    @field_validator("name")
    @classmethod
    def _check_name(cls, v: str) -> str:
        if not _NAME_RE.match(v):
            raise ValueError(f"value name must be an identifier: {v!r}")
        return v

    def describe(self) -> str:
        return "{{$" + self.name + "}} " + f"{self.op} {json.dumps(self.value)}"


EvidenceCheck = Annotated[
    Union[UrlCheck, RequestCheck, TextCheck, ValueCheck],
    Field(discriminator="type"),
]


class CheckResult(BaseModel):
    type: str          # the declared check type
    description: str   # planner-authored; safe to show anywhere
    passed: bool
    detail: str        # what was observed; may contain page or network detail


@dataclass
class EvidenceContext:
    """Everything checks can look at, gathered by the orchestrator."""
    page_url: str
    page_text: str
    events: list[NetworkEvent] = field(default_factory=list)  # since the sub-goal started
    values: dict[str, ExtractedValue] = field(default_factory=dict)


def evaluate(check: EvidenceCheck, context: EvidenceContext) -> CheckResult:
    passed, detail = _EVALUATORS[type(check)](check, context)
    return CheckResult(type=check.type, description=check.describe(), passed=passed, detail=detail)


def _check_url(check: UrlCheck, context: EvidenceContext) -> tuple[bool, str]:
    return url_matches(context.page_url, check.pattern), f"page is at {context.page_url}"


def _check_request(check: RequestCheck, context: EvidenceContext) -> tuple[bool, str]:
    matching = [
        event for event in context.events
        if event.method == check.method and url_matches(event.url, check.url)
    ]
    for event in matching:
        if event.status is not None and check.status_min <= event.status <= check.status_max:
            return True, event.describe()
    if matching:
        return False, "matching requests didn't succeed: " + "; ".join(
            event.describe() for event in matching[-3:]
        )
    return False, "no matching request was sent"


def _normalize(text: str) -> str:
    return " ".join(text.split()).lower()


def _check_text(check: TextCheck, context: EvidenceContext) -> tuple[bool, str]:
    if _normalize(check.contains) in _normalize(context.page_text):
        return True, "found on the page"
    return False, "not found on the page"


def _check_value(check: ValueCheck, context: EvidenceContext) -> tuple[bool, str]:
    ref = "{{$" + check.name + "}}"
    value = context.values.get(check.name)
    if value is None:
        return False, f"{ref} was not read"

    actual, expected = value.value, check.value

    def numeric(x: object) -> bool:
        return isinstance(x, (int, float)) and not isinstance(x, bool)

    if (numeric(actual) and numeric(expected)) or (isinstance(actual, str) and isinstance(expected, str)):
        passed = _OPS[check.op](actual, expected)
    elif isinstance(actual, bool) and isinstance(expected, bool) and check.op in ("==", "!="):
        passed = _OPS[check.op](actual, expected)
    else:
        return False, f"can't compare a {value.type.value} value with {json.dumps(expected)} using {check.op}"
    return passed, f"{ref} is {json.dumps(actual)}"


_EVALUATORS = {
    UrlCheck: _check_url,
    RequestCheck: _check_request,
    TextCheck: _check_text,
    ValueCheck: _check_value,
}
