"""Fakes shared by the agent-loop tests."""

from __future__ import annotations


class FakeUI:
    """Stands in for the dashboard: records events and approves confirmations."""

    def __init__(self):
        self.events: list[tuple[str, tuple]] = []

    def __getattr__(self, name):
        async def record(*args, **kwargs):
            self.events.append((name, args))
        return record

    async def wait_if_paused(self):
        pass

    async def request_confirmation(self, action, reason):
        return True


def element_id(observation, label: str) -> int:
    ids = [el.id for el in observation.elements if label in el.label]
    assert ids, f"no {label!r} on {observation.url}; saw {[el.label for el in observation.elements]}"
    return ids[0]
