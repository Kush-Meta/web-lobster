"""Request and result shapes for the web-lobster MCP server.

Calling agents see these as tool schemas, so the field descriptions are written
for a model filling them in.
"""

from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field, model_validator

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


class WebTaskResult(BaseModel):
    run_id: str
    done: bool = Field(description="Every sub-goal was completed.")
    verified: bool = Field(
        description="Done, and every completed sub-goal was proven by evidence checks rather than judged by a model.",
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


class MandateCheck(BaseModel):
    valid: bool
    problems: list[str] = Field(default_factory=list)
    approval_text: str = Field(description="A plain summary of the mandate to show the user before running it.")


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
