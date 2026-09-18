"""Runs web tasks for the MCP server.

Nothing here depends on MCP, so the logic can be tested directly and served
over any transport. See docs/design/step-5-mcp-server.md for the reasoning
behind the defaults.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
import uuid
from pathlib import Path
from typing import Callable, Mapping, Optional, Sequence

from pydantic import ValidationError

from web_lobster.core.briefing import TaskBrief
from web_lobster.core.config import WebLobsterConfig
from web_lobster.core.orchestrator import AgentResult, Orchestrator
from web_lobster.core.values import ValueType, origin_of
from web_lobster.mandate.enforcer import Violation, sent_by_page_script
from web_lobster.mandate.schema import DataGrant, Mandate
from web_lobster.mcp_server.agents import ProgressFn, ServerUI, ValueRequestingPlanner
from web_lobster.mcp_server.models import (
    BlockedAction,
    BriefResult,
    ChainCheck,
    MandateCheck,
    MandateInput,
    ReceiptSummary,
    RunDetails,
    ValueOut,
    WebTaskRequest,
    WebTaskResult,
)
from web_lobster.verify.receipts import read_receipts, verify_chain, write_receipts

DEFAULT_RUNS_DIR = Path.home() / ".web_lobster" / "runs"
_RUN_ID_RE = re.compile(r"^[0-9a-f]{12}$")
_URL_RE = re.compile(r"(?:https?|wss?)://[^\s'\"<>()]+")

# Called with the orchestrator before a run; tests use it to swap in scripted models.
PrepareFn = Callable[[Orchestrator, WebTaskRequest], None]


class MandateError(ValueError):
    """The requested mandate can't be built. Messages never include data values."""


def sanitize(text: str, limit: int = 300) -> str:
    """Reduce URLs to their origins and flatten whitespace: sites control paths and queries."""
    text = _URL_RE.sub(lambda match: origin_of(match.group(0)), text)
    return " ".join(text.split())[:limit]


def _problems(error: ValidationError) -> str:
    return "; ".join(
        f"{'.'.join(str(part) for part in e['loc']) or 'mandate'}: {e['msg']}"
        for e in error.errors(include_input=False, include_url=False)
    )


def build_mandate(
    task: str, spec: MandateInput, environ: Optional[Mapping[str, str]] = None,
) -> Mandate:
    """Turn a tool call's mandate into an enforceable Mandate, resolving env-var data."""
    environ = os.environ if environ is None else environ
    try:
        grants = []
        for item in spec.data:
            if item.value_env is not None:
                value = environ.get(item.value_env)
                if not value:
                    raise MandateError(f"data '{item.name}': {item.value_env} is not set on the server")
            else:
                value = item.value
            grants.append(DataGrant(name=item.name, value=value, origins=item.origins))
        return Mandate(
            task=task,
            origins=spec.origins,
            data=grants,
            writes=None if spec.allow_any_write else list(spec.writes),
            expires_at=time.time() + spec.expires_in_minutes * 60,
        )
    except ValidationError as e:
        raise MandateError(_problems(e)) from None


def approval_text(task: str, spec: MandateInput) -> str:
    """What a person should read before letting the task run."""
    lines = [f"Task: {task}", "Sites: " + ", ".join(spec.origins)]
    if spec.data:
        lines.append("Data it may type: " + "; ".join(
            f"{item.name} (only on {', '.join(item.origins)})" for item in spec.data
        ))
    else:
        lines.append("Data it may type: none")
    if spec.allow_any_write:
        lines.append("Writes: any request that changes something on those sites")
    elif spec.writes:
        lines.append("Writes it may make: " + "; ".join(spec.writes))
    else:
        lines.append("Writes: none (read-only)")
    lines.append(f"Expires: {spec.expires_in_minutes:g} minutes after it starts")
    return "\n".join(lines)


