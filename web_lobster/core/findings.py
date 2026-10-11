"""Findings: facts about the world that code established while running.

Twelve of the fixes this project accumulated were reaching for this one idea.
Execution learns a great deal — this URL answers 404, this field won't keep a
typed value, this proof can never pass — and until now almost none of it could
travel. The only channel back to planning was a free-text failure string, so a
plan built on a wrong assumption could not be corrected, only thrown away.

A finding is page-DERIVED but never page-CONTENT, and that distinction is the
whole safety argument. `PageMissing(url=..., status=404)` carries a URL the
planner wrote itself and a status code the network measured; nothing a page
authored crosses the line. Anything that would — a label, a heading, a product
name — is marked `executor` and stops below the planner, exactly as text values
already do (see core/values.py).

The visibility field is therefore load-bearing, not bookkeeping. Adding a new
kind of finding means deciding, explicitly, who is allowed to hear it.
"""

from __future__ import annotations

from enum import Enum
from typing import Optional

from pydantic import BaseModel, Field


class Seen(str, Enum):
    """Who may hear a finding."""

    EXECUTOR = "executor"  # the step loop and the executor prompt; never the planner
    PLANNER = "planner"    # safe for the planner: nothing a page authored
    RECORD = "record"      # the run record and receipts only


class Finding(BaseModel):
    """One thing code worked out about the world during a run."""

    kind: str
    detail: str                     # worded for whoever is allowed to read it
    seen_by: Seen = Seen.EXECUTOR
    url: Optional[str] = None       # only ever a URL the plan or the browser already had
    status: Optional[int] = None    # measured, not read off a page

    def describe(self) -> str:
        return self.detail


def page_missing(url: str, status: Optional[int] = None) -> Finding:
    """A page the agent was sent to that isn't there.

    Safe for the planner: it wrote the URL, and the status came from the network.
    """
    answered = f"answered {status}" if status and status >= 400 else "has nothing on it"
    return Finding(
        kind="page_missing", url=url, status=status, seen_by=Seen.PLANNER,
        detail=f"{url} {answered}",
    )


def check_unreachable(check: str, because: str) -> Finding:
    """A proof that cannot pass, so the step it guards can never finish.

    Safe for the planner: the check is text the planner authored itself.
    """
    return Finding(
        kind="check_unreachable", seen_by=Seen.PLANNER,
        detail=f"the check {check!r} can never pass: {because}",
    )


def value_not_here(name: str, url: str) -> Finding:
    """A requested value that isn't on the page the plan expected it on.

    Safe for the planner: it asked for the name, and the URL is the plan's own.
    """
    return Finding(
        kind="value_not_here", url=url, seen_by=Seen.PLANNER,
        detail=f"{name} isn't on {url}",
    )


def field_needs_choosing(label: str) -> Finding:
    """A field that won't keep what's typed into it.

    Executor-only: the label is text the page wrote, so it stops here.
    """
    return Finding(
        kind="field_needs_choosing", seen_by=Seen.EXECUTOR,
        detail=f"{label!r} won't keep a typed value — it has to be chosen from its list",
    )


def no_progress(attempts: int) -> Finding:
    """The same state, over and over. A count, so the planner may hear it."""
    return Finding(
        kind="no_progress", seen_by=Seen.PLANNER,
        detail=f"{attempts} attempts on this step changed nothing",
    )


def for_planner(findings: list[Finding]) -> list[str]:
    """What the planner is allowed to hear, as lines for its prompt.

    The filter is the isolation boundary. Nothing else in this module decides it.
    """
    return [f.describe() for f in findings if f.seen_by is Seen.PLANNER]


def for_executor(findings: list[Finding]) -> list[str]:
    """What the executor is allowed to hear: everything except record-only."""
    return [f.describe() for f in findings if f.seen_by is not Seen.RECORD]
