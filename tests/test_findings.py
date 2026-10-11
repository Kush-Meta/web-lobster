"""Phase 3: findings, their visibility, and giving up on a proof that can't pass."""

from __future__ import annotations

import pytest

from web_lobster.core.findings import (
    Seen,
    check_unreachable,
    field_needs_choosing,
    for_executor,
    for_planner,
    no_progress,
    page_missing,
    value_not_here,
)


class TestVisibility:
    def test_the_planner_hears_only_what_no_page_authored(self):
        # The isolation boundary, as a filter. A finding is page-derived but
        # never page-content: a URL the plan wrote and a status the network
        # measured may travel; a label the page wrote may not.
        findings = [
            page_missing("https://site.example/speakers/2026", 404),
            check_unreachable('page text contains "Where from?"', "that is a field label"),
            value_not_here("a_speaker", "https://site.example/speakers"),
            no_progress(17),
            field_needs_choosing("Where to?"),          # the page wrote this
        ]
        for_the_planner = for_planner(findings)
        assert len(for_the_planner) == 4
        assert not any("Where to?" in line for line in for_the_planner)
        # The executor may hear all of it; it reads pages anyway.
        assert len(for_executor(findings)) == 5
        assert any("Where to?" in line for line in for_executor(findings))

    def test_a_label_is_executor_only_and_a_url_is_not(self):
        assert field_needs_choosing("Password").seen_by is Seen.EXECUTOR
        assert page_missing("https://site.example/x", 404).seen_by is Seen.PLANNER

    def test_a_finding_says_what_it_knows(self):
        assert "404" in page_missing("https://site.example/x", 404).detail
        assert "nothing on it" in page_missing("https://site.example/x").detail
        assert "can never pass" in check_unreachable("a check", "a reason").detail
