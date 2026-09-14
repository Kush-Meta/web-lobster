"""End-to-end mandate enforcement against real Chromium and two local sites.

Site A is what the mandate allows; site B plays the attacker. Each test checks
what B's server actually received, not just what the enforcer logged.
"""

from __future__ import annotations

import asyncio

import pytest

from tests.sites import EMAIL, Site
from web_lobster.browser.controller import BrowserController
from web_lobster.core.config import BrowserConfig
from web_lobster.core.schemas import Action, ActionType
from web_lobster.mandate.enforcer import MandateEnforcer, MandateViolationError, ViolationKind
from web_lobster.mandate.schema import DataGrant, Mandate


def _mandate(site: Site, data_origins: list[str] | None = None) -> Mandate:
    return Mandate(
        task="test",
        origins=[site.origin],
        data=[DataGrant(name="email", value=EMAIL, origins=data_origins or [site.origin])],
    )


async def _start(mandate: Mandate, url: str) -> tuple[BrowserController, MandateEnforcer]:
    enforcer = MandateEnforcer(mandate)
    controller = BrowserController(BrowserConfig(headless=True, default_timeout=5.0), enforcer)
    try:
        await controller.start(url)
    except Exception as e:
        await controller.close()
        if "Executable doesn't exist" in str(e):
            pytest.skip("Playwright Chromium isn't installed")
        raise
    return controller, enforcer


async def _wait_for(predicate, timeout: float = 5.0) -> bool:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.05)
    return predicate()


async def _element_id(controller: BrowserController, label: str) -> int:
    obs = await controller.observer.observe(include_screenshot=False)
    controller.last_observation = obs
    return next(el.id for el in obs.elements if label in el.label)


def _kinds(enforcer: MandateEnforcer) -> set[ViolationKind]:
    return {v.kind for v in enforcer.violations}


async def test_start_url_outside_mandate_is_rejected(sites):
    a, b = sites
    controller = BrowserController(BrowserConfig(headless=True), MandateEnforcer(_mandate(a)))
    with pytest.raises(MandateViolationError):
        await controller.start(f"{b.origin}/")
    await controller.close()
    assert b.requests == []


async def test_navigate_action_outside_mandate_is_blocked(sites):
    a, b = sites
    controller, enforcer = await _start(_mandate(a), f"{a.origin}/")
    try:
        ok = await controller.execute(Action(action=ActionType.NAVIGATE, url=f"{b.origin}/prize"))
        assert ok is False
        assert ViolationKind.NAVIGATION in _kinds(enforcer)
    finally:
        await controller.close()
    assert b.requests == []


async def test_clicked_link_outside_mandate_is_blocked(sites):
    a, b = sites
    controller, enforcer = await _start(_mandate(a), f"{a.origin}/")
    try:
        link = await _element_id(controller, "Claim prize")
        await controller.execute(Action(action=ActionType.CLICK, element_id=link))
        assert await _wait_for(lambda: ViolationKind.NAVIGATION in _kinds(enforcer))
        drained = enforcer.drain()
        assert [v.kind for v in drained] == [ViolationKind.NAVIGATION]
        assert drained[0].main_frame is True
    finally:
        await controller.close()
    assert b.requests == []


async def test_form_post_to_other_origin_is_blocked(sites):
    a, b = sites
    controller, enforcer = await _start(_mandate(a), f"{a.origin}/form")
    try:
        button = await _element_id(controller, "Send")
        await controller.execute(Action(action=ActionType.CLICK, element_id=button))
        assert await _wait_for(lambda: enforcer.violations)
    finally:
        await controller.close()
    assert b.requests == []


async def test_typed_grant_is_filled_in_but_cannot_leak(sites):
    a, b = sites
    controller, enforcer = await _start(_mandate(a), f"{a.origin}/leaky")
    try:
        field = await _element_id(controller, "Email")
        ok = await controller.execute(Action(action=ActionType.TYPE, element_id=field, text="{{email}}"))
        assert ok is True
        assert await controller._page.input_value("input") == EMAIL
        assert await _wait_for(lambda: ViolationKind.DATA_LEAK in _kinds(enforcer))
        await asyncio.sleep(0.3)  # give any straggling request time to arrive
    finally:
        await controller.close()
    assert not b.received_email()
    # The script's cross-origin POSTs never reach B. Its empty-value image GETs
    # do: data-free cross-origin GETs are allowed so pages keep working.
    assert not any(method == "POST" for method, _, _ in b.requests)
    for v in enforcer.violations:
        assert EMAIL not in v.url and EMAIL not in v.detail


