"""Thinking before acting: briefs, questions, planning context, and plan review.

Everything here is pure code on the planner's trusted side, so it's tested
without a browser or a model.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

from web_lobster.core.briefing import (
    MANDATE_QUESTION_ID,
    PlanningContext,
    Question,
    TaskBrief,
    mandate_gaps,
    mandate_lines,
    mandate_question,
    parse_brief,
    resolve_answers,
    review_plan,
)
from web_lobster.core.schemas import SubGoal, TaskPlan
from web_lobster.core.values import ValueSpec, ValueType
from web_lobster.mandate.schema import DataGrant, Mandate
from web_lobster.verify.evidence import RequestCheck, UrlCheck

SHOP = "https://www.united.com"
GAP = "it expects to use www.google.com, which the mandate doesn't allow"


def _mandate(**overrides) -> Mandate:
    fields = dict(
        task="t", origins=[SHOP], writes=[],
        data=[DataGrant(name="email", value="me@example.com", origins=[SHOP])],
    )
    fields.update(overrides)
    return Mandate(**fields)


class TestParseBrief:
    def test_reads_a_brief_wrapped_in_prose_and_fences(self):
        reply = "Here you go:\n```json\n" + json.dumps({
            "goal": "Find flights", "thinking": "Dates matter.", "assumptions": ["Economy", 3],
            "questions": [
                {"id": "Travel Dates!", "question": "Which dates?", "type": "date"},
                {"question": "How many adults?", "type": "int", "default": "2"},
                {"id": "cabin", "question": "Which cabin?", "type": "choice"},
            ],
            "sites": "https://www.google.com", "changes_something": "yes",
        }) + "\n```"
        brief = parse_brief(reply)
        assert brief.goal == "Find flights" and brief.thinking == "Dates matter."
        assert brief.assumptions == ["Economy"]
        assert [(q.id, q.type) for q in brief.questions] == [
            ("travel_dates", ValueType.DATE), ("q2", ValueType.INTEGER),
            ("cabin", ValueType.TEXT),  # a choice with no choices is asked as text
        ]
        assert brief.questions[1].default == 2
        assert brief.sites == ["https://www.google.com"]
        assert brief.changes_something is False  # only a real true counts

    def test_at_most_three_questions_and_never_the_mandate_question(self):
        questions = [{"id": MANDATE_QUESTION_ID, "question": "Run anyway?"}]
        questions += [{"question": f"Question {i}?"} for i in range(5)]
        brief = parse_brief(json.dumps({"goal": "g", "questions": questions}))
        assert len(brief.questions) == 3
        assert MANDATE_QUESTION_ID not in [q.id for q in brief.questions]

    def test_no_json_object_means_no_brief(self):
        assert parse_brief("I can't do that.") is None
        assert parse_brief("[1, 2]") is None
        assert parse_brief("{not json}") is None


class TestQuestions:
    def test_answers_are_checked_against_their_type_and_never_echoed(self):
        adults = Question(id="adults", question="How many?", type=ValueType.INTEGER, default=2)
        when = Question(id="date", question="When?", type=ValueType.DATE)
        cabin = Question(id="cabin", question="Cabin?", type=ValueType.CHOICE, choices=["Economy", "Business"])
        resolution = resolve_answers(
            [adults, when, cabin], {"adults": "three", "cabin": "business"}, use_defaults=True,
        )
        assert resolution.answers == {"adults": 2, "cabin": "Business"}
        assert resolution.defaulted == ["adults"]
        assert [q.id for q in resolution.unanswered] == ["date"]
        assert resolution.problems == ["adults: expected a whole number"]

    def test_without_defaults_open_questions_stay_open(self):
        adults = Question(id="adults", question="How many?", type=ValueType.INTEGER, default=2)
        resolution = resolve_answers([adults], None, use_defaults=False)
        assert resolution.answers == {} and [q.id for q in resolution.unanswered] == ["adults"]

    def test_a_default_that_does_not_fit_is_dropped(self):
        assert Question(id="d", question="When?", type=ValueType.DATE, default="next week").default is None


class TestMandateGaps:
    def test_flags_sites_writes_and_data_the_mandate_lacks(self):
        brief = TaskBrief(
            sites=["https://www.google.com", "united.com", "United"],
            data=["email_address", "phone"], changes_something=True,
        )
        assert mandate_gaps(brief, _mandate()) == [
            GAP,
            "it expects to submit or change something, but the mandate is read-only",
            "it expects to type the user's phone, which the mandate doesn't grant",
        ]

    def test_allowed_writes_mean_no_write_gap(self):
        brief = TaskBrief(changes_something=True)
        assert mandate_gaps(brief, _mandate(writes=None)) == []
        assert mandate_gaps(brief, _mandate(writes=[f"POST {SHOP}/api/book"])) == []

    def test_gaps_become_one_yes_no_question_without_a_default(self):
        question = mandate_question([GAP])
        assert question.id == MANDATE_QUESTION_ID and question.type == ValueType.BOOLEAN
        assert question.default is None and GAP in question.question
        assert mandate_question([]) is None


class TestPlanningContext:
    def _context(self, **overrides) -> PlanningContext:
        fields = dict(
            task="Book Nopa",
            now=datetime(2026, 9, 14, 20, 30, tzinfo=timezone.utc),
            mandate=mandate_lines(_mandate()),
            requested_values=[ValueSpec(name="total", type=ValueType.NUMBER, description="price")],
            notes="I'm vegetarian.",
            memory="RELEVANT PAST EXPERIENCE (use as guidance, adapt as needed):",
            brief=TaskBrief(goal="Book a table", assumptions=["A party of 2"],
                            risks=["Makes a real booking"], success="A confirmation shows"),
            questions=[Question(id="time", question="What time?", default="19:00"),
                       Question(id="seat", question="Inside or out?")],
            answers={"time": "20:00"},
            gaps=[GAP],
        )
        fields.update(overrides)
        return PlanningContext(**fields)

    def test_render_includes_every_trusted_section(self):
        text = self._context().render()
        assert text.startswith("NOW: Monday 14 September 2026, 20:30 UTC")
        for part in [
            "Sites: https://www.united.com",
            "{{email}} (only on https://www.united.com)",
            "Changes allowed: none. The task is read-only",
            "- total (number): price",
            "USER NOTES", "I'm vegetarian.",
            "Goal: Book a table", "Done when: A confirmation shows", "- A party of 2", "- Makes a real booking",
            "What time? → 20:00",
            "NOT ANSWERED", "- Inside or out?",
            "BEYOND THE MANDATE", GAP,
            "RELEVANT PAST EXPERIENCE",
        ]:
            assert part in text, part

    def test_replans_leave_memory_out(self):
        assert "RELEVANT PAST EXPERIENCE" not in self._context().render(include_memory=False)

    def test_executor_briefing_carries_goal_answers_assumptions_and_notes(self):
        text = self._context().for_executor()
        assert "Overall goal: Book a table" in text
        assert 'The user answered "What time?": 20:00' in text
        assert "Assume: A party of 2" in text
        assert "User notes: I'm vegetarian." in text
        assert self._context(brief=None, answers={}, notes=None).for_executor() is None


    def test_answers_given_up_front_are_shown_even_without_a_question(self):
        context = self._context(given={"dates": "2026-12-15", "budget": 900})
        text = context.render()
        assert "GIVEN BY THE USER UP FRONT" in text
        assert "- dates: 2026-12-15" in text and "- budget: 900" in text
        assert "The user said dates: 2026-12-15" in context.for_executor()


class TestPlanReview:
    def test_finds_what_code_can_see(self):
        plan = TaskPlan(task="t", sub_goals=[
            SubGoal(id=1, goal="Open the order history", success_criteria="Orders are listed",
                    evidence=[UrlCheck(pattern="https://evil.example/*")]),
            SubGoal(id=2, goal="Rebook the trip for {{$new_date}}", success_criteria="Rebooked"),
            SubGoal(id=3, goal="Fill in the form with {{phone}}", success_criteria="Phone entered"),
        ])
        issues = review_plan(plan, _mandate(writes=None), max_sub_goals=2)
        assert len(issues) == 5
        assert "3 sub-goals" in issues[0]
        assert "url check on a site the mandate doesn't allow" in issues[1]
        assert "{{$new_date}}" in issues[2]
        assert 'give it a "request" evidence check' in issues[3]
        assert "grants no data called phone" in issues[4]

    def test_a_paragraph_of_instructions_is_not_a_goal(self):
        # A 7B planner under pressure starts writing the executor a letter.
        # Live, a failed run replanned into eight of these and finished none.
        plan = TaskPlan(task="t", sub_goals=[
            SubGoal(id=1, goal=(
                "Navigate to the Jaipur Literature Festival website and locate the section "
                "for the 2027 festival. Click on the link to go to the 2027 festival page."
            ), success_criteria="On the page"),
            SubGoal(id=2, goal="Open the speakers page", success_criteria="Speakers are listed"),
        ])
        issues = review_plan(plan, None, max_sub_goals=6)
        assert len(issues) == 1
        assert issues[0].startswith("Sub-goal 1") and "paragraph of instructions" in issues[0]

    def test_click_level_steps_are_flagged_for_merging(self):
        plan = TaskPlan(task="t", sub_goals=[
            SubGoal(id=1, goal="Type 'Mount Everest' into the search box", success_criteria="Typed"),
            SubGoal(id=2, goal="Click the search button", success_criteria="Results are shown"),
            SubGoal(id=3, goal="Click on the 'Mount Everest' article", success_criteria="The article is open"),
        ])
        issues = review_plan(plan, None, max_sub_goals=6)
        assert len(issues) == 2
        assert issues[0].startswith("Sub-goal 1") and issues[1].startswith("Sub-goal 2")
        assert all("single click or keystroke" in issue for issue in issues)
        # A one-step plan has nothing to merge into.
        lone = TaskPlan(task="t", sub_goals=[SubGoal(id=1, goal="Type hello into the box", success_criteria="Typed")])
        assert review_plan(lone, None, max_sub_goals=6) == []

    def test_read_only_mandate_calls_out_steps_that_change_things(self):
        plan = TaskPlan(task="t", sub_goals=[SubGoal(
            id=1, goal="Submit the form", success_criteria="Sent",
            evidence=[RequestCheck(method="POST", url=f"{SHOP}/api/send")],
        )])
        [issue] = review_plan(plan, _mandate(), max_sub_goals=6)
        assert "the mandate is read-only" in issue

    def test_a_good_plan_has_no_issues(self):
        plan = TaskPlan(task="t", sub_goals=[
            SubGoal(id=1, goal="Open fares", success_criteria="Fares are listed",
                    extract=[ValueSpec(name="price", type=ValueType.NUMBER)]),
            SubGoal(id=2, goal="Book the cheapest fare under {{$price}} for {{email}}", success_criteria="Booked",
                    evidence=[RequestCheck(method="POST", url=f"{SHOP}/api/book")]),
        ])
        assert review_plan(plan, _mandate(writes=None), max_sub_goals=6) == []
