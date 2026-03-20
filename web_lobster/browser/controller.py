"""Browser controller — wraps Playwright for action execution.

Handles the mapping from our Action schema to actual browser operations.
Manages the browser lifecycle (launch, navigate, close).
"""

from __future__ import annotations

import asyncio
from typing import Optional

from playwright.async_api import async_playwright, Browser, BrowserContext, Page

from web_lobster.core.schemas import Action, ActionType, ScrollDirection
from web_lobster.core.config import BrowserConfig
from web_lobster.browser.observer import Observer
from web_lobster.utils.logging import get_logger

logger = get_logger(__name__)


class BrowserController:
    """Manages a Playwright browser instance and executes actions."""

    def __init__(self, config: BrowserConfig):
        self.config = config
        self._playwright = None
        self._browser: Optional[Browser] = None
        self._context: Optional[BrowserContext] = None
        self._page: Optional[Page] = None
        self.observer: Optional[Observer] = None

    async def start(self, start_url: str = "about:blank") -> None:
        """Launch the browser and navigate to the start URL."""
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
        )
        self._page = await self._context.new_page()
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
                    pass  # No browser action needed
                case ActionType.SCREENSHOT:
                    pass  # Handled by observer
                case _:
                    logger.warning("unknown_action", action=action.action)
                    return False

            # Brief settle time for the page to react
            if action.action not in (ActionType.WAIT, ActionType.DONE):
                await asyncio.sleep(0.5)

            logger.debug("action_executed", action=action.action.value)
            return True

        except Exception as e:
            logger.error(
                "action_failed",
                action=action.action.value,
                element_id=action.element_id,
                error=str(e),
            )
            return False

    async def _click(self, element_id: Optional[int]) -> None:
        if element_id is None:
            raise ValueError("click requires element_id")
        locator = self._get_locator(element_id)
        await locator.click(timeout=self.config.default_timeout * 1000)

    async def _type(self, element_id: Optional[int], text: str) -> None:
        if element_id is None:
            raise ValueError("type requires element_id")
        locator = self._get_locator(element_id)
        # Clear existing content first, then type
        await locator.click(timeout=self.config.default_timeout * 1000)
        await locator.fill(text)

    async def _scroll(self, direction: ScrollDirection) -> None:
        delta = -500 if direction == ScrollDirection.UP else 500
        await self._page.mouse.wheel(0, delta)

    async def _navigate(self, url: str) -> None:
        if not url.startswith(("http://", "https://")):
            url = f"https://{url}"
        await self._page.goto(url, wait_until="domcontentloaded",
                              timeout=self.config.default_timeout * 1000)

    async def _select(self, element_id: Optional[int], text: str) -> None:
        if element_id is None:
            raise ValueError("select requires element_id")
        locator = self._get_locator(element_id)
        await locator.select_option(label=text, timeout=self.config.default_timeout * 1000)

    async def _hover(self, element_id: Optional[int]) -> None:
        if element_id is None:
            raise ValueError("hover requires element_id")
        locator = self._get_locator(element_id)
        await locator.hover(timeout=self.config.default_timeout * 1000)

    def _get_locator(self, element_id: int):
        """Get a Playwright locator for an element by our internal ID.

        Elements are tagged with data-wl-id during observation.
        """
        return self._page.locator(f'[data-wl-id="{element_id}"]')

    @property
    def current_url(self) -> str:
        return self._page.url if self._page else ""

    async def close(self) -> None:
        if self._context:
            await self._context.close()
        if self._browser:
            await self._browser.close()
        if self._playwright:
            await self._playwright.stop()
        logger.info("browser_closed")
