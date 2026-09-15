"""Thinking before acting: the task brief, follow-up questions, planning context,
and plan review.

Before the browser opens, the planner writes a brief about the task: the outcome
the user wants, what it will assume, what it has to ask, what the task needs
from the mandate, and what done looks like. Questions go to the user. Code checks
the brief against the mandate, and after planning checks the plan against the
mandate, and the planner gets a round to fix what that review finds.

All of this is on the trusted side of the agent (see core/orchestrator.py). The
brief is written before any page loads, from a PlanningContext built only from
the user, the caller, the mandate, the clock, and trusted memory. Answers come
from the user, and the review is code comparing planner output with the mandate.
No page content goes in.
"""

from __future__ import annotations

import json
import re
import time
from datetime import datetime
from typing import Optional

from pydantic import BaseModel, Field, ValidationError, field_validator, model_validator

from web_lobster.core.schemas import TaskPlan
from web_lobster.core.values import VALUE_REF_RE, Scalar, ValueSpec, ValueType, coerce_value
from web_lobster.mandate.schema import (
    PLACEHOLDER_RE,
    Mandate,
    ensure_scheme,
    origin_matches,
    parse_origin_pattern,
    parse_url_pattern,
    url_origin,
)

MAX_QUESTIONS = 3
MAX_LIST_ITEMS = 5
MAX_LINE = 300
MAX_THINKING = 1500
MAX_NOTES = 2000
MAX_EXECUTOR_BRIEFING = 1000
# Code asks this when the brief expects more than the mandate allows.
MANDATE_QUESTION_ID = "run_within_mandate"

_IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_NON_IDENTIFIER_RE = re.compile(r"[^a-z0-9_]+")
_TYPE_ALIASES = {
    "yes_no": "boolean", "yes/no": "boolean", "bool": "boolean",
    "string": "text", "str": "text", "float": "number", "int": "integer",
}
_EXPECTED = {
    ValueType.NUMBER: "a number",
    ValueType.INTEGER: "a whole number",
    ValueType.BOOLEAN: "yes or no",
    ValueType.DATE: "a date as YYYY-MM-DD",
    ValueType.TEXT: "text",
}
# Sub-goals that change something on a site. Anchored to the opening verb, so
# "Open the order history" doesn't count as ordering.
_WRITE_GOAL = re.compile(
    r"^\s*(submit|send|book|buy|purchase|place|pay|check ?out|subscribe|sign up|register|"
    r"post|publish|delete|remove|cancel|save|confirm|apply|reserve|rebook|transfer|upload)\b",
    re.IGNORECASE,
)
# Sub-goals that are one click or keystroke rather than an outcome: small planners
# split "search for X" into typing, clicking the button, and opening the result,
# and the typing step then fails its own check once the search moves the page.
_CLICK_LEVEL_GOAL = re.compile(
    r"^\s*(type|enter|input|press|hit|tap|scroll)\b|^\s*click\b.*\b(button|box|field|icon|tab)\b",
    re.IGNORECASE,
)
# Words that mean a step changes something, wherever they appear ("Click the Buy button").
_CHANGE_WORD = re.compile(
    r"\b(submit|send|book|buy|purchase|order|pay|check ?out|subscribe|sign up|register|post|publish|"
    r"delete|remove|cancel|save|confirm|apply|reserve|rebook|transfer|upload)\b",
    re.IGNORECASE,
)