async def test_placeholder_not_filled_on_page_outside_grant(sites):
    a, b = sites
    mandate = _mandate(a, data_origins=["https://www.united.com"])
    controller, enforcer = await _start(mandate, f"{a.origin}/")
    try:
        field = await _element_id(controller, "Email")
        ok = await controller.execute(Action(action=ActionType.TYPE, element_id=field, text="{{email}}"))
        assert ok is False
        assert await controller._page.input_value("input") == ""
        assert [v.kind for v in enforcer.drain()] == [ViolationKind.DATA_ENTRY]
    finally:
        await controller.close()


async def test_redirect_out_of_mandate_is_blocked(sites):
    a, b = sites
    controller, enforcer = await _start(_mandate(a), f"{a.origin}/")
    try:
        await controller.execute(Action(action=ActionType.NAVIGATE, url=f"{a.origin}/redirect-out"))
        assert await _wait_for(lambda: any("redirect" in v.detail for v in enforcer.violations))
    finally:
        await controller.close()
    assert b.requests == []


async def test_allowed_redirect_lands(sites):
    a, b = sites
    controller, _ = await _start(_mandate(a), f"{a.origin}/form")
    try:
        await controller.execute(Action(action=ActionType.NAVIGATE, url=f"{a.origin}/redirect-home"))
        await controller._page.wait_for_url(f"{a.origin}/", timeout=5000)
    finally:
        await controller.close()


async def test_every_redirect_hop_is_checked(sites):
    a, b = sites
    controller, enforcer = await _start(_mandate(a), f"{a.origin}/")
    try:
        await controller.execute(Action(action=ActionType.NAVIGATE, url=f"{a.origin}/chain"))
        assert await _wait_for(lambda: any("redirect" in v.detail for v in enforcer.violations))
    finally:
        await controller.close()
    assert ("GET", "/hop", b"") in a.requests
    assert b.requests == []


async def test_websocket_to_other_origin_is_closed(sites):
    a, b = sites
    controller, enforcer = await _start(_mandate(a), f"{a.origin}/socket")
    try:
        assert await _wait_for(lambda: ViolationKind.WEBSOCKET in _kinds(enforcer))
    finally:
        await controller.close()
    assert b.requests == []


async def test_page_text_can_read_just_the_main_content(sites):
    a, _ = sites
    controller, _ = await _start(_mandate(a), f"{a.origin}/article")
    try:
        main = await controller.page_text(main_only=True)
        full = await controller.page_text()
        limited = await controller.page_text(limit=20)
    finally:
        await controller.close()
    assert main.strip() == "The tower is 330 metres tall."
    assert full.startswith("Menu item") and "330 metres" in full
    assert len(limited) == 20


async def test_page_beacons_are_blocked_but_not_reported_to_the_executor(sites):
    a, _ = sites
    mandate = Mandate(task="test", origins=[a.origin], writes=[])
    controller, enforcer = await _start(mandate, f"{a.origin}/beacon")
    try:
        assert await _wait_for(lambda: ViolationKind.UNAPPROVED_WRITE in _kinds(enforcer))
        assert enforcer.violations[0].resource_type == "ping"
        assert enforcer.drain() == []
    finally:
        await controller.close()
    assert not a.saw("POST", "/api/analytics")


@pytest.mark.parametrize("listed", [False, True])
async def test_same_origin_write_needs_a_listed_rule(sites, listed):
    a, _ = sites
    rule = f"POST {a.origin}/api/book" if listed else f"POST {a.origin}/api/other"
    mandate = Mandate(task="test", origins=[a.origin], writes=[rule])
    controller, enforcer = await _start(mandate, f"{a.origin}/checkout")
    try:
        button = await _element_id(controller, "Book now")
        await controller.execute(Action(action=ActionType.CLICK, element_id=button))
        if listed:
            await controller._page.wait_for_url(f"{a.origin}/confirmation/*", timeout=5000)
        else:
            assert await _wait_for(lambda: ViolationKind.UNAPPROVED_WRITE in _kinds(enforcer))
    finally:
        await controller.close()
    assert (("POST", "/api/book", b"") in a.requests) is listed
