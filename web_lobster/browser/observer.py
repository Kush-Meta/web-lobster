"""Observer — extracts the current page state for model consumption.

Implements the hybrid perception approach:
1. Extract the accessibility tree and tag interactive elements with IDs
2. Take a screenshot
3. Optionally annotate the screenshot with element labels (via Annotator)
4. Package everything as an Observation
"""

from __future__ import annotations

import base64
from typing import Optional

from playwright.async_api import Page

from web_lobster.core.schemas import BoundingBox, Observation, PageElement
from web_lobster.core.config import BrowserConfig
from web_lobster.browser.annotator import Annotator
from web_lobster.utils.logging import get_logger

logger = get_logger(__name__)

# JavaScript to inject into the page that tags interactive elements with
# data-wl-id attributes and returns their metadata.
EXTRACT_ELEMENTS_JS = """
() => {
    const interactive = [
        'a[href]', 'button', 'input', 'select', 'textarea',
        '[role="button"]', '[role="link"]', '[role="tab"]',
        '[role="menuitem"]', '[role="checkbox"]', '[role="radio"]',
        '[role="textbox"]', '[role="combobox"]', '[role="searchbox"]',
        '[onclick]', '[tabindex]',
        'summary', 'details',
    ];

    const selector = interactive.join(', ');
    const elements = document.querySelectorAll(selector);
    const results = [];
    let idCounter = 1;

    for (const el of elements) {
        // Skip hidden elements
        const style = window.getComputedStyle(el);
        if (style.display === 'none' || style.visibility === 'hidden' || style.opacity === '0') {
            continue;
        }

        const rect = el.getBoundingClientRect();
        if (rect.width === 0 || rect.height === 0) continue;

        // Check if element is in viewport (with some buffer)
        const inViewport = rect.top < window.innerHeight + 200 &&
                          rect.bottom > -200 &&
                          rect.left < window.innerWidth + 200 &&
                          rect.right > -200;

        const id = idCounter++;
        el.setAttribute('data-wl-id', id);

        // Build a useful label
        let label = el.getAttribute('aria-label') ||
                    el.getAttribute('title') ||
                    el.getAttribute('placeholder') ||
                    el.getAttribute('alt') ||
                    el.innerText?.trim().substring(0, 80) ||
                    el.getAttribute('name') ||
                    el.getAttribute('id') ||
                    el.tagName.toLowerCase();

        results.push({
            id: id,
            role: el.getAttribute('role') || el.tagName.toLowerCase(),
            label: label,
            value: el.value || null,
            tag: el.tagName.toLowerCase(),
            href: el.href || null,
            is_visible: inViewport,
            is_enabled: !el.disabled,
            bbox: {
                x: Math.round(rect.x),
                y: Math.round(rect.y),
                width: Math.round(rect.width),
                height: Math.round(rect.height),
            }
        });
    }

    return results;
}
"""


class Observer:
    """Extracts page state as a structured Observation."""

    def __init__(self, page: Page, config: BrowserConfig):
        self.page = page
        self.config = config
        self.annotator = Annotator()

    async def observe(self, include_screenshot: bool = True) -> Observation:
        """Capture the current page state.

        Args:
            include_screenshot: Whether to take a screenshot (slower but
                needed for vision-based validation).

        Returns:
            An Observation with elements, screenshots, and metadata.
        """
        logger.debug("observing", url=self.page.url)

        # 1. Inject element tagging and extract metadata
        elements_raw = await self.page.evaluate(EXTRACT_ELEMENTS_JS)
        elements = [
            PageElement(
                id=el["id"],
                role=el["role"],
                label=el["label"],
                value=el.get("value"),
                tag=el.get("tag"),
                href=el.get("href"),
                is_visible=el.get("is_visible", True),
                is_enabled=el.get("is_enabled", True),
                bbox=BoundingBox(**el["bbox"]) if el.get("bbox") else None,
            )
            for el in elements_raw
        ]

        # 2. Take screenshot
        screenshot_b64 = None
        annotated_b64 = None
        if include_screenshot:
            screenshot_bytes = await self.page.screenshot(
                type="jpeg",
                quality=self.config.screenshot_quality,
            )
            screenshot_b64 = base64.b64encode(screenshot_bytes).decode("utf-8")

            # 3. Annotate screenshot with element labels
            annotated_bytes = self.annotator.annotate(
                screenshot_bytes,
                [el for el in elements if el.is_visible and el.bbox],
            )
            annotated_b64 = base64.b64encode(annotated_bytes).decode("utf-8")

        # 4. Get simplified accessibility tree
        try:
            a11y_snapshot = await self.page.accessibility.snapshot()
            a11y_tree = self._format_a11y_tree(a11y_snapshot) if a11y_snapshot else None
        except Exception:
            a11y_tree = None

        # 5. Get visible text (truncated)
        try:
            page_text = await self.page.inner_text("body")
            page_text = page_text[:3000] if page_text else None
        except Exception:
            page_text = None

        observation = Observation(
            url=self.page.url,
            title=await self.page.title(),
            elements=elements,
            screenshot_base64=screenshot_b64,
            annotated_screenshot_base64=annotated_b64,
            accessibility_tree=a11y_tree,
            page_text=page_text,
        )

        logger.debug(
            "observation_complete",
            num_elements=len(elements),
            has_screenshot=screenshot_b64 is not None,
        )
        return observation

    def _format_a11y_tree(self, node: dict, depth: int = 0) -> str:
        """Format the Playwright accessibility tree as indented text."""
        indent = "  " * depth
        role = node.get("role", "")
        name = node.get("name", "")
        value = node.get("value", "")

        parts = [f"{indent}{role}"]
        if name:
            parts.append(f'"{name}"')
        if value:
            parts.append(f"[{value}]")

        line = " ".join(parts)
        lines = [line]

        for child in node.get("children", []):
            lines.append(self._format_a11y_tree(child, depth + 1))

        return "\n".join(lines)