def blocked_actions(violations: list[Violation]) -> list[BlockedAction]:
    """Group what the mandate blocked by kind and site, flagging requests the page's scripts sent."""
    grouped: dict[tuple[str, str, bool], BlockedAction] = {}
    for violation in violations:
        background = sent_by_page_script(violation)
        key = (violation.kind.value, origin_of(violation.url), background)
        if key in grouped:
            grouped[key].count += 1
        else:
            grouped[key] = BlockedAction(kind=key[0], site=key[1], background=background)
    return list(grouped.values())


class WebTaskService:
    """Runs web tasks under mandates and keeps a record of each run."""

    def __init__(
        self,
        config: WebLobsterConfig,
        *,
        runs_dir: Path = DEFAULT_RUNS_DIR,
        max_concurrent: int = 1,
        approve_confirmations: bool = False,
        prepare: Optional[PrepareFn] = None,
        environ: Optional[Mapping[str, str]] = None,
    ):
        self.config = config
        self.runs_dir = Path(runs_dir)
        self.approve_confirmations = approve_confirmations
        self._prepare = prepare
        self._environ = environ
        # Each task drives a real browser.
        self._slots = asyncio.Semaphore(max_concurrent)

    def check_mandate(self, task: str, spec: MandateInput) -> MandateCheck:
        try:
            build_mandate(task, spec, self._environ)
            problems = []
        except MandateError as e:
            problems = [str(e)]
        return MandateCheck(valid=not problems, problems=problems, approval_text=approval_text(task, spec))

    async def brief(self, request: WebTaskRequest) -> BriefResult:
        """Think a task through without opening a browser.

        Returns the brief, its questions with defaults, and any gaps between what
        the task seems to need and the mandate. Saved like a run, so web_task can
        run the same brief later with the user's answers.
        """
        approval = approval_text(request.task, request.mandate)
        try:
            mandate = build_mandate(request.task, request.mandate, self._environ)
        except MandateError as e:
            return BriefResult(approval_text=approval, problems=[f"The mandate is invalid: {e}"])
        start_url = request.start_url or mandate.default_start_url()
        if not start_url:
            return BriefResult(approval_text=approval, problems=["Every origin is a wildcard, so start_url is required."])

        run_id = uuid.uuid4().hex[:12]
        started = time.monotonic()
        orchestrator = self._orchestrator(request, mandate, progress=None)
        memory_context, _ = orchestrator.recall(request.task, start_url)
        briefing = await orchestrator.think(
            request.task, start_url, notes=request.notes, requested_values=request.values,
            answers=request.answers, memory_context=memory_context,
        )
        context = briefing.context
        result = WebTaskResult(
            run_id=run_id, done=False, verified=False,
            summary="Brief only: nothing was run.",
            seconds=round(time.monotonic() - started, 1),
            needs_input=briefing.needs_input, brief_id=run_id, brief=context.brief,
            questions=briefing.unanswered, answers=context.answers, mandate_gaps=context.gaps,
        )
        self._save(run_id, request, mandate, start_url, None, result)
        return BriefResult(
            brief_id=run_id, brief=context.brief, questions=context.questions,
            required=[q.id for q in briefing.unanswered], answers=context.answers,
            mandate_gaps=context.gaps, approval_text=approval, problems=briefing.problems,
        )

    async def run(self, request: WebTaskRequest, progress: Optional[ProgressFn] = None) -> WebTaskResult:
        run_id = uuid.uuid4().hex[:12]
        started = time.monotonic()

        try:
            mandate = build_mandate(request.task, request.mandate, self._environ)
        except MandateError as e:
            return self._failed(run_id, started, f"The mandate is invalid: {e}")
        start_url = request.start_url or mandate.default_start_url()
        if not start_url:
            return self._failed(run_id, started, "Every origin is a wildcard, so start_url is required.")

        brief = None
        if request.brief_id:
            try:
                record = self._load(request.brief_id)
            except ValueError as e:
                return self._failed(run_id, started, str(e))
            if record["task"] != request.task:
                return self._failed(run_id, started, "That brief_id is for a different task; call brief_task again.")
            stored = record["result"].get("brief")
            brief = TaskBrief.model_validate(stored) if stored else None

        async with self._slots:
            orchestrator = self._orchestrator(request, mandate, progress)
            agent_result = await orchestrator.run(
                request.task, start_url=start_url, notes=request.notes,
                requested_values=request.values, brief=brief, answers=request.answers,
            )

        result = self._result(run_id, request, agent_result, time.monotonic() - started)
        self._save(run_id, request, mandate, start_url, agent_result, result)
        return result

    def _orchestrator(
        self, request: WebTaskRequest, mandate: Mandate, progress: Optional[ProgressFn],
    ) -> Orchestrator:
        config = self.config.model_copy(deep=True)
        if request.max_steps:
            config.agent.max_steps = request.max_steps
        # Nobody can be asked mid-call, so "ask" returns unanswered questions instead of running.
        config.agent.questions = request.on_questions
        ui = ServerUI(progress, self.approve_confirmations, config.agent.max_steps)
        orchestrator = Orchestrator(config, shared_state=ui, mandate=mandate)
        if self._prepare:
            self._prepare(orchestrator, request)
        if request.values:
            orchestrator.planner = ValueRequestingPlanner(orchestrator.planner, request.values)
        return orchestrator

    def get_run(self, run_id: str, include_page_text: bool = False) -> RunDetails:
        record = self._load(run_id)
        result = WebTaskResult.model_validate(record["result"])
        violations = record["violations"]
        receipts = []
        if include_page_text:
            result.answer = record["page_text"]["answer"]
            for name, value in record["page_text"]["text_values"].items():
                if name in result.values:
                    result.values[name] = result.values[name].model_copy(update={"value": value, "withheld": False})
            receipts = read_receipts(self._receipts_path(run_id))
        else:
            violations = [{"kind": v["kind"], "site": origin_of(v["url"])} for v in violations]
        return RunDetails(
            run_id=run_id,
            task=record["task"],
            created_at=record["created_at"],
            mandate=record["mandate"],
            result=result,
            violations=violations,
            receipts=receipts,
            receipts_file=str(self._receipts_path(run_id)),
        )

    def verify_receipts(self, run_id: str, expected_head: Optional[str] = None) -> ChainCheck:
        self._load(run_id)
        receipts = read_receipts(self._receipts_path(run_id))
        head = receipts[-1].digest if receipts else None
        return ChainCheck(
            run_id=run_id,
            receipts=len(receipts),
            intact=verify_chain(receipts),
            head=head,
            matches_expected=None if expected_head is None else head == expected_head,
        )

    # ── Internals ────────────────────────────────────────────────────────────

    def _result(
        self, run_id: str, request: WebTaskRequest, agent: AgentResult, seconds: float,
    ) -> WebTaskResult:
        receipts = [
            ReceiptSummary(
                sub_goal_id=r.sub_goal_id,
                goal=r.goal,
                done=r.achieved,
                verified_by=r.basis,
                checks_passed=sum(check.passed for check in r.checks),
                checks_total=len(r.checks),
                writes_sent=len(r.writes),
            )
            for r in agent.receipts
        ]
        values = {}
        for name, value in agent.values.items():
            withheld = value.type == ValueType.TEXT and not request.include_page_text
            values[name] = ValueOut(
                type=value.type, value=None if withheld else value.value,
                site=value.origin, withheld=withheld,
            )
        completed = [r for r in agent.receipts if r.achieved]
        # Requested values that weren't read, or failed their type and shape checks
        missing = [] if agent.needs_input else [spec.name for spec in request.values if spec.name not in agent.values]
        verified = (
            agent.success and bool(completed) and all(r.basis == "evidence" for r in completed) and not missing
        )
        blocked = blocked_actions(agent.violations)

        return WebTaskResult(
            run_id=run_id,
            done=agent.success,
            verified=verified,
            summary=self._summary(
                agent.success, verified, receipts, blocked, agent.error,
                needs_input=agent.needs_input, problems=agent.problems, missing=missing,
            ),
            values=values,
            answer=agent.answer if request.include_page_text else None,
            receipts=receipts,
            receipt_chain_head=agent.receipts[-1].digest if agent.receipts else None,
            blocked=blocked,
            steps=agent.steps_taken,
            seconds=round(seconds, 1),
            error=sanitize(agent.error) if agent.error else None,
            needs_input=agent.needs_input,
            brief_id=run_id if agent.brief else None,
            brief=agent.brief,
            questions=agent.questions,
            answers=agent.answers,
            mandate_gaps=agent.gaps,
            missing_values=missing,
        )

    @staticmethod
    def _summary(
        done: bool, verified: bool, receipts: list[ReceiptSummary],
        blocked: list[BlockedAction], error: Optional[str],
        needs_input: bool = False, problems: Sequence[str] = (), missing: Sequence[str] = (),
    ) -> str:
        if needs_input:
            parts = [
                "Not started: the task needs answers first. Call web_task again with brief_id and "
                "answers to the questions, or with on_questions='assume'."
            ]
            if problems:
                parts.append("Answers that didn't fit: " + "; ".join(problems) + ".")
            return " ".join(parts)
        completed = sum(r.done for r in receipts)
        if done and verified:
            parts = [f"Done and verified: all {completed} completed sub-goal(s) were proven by evidence checks."]
        elif done:
            judged = sum(r.done and r.verified_by == "model" for r in receipts)
            parts = [
                f"Done, not fully verified: {judged} of {completed} completed sub-goal(s) were judged by a model."
                if judged else "Done, but not verified."
            ]
        else:
            parts = [f"Not done: {completed} sub-goal(s) completed, {len(receipts) - completed} did not."]
        if done and missing:
            parts.append(
                f"{len(missing)} requested value(s) weren't read or failed their checks: {', '.join(missing)}."
            )
        if blocked:
            total = sum(b.count for b in blocked)
            background = sum(b.count for b in blocked if b.background)
            note = (
                f", {background} of them sent by the page's own scripts, such as analytics and error reporting"
                if background else ""
            )
            parts.append(f"The mandate blocked {total} request(s){note}.")
        if error:
            parts.append("The run stopped with an error.")
        return " ".join(parts)

    @staticmethod
    def _failed(run_id: str, started: float, message: str) -> WebTaskResult:
        return WebTaskResult(
            run_id=run_id, done=False, verified=False, summary=message,
            seconds=round(time.monotonic() - started, 1), error=message,
        )

    def _save(
        self, run_id: str, request: WebTaskRequest, mandate: Mandate, start_url: str,
        agent: Optional[AgentResult], result: WebTaskResult,
    ) -> None:
        """Save a run's record and receipts. A brief that ran nothing has no agent result."""
        self.runs_dir.mkdir(parents=True, exist_ok=True)
        write_receipts(agent.receipts if agent else [], self._receipts_path(run_id))
        record = {
            "run_id": run_id,
            "task": request.task,
            "created_at": time.time(),
            "start_url": start_url,
            "mandate": mandate.model_dump(mode="json"),  # data values are excluded by the model
            "result": result.model_dump(mode="json"),
            "page_text": {
                "answer": agent.answer if agent else None,
                "text_values": {
                    name: value.value for name, value in agent.values.items() if value.type == ValueType.TEXT
                } if agent else {},
            },
            "violations": [violation.model_dump(mode="json") for violation in agent.violations] if agent else [],
            # Redacted in the orchestrator; a run that failed is read from here.
            "actions": agent.actions if agent else [],
        }
        self._record_path(run_id).write_text(json.dumps(record, indent=2))

    def _load(self, run_id: str) -> dict:
        if not _RUN_ID_RE.match(run_id):
            raise ValueError("run_id must be 12 lowercase hex characters")
        path = self._record_path(run_id)
        if not path.exists():
            raise ValueError(f"no run with id {run_id}")
        return json.loads(path.read_text())

    def _record_path(self, run_id: str) -> Path:
        return self.runs_dir / f"{run_id}.json"

    def _receipts_path(self, run_id: str) -> Path:
        return self.runs_dir / f"{run_id}.receipts.jsonl"
