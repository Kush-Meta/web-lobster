"""Browser controller — wraps Playwright for action execution.

Handles the mapping from our Action schema to actual browser operations.
Manages the browser lifecycle (launch, navigate, close).

Clicking strategy (three-layer fallback):
  1. Screen coordinates from the last observation's bbox — survives React/Vue/Angular
     re-renders because the element is still visually at the same position even if
     the DOM was rebuilt.
  2. Stable CSS selector generated at observation time (aria-label / placeholder /
     data-testid / id / name) — survives re-renders because it targets semantic
     attributes, not transient DOM structure.
  3. data-wl-id locator — fast but fragile; only used if no bbox or stable selector.

With a MandateEnforcer attached, every request the browser makes is checked
against the mandate, and {{placeholders}} in typed text resolve to granted
values only on pages the mandate allows.
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import Optional, TYPE_CHECKING

from playwright.async_api import async_playwright, Browser, BrowserContext, Page

from web_lobster.core.schemas import Action, ActionType, Observation, PageElement, ScrollDirection
from web_lobster.core.config import BrowserConfig
from web_lobster.browser.observer import Observer
from web_lobster.mandate.enforcer import MandateEnforcer, MandateViolationError
from web_lobster.mandate.schema import ensure_scheme
from web_lobster.verify.network import NetworkRecorder
from web_lobster.utils.logging import get_logger

logger = get_logger(__name__)

# JS that waits until DOM mutations stop for `quiet_ms` milliseconds (max `timeout_ms`).
# Essential for React/Vue/Angular SPAs that make cascading renders after each click.
_WAIT_FOR_DOM_STABLE_JS = """
(quiet_ms, timeout_ms) => new Promise(resolve => {
    let timer;
    const observer = new MutationObserver(() => {
        clearTimeout(timer);
        timer = setTimeout(() => { observer.disconnect(); resolve(); }, quiet_ms);
    });
    observer.observe(document.body, {childList: true, subtree: true, attributes: true});
    timer = setTimeout(() => { observer.disconnect(); resolve(); }, timeout_ms);
})
"""

# The page's main content, when it marks one, without menus and footers.
_MAIN_TEXT_JS = """
() => {
    const main = document.querySelector('main, [role="main"], article') || document.body;
    return main ? main.innerText : '';
}
"""

# After clicking, check whether a real input/textarea now has focus (handles
# combobox wrappers where the inner input only appears after the click).
_GET_FOCUSED_INPUT_JS = """
() => {
    const el = document.activeElement;
    if (!el) return null;
    const tag = el.tagName.toLowerCase();
    if (tag === 'input' || tag === 'textarea') return true;
    // Look for a focused input *inside* the active element (custom comboboxes)
    const inner = el.querySelector('input:not([type="hidden"]), textarea');
    if (inner) { inner.focus(); return true; }
    return false;
}
"""

# Clears a React-controlled input without breaking its internal state.
# Setting .value directly doesn't work in React because it intercepts the
# property descriptor. Using the native HTMLInputElement.prototype setter
# bypasses React's wrapper, then we dispatch a real 'input' event so React
# treats it as user input and resets its controlled-value state to "".
_CLEAR_FOCUSED_INPUT_JS = """
() => {
    let el = document.activeElement;
    if (!el) return false;
    // Drill into combobox wrapper to find the real text input
    if (el.tagName.toLowerCase() !== 'input' && el.tagName.toLowerCase() !== 'textarea') {
        const inner = el.querySelector('input:not([type="hidden"]), textarea');
        if (inner) { inner.focus(); el = inner; }
    }
    const tag = el.tagName.toLowerCase();
    if (tag !== 'input' && tag !== 'textarea') return false;

    // Use the native setter to bypass React's synthetic value tracking
    const proto = tag === 'textarea'
        ? window.HTMLTextAreaElement.prototype
        : window.HTMLInputElement.prototype;
    const nativeSetter = Object.getOwnPropertyDescriptor(proto, 'value')?.set;
    if (nativeSetter) {
        nativeSetter.call(el, '');
        el.dispatchEvent(new Event('input',  { bubbles: true }));
        el.dispatchEvent(new Event('change', { bubbles: true }));
    } else {
        el.value = '';
    }
    return true;
}
"""


# True when the focused field completes text as you type (aria-autocomplete), where
# key-by-key typing races the browser's own completions.
_OPEN_DIALOGS_JS = """() => [...document.querySelectorAll('[role=dialog], dialog')]
  .filter(el => el.offsetParent).length"""

# A field that opens a dialog keeps its value only when the dialog is confirmed:
# Google Flights' date boxes hold nothing until "Done" is pressed. Search boxes
# open a listbox, never a dialog, so this can't touch them.
_DIALOG_CONFIRM_JS = """() => {
  const dialogs = [...document.querySelectorAll('[role=dialog], dialog')].filter(el => el.offsetParent);
  const dialog = dialogs[dialogs.length - 1];
  if (!dialog) return null;
  const words = ['done', 'ok', 'apply', 'confirm', 'save', 'set', 'select'];
  for (const button of dialog.querySelectorAll('button, [role=button]')) {
    if (!button.offsetParent) continue;
    const label = ((button.innerText || button.getAttribute('aria-label') || '').trim()).toLowerCase();
    if (!words.includes(label)) continue;
    const rect = button.getBoundingClientRect();
    if (rect.width === 0 || rect.height === 0) continue;
    return {x: rect.x + rect.width / 2, y: rect.y + rect.height / 2, text: label};
  }
  return null;
}"""

_FIELD_VALUES_JS = """() => [...document.querySelectorAll('input, textarea, select')]
  .map(el => (el.value || '').trim()).filter(value => value)"""

_INLINE_AUTOCOMPLETE_JS = """() => {
  const el = document.activeElement;
  if (!el) return false;
  const role = (el.getAttribute('role') || '').toLowerCase();
  const auto = (el.getAttribute('aria-autocomplete') || '').toLowerCase();
  return auto === 'inline' || auto === 'both' || (role === 'combobox' && auto !== 'none');
}"""

# Where to click to take a combobox's first suggestion, if it is showing one.
_FIRST_SUGGESTION_JS = """() => {
  const options = [...document.querySelectorAll('[role="option"]')].filter(o => o.offsetParent);
  if (!options.length) return null;
  const rect = options[0].getBoundingClientRect();
  if (rect.width === 0 || rect.height === 0) return null;
  return {x: rect.x + rect.width / 2, y: rect.y + rect.height / 2, text: (options[0].innerText || '').slice(0, 80)};
}"""


class BrowserController:
    """Manages a Playwright browser instance and executes actions."""

    def __init__(self, config: BrowserConfig, enforcer: Optional[MandateEnforcer] = None):
        self.config = config
        self.enforcer = enforcer
        # What the browser actually sent and got back; evidence checks read this.
        self.network = NetworkRecorder(redact=enforcer.redact if enforcer else None)
        self._playwright = None
        self._browser: Optional[Browser] = None
        self._context: Optional[BrowserContext] = None
        self._page: Optional[Page] = None
        self._page_status: Optional[int] = None  # what the current document answered
        self.observer: Optional[Observer] = None
        # Set by the orchestrator before each execute() call so the controller
        # can use stored bbox/stable_selector for coordinate-based clicking.
        self.last_observation: Optional[Observation] = None

    async def start(self, start_url: str = "about:blank") -> None:
        """Launch the browser and navigate to the start URL."""
        if self.enforcer and start_url != "about:blank":
            violation = self.enforcer.check_request(start_url, main_frame_navigation=True)
            if violation:
                raise MandateViolationError(f"Start URL rejected: {violation.detail}")

        self._playwright = await async_playwright().start()
        self._browser = await self._playwright.chromium.launch(
            headless=self.config.headless,
            slow_mo=self.config.slow_mo,
        )
        self._context = await self._browser.new_context(
            viewport={
                "width": self.config.viewport_width,
                "height": self.config.viewport_height,
            },
            # Service-worker requests bypass context routing, so a mandate needs them off.
            service_workers="block" if self.enforcer else "allow",
        )
        self.network.attach(self._context)
        if self.enforcer:
            await self.enforcer.attach(self._context)
        self._page = await self._context.new_page()
        self._page.on("response", self._note_page_status)
        self.observer = Observer(self._page, self.config)

        if start_url != "about:blank":
            await self._page.goto(start_url, wait_until="domcontentloaded")
            logger.info("browser_started", url=start_url)

    async def execute(self, action: Action) -> bool:
        """Execute a single action in the browser.

        Returns True if the action was executed, False if it failed.
        """
        if not self._page:
            raise RuntimeError("Browser not started. Call start() first.")

        try:
            match action.action:
                case ActionType.CLICK:
                    await self._click(action.element_id)
                case ActionType.TRIPLE_CLICK:
                    await self._triple_click(action.element_id)
                case ActionType.TYPE:
                    await self._type(action.element_id, action.text or "")
                case ActionType.SCROLL:
                    await self._scroll(action.direction or ScrollDirection.DOWN)
                case ActionType.NAVIGATE:
                    await self._navigate(action.url or "")
                case ActionType.WAIT:
                    await asyncio.sleep(action.seconds or 2)
                case ActionType.SELECT:
                    await self._select(action.element_id, action.text or "")
                case ActionType.HOVER:
                    await self._hover(action.element_id)
                case ActionType.GO_BACK:
                    await self._page.go_back(wait_until="domcontentloaded")
                case ActionType.DONE:
                    pass
                case ActionType.SCREENSHOT:
                    pass
                case _:
                    logger.warning("unknown_action", action=action.action)
                    return False

            # Wait for React/Angular/Vue to finish re-rendering before the next step.
            # This prevents the observer from capturing a half-rendered DOM.
            if action.action not in (ActionType.WAIT, ActionType.DONE, ActionType.SCREENSHOT):
                await self._wait_for_dom_stable(quiet_ms=200, timeout_ms=1500)

            logger.debug("action_executed", action=action.action.value)
            return True

        except Exception as e:
            # Browser errors quote URLs, which can carry granted values.
            error = self.enforcer.redact(str(e)) if self.enforcer else str(e)
            logger.error(
                "action_failed",
                action=action.action.value,
                element_id=action.element_id,
                error=error,
            )
            return False

    # ── Action implementations ───────────────────────────────────────────────

    async def _click(self, element_id: Optional[int]) -> None:
        if element_id is None:
            raise ValueError("click requires element_id")

        el = self._find_element(element_id)

        # Layer 1: coordinates — immune to React re-renders
        if el and el.bbox:
            cx = el.bbox.x + el.bbox.width / 2
            cy = el.bbox.y + el.bbox.height / 2
            logger.debug("click_by_coords", element_id=element_id, x=cx, y=cy)
            await self._page.mouse.click(cx, cy)
            return

        # Layer 2: stable CSS selector
        if el and el.stable_selector:
            try:
                logger.debug("click_by_stable_selector", sel=el.stable_selector)
                await self._page.locator(el.stable_selector).first.click(
                    timeout=5_000
                )
                return
            except Exception:
                pass

        # Layer 3: data-wl-id (last resort)
        logger.debug("click_by_wl_id", element_id=element_id)
        await self._page.locator(f'[data-wl-id="{element_id}"]').click(
            timeout=self.config.default_timeout * 1000
        )

    async def _type(self, element_id: Optional[int], text: str) -> bool:
        """Enter text. Returns True when a dialog the field opened was confirmed."""
        if element_id is None:
            raise ValueError("type requires element_id")

        dialogs_before = await self._page.evaluate(_OPEN_DIALOGS_JS)

        el = self._find_element(element_id)

        # Click the element to give it focus (using the same three-layer strategy)
        if el and el.bbox:
            cx = el.bbox.x + el.bbox.width / 2
            cy = el.bbox.y + el.bbox.height / 2
            await self._page.mouse.click(cx, cy)
        elif el and el.stable_selector:
            try:
                await self._page.locator(el.stable_selector).first.click(timeout=5_000)
            except Exception:
                await self._page.locator(f'[data-wl-id="{element_id}"]').click(
                    timeout=self.config.default_timeout * 1000
                )
        else:
            await self._page.locator(f'[data-wl-id="{element_id}"]').click(
                timeout=self.config.default_timeout * 1000
            )

        # Wait for React to process the click and potentially mount the
        # inner <input> (common pattern in custom comboboxes like Google Flights)
        await asyncio.sleep(0.4)

        # Ensure a real input/textarea has focus; if a combobox wrapper got the
        # click, JS shifts focus to its inner input automatically.
        await self._page.evaluate(_GET_FOCUSED_INPUT_JS)
        await asyncio.sleep(0.1)

        # Clear pre-filled content via React's native setter (bypasses React's
        # synthetic event wrapper so it sees the field as empty).
        cleared = await self._page.evaluate(_CLEAR_FOCUSED_INPUT_JS)

        if not cleared:
            # Fallback: keyboard select-all + delete for non-React fields
            await self._page.keyboard.press("Control+a")
            await asyncio.sleep(0.05)
            await self._page.keyboard.press("Meta+a")
            await asyncio.sleep(0.05)
            await self._page.keyboard.press("Delete")
            await asyncio.sleep(0.05)

        # Small pause so the page can react to the cleared field before we type
        await asyncio.sleep(0.15)

        # Resolve placeholders against the page as it is now, after the focusing
        # click, which may itself have navigated.
        text = self._resolve_text(text)
        inline = await self._page.evaluate(_INLINE_AUTOCOMPLETE_JS)
        if inline or (self.enforcer and self.enforcer.contains_grant(text)):
            # One input event carrying the whole value. A granted value typed key by
            # key could leak as prefixes too short to recognise, and a field that
            # completes inline (Google Flights' city boxes) interleaves its own
            # completions with the keystrokes: "Tokyo" became "TokTokyoyo" live.
            await self._page.keyboard.insert_text(text)
        else:
            # Type character-by-character so autocomplete listeners fire on each keystroke.
            await self._page.keyboard.type(text, delay=70)

        return await self._confirm_dialog(dialogs_before)

    async def _confirm_dialog(self, dialogs_before: int) -> bool:
        """Press a dialog's confirm button, when typing is what opened it.

        A date box holds nothing until its picker is confirmed: live run 27 typed
        both dates and searched with neither. Only a dialog that wasn't open
        before counts, and only its own Done-shaped button is pressed, so a
        suggestion list — which is where committing on type did damage — is
        never touched.
        """
        await asyncio.sleep(0.4)
        if await self._page.evaluate(_OPEN_DIALOGS_JS) <= dialogs_before:
            return False
        confirm = await self._page.evaluate(_DIALOG_CONFIRM_JS)
        if not confirm:
            return False
        logger.info("dialog_confirmed", button=confirm["text"])
        await self._page.mouse.click(confirm["x"], confirm["y"])
        await asyncio.sleep(0.3)
        return True

    async def _triple_click(self, element_id: Optional[int]) -> None:
        """Triple-click to select all text in a field — ideal for clearing pre-filled inputs."""
        if element_id is None:
            raise ValueError("triple_click requires element_id")
        el = self._find_element(element_id)
        if el and el.bbox:
            cx = el.bbox.x + el.bbox.width / 2
            cy = el.bbox.y + el.bbox.height / 2
            await self._page.mouse.click(cx, cy, click_count=3)
        elif el and el.stable_selector:
            try:
                await self._page.locator(el.stable_selector).first.click(click_count=3, timeout=5_000)
                return
            except Exception:
                pass
            await self._page.locator(f'[data-wl-id="{element_id}"]').click(
                click_count=3, timeout=self.config.default_timeout * 1000
            )
        else:
            await self._page.locator(f'[data-wl-id="{element_id}"]').click(
                click_count=3, timeout=self.config.default_timeout * 1000
            )

    async def _scroll(self, direction: ScrollDirection) -> None:
        delta = -500 if direction == ScrollDirection.UP else 500
        await self._page.mouse.wheel(0, delta)

    async def _navigate(self, url: str) -> None:
        await self._page.goto(
            ensure_scheme(url),
            wait_until="domcontentloaded",
            timeout=self.config.default_timeout * 1000,
        )

    async def _select(self, element_id: Optional[int], text: str) -> None:
        """Choose an option.

        A real <select> uses the browser's own picker. Anything else is a custom
        combobox (Google Flights' city fields are role="combobox" widgets), where
        select_option fails: those are typed into, and the suggestion is taken with
        Enter. A live run spent its whole step budget failing on that.
        """
        if element_id is None:
            raise ValueError("select requires element_id")
        locator = self._page.locator(f'[data-wl-id="{element_id}"]')
        try:
            tag = (await locator.evaluate("el => el.tagName") or "").lower()
        except Exception:
            tag = ""

        if tag == "select":
            await locator.select_option(
                label=self._resolve_text(text), timeout=self.config.default_timeout * 1000
            )
            return

        logger.info("select_on_combobox", element_id=element_id)
        if await self._type(element_id, text):
            return  # a dialog took it
        if not await self._take_suggestion():
            # No list appeared: Enter is what these widgets accept otherwise.
            await self._page.keyboard.press("Enter")

    async def _take_suggestion(self, attempts: int = 6) -> bool:
        """Click the first suggestion a combobox offers. False when none appears.

        Choosing an option is what commits these widgets; typing alone leaves the
        text uncommitted, and the next click throws it away.
        """
        for _ in range(attempts):
            await asyncio.sleep(0.25)
            spot = await self._page.evaluate(_FIRST_SUGGESTION_JS)
            if spot:
                await self._page.mouse.click(spot["x"], spot["y"])
                logger.info("suggestion_taken", text=str(spot.get("text", ""))[:60])
                return True
        return False

    async def _hover(self, element_id: Optional[int]) -> None:
        if element_id is None:
            raise ValueError("hover requires element_id")
        el = self._find_element(element_id)
        if el and el.bbox:
            cx = el.bbox.x + el.bbox.width / 2
            cy = el.bbox.y + el.bbox.height / 2
            await self._page.mouse.move(cx, cy)
            return
        locator = self._page.locator(f'[data-wl-id="{element_id}"]')
        await locator.hover(timeout=self.config.default_timeout * 1000)

    # ── Helpers ──────────────────────────────────────────────────────────────

    def _resolve_text(self, text: str) -> str:
        """Fill in {{placeholders}} with granted values the current page may receive."""
        if not self.enforcer:
            return text
        resolved, violation = self.enforcer.resolve_text(text, self._page.url)
        if violation:
            self.enforcer.record(violation, pending=True)
            raise MandateViolationError(violation.detail)
        return resolved

    def _find_element(self, element_id: int) -> Optional[PageElement]:
        """Look up a PageElement by ID from the most recent observation."""
        if not self.last_observation:
            return None
        for el in self.last_observation.elements:
            if el.id == element_id:
                return el
        return None

    async def _wait_for_dom_stable(self, quiet_ms: int = 200, timeout_ms: int = 1500) -> None:
        """Block until no DOM mutations occur for `quiet_ms` ms (or `timeout_ms` elapses)."""
        try:
            await self._page.evaluate(_WAIT_FOR_DOM_STABLE_JS, quiet_ms, timeout_ms)
        except Exception:
            pass  # Non-fatal — page may be navigating

    async def leave_page(self) -> None:
        """Step back off the current page, falling back to a blank one."""
        with contextlib.suppress(Exception):
            await self._page.go_back(wait_until="domcontentloaded")
        if self.enforcer and self.enforcer.check_page(self.current_url, record=False):
            await self._page.goto("about:blank")

    async def recover_from_blocked_navigation(self, timeout: float = 2.0) -> None:
        """Return to the working page after the mandate blocked a navigation.

        Chromium commits its error page a moment after the request is aborted,
        so wait for it to appear before stepping back; otherwise the next
        observation lands mid-swap.
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while not self.current_url.startswith("chrome-error://"):
            if loop.time() >= deadline:
                return
            await asyncio.sleep(0.05)
        await self.leave_page()

    async def page_text(self, main_only: bool = False, limit: Optional[int] = None) -> str:
        """Visible text of the page (observations truncate it), redacted under a mandate.

        main_only reads the page's main content (<main>, [role=main] or <article>)
        when it has one, skipping navigation menus that can crowd out what matters.
        """
        try:
            if main_only:
                text = await self._page.evaluate(_MAIN_TEXT_JS)
            else:
                text = await self._page.inner_text("body")
        except Exception:
            return ""
        # Redact before truncating, so a cut can't leave part of a granted value behind.
        text = self.enforcer.redact(text) if self.enforcer else text
        return text[:limit] if limit else text

    def _note_page_status(self, response) -> None:
        """Remember what the page itself answered, so a 404 can't pass for a page.

        Only the main frame's own document counts: a sub-resource's 404 says
        nothing about whether the agent is where it thinks it is.
        """
        request = response.request
        if request.resource_type == "document" and request.frame == self._page.main_frame:
            self._page_status = response.status

    async def field_values(self, limit: int = 40) -> list[str]:
        """What the page's own fields hold, read live, for evidence checks.

        A value typed into a field never appears in the page's text, so a check
        about what was entered has to look here. Redacted under a mandate: a
        granted value reaches the page, never a check or a receipt.
        """
        if not self._page:
            return []
        try:
            values = await self._page.evaluate(_FIELD_VALUES_JS)
        except Exception:
            return []
        redact = self.enforcer.redact if self.enforcer else (lambda text: text)
        return [redact(value)[:200] for value in values[:limit]]

    @property
    def current_url(self) -> str:
        return self._page.url if self._page else ""

    @property
    def page_status(self) -> Optional[int]:
        """The HTTP status the current page answered, or None if it isn't known.

        A client-side route change leaves the last document's status in place,
        which is right: the document is still the one the server sent.
        """
        return self._page_status

    async def close(self) -> None:
        if self._context:
            await self._context.close()
        if self._browser:
            await self._browser.close()
        if self._playwright:
            await self._playwright.stop()
        logger.info("browser_closed")
