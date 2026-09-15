"""Planner model — thinks a task through, then decomposes it into ordered sub-goals.

Called before the browser opens to write a brief (core/briefing.py), then to
plan, once more to fix what plan review finds, and again if a sub-goal fails.
Uses the largest available model for best reasoning quality.

The planner is the trusted side of the agent and never sees page content. Its
inputs are the user's task, notes, and answers, the mandate, the clock, trusted
memory, its own brief and sub-goals, counts, the current origin, and values that
passed type checks (core/values.py).
"""

from __future__ import annotations

import json
import re
from typing import Optional

from pydantic import TypeAdapter, ValidationError

from web_lobster.core.schemas import SubGoal, TaskPlan
from web_lobster.core.briefing import TaskBrief, is_click_level, mentions_a_change, parse_brief
from web_lobster.core.values import ExtractedValue, ValueSpec
from web_lobster.verify.evidence import EvidenceCheck
from web_lobster.verify.receipts import page_label
from web_lobster.models.base import ModelBackend
from web_lobster.utils.logging import get_logger

logger = get_logger(__name__)

_EVIDENCE_CHECK = TypeAdapter(EvidenceCheck)

# Sub-goals that only read what a page shows. Values are read off the page a sub-goal
# reaches, so a separate "Extract the version number" step just sends the executor
# clicking around the page that already has the answer.
_READ_ONLY_GOAL = re.compile(
    r"^\s*(extract|read|note|record|report|identify|determine|retrieve|copy|write down)\b",
    re.IGNORECASE,
)
# Keys models use for the goal text when they don't use "goal".
_GOAL_KEYS = ("goal", "description", "sub_goal", "subgoal", "task", "name")


def tidy_sub_goals(sub_goals: list[SubGoal]) -> list[SubGoal]:
    """Undo plan mistakes small models make despite the prompt.

    - A "url" check that spells out a query string is dropped. Search and results
      URLs are where guesses go wrong (sites encode and order parameters their own
      way, or send a search straight to an article), and a check that can never
      pass makes its sub-goal impossible to finish.
    - A sub-goal that only reads information is folded into the one before it,
      which takes its values and its text and value checks. One with a url or
      request check stays.
    - Click-level steps (typing, pressing, clicking a button) are folded into the
      outcome they lead to: "Click the search button" becomes the start of "...,
      then click on the article", proven by that step's evidence. A click step's
      own check fails once the page moves on, which stalled live runs. A step with
      a url, request, or value check, values to read, or wording about a change
      stays, and plan review asks the planner about it instead.
    """
    kept: list[SubGoal] = []
    for goal in sub_goals:
        checks = [c for c in goal.evidence if not (c.type == "url" and "?" in c.pattern)]
        if len(checks) < len(goal.evidence):
            logger.warning("planner_query_url_check_dropped", sub_goal=goal.id)
            goal.evidence = checks
        if kept and _READ_ONLY_GOAL.match(goal.goal) and not any(
            check.type in ("url", "request") for check in goal.evidence
        ):
            previous = kept[-1]
            names = {spec.name for spec in previous.extract}
            previous.extract.extend(spec for spec in goal.extract if spec.name not in names)
            previous.evidence.extend(goal.evidence)
            logger.info("planner_read_only_sub_goal_folded", sub_goal=goal.id)
            continue
        kept.append(goal)
    return _fold_click_level_steps(kept)


def _foldable(goal: SubGoal) -> bool:
    return (
        is_click_level(goal.goal)
        and not goal.extract
        and all(check.type == "text" for check in goal.evidence)
        and not mentions_a_change(goal.goal)
    )


def _lower_first(text: str) -> str:
    return text[:1].lower() + text[1:]


def _fold_click_level_steps(goals: list[SubGoal]) -> list[SubGoal]:
    """Fold click-level steps forward into the next step, or back into the last one."""
    if len(goals) < 2 or not any(_foldable(goal) for goal in goals):
        return goals
    kept: list[SubGoal] = []
    carried: list[str] = []
    for goal in goals:
        if _foldable(goal):
            carried.append(goal.goal)
            continue
        if carried:
            goal.goal = ", then ".join([carried[0], *map(_lower_first, carried[1:]), _lower_first(goal.goal)])
            carried = []
        kept.append(goal)
    if carried:
        if not kept:
            return goals  # every step is click-level, so there's no outcome to fold into
        kept[-1].goal = ", then ".join([kept[-1].goal, *map(_lower_first, carried)])
    logger.info("planner_click_level_steps_folded", folded=len(goals) - len(kept))
    return kept

