"""Mandate enforcer — applies a Mandate to every request the browser makes.

The executor reads untrusted pages, so any action it picks may be an attacker's
instruction. The enforcer doesn't try to recognise those instructions; it makes
acting on them impossible. It sits in Playwright's network layer and blocks,
whatever the model intended:

- main-frame navigations outside the mandate's origins, including every
  redirect hop
- writes (POST/PUT/PATCH/DELETE) and WebSockets to origins outside the mandate
- writes and WebSockets to an allowed origin that the mandate's write rules
  don't list, when it lists any
- requests and WebSocket messages carrying a granted value (raw, URL-encoded,
  JSON-escaped or base64) to an origin that grant doesn't cover
- typing a granted value, or its {{placeholder}}, on a page the grant doesn't cover
- everything, once the mandate has expired

Known gaps, stated rather than papered over:
- cross-origin GETs (images, scripts, iframes) still load so pages work; data
  the agent never typed (page text, cookies) can leave that way
- a page script that transforms a value beyond the encodings above isn't caught
- redirect hops of non-navigation requests aren't re-checked, and a main-frame
  POST answered with 307/308 is re-issued as a GET
- WebRTC and DNS prefetch aren't visible to Playwright routing
- screenshots sent to a vision model can show a value once it's typed
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import html
import json
import re
import time
from enum import Enum
from typing import Optional, Union
from urllib.parse import quote, unquote_plus, urljoin, urlsplit

from playwright.async_api import BrowserContext, Request, Route, WebSocketRoute
from pydantic import BaseModel, Field

from web_lobster.core.schemas import Action, ActionType, Observation
from web_lobster.mandate.schema import (
    PLACEHOLDER_RE,
    DataGrant,
    Mandate,
    ensure_scheme,
    url_origin,
)
from web_lobster.utils.logging import get_logger

logger = get_logger(__name__)

SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})
# Schemes a main frame may show without anything leaving the browser.
LOCAL_SCHEMES = frozenset({"about", "data", "blob"})
# Allowed redirects are bounced through a page; cap them so a loop can't spin forever.
MAX_BOUNCES_PER_WINDOW = 20
BOUNCE_WINDOW_SECONDS = 10.0


class ViolationKind(str, Enum):
    NAVIGATION = "navigation"
    CROSS_ORIGIN_WRITE = "cross_origin_write"
    UNAPPROVED_WRITE = "unapproved_write"
    WEBSOCKET = "websocket"
    DATA_LEAK = "data_leak"
    DATA_ENTRY = "data_entry"
    UNKNOWN_PLACEHOLDER = "unknown_placeholder"
    EXPIRED = "expired"


class Violation(BaseModel):
    """Something the mandate blocked. URL and detail never contain granted values."""
    kind: ViolationKind
    url: str
    detail: str
    method: Optional[str] = None
    grant: Optional[str] = None
    # True when the browser blocked a top-level navigation, which leaves its error page
    main_frame: bool = False
    # Chromium's type for a request the browser blocked: document, fetch, ping, ...
    resource_type: str = ""
    timestamp: float = Field(default_factory=time.time)


def sent_by_page_script(violation: Violation) -> bool:
    """Whether a blocked request came from the page's own scripts (analytics, error
    reporting, API calls) rather than a page load or form submission."""
    return violation.resource_type not in ("", "document")


# Kinds the executor needs to hear about even when a script sent the request: the
# agent typed the data, or the whole task has to stop.
_ALWAYS_REPORTED = frozenset({ViolationKind.DATA_LEAK, ViolationKind.EXPIRED})


def worth_reporting(violation: Violation) -> bool:
    """Whether to tell the executor about a block, i.e. whether its own action likely
    caused it: a page load or form, a script call back to the site it's working on
    (an app's submit button), or typed data about to leave. A page's own traffic to
    third parties (python.org's error reporting retried 427 times in one task) is
    still blocked and recorded, but the executor can't act on it.
    """
    if violation.main_frame or violation.kind in _ALWAYS_REPORTED:
        return True
    if violation.kind is ViolationKind.UNAPPROVED_WRITE:
        return violation.resource_type in ("fetch", "xhr")
    return not sent_by_page_script(violation)


class MandateViolationError(RuntimeError):
    """A browser operation would have broken the mandate."""


class MandateExpiredError(MandateViolationError):
    """The mandate expired while the task was still running."""


def _origin_label(url: str) -> str:
    origin = url_origin(url)
    if origin is None:
        return url.split(":", 1)[0] + ":"
    scheme, host, port = origin
    return f"{scheme}://{host}:{port}"


def _placeholder(name: str) -> str:
    return "{{" + name + "}}"


def _value_forms(value: str) -> tuple[list[str], list[str]]:
    """Lowercased text encodings and case-sensitive base64 encodings of a value."""
    raw = value.encode()
    text_forms = {value, quote(value, safe=""), json.dumps(value)[1:-1]}
    b64_forms = {
        base64.b64encode(raw).decode().rstrip("="),
        base64.urlsafe_b64encode(raw).decode().rstrip("="),
    }
    return sorted(f.lower() for f in text_forms), sorted(b64_forms)


def _bounce_page(target: str) -> str:
    """A page whose only job is to navigate on, so the next hop is routed again."""
    js_target = json.dumps(target).replace("</", "<\\/")
    return (
        "<!doctype html>"
        f'<meta http-equiv="refresh" content="0;url={html.escape(target)}">'
        f"<script>location.replace({js_target})</script>"
    )


class MandateEnforcer:
    """Checks browser traffic and agent input against a mandate."""

    def __init__(self, mandate: Mandate):
        self.mandate = mandate
        self.violations: list[Violation] = []
        # Violations raised inside the browser (network, typing) that the
        # orchestrator hasn't reported to the executor yet.
        self._pending: list[Violation] = []
        self._pending_event = asyncio.Event()
        self._forms = {g.name: _value_forms(g.value) for g in mandate.data}
        self._bounce_times: list[float] = []

    # ── Decisions (pure; no browser needed) ──────────────────────────────────

    def placeholder_names(self) -> list[str]:
        return [g.name for g in self.mandate.data]

    def check_request(
        self,
        url: str,
        method: str = "GET",
        main_frame_navigation: bool = False,
        body: Optional[bytes] = None,
    ) -> Optional[Violation]:
        """Decide whether a request may proceed. Returns the violation if not."""
        method = method.upper()
        if self.mandate.is_expired():
            return self._violation(ViolationKind.EXPIRED, url, "the mandate has expired", method)

        leaked = self._leaked_grant(url, body)
        if leaked:
            return self._violation(
                ViolationKind.DATA_LEAK, url,
                f"request would send {_placeholder(leaked.name)} to {_origin_label(url)}, "
                "which that data isn't granted to",
                method, grant=leaked.name,
            )

        if url_origin(url) is None:
            scheme = url.split(":", 1)[0].lower()
            if main_frame_navigation and scheme not in LOCAL_SCHEMES:
                return self._violation(
                    ViolationKind.NAVIGATION, url, f"navigation to a {scheme}: URL", method
                )
            return None

        if self.mandate.allows_origin(url):
            if method not in SAFE_METHODS and not self.mandate.allows_write(method, url):
                return self._violation(
                    ViolationKind.UNAPPROVED_WRITE, url,
                    f"{method} {_origin_label(url)}{urlsplit(url).path} isn't one of the "
                    "writes this mandate allows", method,
                )
            return None
        if main_frame_navigation:
            return self._violation(
                ViolationKind.NAVIGATION, url,
                f"navigation to {_origin_label(url)} is outside the mandate", method,
            )
        if method not in SAFE_METHODS:
            return self._violation(
                ViolationKind.CROSS_ORIGIN_WRITE, url,
                f"{method} to {_origin_label(url)} is outside the mandate", method,
            )
        return None

    def check_websocket(self, url: str) -> Optional[Violation]:
        if self.mandate.is_expired():
            return self._violation(ViolationKind.EXPIRED, url, "the mandate has expired")
        leaked = self._leaked_grant(url, None)
        if leaked:
            return self._violation(
                ViolationKind.DATA_LEAK, url,
                f"WebSocket URL carries {_placeholder(leaked.name)}", grant=leaked.name,
            )
        if self.mandate.allows_origin(url):
            if not self.mandate.allows_write("WS", url):
                return self._violation(
                    ViolationKind.UNAPPROVED_WRITE, url,
                    f"WebSocket to {_origin_label(url)}{urlsplit(url).path} isn't one of the "
                    "writes this mandate allows",
                )
            return None
        return self._violation(
            ViolationKind.WEBSOCKET, url,
            f"WebSocket to {_origin_label(url)} is outside the mandate",
        )

    def check_websocket_message(
        self, url: str, message: Union[str, bytes]
    ) -> Optional[Violation]:
        if self.mandate.is_expired():
            return self._violation(ViolationKind.EXPIRED, url, "the mandate has expired")
        body = message if isinstance(message, bytes) else message.encode()
        leaked = self._leaked_grant(url, body)
        if leaked:
            return self._violation(
                ViolationKind.DATA_LEAK, url,
                f"WebSocket message would send {_placeholder(leaked.name)} to "
                f"{_origin_label(url)}, which that data isn't granted to",
                grant=leaked.name,
            )
        return None

    def resolve_text(self, text: str, page_url: str) -> tuple[str, Optional[Violation]]:
        """Substitute {{placeholders}} with granted values for the given page.

        Returns (resolved_text, None), or (text, violation) if the page may not
        receive the data. Raw granted values get the same check as placeholders.
        """
        if self.mandate.is_expired():
            return text, self._violation(ViolationKind.EXPIRED, page_url, "the mandate has expired")

        for name in PLACEHOLDER_RE.findall(text):
            grant = self.mandate.grant(name)
            if grant is None:
                return text, self._violation(
                    ViolationKind.UNKNOWN_PLACEHOLDER, page_url,
                    f"{_placeholder(name)} isn't data this mandate grants", grant=name,
                )
            if not grant.allows(page_url):
                return text, self._entry_violation(grant, page_url)

        folded = text.lower()
        for grant in self.mandate.data:
            if grant.value.lower() in folded and not grant.allows(page_url):
                return text, self._entry_violation(grant, page_url)

        resolved = PLACEHOLDER_RE.sub(lambda m: self.mandate.grant(m.group(1)).value, text)
        return resolved, None

    def approves_entry(self, text: str, page_url: str) -> bool:
        """True when the text is only granted {{placeholders}} that this page may receive.

        The mandate is the user's approval for exactly that entry, so a separate
        confirmation isn't needed.
        """
        if not PLACEHOLDER_RE.search(text) or PLACEHOLDER_RE.sub("", text).strip():
            return False
        _, violation = self.resolve_text(text, page_url)
        return violation is None

    def contains_grant(self, text: str) -> bool:
        folded = text.lower()
        return any(g.value.lower() in folded for g in self.mandate.data)

    def check_action(self, action: Action, page_url: str) -> Optional[Violation]:
        """Pre-flight check for an executor action. Records the violation if blocked."""
        violation = None
        if self.mandate.is_expired():
            violation = self._violation(ViolationKind.EXPIRED, page_url, "the mandate has expired")
        elif action.action == ActionType.NAVIGATE and action.url:
            violation = self.check_request(ensure_scheme(action.url), main_frame_navigation=True)
        elif action.action in (ActionType.TYPE, ActionType.SELECT) and action.text:
            _, violation = self.resolve_text(action.text, page_url)
        if violation:
            self.record(violation)
        return violation

    def check_page(self, page_url: str, record: bool = True) -> Optional[Violation]:
        """Backstop: flag a page that is outside the mandate however it got there."""
        if url_origin(page_url) is None or self.mandate.allows_origin(page_url):
            return None
        violation = self._violation(
            ViolationKind.NAVIGATION, page_url,
            f"the page ended up on {_origin_label(page_url)}, outside the mandate",
        )
        if record:
            self.record(violation, pending=False)
        return violation

    # ── Bookkeeping ──────────────────────────────────────────────────────────

    def record(self, violation: Violation, pending: bool = False) -> None:
        """Keep a violation. pending=True queues it for drain()."""
        self.violations.append(violation)
        if pending:
            self._pending.append(violation)
            self._pending_event.set()
        log = logger.warning if worth_reporting(violation) else logger.info
        log("mandate_violation", kind=violation.kind.value, detail=violation.detail,
            resource_type=violation.resource_type or None)

    def drain(self) -> list[Violation]:
        """Violations raised inside the browser since the last drain."""
        drained, self._pending = self._pending, []
        self._pending_event.clear()
        return drained

    async def wait_for_pending(self, timeout: float) -> None:
        """Wait up to `timeout` seconds for a browser-side violation to be queued.

        A click's navigation reaches the network layer a few milliseconds after
        the click itself returns, so read the verdict only after this.
        """
        with contextlib.suppress(asyncio.TimeoutError):
            await asyncio.wait_for(self._pending_event.wait(), timeout)

    def redact(self, text: str) -> str:
        """Replace granted values, in every tracked encoding, with their placeholders."""
        for grant in self.mandate.data:
            text_forms, b64_forms = self._forms[grant.name]
            for form in text_forms:
                text = re.sub(re.escape(form), _placeholder(grant.name), text, flags=re.IGNORECASE)
            for form in b64_forms:
                text = text.replace(form, _placeholder(grant.name))
        return text

    def redact_observation(self, observation: Observation) -> Observation:
        """Copy of an observation with granted values swapped for placeholders, so a
        typed value doesn't reach the model, the UI log, or memory as text.
        Screenshots are left alone: pixels can still show a typed value.
        """
        if not self.mandate.data:
            return observation

        def redact(text: Optional[str]) -> Optional[str]:
            return self.redact(text) if text else text

        return observation.model_copy(update={
            "url": redact(observation.url),
            "title": redact(observation.title),
            "page_text": redact(observation.page_text),
            "accessibility_tree": redact(observation.accessibility_tree),
            "dom_structured": redact(observation.dom_structured),
            "elements": [
                el.model_copy(update={
                    "label": redact(el.label),
                    "value": redact(el.value),
                    "href": redact(el.href),
                })
                for el in observation.elements
            ],
        })

    def _violation(
        self,
        kind: ViolationKind,
        url: str,
        detail: str,
        method: Optional[str] = None,
        grant: Optional[str] = None,
    ) -> Violation:
        return Violation(
            kind=kind, url=self.redact(url), detail=self.redact(detail),
            method=method, grant=grant,
        )

    def _entry_violation(self, grant: DataGrant, page_url: str) -> Violation:
        return self._violation(
            ViolationKind.DATA_ENTRY, page_url,
            f"{_placeholder(grant.name)} may not be entered on {_origin_label(page_url)}",
            grant=grant.name,
        )

    def _leaked_grant(self, url: str, body: Optional[bytes]) -> Optional[DataGrant]:
        haystack = url if body is None else url + "\n" + body.decode("utf-8", errors="ignore")
        folded = haystack.lower()
        decoded = unquote_plus(haystack).lower()
        for grant in self.mandate.data:
            if grant.allows(url):
                continue
            text_forms, b64_forms = self._forms[grant.name]
            if any(f in folded or f in decoded for f in text_forms):
                return grant
            if any(f in haystack for f in b64_forms):
                return grant
        return None

    # ── Browser wiring ───────────────────────────────────────────────────────

    async def attach(self, context: BrowserContext) -> None:
        """Route every request and WebSocket in the context through the mandate.

        Create the context with service_workers="block": requests made by a
        service worker don't pass through context routing.
        """
        await context.route("**/*", self._on_request)
        await context.route_web_socket("**/*", self._on_websocket)

    async def _on_request(self, route: Route, request: Request) -> None:
        try:
            main_frame_navigation = (
                request.is_navigation_request() and request.frame.parent_frame is None
            )
        except Exception:
            main_frame_navigation = False  # requests with no frame, e.g. from workers
        try:
            violation = self.check_request(
                request.url, request.method, main_frame_navigation, request.post_data_buffer
            )
            if violation:
                violation.main_frame = main_frame_navigation
                violation.resource_type = request.resource_type
                self.record(violation, pending=worth_reporting(violation))
                await route.abort("blockedbyclient")
            elif main_frame_navigation:
                await self._navigate_checked(route, request)
            else:
                await route.continue_()
        except Exception as e:
            # Fail closed: an error while deciding must not let the request through.
            logger.warning("mandate_route_error", error=self.redact(str(e))[:160])
            with contextlib.suppress(Exception):
                await route.abort("blockedbyclient")

    async def _navigate_checked(self, route: Route, request: Request) -> None:
        """Fetch a main-frame navigation ourselves so each redirect hop is checked.

        Chromium follows redirects without consulting routes, so an allowed page
        could otherwise 302 the browser anywhere. We fetch with redirects off and
        check the Location. For an allowed hop we serve a page that navigates on;
        that navigation comes back through _on_request and is checked in turn.
        Cookies set on the hop are kept: route.fetch shares the context's jar.
        """
        response = await route.fetch(max_redirects=0)
        location = response.headers.get("location")
        if not (300 <= response.status < 400 and location):
            await route.fulfill(response=response)
            return

        target = urljoin(request.url, location)
        violation = self.check_request(target, main_frame_navigation=True)
        if violation:
            violation.detail = f"redirect from {_origin_label(request.url)}: {violation.detail}"
            violation.main_frame = True
            self.record(violation, pending=True)
            await route.abort("blockedbyclient")
            return

        now = time.monotonic()
        self._bounce_times = [t for t in self._bounce_times if now - t < BOUNCE_WINDOW_SECONDS]
        self._bounce_times.append(now)
        if len(self._bounce_times) > MAX_BOUNCES_PER_WINDOW:
            logger.warning("mandate_redirect_loop", url=self.redact(target))
            await route.abort("failed")
            return

        await route.fulfill(status=200, content_type="text/html", body=_bounce_page(target))

    async def _on_websocket(self, ws: WebSocketRoute) -> None:
        violation = self.check_websocket(ws.url)
        if violation:
            self.record(violation, pending=True)
            await ws.close(code=1008, reason="Blocked by mandate")
            return

        server = ws.connect_to_server()

        def forward_page_message(message: Union[str, bytes]) -> None:
            leak = self.check_websocket_message(ws.url, message)
            if leak:
                self.record(leak, pending=True)
                return  # drop it; server-to-page messages still flow untouched
            server.send(message)

        ws.on_message(forward_page_message)
