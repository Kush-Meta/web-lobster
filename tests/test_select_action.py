"""Choosing an option: real dropdowns and custom comboboxes.

Google Flights' city fields are role="combobox" widgets, not <select> elements,
and Playwright's select_option fails on them. A live run spent its whole step
budget there, so select falls back to typing and taking the suggestion. The
guards that cover typing have to cover it too.
"""

from __future__ import annotations

import pytest

from tests.sites import EMAIL
from web_lobster.actions.safety import SafetyGate
from web_lobster.browser.controller import BrowserController
from web_lobster.core.config import BrowserConfig, SafetyConfig, WebLobsterConfig
from web_lobster.core.orchestrator import Orchestrator
from web_lobster.core.schemas import Action, ActionType, Observation, PageElement
from web_lobster.mandate.enforcer import MandateEnforcer
from web_lobster.mandate.schema import DataGrant, Mandate


def _mandate(site) -> Mandate:
    return Mandate(
        task="Pick a city", origins=[site.origin],
        data=[DataGrant(name="email", value=EMAIL, origins=[site.origin])],
    )


async def _open(site, path: str) -> BrowserController:
    controller = BrowserController(
        BrowserConfig(headless=True, default_timeout=5.0), MandateEnforcer(_mandate(site))
    )
    try:
        await controller.start(f"{site.origin}{path}")
    except Exception as e:
        if "Executable doesn't exist" in str(e):
            pytest.skip("Playwright Chromium isn't installed")
        raise
    return controller


async def _element_id(controller: BrowserController, label: str) -> int:
    observation = await controller.observer.observe(include_screenshot=False, extract_dom=True)
    ids = [el.id for el in observation.elements if label in el.label]
    assert ids, f"no {label!r} on the page; saw {[el.label for el in observation.elements]}"
    return ids[0]


async def _choose(site, path: str) -> str:
    controller = await _open(site, path)
    try:
        chosen = await controller.execute(Action(
            action=ActionType.SELECT, element_id=await _element_id(controller, "City"), text="Tokyo",
        ))
        assert chosen is True
        return await controller.page_text()
    finally:
        await controller.close()


async def test_a_real_dropdown_uses_the_browsers_picker(sites):
    a, _ = sites
    assert "Chosen: Tokyo" in await _choose(a, "/dropdown")


async def test_a_combobox_is_typed_into_and_the_suggestion_taken(sites):
    a, _ = sites
    assert "Chosen: Tokyo" in await _choose(a, "/combobox")


def test_the_sensitive_field_check_covers_select():
    observation = Observation(
        url="https://shop.example/checkout", title="Checkout",
        elements=[PageElement(id=1, role="combobox", label="Email", input_type="email")],
    )
    for action_type in (ActionType.TYPE, ActionType.SELECT):
        gate = SafetyGate(SafetyConfig())
        verdict = gate.check(Action(action=action_type, element_id=1, text="someone@example.com"), observation)
        assert verdict.allowed and verdict.needs_confirmation, action_type


def test_granted_data_entered_through_select_is_approved_by_the_mandate(sites):
    a, _ = sites
    orchestrator = Orchestrator(WebLobsterConfig(), mandate=_mandate(a))
    for action_type in (ActionType.TYPE, ActionType.SELECT):
        action = Action(action=action_type, element_id=1, text="{{email}}")
        assert orchestrator.steps._mandate_approves(action, f"{a.origin}/form"), action_type
    assert not orchestrator.steps._mandate_approves(
        Action(action=ActionType.CLICK, element_id=1), f"{a.origin}/form"
    )


async def _typed_events(site, path: str, label: str, text: str) -> list[str]:
    controller = await _open(site, path)
    try:
        typed = await controller.execute(Action(
            action=ActionType.TYPE, element_id=await _element_id(controller, label), text=text,
        ))
        assert typed is True
        events = await controller._page.evaluate("window.__events")
        return [value for value in events if value]  # the clearing step fires an empty one
    finally:
        await controller.close()