PLANNER_SYSTEM = """You are a web task planner for an autonomous browser agent.

Given a user's task, decompose it into a sequence of sub-goals that a browser
agent can execute. Each sub-goal should be:

1. An outcome a person would notice ("Open the Mount Everest article"), not a single
   click or keystroke. Most tasks need only 1-3 sub-goals.
2. Verifiable — you can tell if it succeeded by looking at the page
3. Ordered — later goals may depend on earlier ones
4. Specific — avoid vague goals like "find information"

CRITICAL RULES:
- Sub-goals are things to get done in the browser: open a page, run a search, fill in
  and submit a form. The browser agent works out the clicks and typing itself.
- Do NOT create sub-goals for "extracting", "reading", "reporting", "finding" or
  "identifying" specific information. Information extraction is handled automatically
  after navigation completes — you must NOT include it as a sub-goal.
- Your final sub-goal should always be to navigate to or reach the page that contains
  the answer/result — NOT to read from it.
  WRONG final sub-goal: "Extract the tower's height from the article".
  RIGHT final sub-goal: "Open the Eiffel Tower article".
- If you're told where the browser starts, don't plan steps to get there.

CONTEXT: the prompt may start with NOW (resolve dates like "next Friday" against it),
the MANDATE (the sites, data, and changes the user approved; the browser blocks
everything else, so plan inside it, and type granted data as {{name}}), VALUES THE
USER WANTS BACK, USER NOTES, answers GIVEN BY THE USER UP FRONT, your own TASK BRIEF,
USER ANSWERS, and past experience.
The user's answers and notes win over your assumptions.

VALUES: you never see web pages yourself. If a later sub-goal depends on something
a page shows (a price, a date, a count, yes/no), add "extract" to the sub-goal that
reaches that page, for example:
  "extract": [{"name": "cheapest_price", "type": "number", "description": "lowest fare in USD"}]
Types: number, integer, boolean, date (YYYY-MM-DD), choice (add "choices": [...]), text.
Later sub-goals can use a value as {{$name}}. Text values go to the browser agent
but are never shown to you.

EVIDENCE: a sub-goal counts as done only when the browser can prove it. When you can
say what success looks like, add "evidence": checks run in code, all of which must pass:
  {"type": "url", "pattern": "https://www.united.com/confirmation/*"}
  {"type": "request", "method": "POST", "url": "https://www.united.com/*"}  (answered 2xx/3xx)
  {"type": "text", "contains": "Booking confirmed"}
  {"type": "value", "name": "total_price", "op": "<=", "value": 400}
Give a sub-goal evidence when you can say for sure what success looks like. Only use
a "url" check for a URL you're sure of: never guess query strings or search-result
URLs, because a wrong guess makes the sub-goal impossible to finish. When unsure,
check for text the target page must show, or leave evidence out.
Add a "request" check to every sub-goal that submits, books, buys, sends, or saves
something. Without evidence, a model judges the page instead, which is easy to fool.

Think step by step about what a human would do to complete this task in a browser.

Respond ONLY with a JSON array of sub-goals. No other text.

Example, for "Find the cheapest round trip from LAX to JFK, Dec 15-22":
[
  {"id": 1, "goal": "Open Google Flights", "success_criteria": "The flight search form is visible",
   "evidence": [{"type": "url", "pattern": "https://www.google.com/travel/flights*"}]},
  {"id": 2, "goal": "Search for round trips from LAX to JFK, Dec 15 to Dec 22",
   "success_criteria": "Flight results for LAX to JFK on those dates are listed",
   "extract": [{"name": "cheapest_price", "type": "number", "description": "lowest round-trip fare in USD"}]}
]"""

REPLAN_SYSTEM = """You are a web task planner. A previous plan partially failed.
Review what was accomplished and what failed, and create a revised plan for the
remaining work. Only include sub-goals that still need to be done.

You never see web pages. EXTRACTED VALUES were read from pages and checked against
their types; text values are withheld from you, but any value can still be used in
a sub-goal as {{$name}}. Sub-goals may declare "extract" and "evidence" as in the
original plan.

Respond ONLY with a JSON array of sub-goals. No other text."""

