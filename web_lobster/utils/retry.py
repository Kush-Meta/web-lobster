"""Retry and backoff utilities for handling transient failures.

Used by the orchestrator to retry failed actions and model calls
with exponential backoff.
"""

from __future__ import annotations

import asyncio
import functools
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
    """Detects when the agent is stuck in a loop.

    Tracks recent observations (URLs + element counts) and flags
    when the state hasn't meaningfully changed across multiple steps.
    """

    def __init__(self, window_size: int = 5, similarity_threshold: float = 0.9):
        self.window_size = window_size
        self.similarity_threshold = similarity_threshold
        self._history: list[str] = []

    def record(self, url: str, num_elements: int, action_type: str) -> None:
        """Record a state snapshot."""
        fingerprint = f"{url}|{num_elements}|{action_type}"
        self._history.append(fingerprint)
        if len(self._history) > self.window_size * 2:
            self._history = self._history[-self.window_size * 2:]

    def is_stuck(self) -> bool:
        """Check if recent states are repetitive."""
        if len(self._history) < self.window_size:
            return False

        recent = self._history[-self.window_size:]
        unique = set(recent)

        # If most recent actions produced the same fingerprint, we're stuck
        return len(unique) <= 2

    def reset(self) -> None:
        self._history.clear()
