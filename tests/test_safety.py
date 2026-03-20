"""Tests for safety gate and utility components."""

import pytest
from web_lobster.core.schemas import (
    Action,
    ActionType,
    BoundingBox,
    Observation,
    PageElement,
)
from web_lobster.core.config import SafetyConfig
from web_lobster.actions.safety import SafetyGate
from web_lobster.utils.retry import StuckDetector


def _make_observation(url="https://example.com", elements=None):
    return Observation(
        url=url,
        title="Test",
        elements=elements or [],
    )


class TestSafetyGate:
    def test_allows_normal_click(self):
        gate = SafetyGate(SafetyConfig())
        obs = _make_observation(elements=[
            PageElement(id=1, role="button", label="Next Page"),
        ])
        action = Action(action=ActionType.CLICK, element_id=1)
        verdict = gate.check(action, obs)
        assert verdict.allowed is True
        assert verdict.needs_confirmation is False

    def test_flags_purchase_button(self):
        gate = SafetyGate(SafetyConfig())
        obs = _make_observation(elements=[
            PageElement(id=1, role="button", label="Purchase Now"),
        ])
        action = Action(action=ActionType.CLICK, element_id=1)
        verdict = gate.check(action, obs)
        assert verdict.allowed is True
        assert verdict.needs_confirmation is True
        assert "Purchase" in verdict.reason

    def test_blocks_blacklisted_url(self):
        gate = SafetyGate(SafetyConfig(url_blocklist=["*/admin/*"]))
        obs = _make_observation()
        action = Action(action=ActionType.NAVIGATE, url="https://site.com/admin/delete")
        verdict = gate.check(action, obs)
        assert verdict.allowed is False

    def test_dry_run_blocks_everything(self):
        gate = SafetyGate(SafetyConfig(dry_run=True))
        obs = _make_observation()
        action = Action(action=ActionType.CLICK, element_id=1)
        verdict = gate.check(action, obs)
        assert verdict.allowed is False
        assert "Dry run" in verdict.reason

    def test_action_budget(self):
        gate = SafetyGate(SafetyConfig(max_actions_per_subgoal=3))
        obs = _make_observation()
        action = Action(action=ActionType.SCROLL, direction="down")
        for _ in range(3):
            verdict = gate.check(action, obs)
            assert verdict.allowed is True
        verdict = gate.check(action, obs)
        assert verdict.allowed is False
        assert "Exceeded" in verdict.reason


class TestStuckDetector:
    def test_not_stuck_initially(self):
        detector = StuckDetector()
        assert detector.is_stuck() is False

    def test_detects_repetitive_state(self):
        detector = StuckDetector(repeat_threshold=3)
        for _ in range(3):
            detector.record("click", 14, "https://example.com")
        assert detector.is_stuck() is True

    def test_not_stuck_with_variety(self):
        detector = StuckDetector()
        detector.record("click", 10, "https://example.com/a")
        detector.record("type", 15, "https://example.com/b")
        detector.record("click", 20, "https://example.com/c")
        assert detector.is_stuck() is False

    def test_reset(self):
        detector = StuckDetector(repeat_threshold=3)
        for _ in range(3):
            detector.record("click", 14, "https://example.com")
        assert detector.is_stuck() is True
        detector.reset()
        assert detector.is_stuck() is False
