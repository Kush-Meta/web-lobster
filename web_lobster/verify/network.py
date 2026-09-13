"""Network log — what the browser actually sent and what came back.

Evidence checks and receipts read this instead of trusting what a page claims.
Only the method, URL, status, and resource type are kept; bodies never are.
"""

from __future__ import annotations

import time
from typing import Callable, Optional

from pydantic import BaseModel, Field

WRITE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


class NetworkEvent(BaseModel):
    """One request the browser made, and how it ended."""
    seq: int
    method: str
    url: str
    status: Optional[int] = None  # None when the request failed or was blocked
    resource_type: str = ""
    failure: Optional[str] = None
    timestamp: float = Field(default_factory=time.time)

    @property
    def is_write(self) -> bool:
        return self.method in WRITE_METHODS

    def describe(self) -> str:
        outcome = str(self.status) if self.status is not None else f"failed ({self.failure or 'no response'})"
        return f"{self.method} {self.url} -> {outcome}"


class NetworkRecorder:
    """Keeps a rolling log of every request in a browser context."""

    def __init__(self, redact: Optional[Callable[[str], str]] = None, capacity: int = 2000):
        self._redact = redact or (lambda text: text)
        self._capacity = capacity
        self._events: list[NetworkEvent] = []
        self._seq = 0
        # Requests sent but not yet finished, keyed by id; holding the object
        # keeps its id from being reused while it's in flight.
        self._inflight: dict[int, object] = {}
        self._last_activity = time.monotonic()

    def attach(self, context) -> None:
        """Start logging a Playwright BrowserContext (covers every page and popup)."""
        context.on("request", self._on_request)
        context.on("response", self._on_response)
        context.on("requestfinished", self._on_request_done)
        context.on("requestfailed", self._on_request_failed)

    def mark(self) -> int:
        """A position in the log, to pass to since() later."""
        return self._seq

    def since(self, mark: int) -> list[NetworkEvent]:
        return [event for event in self._events if event.seq > mark]

    def settled(self, quiet_seconds: float = 0.5) -> bool:
        """True when nothing is in flight and the network has been quiet for a while."""
        return not self._inflight and time.monotonic() - self._last_activity >= quiet_seconds

    def record(
        self,
        method: str,
        url: str,
        status: Optional[int] = None,
        resource_type: str = "",
        failure: Optional[str] = None,
    ) -> NetworkEvent:
        self._seq += 1
        event = NetworkEvent(
            seq=self._seq,
            method=method.upper(),
            url=self._redact(url),
            status=status,
            resource_type=resource_type,
            failure=self._redact(failure) if failure else failure,
        )
        self._events.append(event)
        if len(self._events) > self._capacity:
            del self._events[: len(self._events) - self._capacity]
        return event

    def _on_request(self, request) -> None:
        self._inflight[id(request)] = request
        self._last_activity = time.monotonic()

    def _on_response(self, response) -> None:
        request = response.request
        self.record(request.method, request.url, response.status, request.resource_type)
        self._last_activity = time.monotonic()

    def _on_request_done(self, request) -> None:
        self._inflight.pop(id(request), None)
        self._last_activity = time.monotonic()

    def _on_request_failed(self, request) -> None:
        self.record(request.method, request.url, None, request.resource_type, request.failure)
        self._on_request_done(request)
