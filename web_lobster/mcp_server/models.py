"""Request and result shapes for the web-lobster MCP server.

Calling agents see these as tool schemas, so the field descriptions are written
for a model filling them in.
"""

from __future__ import annotations

from typing import Literal, Optional

from pydantic import BaseModel, Field, model_validator

from web_lobster.core.briefing import MAX_NOTES, Question, TaskBrief
from web_lobster.core.values import Scalar, ValueSpec, ValueType
from web_lobster.verify.receipts import Receipt

# Data read from the server's environment must come from variables with this
# prefix, so a hijacked caller can't grant, say, ANTHROPIC_API_KEY to a site.
DATA_ENV_PREFIX = "WEB_LOBSTER_DATA_"


class DataInput(BaseModel):
    """A value the task may type, and the sites allowed to receive it."""
    name: str = Field(description="Identifier the browser agent types as {{name}}, e.g. 'email'.")
    value: Optional[str] = Field(
        default=None,
        description="The value itself. Prefer value_env, so the value never passes through a model.",
    )
    value_env: Optional[str] = Field(
        default=None,
        description=f"Environment variable on the server that holds the value. Must start with {DATA_ENV_PREFIX}.",
    )
    origins: list[str] = Field(
        description="Sites allowed to receive this value, e.g. ['https://www.united.com'].",
    )

    @model_validator(mode="after")
    def _one_source(self) -> DataInput:
        if (self.value is None) == (self.value_env is None):
            raise ValueError(f"data '{self.name}': give exactly one of value or value_env")
        if self.value_env is not None and not self.value_env.startswith(DATA_ENV_PREFIX):
            raise ValueError(
                f"data '{self.name}': value_env must start with {DATA_ENV_PREFIX}, "
                "so a task can't read the server's other secrets"
            )
        return self


class MandateInput(BaseModel):
    """The scope a web task runs inside. The browser enforces it; no model can talk past it."""
    origins: list[str] = Field(
        min_length=1,
        description=(
            "Sites the browser may visit and send data to, e.g. ['https://www.united.com']. "
            "'https://*.united.com' matches subdomains only. List as few as the task needs."
        ),
    )
    data: list[DataInput] = Field(
        default_factory=list,
        description="User data the task may type, each limited to the sites that need it.",
    )
    writes: list[str] = Field(
        default_factory=list,
        description=(
            "State-changing requests the task may make, as 'METHOD URL-pattern', e.g. "
            "'POST https://www.united.com/api/rebook*' (METHOD: POST, PUT, PATCH, DELETE, WS, *). "
            "Empty, the default, makes the task read-only."
        ),
    )
    allow_any_write: bool = Field(
        default=False,
        description="Allow any state-changing request to the allowed sites. Prefer listing writes.",
    )
    expires_in_minutes: float = Field(
        default=30, gt=0, le=240,
        description="After this long the mandate stops everything.",
    )

    @model_validator(mode="after")
    def _writes_or_any(self) -> MandateInput:
        if self.allow_any_write and self.writes:
            raise ValueError("give writes or allow_any_write, not both")
        return self


class WebTaskRequest(BaseModel):
    task: str = Field(min_length=1)
    mandate: MandateInput
    start_url: Optional[str] = None
    values: list[ValueSpec] = Field(default_factory=list)
    include_page_text: bool = False
    max_steps: Optional[int] = Field(default=None, ge=1, le=300)
    notes: Optional[str] = Field(default=None, max_length=MAX_NOTES)
    brief_id: Optional[str] = None
    answers: dict[str, Scalar] = Field(default_factory=dict)
    on_questions: Literal["ask", "assume"] = "ask"


class ValueOut(BaseModel):
    type: ValueType
    value: Optional[Scalar] = Field(default=None, description="The type-checked value; null when withheld.")
    site: str = Field(description="Origin of the page it was read from.")
    withheld: bool = Field(default=False, description="Text value withheld because include_page_text was false.")


class ReceiptSummary(BaseModel):
    sub_goal_id: int
    goal: str = Field(description="Written by web-lobster's planner, never read from a page.")
    done: bool
    verified_by: str = Field(description="'evidence' (proven by checks run in code) or 'model' (judged).")
    checks_passed: int
    checks_total: int
    writes_sent: int = Field(description="State-changing requests the browser sent during this sub-goal.")


