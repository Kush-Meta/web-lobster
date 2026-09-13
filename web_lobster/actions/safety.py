"""Safety rails — prevents the agent from taking destructive actions.

Implements:
- Action gating: blocks or confirms high-risk actions
- URL filtering: allowlist/blocklist enforcement
- Dry-run mode: logs actions without executing
- Ensemble voting: optional multi-model consensus for critical actions
"""

from __future__ import annotations

import fnmatch
from typing import Optional

from web_lobster.core.schemas import Action, ActionType, Observation
from web_lobster.core.config import SafetyConfig
from web_lobster.utils.logging import get_logger

logger = get_logger(__name__)


class SafetyGate:
    """Intercepts actions and applies safety checks before execution."""

    def __init__(self, config: SafetyConfig):
        self.config = config
        self._action_count = 0

    def check(self, action: Action, observation: Observation) -> SafetyVerdict:
        """Evaluate an action against all safety rules.

        Returns a SafetyVerdict indicating whether to proceed, block, or confirm.
        """
        # Dry run — log everything, execute nothing
        if self.config.dry_run:
            logger.info(
                "dry_run_action",
                action=action.action.value,
                element_id=action.element_id,
                text=action.text,
                url=action.url,
            )
            return SafetyVerdict(allowed=False, reason="Dry run mode", needs_confirmation=False)

        # URL blocklist check
        if action.action == ActionType.NAVIGATE and action.url:
            if self._url_blocked(action.url):
                return SafetyVerdict(
                    allowed=False,
                    reason=f"URL blocked by policy: {action.url}",
                )

        # URL allowlist check (if set)
        if action.action == ActionType.NAVIGATE and action.url and self.config.url_allowlist:
            if not self._url_allowed(action.url):
                return SafetyVerdict(
                    allowed=False,
                    reason=f"URL not in allowlist: {action.url}",
                )

        # Action budget check
        self._action_count += 1
        if self._action_count > self.config.max_actions_per_subgoal:
            return SafetyVerdict(
                allowed=False,
                reason=f"Exceeded max actions per sub-goal ({self.config.max_actions_per_subgoal})",
            )

        # High-risk action detection (check element labels for danger words)
        if action.action == ActionType.CLICK and action.element_id:
            element = self._find_element(action.element_id, observation)
            if element and self._is_high_risk(element.label):
                return SafetyVerdict(
                    allowed=True,
                    needs_confirmation=True,
                    reason=f"High-risk action: clicking '{element.label}'",
                )

        # Form submission detection
        if action.action == ActionType.TYPE and action.element_id:
            element = self._find_element(action.element_id, observation)
            if element and element.input_type in ("password", "email"):
                return SafetyVerdict(
                    allowed=True,
                    needs_confirmation=True,
                    reason=f"Entering data in sensitive field: {element.input_type}",
                )

        return SafetyVerdict(allowed=True)

    def reset_counter(self):
        """Reset action counter (call when starting a new sub-goal)."""
        self._action_count = 0

    def _url_blocked(self, url: str) -> bool:
        return any(fnmatch.fnmatch(url, pattern) for pattern in self.config.url_blocklist)

    def _url_allowed(self, url: str) -> bool:
        return any(fnmatch.fnmatch(url, pattern) for pattern in self.config.url_allowlist)

    def _is_high_risk(self, label: str) -> bool:
        label_lower = label.lower()
        return any(word in label_lower for word in self.config.confirm_before)

    def _find_element(self, element_id: int, observation: Observation):
        for el in observation.elements:
            if el.id == element_id:
                return el
        return None


class SafetyVerdict:
    """Result of a safety check."""

    def __init__(
        self,
        allowed: bool = True,
        reason: str = "",
        needs_confirmation: bool = False,
    ):
        self.allowed = allowed
        self.reason = reason
        self.needs_confirmation = needs_confirmation
