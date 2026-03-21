"""Observer — extracts the current page state for model consumption.

Implements the hybrid perception approach:
1. Extract the accessibility tree and tag interactive elements with IDs
2. Take a screenshot
3. Optionally annotate the screenshot with element labels (via Annotator)
4. Package everything as an Observation
"""

from __future__ import annotations

import asyncio
import base64
from typing import Optional

from playwright.async_api import Page

from web_lobster.core.schemas import BoundingBox, Observation, PageElement
from web_lobster.core.config import BrowserConfig
from web_lobster.browser.annotator import Annotator
from web_lobster.utils.logging import get_logger

# Login page heuristics applied to URL + title (lowercased)
_LOGIN_URL_TOKENS = {"login", "signin", "sign-in", "authenticate", "auth/", "/sso", "account/login"}
_LOGIN_TITLE_TOKENS = {"log in", "sign in", "login", "signin", "create account", "register"}

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

# Extracts a rich structured DOM summary: forms, headings, links, buttons.
# Used in DOM mode to give the model a text-based view of the page instead of
# (or in addition to) a screenshot.
EXTRACT_DOM_JS = """
() => {
    const hasPasswordField = !!document.querySelector('input[type="password"]');

    // Forms with labelled fields
    const forms = [];
    document.querySelectorAll('form').forEach((form, fi) => {
        const fields = [];
        form.querySelectorAll('input, select, textarea, button[type="submit"], button[type="button"]').forEach(el => {
            const id = el.getAttribute('data-wl-id');
            if (!id) return;
            const type = el.type || el.tagName.toLowerCase();
            if (type === 'hidden') return;
            const labelEl = el.id ? form.querySelector(`label[for="${el.id}"]`) : null;
            const label = el.getAttribute('aria-label') ||
                          (labelEl ? labelEl.innerText.trim() : '') ||
                          el.placeholder || el.name || type;
            const val = el.value ? ` (current: "${el.value.substring(0,60)}")` : '';
            fields.push(`  [${id}] ${type}: "${label}"${val}`);
        });
        if (!fields.length) return;
        const heading = form.querySelector('h1,h2,h3,legend')?.innerText?.trim() ||
                        form.getAttribute('aria-label') || `Form ${fi + 1}`;
        forms.push(`${heading}:\\n${fields.join('\\n')}`);
    });

    // Headings
    const headings = [];
    document.querySelectorAll('h1,h2,h3').forEach(h => {
        const text = h.innerText?.trim();
        if (text && text.length < 200) headings.push(`${h.tagName}: ${text}`);
    });

    // In-viewport links (capped)
    const links = [];
    document.querySelectorAll('a[data-wl-id]').forEach(a => {
        const rect = a.getBoundingClientRect();
        if (rect.top > window.innerHeight + 100) return;
        const id = a.getAttribute('data-wl-id');
        const text = a.innerText?.trim().replace(/\\s+/g,' ').substring(0, 80);
        if (text) links.push(`[${id}] ${text}`);
    });

    // In-viewport buttons
    const buttons = [];
    document.querySelectorAll('button[data-wl-id],[role="button"][data-wl-id]').forEach(b => {
        const rect = b.getBoundingClientRect();
        if (rect.top > window.innerHeight + 100) return;
        const id = b.getAttribute('data-wl-id');
        const text = (b.innerText?.trim() || b.getAttribute('aria-label') || '').substring(0, 60);
        if (text) buttons.push(`[${id}] ${text}`);
    });

    // Visible selects / dropdowns
    const selects = [];
    document.querySelectorAll('select[data-wl-id]').forEach(s => {
        const id = s.getAttribute('data-wl-id');
        const opts = Array.from(s.options).map(o=>o.text).slice(0,8).join(' | ');
        selects.push(`[${id}] "${s.getAttribute('aria-label')||s.name||'select'}": ${opts}`);
    });

    return {hasPasswordField, forms, headings,
            links: links.slice(0,40), buttons: buttons.slice(0,25), selects};
}
"""