BRIEF_SYSTEM = """You are the planner for a browser agent. Before the browser opens, think the
task through and write a brief. You never see web pages, and nothing has loaded yet.

Work out:
- goal: the outcome the user wants, in one sentence.
- thinking: your reasoning in a few sentences: what the task really asks, the likely
  route through the site, and what could go wrong.
- assumptions: what you'll assume where the task is silent and almost anyone would
  guess the same: units, the site's own language, the cheapest option when the user
  asks for cheap.
- questions: facts only the user knows that the task leaves out: dates, where a trip
  starts, how many people, a budget, which account, whether to really submit. Don't
  assume these. Ask, and put your best guess in "default" so the task can still run
  if nobody answers. At most 3; a task that already says everything needs none. Each
  has an "id", the "question", "why" it matters, a "type" (text, number, integer,
  boolean, date, or choice with "choices"), and the "default".
- success: what the finished page or result looks like.
- sites: the sites the task needs, as https:// origins.
- data: the user's personal details it must type, by name (email, full_name, phone).
- changes_something: true if it submits, buys, books, sends, saves, or deletes anything.
- risks: anything costly or hard to undo, like spending money or sending a message.

Use the context you're given: NOW for dates, the MANDATE for what's allowed, USER
NOTES, answers GIVEN BY THE USER UP FRONT, and past experience. Don't ask about
anything they already settle.

Respond ONLY with a JSON object. No other text.

Example, for "Book a table for two at Nopa on Friday":
{"goal": "Reserve a table for 2 at Nopa on the coming Friday",
 "thinking": "Nopa takes bookings on its website. Friday means the coming Friday. No time is given, and the wrong time books the wrong table.",
 "assumptions": ["A party of 2", "The coming Friday"],
 "questions": [
   {"id": "time", "question": "What time should the table be for?", "why": "Bookings are for a specific time", "type": "text", "default": "19:00"},
   {"id": "complete_booking", "question": "Should I complete the booking, or stop at the confirmation step?", "why": "Completing it reserves a real table", "type": "boolean", "default": false}],
 "success": "A confirmation for a table for 2 at Nopa on Friday",
 "sites": ["https://www.nopasf.com"],
 "data": ["full_name", "phone"],
 "changes_something": true,
 "risks": ["Makes a real reservation in the user's name"]}"""

REVISE_SYSTEM = PLANNER_SYSTEM + """

REVISING: you already wrote a plan, and code reviewed it against the user's mandate
and found problems. Return the whole plan again with every problem fixed, keeping
the parts that were fine. Respond ONLY with the JSON array."""