async def test_an_autocomplete_field_gets_the_text_in_one_go(sites):
    a, _ = sites
    # Key-by-key typing races the field's own inline completion: live, "Tokyo"
    # became "TokTokyoyo" on Google Flights.
    assert await _typed_events(a, "/combobox", "City", "Tokyo") == ["Tokyo"]  # one event, not five


async def test_a_plain_field_still_gets_each_keystroke(sites):
    a, _ = sites
    events = await _typed_events(a, "/plainfield", "Notes", "Tokyo")
    assert events == ["T", "To", "Tok", "Toky", "Tokyo"]


async def test_elements_under_an_overlay_are_not_offered(sites):
    a, _ = sites
    controller = await _open(a, "/covered")
    try:
        observation = await controller.observer.observe(include_screenshot=False, extract_dom=True)
    finally:
        await controller.close()
    assert [el.label for el in observation.elements] == ["City"]  # only the one on top


async def test_a_search_box_in_a_form_is_only_filled(sites):
    a, _ = sites
    # The guarantee that replaced "type never commits". Wikipedia's search box
    # is a combobox with suggestions, and taking one of them turned every
    # search into a jump to the site's first guess: 0 of 3 Everest runs done.
    # Because it sits in a search form, the page says it takes a fill.
    controller = await _open(a, "/searchbox")
    try:
        [box] = [el for el in (await controller.observer.observe(
            include_screenshot=False, extract_dom=True)).elements if el.input_type]
        assert box.takes == "fill"
        await controller.execute(Action(action=ActionType.TYPE, element_id=box.id, text="Mount Everest"))
        assert await controller._page.input_value("input") == "Mount Everest"
        assert "Chosen" not in await controller.page_text()   # no suggestion taken
    finally:
        await controller.close()

async def test_typing_confirms_the_dialog_it_opened(sites):
    a, _ = sites
    # A date box holds nothing until its picker is confirmed: live run 27 typed
    # both dates and searched with neither.
    controller = await _open(a, "/datebox")
    try:
        await controller.execute(Action(
            action=ActionType.TYPE,
            element_id=await _element_id(controller, "Departure"), text="2026-10-15",
        ))
        held = await controller._page.input_value("input")
    finally:
        await controller.close()
    assert held == "Thu Oct 15"  # the site's own wording, kept because Done was pressed


async def test_the_affordance_decides_the_gesture_not_the_verb(sites):
    a, _ = sites
    # The point of the whole affordance layer: "type" into a combobox still
    # commits, because the page said the element takes a choice. Making type
    # commit *everything* was tried and reverted (it cost Everest 3 of 3);
    # scoping it by what the element is costs nothing.
    controller = await _open(a, "/combobox")
    try:
        [city] = [el for el in (await controller.observer.observe(
            include_screenshot=False, extract_dom=True)).elements if "City" in el.label]
        assert city.takes == "choose"          # decided by the page
        await controller.execute(Action(action=ActionType.TYPE, element_id=city.id, text="Tokyo"))
        assert "Chosen: Tokyo" in await controller.page_text()
    finally:
        await controller.close()


async def test_a_plain_field_is_still_only_filled(sites):
    a, _ = sites
    # The other half of the same rule: nothing commits on a field that keeps
    # what it is given, so a search box behaves exactly as it always did.
    controller = await _open(a, "/plainfield")
    try:
        [notes] = [el for el in (await controller.observer.observe(
            include_screenshot=False, extract_dom=True)).elements if "Notes" in el.label]
        assert notes.takes == "fill"
        await controller.execute(Action(action=ActionType.CHOOSE, element_id=notes.id, text="Tokyo"))
        assert await controller._page.input_value("input") == "Tokyo"
        assert "Chosen" not in await controller.page_text()
    finally:
        await controller.close()