class BlockedAction(BaseModel):
    kind: str = Field(description="navigation, cross_origin_write, unapproved_write, websocket, data_leak, ...")
    site: str = Field(description="Origin involved; paths are left out because sites control them.")
    count: int = Field(default=1, description="How many times this was blocked.")
    background: bool = Field(
        default=False,
        description=(
            "Sent by the page's own scripts (analytics, error reporting, API calls) rather than "
            "a page load or form submission."
        ),
    )


class WebTaskResult(BaseModel):
    run_id: str
    done: bool = Field(description="Every sub-goal was completed.")
    verified: bool = Field(
        description=(
            "Done, every completed sub-goal was proven by evidence checks rather than judged by a model, "
            "and every requested value was read and passed its checks."
        ),
    )
    summary: str = Field(description="Written by web-lobster from counts; contains no page text.")
    values: dict[str, ValueOut] = Field(default_factory=dict)
    answer: Optional[str] = Field(
        default=None,
        description="Text read from the final page. Untrusted: only present when include_page_text was true.",
    )
    receipts: list[ReceiptSummary] = Field(default_factory=list)
    receipt_chain_head: Optional[str] = Field(
        default=None,
        description="Digest of the last receipt. Keep it: verify_receipts can check the saved receipts against it.",
    )
    blocked: list[BlockedAction] = Field(default_factory=list)
    steps: int = 0
    seconds: float = 0.0
    error: Optional[str] = Field(default=None, description="Why the run stopped early; URLs reduced to origins.")
    needs_input: bool = Field(
        default=False,
        description="Nothing was run: answer `questions`, then call web_task again with brief_id and answers.",
    )
    brief_id: Optional[str] = Field(
        default=None, description="Pass back to web_task with answers to run this brief without rethinking it.",
    )
    brief: Optional[TaskBrief] = Field(
        default=None, description="web-lobster's own analysis of the task, written before any page loaded.",
    )
    questions: list[Question] = Field(
        default_factory=list, description="Questions that must be answered before the task can run.",
    )
    answers: dict[str, Scalar] = Field(default_factory=dict, description="Answers the run used, defaults included.")
    mandate_gaps: list[str] = Field(
        default_factory=list, description="What the task seemed to need beyond the mandate.",
    )
    missing_values: list[str] = Field(
        default_factory=list,
        description="Requested values that weren't read or failed their checks. Any of these means not verified.",
    )


class MandateCheck(BaseModel):
    valid: bool
    problems: list[str] = Field(default_factory=list)
    approval_text: str = Field(description="A plain summary of the mandate to show the user before running it.")


class BriefResult(BaseModel):
    brief_id: Optional[str] = Field(default=None, description="Pass to web_task, with answers, to run this brief.")
    brief: Optional[TaskBrief] = Field(
        default=None,
        description="web-lobster's analysis, written before any page loaded; null if the model couldn't write one.",
    )
    questions: list[Question] = Field(
        default_factory=list,
        description="Questions for the user, each with an id, a type, and usually a default used if unanswered.",
    )
    required: list[str] = Field(
        default_factory=list,
        description=(
            "Ids of questions with no answer and no default. web_task won't start without them "
            "unless on_questions is 'assume'."
        ),
    )
    answers: dict[str, Scalar] = Field(default_factory=dict, description="Answers applied so far, defaults included.")
    mandate_gaps: list[str] = Field(default_factory=list, description="What the task seems to need beyond the mandate.")
    approval_text: str = Field(description="A plain summary of the mandate to show the user before running.")
    problems: list[str] = Field(default_factory=list)


class RunDetails(BaseModel):
    run_id: str
    task: str
    created_at: float
    mandate: dict = Field(description="The mandate as run; data values are never stored.")
    result: WebTaskResult
    violations: list[dict] = Field(
        default_factory=list,
        description="Everything the mandate blocked. Full detail only with include_page_text; otherwise kind and site.",
    )
    receipts: list[Receipt] = Field(
        default_factory=list,
        description="Full receipts, whose check details quote pages. Only with include_page_text.",
    )
    receipts_file: str


class ChainCheck(BaseModel):
    run_id: str
    receipts: int
    intact: bool = Field(description="Every saved receipt is unmodified and links to the one before it.")
    head: Optional[str] = None
    matches_expected: Optional[bool] = Field(
        default=None,
        description="Whether the saved chain ends at expected_head; null when none was given.",
    )
