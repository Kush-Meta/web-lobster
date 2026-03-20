"""Retry and backoff utilities for handling transient failures.

Used by the orchestrator to retry failed actions and model calls
with exponential backoff.
"""

from __future__ import annotations

import asyncio
import functools
from collections import Counter
from typing import Any, Callable, Optional, TypeVar

from web_lobster.utils.logging import get_logger

logger = get_logger(__name__)

T = TypeVar("T")


async def retry_async(
    fn: Callable[..., Any],
    *args,
    max_retries: int = 3,
    base_delay: float = 1.0,
    max_delay: float = 30.0,
    backoff_factor: float = 2.0,
    exceptions: tuple = (Exception,),
    **kwargs,
) -> Any:
    """Retry an async function with exponential backoff.

    Args:
        fn: Async function to retry
        max_retries: Maximum number of attempts
        base_delay: Initial delay in seconds
        max_delay: Maximum delay between retries
        backoff_factor: Multiplier for delay on each retry
        exceptions: Tuple of exception types to catch

    Returns:
        The function's return value on success

    Raises:
        The last exception if all retries fail
    """
    last_exception = None
    delay = base_delay

    for attempt in range(1, max_retries + 1):
        try:
            return await fn(*args, **kwargs)
        except exceptions as e:
            last_exception = e
            if attempt == max_retries:
                logger.error(
                    "retry_exhausted",
                    function=fn.__name__,
                    attempts=attempt,
                    error=str(e),
                )
                raise

            logger.warning(
                "retry_attempt",
                function=fn.__name__,
                attempt=attempt,
                max_retries=max_retries,
                delay=round(delay, 1),
                error=str(e),
            )
            await asyncio.sleep(delay)
            delay = min(delay * backoff_factor, max_delay)

    raise last_exception  # Should never reach here


class StuckDetector:
    """Detects when the agent is stuck in a meaningless loop.

    Tracks (action_type, element_id) pairs. Fires only when the exact
    same (action, element) combination has been repeated — not on
    legitimate repetition like sequential scrolls that load new content.
    """

    def __init__(self, repeat_threshold: int = 3, scroll_threshold: int = 6):
        # Fire if same (action, element) pair seen this many times
        self.repeat_threshold = repeat_threshold
        # Fire if this many consecutive scrolls with no other action type
        self.scroll_threshold = scroll_threshold
        self._pairs: list[tuple[str, Optional[int]]] = []
        self._failed_actions: list[str] = []  # human-readable history for reflection

    def record(self, action_type: str, element_id: Optional[int], url: str) -> None:
        """Record an action taken."""
        self._pairs.append((action_type, element_id))
        label = f"{action_type}(el={element_id})" if element_id else action_type
        self._failed_actions.append(f"{label} @ {url}")
        if len(self._pairs) > 30:
            self._pairs = self._pairs[-30:]
            self._failed_actions = self._failed_actions[-30:]

    def is_stuck(self) -> bool:
        """True if the agent is demonstrably looping."""
        if len(self._pairs) < self.repeat_threshold:
            return False

        # Rule 1: same (action, element) repeated N times in last window
        recent = self._pairs[-self.repeat_threshold * 2:]
        from collections import Counter
        counts = Counter(recent)
        if counts.most_common(1)[0][1] >= self.repeat_threshold:
            return True


        # Rule 2: too many consecutive scrolls with no page change
        if len(self._pairs) >= self.scroll_threshold:
            last_n = self._pairs[-self.scroll_threshold:]
            if all(a == "scroll" for a, _ in last_n):
                return True

        return False

    def get_failed_actions(self) -> list[str]:
        """Return human-readable list of recent actions for reflection."""
        return self._failed_actions[-10:]

    def reset(self) -> None:
        self._pairs.clear()
        self._failed_actions.clear()
