"""What an element affords: the gesture that actually commits a value to it.

Five days of live testing kept producing the same shape of bug one layer at a
time — a city typed into a combobox, a date typed into a picker, a value the
next click threw away. Each was patched in the controller. They are all one
question, and it belongs to the page, not to a 7B model guessing from a label:

    this element takes a *choice*, and typing into it alone does nothing.

So the observer answers it once, here, and the actuator acts on the answer. A
wrong verb from the executor then costs nothing: the gesture is chosen from
what the element is, not from what the model called it.

What can be known by looking at the page is decided here. What can only be
learned by acting — a picker that appears after the first keystroke — stays in
the actuator, which already presses a dialog's Done when typing opened one.
"""

from __future__ import annotations

from enum import Enum
from typing import Optional


class Takes(str, Enum):
    """The gesture that commits something to an element."""

    OPEN = "open"        # a link: follow it
    PRESS = "press"      # a button: press it
    FILL = "fill"        # a plain field: text goes in and stays
    CHOOSE = "choose"    # a list, combobox or picker: a value counts once chosen
    TOGGLE = "toggle"    # a checkbox or radio
    CONFIRM = "confirm"  # a dialog's own Done/OK, which keeps what's inside it


# Roles and tags that only ever want a press.
_PRESSED = frozenset({"button", "submit", "reset", "image", "tab", "menuitem"})
# Dialog buttons that keep what the dialog holds, rather than doing something new.
_CONFIRM_WORDS = frozenset({"done", "ok", "apply", "confirm", "save", "set", "select"})
# Input types that are their own little widget and commit on choosing.
_CHOSEN_INPUTS = frozenset({"date", "datetime-local", "month", "week", "time", "color", "range", "file"})


def takes(
    *,
    role: str,
    tag: Optional[str] = None,
    input_type: Optional[str] = None,
    href: Optional[str] = None,
    label: str = "",
    autocomplete: Optional[str] = None,
    in_dialog: bool = False,
    in_search_form: bool = False,
    opens_dialog: bool = False,
) -> Takes:
    """The gesture this element commits to, from what the page says it is.

    Order matters: a date input is a picker even though it looks like a field,
    and a combobox is a choice even though you type into it.
    """
    role, tag = (role or "").lower(), (tag or "").lower()
    input_type = (input_type or "").lower()
    autocomplete = (autocomplete or "").lower()

    # A dialog's own Done keeps what the dialog holds; it isn't a new action.
    if in_dialog and tag in ("button", "input") and _normalise(label) in _CONFIRM_WORDS:
        return Takes.CONFIRM

    if input_type in ("checkbox", "radio") or role in ("checkbox", "radio", "switch"):
        return Takes.TOGGLE

    # A real <select> is always a choice, whatever else it looks like.
    if tag == "select":
        return Takes.CHOOSE

    # A search box in a form is filled and submitted — that's what it's for —
    # even when the site makes it a combobox with suggestions. This exception
    # is not a nicety: Wikipedia's search box becomes role="combobox" the moment
    # it is clicked and cleared, and treating it as a choice cost the Everest
    # task every run it had been winning (0 of 3 done).
    if in_search_form and (tag in ("input", "textarea") or role in ("textbox", "searchbox", "combobox")):
        return Takes.FILL

    # Typing into one of these leaves a value the next click throws away: the
    # live bug this module exists for. A picker the page only reveals after the
    # first keystroke is the actuator's problem, not something to see from here.
    if role in ("combobox", "listbox") or autocomplete in ("inline", "both", "list") or opens_dialog:
        return Takes.CHOOSE

    if input_type in _CHOSEN_INPUTS:
        return Takes.CHOOSE

    # Before the field rule: <input type="submit"> is a button, not a box, and
    # saucedemo's Login is exactly that.
    if input_type in _PRESSED or role in _PRESSED or tag == "button":
        return Takes.PRESS

    if tag in ("input", "textarea") or role in ("textbox", "searchbox"):
        return Takes.FILL

    if href and (tag == "a" or role == "link"):
        return Takes.OPEN

    return Takes.PRESS  # clickable and nothing more specific: pressing is the safe read


def _normalise(label: str) -> str:
    return " ".join(label.split()).strip().lower()