def clip(text: object, limit: int = MAX_LINE) -> str:
    """One line of at most limit characters."""
    flat = " ".join(str(text).split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


def format_answer(value: Scalar) -> str:
    if isinstance(value, bool):
        return "yes" if value else "no"
    return str(value)


def is_click_level(goal: str) -> bool:
    """Whether a sub-goal is one click or keystroke rather than an outcome."""
    return bool(_CLICK_LEVEL_GOAL.search(goal))


def mentions_a_change(text: str) -> bool:
    """Whether text talks about changing something anywhere in it."""
    return bool(_CHANGE_WORD.search(text))


def _identifier(raw: object, fallback: str) -> str:
    ident = _NON_IDENTIFIER_RE.sub("_", str(raw).strip().lower()).strip("_")[:40]
    return ident if ident and ident[0].isalpha() else fallback


def _strings(raw: object, limit: int = MAX_LIST_ITEMS) -> list[str]:
    items = [raw] if isinstance(raw, str) else raw if isinstance(raw, list) else []
    return [clip(item) for item in items if isinstance(item, str) and item.strip()][:limit]


# ── Questions ────────────────────────────────────────────────────────────────

class Question(BaseModel):
    """Something the planner needs the user to decide before the task can go well."""
    id: str
    question: str
    why: str = ""
    type: ValueType = ValueType.TEXT
    choices: list[str] = Field(default_factory=list)
    # Used when nobody answers. A question without one blocks a run that can't ask.
    default: Optional[Scalar] = None

    @field_validator("id")
    @classmethod
    def _check_id(cls, v: str) -> str:
        if not _IDENTIFIER_RE.match(v):
            raise ValueError(f"question id must be an identifier: {v!r}")
        return v

    @model_validator(mode="after")
    def _check(self) -> Question:
        if self.type == ValueType.CHOICE and not self.choices:
            raise ValueError(f"choice question {self.id!r} needs choices")
        if self.default is not None:
            self.default = self.coerce(self.default)  # a default that doesn't fit is dropped
        return self

    def spec(self) -> ValueSpec:
        return ValueSpec(name=self.id, type=self.type, description=self.question, choices=self.choices)

    def coerce(self, raw: object) -> Optional[Scalar]:
        """The answer checked against the question's type, or None if it doesn't fit."""
        return coerce_value(self.spec(), raw)

    def expected(self) -> str:
        if self.type == ValueType.CHOICE:
            return "one of: " + ", ".join(self.choices)
        return _EXPECTED[self.type]


def _parse_question(item: object, index: int) -> Optional[Question]:
    if isinstance(item, str):
        item = {"question": item}
    if not isinstance(item, dict):
        return None
    text = clip(item.get("question") or "")
    if not text:
        return None
    fallback_id = f"q{index + 1}"
    fields = dict(
        id=_identifier(item.get("id") or fallback_id, fallback_id),
        question=text,
        why=clip(item.get("why") or ""),
    )
    type_name = str(item.get("type") or "text").strip().lower()
    choices = [clip(c, 80) for c in item.get("choices") or [] if isinstance(c, (str, int, float))]
    default = item.get("default")
    try:
        return Question(
            **fields,
            type=_TYPE_ALIASES.get(type_name, type_name),
            choices=choices,
            default=default if isinstance(default, (str, int, float, bool)) else None,
        )
    except ValidationError:
        # An unknown type, or a choice without choices: ask it as plain text.
        return Question(**fields)


# ── The brief ────────────────────────────────────────────────────────────────

class TaskBrief(BaseModel):
    """The planner's own analysis of a task, written before the browser opens."""
    goal: str = ""
    thinking: str = ""
    assumptions: list[str] = Field(default_factory=list)
    questions: list[Question] = Field(default_factory=list)
    success: str = ""
    sites: list[str] = Field(default_factory=list)
    data: list[str] = Field(default_factory=list)
    changes_something: bool = False
    risks: list[str] = Field(default_factory=list)


def parse_brief(response: str) -> Optional[TaskBrief]:
    """Read a brief from a model reply, tolerating fences, prose, and missing
    fields. None when the reply holds no JSON object."""
    text = response.strip()
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        raw = json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        return None
    if not isinstance(raw, dict):
        return None

    questions: list[Question] = []
    for index, item in enumerate(raw.get("questions") or []):
        question = _parse_question(item, index)
        if question and question.id != MANDATE_QUESTION_ID and all(q.id != question.id for q in questions):
            questions.append(question)
        if len(questions) == MAX_QUESTIONS:
            break

    return TaskBrief(
        goal=clip(raw.get("goal") or ""),
        thinking=clip(raw.get("thinking") or "", MAX_THINKING),
        assumptions=_strings(raw.get("assumptions")),
        questions=questions,
        success=clip(raw.get("success") or ""),
        sites=_strings(raw.get("sites")),
        data=[_identifier(name, "data") for name in _strings(raw.get("data"))],
        changes_something=raw.get("changes_something") is True,
        risks=_strings(raw.get("risks")),
    )


# ── The mandate, as the planner sees it ──────────────────────────────────────

def mandate_lines(mandate: Mandate, now: Optional[float] = None) -> list[str]:
    """A plain description of a mandate. Data is named, never shown."""
    lines = ["Sites: " + ", ".join(mandate.origins)]
    if mandate.data:
        lines.append("Data the browser can type for the user: " + "; ".join(
            "{{" + grant.name + "}} (only on " + ", ".join(grant.origins) + ")" for grant in mandate.data
        ))
    else:
        lines.append("Data the browser can type for the user: none")
    if mandate.writes is None:
        lines.append("Changes allowed: any request that changes something on those sites")
    elif not mandate.writes:
        lines.append("Changes allowed: none. The task is read-only: nothing can be submitted, bought, sent, or saved")
    else:
        lines.append("Changes allowed only: " + "; ".join(str(rule) for rule in mandate.writes))
    if mandate.expires_at is not None:
        minutes = max(0, round((mandate.expires_at - (now or time.time())) / 60))
        lines.append(f"Expires: in {minutes} minutes")
    return lines


def _same_site(host: str, allowed: str) -> bool:
    return host == allowed or host.endswith("." + allowed) or allowed.endswith("." + host)


def _compact(name: str) -> str:
    return name.replace("_", "").lower()


def mandate_gaps(brief: TaskBrief, mandate: Mandate) -> list[str]:
    """What the brief expects to need that the mandate doesn't allow.

    Deliberately lenient: a gap becomes a question to the user, so a false alarm
    costs a question and a missed gap costs only what it costs today.
    """
    gaps: list[str] = []
    allowed_hosts = [parse_origin_pattern(p)[1].removeprefix("*.") for p in mandate.origins]
    for site in brief.sites:
        origin = url_origin(ensure_scheme(site.strip()))
        if origin is None or "." not in origin[1]:
            continue  # "Wikipedia" names a site, not a host
        if not any(_same_site(origin[1], host) for host in allowed_hosts):
            gaps.append(f"it expects to use {origin[1]}, which the mandate doesn't allow")
    if brief.changes_something and mandate.writes == []:
        gaps.append("it expects to submit or change something, but the mandate is read-only")
    granted = [_compact(grant.name) for grant in mandate.data]
    for name in brief.data:
        key = _compact(name)
        if not any(key in g or g in key for g in granted):
            gaps.append(f"it expects to type the user's {name.replace('_', ' ')}, which the mandate doesn't grant")
    return gaps


def mandate_question(gaps: list[str]) -> Optional[Question]:
    if not gaps:
        return None
    return Question(
        id=MANDATE_QUESTION_ID,
        type=ValueType.BOOLEAN,
        question="This task may need more than the mandate allows: " + "; ".join(gaps)
        + ". Run it anyway, inside the mandate?",
        why="The browser blocks anything outside the mandate, so the task may not finish.",
    )


# ── Answers ──────────────────────────────────────────────────────────────────

class Resolution(BaseModel):
    """Where each question stands after answers and defaults are applied."""
    answers: dict[str, Scalar] = Field(default_factory=dict)
    defaulted: list[str] = Field(default_factory=list)
    unanswered: list[Question] = Field(default_factory=list)
    # Answers that didn't fit their question. Never echoes the answer itself.
    problems: list[str] = Field(default_factory=list)


def resolve_answers(
    questions: list[Question], supplied: Optional[dict[str, object]], use_defaults: bool,
) -> Resolution:
    resolution = Resolution()
    for question in questions:
        raw = (supplied or {}).get(question.id)
        if raw is not None:
            value = question.coerce(raw)
            if value is not None:
                resolution.answers[question.id] = value
                continue
            resolution.problems.append(f"{question.id}: expected {question.expected()}")
        if use_defaults and question.default is not None:
            resolution.answers[question.id] = question.default
            resolution.defaulted.append(question.id)
            continue
        resolution.unanswered.append(question)
    return resolution


# ── Planning context ─────────────────────────────────────────────────────────

class PlanningContext(BaseModel):
    """Everything the planner may know about a task, from trusted sources only.

    The task, notes, and requested values come from the user or calling agent,
    the mandate from its approval, the time from the clock, memory from trusted
    records, the brief from the planner itself, and answers from the user.
    """
    task: str
    now: datetime = Field(default_factory=lambda: datetime.now().astimezone())
    mandate: list[str] = Field(default_factory=list)
    requested_values: list[ValueSpec] = Field(default_factory=list)
    notes: Optional[str] = None
    memory: Optional[str] = None
    brief: Optional[TaskBrief] = None
    questions: list[Question] = Field(default_factory=list)
    answers: dict[str, Scalar] = Field(default_factory=dict)
    # Answers supplied before any question, under the user's own labels, that no question matched
    given: dict[str, Scalar] = Field(default_factory=dict)
    gaps: list[str] = Field(default_factory=list)

    def render(self, *, include_memory: bool = True) -> str:
        now = self.now
        zone = f" {now.tzname()}" if now.tzname() else ""
        sections = [f"NOW: {now:%A} {now.day} {now:%B %Y, %H:%M}{zone}"]

        if self.mandate:
            sections.append(
                "MANDATE (approved by the user and enforced by the browser; plan inside it):\n"
                + "\n".join(f"  {line}" for line in self.mandate)
            )
        if self.requested_values:
            sections.append("VALUES THE USER WANTS BACK (plan to reach the page that shows them):\n" + "\n".join(
                f"  - {spec.name} ({spec.type.value})" + (f": {clip(spec.description, 160)}" if spec.description else "")
                for spec in self.requested_values
            ))
        if self.notes:
            sections.append("USER NOTES (written by the user; follow them where they apply):\n  "
                            + clip(self.notes, MAX_NOTES))
        if self.given:
            sections.append("GIVEN BY THE USER UP FRONT (settled; don't ask about these):\n" + "\n".join(
                f"  - {clip(key, 40)}: {clip(format_answer(value))}" for key, value in self.given.items()
            ))
        if self.brief:
            sections.append(self._brief_section(self.brief))

        asked = [q for q in self.questions if q.id != MANDATE_QUESTION_ID]
        answered = [q for q in asked if q.id in self.answers]
        if answered:
            sections.append("USER ANSWERS:\n" + "\n".join(
                f"  - {q.question} → {format_answer(self.answers[q.id])}" for q in answered
            ))
        open_questions = [q for q in asked if q.id not in self.answers]
        if open_questions:
            sections.append(
                "NOT ANSWERED (decide sensibly, follow your assumptions, and don't stall on them):\n"
                + "\n".join(f"  - {q.question}" for q in open_questions)
            )
        if self.gaps:
            sections.append(
                "BEYOND THE MANDATE (the user chose to run anyway; stay inside the mandate and "
                "stop before anything it blocks):\n" + "\n".join(f"  - {gap}" for gap in self.gaps)
            )
        if include_memory and self.memory:
            sections.append(self.memory)
        return "\n\n".join(sections)

    @staticmethod
    def _brief_section(brief: TaskBrief) -> str:
        lines = ["TASK BRIEF (your own analysis, written before the browser opened):"]
        if brief.goal:
            lines.append(f"  Goal: {brief.goal}")
        if brief.success:
            lines.append(f"  Done when: {brief.success}")
        if brief.assumptions:
            lines.append("  Assumptions:\n" + "\n".join(f"    - {a}" for a in brief.assumptions))
        if brief.risks:
            lines.append("  Risks:\n" + "\n".join(f"    - {r}" for r in brief.risks))
        return "\n".join(lines)

    def for_executor(self) -> Optional[str]:
        """The part of the brief that helps pick actions: the goal, answers, assumptions, notes."""
        lines = []
        if self.brief and self.brief.goal:
            lines.append(f"Overall goal: {self.brief.goal}")
        for key, value in self.given.items():
            lines.append(f"The user said {clip(key, 40)}: {clip(format_answer(value), 120)}")
        for question in self.questions:
            if question.id in self.answers and question.id != MANDATE_QUESTION_ID:
                lines.append(
                    f'The user answered "{clip(question.question, 120)}": {format_answer(self.answers[question.id])}'
                )
        if self.brief and self.brief.assumptions:
            lines.append("Assume: " + "; ".join(self.brief.assumptions))
        if self.notes:
            lines.append("User notes: " + clip(self.notes, 400))
        text = "\n".join(lines)[:MAX_EXECUTOR_BRIEFING]
        return text or None


# ── Plan review ──────────────────────────────────────────────────────────────

def _url_pattern_allowed(pattern: str, mandate: Mandate) -> bool:
    try:
        origin, _ = parse_url_pattern(pattern)
    except ValueError:
        return True  # malformed checks are dropped when the plan is parsed
    for allowed in (parse_origin_pattern(p) for p in mandate.origins):
        if origin == allowed:
            return True
        if not origin[1].startswith("*.") and origin_matches(origin, allowed):
            return True
    return False


def review_plan(plan: TaskPlan, mandate: Optional[Mandate], max_sub_goals: int) -> list[str]:
    """Problems code can see in a plan, worded for the planner to fix.

    Built only from the plan's own text and the mandate, so feeding them back to
    the planner keeps it isolated from pages.
    """
    issues: list[str] = []
    goals = plan.sub_goals
    if len(goals) > max_sub_goals:
        issues.append(
            f"The plan has {len(goals)} sub-goals. Merge click-level steps into outcomes "
            f"(at most {max_sub_goals}); the browser agent works out the clicks itself."
        )

    declared: set[str] = set()
    granted = {grant.name for grant in mandate.data} if mandate else set()
    for goal in goals:
        label = f'Sub-goal {goal.id} ("{clip(goal.goal, 80)}")'
        text = f"{goal.goal} {goal.success_criteria}"

        for name in VALUE_REF_RE.findall(text):
            if name not in declared:
                issues.append(f"{label} uses {{{{${name}}}}}, but no earlier sub-goal reads a value called {name}.")
        for name in PLACEHOLDER_RE.findall(text):
            if name not in granted:
                issues.append(
                    f"{label} types {{{{{name}}}}}, but the mandate grants no data called {name}, "
                    "so the browser can't fill it in."
                )

        if len(goals) > 1 and is_click_level(goal.goal):
            issues.append(
                f"{label} is a single click or keystroke. Merge it into the outcome it serves "
                '(for example "Search the site for …"); the browser agent works out the clicks itself.'
            )

        if _WRITE_GOAL.match(goal.goal):
            if mandate is not None and mandate.writes == []:
                issues.append(
                    f"{label} would change something, but the mandate is read-only. "
                    "Drop it, or end the plan before it."
                )
            elif not any(check.type == "request" for check in goal.evidence):
                issues.append(
                    f'{label} changes something, so give it a "request" evidence check '
                    "for the request it has to send."
                )

        if mandate is not None:
            for check in goal.evidence:
                pattern = check.pattern if check.type == "url" else check.url if check.type == "request" else None
                if pattern and not _url_pattern_allowed(pattern, mandate):
                    issues.append(
                        f"{label} has a {check.type} check on a site the mandate doesn't allow, "
                        "so it can never pass."
                    )

        declared.update(spec.name for spec in goal.extract)
    return issues


def origin_strings(patterns: list[str]) -> list[str]:
    """The concrete origins among mandate origin patterns, skipping wildcards."""
    origins = []
    for pattern in patterns:
        scheme, host, port = parse_origin_pattern(pattern)
        if host.startswith("*."):
            continue
        default_port = 443 if scheme == "https" else 80
        origins.append(f"{scheme}://{host}" + ("" if port == default_port else f":{port}"))
    return origins


class Briefing(BaseModel):
    """Where a task stands after thinking it through, before the browser opens."""
    context: PlanningContext
    # Questions without an answer or a default, when nobody could be asked
    unanswered: list[Question] = Field(default_factory=list)
    problems: list[str] = Field(default_factory=list)
    # The user chose not to run a task that needs more than the mandate allows
    declined: bool = False

    @property
    def needs_input(self) -> bool:
        return bool(self.unanswered)