class Observer:
    """Extracts page state as a structured Observation."""

    def __init__(self, page: Page, config: BrowserConfig):
        self.page = page
        self.config = config
        self.annotator = Annotator()

    async def observe(self, include_screenshot: bool = True,
                      extract_dom: bool = False) -> Observation:
        """Capture the current page state.

        Args:
            include_screenshot: Whether to take a screenshot.
            extract_dom: Whether to run the rich DOM extraction for DOM mode.

        Returns:
            An Observation with elements, screenshots, and metadata.
        """
        logger.debug("observing", url=self.page.url)

        # 1. Inject element tagging and extract metadata
        # Wrap in retry logic: if the page is mid-navigation the execution context
        # gets destroyed; wait briefly and try once more before giving up.
        elements: list[PageElement] = []
        screenshot_b64: Optional[str] = None
        annotated_b64: Optional[str] = None
        dom_structured: Optional[str] = None
        login_detected: bool = False
        page_title = ""

        for attempt in range(2):
            try:
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

                page_title = await self.page.title()

                # Rich DOM extraction (DOM mode)
                if extract_dom:
                    dom_structured = await self._build_dom_summary()

                # Login detection — run always so the orchestrator can pause
                login_detected = self._detect_login(
                    self.page.url, page_title, dom_structured
                )

                # Success — break out of retry loop
                break

            except Exception as exc:
                exc_str = str(exc)
                if "Execution context was destroyed" in exc_str or "navigation" in exc_str.lower():
                    if attempt == 0:
                        logger.warning(
                            "observe_context_destroyed_retrying",
                            error=exc_str[:120],
                        )
                        await asyncio.sleep(1.0)
                        continue
                    else:
                        # Second failure — return empty observation so the loop continues
                        logger.warning(
                            "observe_context_destroyed_giving_up",
                            error=exc_str[:120],
                        )
                        return Observation(
                            url=self.page.url,
                            title="",
                            elements=[],
                        )
                else:
                    raise

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
            title=page_title,
            elements=elements,
            screenshot_base64=screenshot_b64,
            annotated_screenshot_base64=annotated_b64,
            accessibility_tree=a11y_tree,
            page_text=page_text,
            dom_structured=dom_structured,
            login_detected=login_detected,
        )

        logger.debug(
            "observation_complete",
            num_elements=len(elements),
            has_screenshot=screenshot_b64 is not None,
        )
        return observation

    async def _build_dom_summary(self) -> str:
        """Run EXTRACT_DOM_JS and format the result as a readable text block."""
        try:
            dom = await self.page.evaluate(EXTRACT_DOM_JS)
        except Exception:
            return ""

        parts: list[str] = []

        if dom.get("headings"):
            parts.append("=== Headings ===\n" + "\n".join(dom["headings"]))

        if dom.get("forms"):
            parts.append("=== Forms ===\n" + "\n\n".join(dom["forms"]))

        if dom.get("buttons"):
            parts.append("=== Buttons ===\n" + "\n".join(dom["buttons"]))

        if dom.get("selects"):
            parts.append("=== Dropdowns ===\n" + "\n".join(dom["selects"]))

        if dom.get("links"):
            parts.append("=== Links ===\n" + "\n".join(dom["links"]))

        return "\n\n".join(parts)

    def _detect_login(self, url: str, title: str, dom_structured: Optional[str]) -> bool:
        """Return True if the page appears to require authentication."""
        url_lower = url.lower()
        title_lower = (title or "").lower()

        if any(t in url_lower for t in _LOGIN_URL_TOKENS):
            return True
        if any(t in title_lower for t in _LOGIN_TITLE_TOKENS):
            return True
        # Password field in DOM summary is the strongest signal
        if dom_structured and 'type: "password"' in dom_structured:
            return True
        return False

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