class Planner:
    """Task decomposition using a large language model."""

    def __init__(self, backend: ModelBackend, temperature: float = 0.2):
        self.backend = backend
        self.temperature = temperature

    async def plan(
        self, task: str, context: Optional[str] = None, start_url: Optional[str] = None,
    ) -> TaskPlan:
        """Decompose a task into sub-goals.

        context is a rendered PlanningContext (core/briefing.py): trusted input
        only. start_url comes from the user, not a page. Only its origin and path
        are shown: query strings often carry tokens.
        """
        logger.info("planning", task=task)

        response = await self.backend.generate(
            prompt=self._task_prompt(task, context, start_url),
            system=PLANNER_SYSTEM,
            temperature=self.temperature,
            max_tokens=4096,
        )

        sub_goals = self._parse_subgoals(response)
        plan = TaskPlan(task=task, sub_goals=sub_goals)

        logger.info("plan_created", num_subgoals=len(sub_goals))
        for sg in sub_goals:
            logger.debug("subgoal", id=sg.id, goal=sg.goal)

        return plan

    async def replan(
        self,
        task: str,
        completed_goals: list[SubGoal],
        failed_goal: SubGoal,
        current_origin: str,
        failure: str,
        values: Optional[list[ExtractedValue]] = None,
        context: Optional[str] = None,
    ) -> list[SubGoal]:
        """Create a revised plan after a failure.

        failure must be built from counts and enums, and values must be
        type-checked: nothing here may carry page text.
        """
        completed_summary = "\n".join(
            f"  ✓ {sg.id}. {sg.goal}" for sg in completed_goals
        )
        values_summary = "\n".join(f"  {v.planner_view()}" for v in values or [])
        prompt = f"""USER TASK: {task}

COMPLETED SUB-GOALS:
{completed_summary or "  (none)"}

FAILED SUB-GOAL:
  ✗ {failed_goal.id}. {failed_goal.goal}
  What happened: {failure}

CURRENT SITE: {current_origin}

EXTRACTED VALUES:
{values_summary or "  (none)"}

Create a revised plan to complete the remaining work from the current state."""
        if context:
            prompt = f"{context}\n\n{prompt}"

        logger.info("replanning", failed_goal=failed_goal.goal)
        response = await self.backend.generate(
            prompt=prompt,
            system=REPLAN_SYSTEM,
            temperature=self.temperature,
            max_tokens=4096,
        )

        return self._parse_subgoals(response)

    async def brief(
        self, task: str, context: Optional[str] = None, start_url: Optional[str] = None,
    ) -> Optional[TaskBrief]:
        """Think the task through before the browser opens. None when the reply
        can't be read; the run then goes ahead without a brief."""
        logger.info("briefing", task=task)
        response = await self.backend.generate(
            prompt=self._task_prompt(task, context, start_url),
            system=BRIEF_SYSTEM,
            temperature=self.temperature,
            max_tokens=1500,
        )
        brief = parse_brief(response)
        if brief is None:
            logger.warning("brief_parse_failed")
        return brief

    async def revise(
        self,
        task: str,
        plan: TaskPlan,
        issues: list[str],
        context: Optional[str] = None,
        start_url: Optional[str] = None,
    ) -> list[SubGoal]:
        """Rewrite a plan to fix what plan review found. The issues are built by
        code from the plan and the mandate, never from pages."""
        current = json.dumps(
            [goal.model_dump(mode="json", include={"id", "goal", "success_criteria", "extract", "evidence"})
             for goal in plan.sub_goals],
            indent=1,
        )
        problems = "\n".join(f"- {issue}" for issue in issues)
        prompt = (
            f"{self._task_prompt(task, context, start_url)}\n\n"
            f"YOUR PLAN:\n{current}\n\nPROBLEMS FOUND:\n{problems}\n\nReturn the fixed plan."
        )
        logger.info("revising_plan", issues=len(issues))
        response = await self.backend.generate(
            prompt=prompt, system=REVISE_SYSTEM, temperature=self.temperature, max_tokens=4096,
        )
        return self._parse_subgoals(response)

    @staticmethod
    def _task_prompt(task: str, context: Optional[str], start_url: Optional[str]) -> str:
        prompt = f"USER TASK: {task}"
        if start_url:
            prompt += f"\nTHE BROWSER STARTS AT: {page_label(start_url)}"
        return f"{context}\n\n{prompt}" if context else prompt

    async def extract_learnings(
        self,
        task: str,
        completed_goals: list[SubGoal],
        failed_goals: list[SubGoal],
    ) -> str:
        """After a task run, extract key learnings for future use.

        Only the task and the planner's own sub-goals go in, never the
        page-derived answer, so the learnings are safe to show a future planner.
        """
        goal_summary = ""
        if completed_goals:
            goal_summary += "Completed: " + "; ".join(g.goal for g in completed_goals)
        if failed_goals:
            goal_summary += "\nFailed: " + "; ".join(g.goal for g in failed_goals)

        prompt = f"""Task: {task}
{goal_summary}

In 1-2 sentences, what is the most useful thing to know for attempting this type of task again?
Focus on: what navigation steps worked, what failed, any tricky elements."""

        response = await self.backend.generate(
            prompt=prompt,
            system="Extract concise, actionable learnings from a completed web task. Be specific.",
            temperature=0.1,
            max_tokens=150,
        )
        return response.strip()

    def _parse_subgoals(self, response: str) -> list[SubGoal]:
        """Parse the model's JSON response into SubGoal objects."""
        # Strip markdown code fences if present
        text = response.strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[1] if "\n" in text else text[3:]
        if text.endswith("```"):
            text = text[:-3]
        text = text.strip()

        # Find JSON array in the response
        start = text.find("[")
        end = text.rfind("]") + 1
        if start == -1 or end == 0:
            logger.error("planner_parse_failed", response=text[:200])
            raise ValueError("Planner did not return a valid JSON array")

        try:
            raw = json.loads(text[start:end])
        except json.JSONDecodeError as e:
            logger.error("planner_json_error", error=str(e), response=text[:200])
            raise ValueError(f"Planner returned invalid JSON: {e}")

        sub_goals = []
        for i, item in enumerate(raw):
            # Model may return plain strings instead of dicts
            if isinstance(item, str):
                item = {"goal": item}
            goal = next(
                (item[key].strip() for key in _GOAL_KEYS
                 if isinstance(item.get(key), str) and item[key].strip()),
                f"Step {i + 1}",
            )
            sub_goals.append(SubGoal(
                id=item.get("id", i + 1),
                goal=goal,
                success_criteria=item.get("success_criteria", goal),
                extract=self._parse_value_specs(item.get("extract")),
                evidence=self._parse_evidence(item.get("evidence")),
            ))
        return tidy_sub_goals(sub_goals)

    def _parse_value_specs(self, raw: object) -> list[ValueSpec]:
        """Keep the well-formed value declarations; drop the rest with a warning."""
        if not isinstance(raw, list):
            return []
        specs = []
        for item in raw:
            try:
                specs.append(ValueSpec(**item))
            except (TypeError, ValidationError) as e:
                logger.warning("planner_bad_value_spec", error=str(e).splitlines()[0][:120])
        return specs

    def _parse_evidence(self, raw: object) -> list:
        """Keep the well-formed evidence checks; drop the rest with a warning."""
        if not isinstance(raw, list):
            return []
        checks = []
        for item in raw:
            try:
                checks.append(_EVIDENCE_CHECK.validate_python(item))
            except ValidationError as e:
                logger.warning("planner_bad_evidence", error=str(e).splitlines()[0][:120])
        return checks
