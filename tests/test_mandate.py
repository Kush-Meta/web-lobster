"""Tests for mandate policy: origin patterns, data grants, and enforcer decisions.

These run without a browser. test_mandate_browser.py covers the same rules
against real Chromium traffic.
"""

from __future__ import annotations

import asyncio
import base64
import json
import time
from urllib.parse import quote

import pytest
from pydantic import ValidationError

from web_lobster.core.schemas import Action, ActionType
from web_lobster.mandate.enforcer import MandateEnforcer, ViolationKind
from web_lobster.mandate.schema import (
    DataGrant,
    Mandate,
    ensure_scheme,
    parse_origin_pattern,
    url_origin,
)

SHOP = "https://www.united.com"
EVIL = "https://evil.example"
EMAIL = "kush.test@example.com"


def _mandate(**overrides) -> Mandate:
    fields = dict(
        task="Book a flight",
        origins=[SHOP, "https://*.united.com"],
        data=[DataGrant(name="email", value=EMAIL, origins=[SHOP])],
    )
    fields.update(overrides)
    return Mandate(**fields)


class TestOriginPatterns:
    def test_wildcard_matches_subdomains_not_apex_or_lookalikes(self):
        m = _mandate(origins=["https://*.united.com"], data=[])
        assert m.allows_origin("https://checkout.united.com/pay")
        assert m.allows_origin("https://a.b.united.com/")
        assert not m.allows_origin("https://united.com/")
        assert not m.allows_origin("https://evilunited.com/")

    def test_bare_host_means_https_default_port(self):
        m = _mandate(origins=["united.com"], data=[])
        assert m.allows_origin("https://united.com/x")
        assert m.allows_origin("https://united.com:443/x")
        assert not m.allows_origin("http://united.com/x")
        assert not m.allows_origin("https://united.com:8443/x")

    def test_explicit_port(self):
        m = _mandate(origins=["http://127.0.0.1:8000"], data=[])
        assert m.allows_origin("http://127.0.0.1:8000/a")
        assert not m.allows_origin("http://127.0.0.1:8001/a")

    def test_websocket_urls_map_to_http_origins(self):
        assert url_origin("wss://www.united.com/ws") == ("https", "www.united.com", 443)
        assert url_origin("ws://127.0.0.1:9000/") == ("http", "127.0.0.1", 9000)

    def test_non_http_urls_have_no_origin(self):
        for url in ["about:blank", "data:text/html,hi", "javascript:alert(1)", "file:///etc/passwd"]:
            assert url_origin(url) is None

    @pytest.mark.parametrize("pattern", [
        "https://united.com/path",
        "*",
        "https://*",
        "ftp://united.com",
        "https://un*ted.com",
        "https://united.com:99999",
    ])
    def test_rejects_invalid_patterns(self, pattern):
        with pytest.raises(ValueError):
            parse_origin_pattern(pattern)

    def test_ensure_scheme(self):
        assert ensure_scheme("evil.example/x") == "https://evil.example/x"
        assert ensure_scheme("http://a.com") == "http://a.com"


class TestMandateModel:
    def test_grant_value_never_dumped_or_repred(self):
        m = _mandate()
        assert EMAIL not in repr(m)
        assert EMAIL not in json.dumps(m.model_dump())

    def test_grant_value_minimum_length(self):
        with pytest.raises(ValidationError):
            DataGrant(name="zip", value="123", origins=[SHOP])

    def test_grant_name_must_be_identifier(self):
        with pytest.raises(ValidationError):
            DataGrant(name="e-mail", value=EMAIL, origins=[SHOP])

    def test_duplicate_grant_names_rejected(self):
        grant = DataGrant(name="email", value=EMAIL, origins=[SHOP])
        with pytest.raises(ValidationError):
            _mandate(data=[grant, grant])

    def test_requires_at_least_one_origin(self):
        with pytest.raises(ValidationError):
            _mandate(origins=[])

    def test_expiry(self):
        assert not _mandate().is_expired()
        assert _mandate(expires_at=time.time() - 1).is_expired()

    def test_from_yaml_relative_expiry(self, tmp_path):
        path = tmp_path / "mandate.yaml"
        path.write_text(
            "task: Search flights\n"
            "origins: [https://www.google.com]\n"
            "expires_in_minutes: 5\n"
        )
        m = Mandate.from_yaml(path)
        assert m.task == "Search flights"
        assert not m.is_expired()
        assert m.expires_at > time.time() + 240

    def test_default_start_url_skips_wildcards(self):
        m = _mandate(origins=["https://*.united.com", "http://127.0.0.1:8000"], data=[])
        assert m.default_start_url() == "http://127.0.0.1:8000/"
        assert _mandate(origins=["united.com"], data=[]).default_start_url() == "https://united.com/"
        assert _mandate(origins=["https://*.united.com"], data=[]).default_start_url() is None


class TestRequestChecks:
    def setup_method(self):
        self.enforcer = MandateEnforcer(_mandate())

    def check(self, url, method="GET", nav=False, body=None):
        return self.enforcer.check_request(url, method, nav, body)

    def test_navigation_inside_mandate_allowed(self):
        assert self.check(f"{SHOP}/flights", nav=True) is None
        assert self.check("https://checkout.united.com/", nav=True) is None

    def test_navigation_outside_mandate_blocked(self):
        v = self.check(f"{EVIL}/prize", nav=True)
        assert v.kind == ViolationKind.NAVIGATION

    def test_non_web_scheme_navigation_blocked(self):
        assert self.check("javascript:alert(1)", nav=True).kind == ViolationKind.NAVIGATION
        assert self.check("about:blank", nav=True) is None

    def test_cross_origin_get_subresource_allowed(self):
        # Pages need third-party images and scripts to render.
        assert self.check("https://cdn.example/app.js") is None

    def test_cross_origin_write_blocked(self):
        for method in ["POST", "PUT", "PATCH", "DELETE"]:
            v = self.check(f"{EVIL}/collect", method=method)
            assert v.kind == ViolationKind.CROSS_ORIGIN_WRITE

    def test_same_origin_write_allowed(self):
        assert self.check(f"{SHOP}/api/search", method="POST", body=b'{"from":"LAX"}') is None

    @pytest.mark.parametrize("url", [
        f"{EVIL}/px?e={EMAIL}",
        f"{EVIL}/px?e={quote(EMAIL, safe='')}",
        f"{EVIL}/px?e={EMAIL.upper()}",
        f"{EVIL}/px?e={base64.b64encode(EMAIL.encode()).decode()}",
        f"{EVIL}/px?e={base64.urlsafe_b64encode(EMAIL.encode()).decode().rstrip('=')}",
    ])
    def test_granted_value_leaving_in_url_blocked(self, url):
        v = self.check(url)
        assert v.kind == ViolationKind.DATA_LEAK
        assert v.grant == "email"

    def test_granted_value_leaving_in_body_blocked(self):
        body = json.dumps({"user": {"email": EMAIL}}).encode()
        v = self.check("https://analytics.example/track", method="POST", body=body)
        assert v.kind == ViolationKind.DATA_LEAK

    def test_granted_value_to_granted_origin_allowed(self):
        body = f"email={quote(EMAIL)}".encode()
        assert self.check(f"{SHOP}/checkout", method="POST", body=body) is None

    def test_granted_value_to_mandate_origin_outside_grant_blocked(self):
        # checkout.united.com is inside the mandate but the grant only covers www.
        v = self.check("https://checkout.united.com/pay", method="POST", body=EMAIL.encode())
        assert v.kind == ViolationKind.DATA_LEAK

    def test_violation_is_redacted(self):
        v = self.check(f"{EVIL}/px?e={quote(EMAIL, safe='')}&b={EMAIL}")
        assert EMAIL not in v.url and quote(EMAIL, safe="") not in v.url
        assert "{{email}}" in v.url

    def test_expired_mandate_blocks_everything(self):
        enforcer = MandateEnforcer(_mandate(expires_at=time.time() - 1))
        assert enforcer.check_request(f"{SHOP}/", "GET", True).kind == ViolationKind.EXPIRED


class TestTextResolution:
    def setup_method(self):
        self.enforcer = MandateEnforcer(_mandate())

    def test_placeholder_resolves_on_granted_page(self):
        text, v = self.enforcer.resolve_text("{{email}}", f"{SHOP}/checkout")
        assert v is None
        assert text == EMAIL

    def test_placeholder_refused_on_page_outside_grant(self):
        text, v = self.enforcer.resolve_text("{{ email }}", "https://checkout.united.com/")
        assert v.kind == ViolationKind.DATA_ENTRY
        assert EMAIL not in text

    def test_unknown_placeholder(self):
        _, v = self.enforcer.resolve_text("{{password}}", f"{SHOP}/")
        assert v.kind == ViolationKind.UNKNOWN_PLACEHOLDER

    def test_raw_value_gets_same_check(self):
        _, v = self.enforcer.resolve_text(f"contact {EMAIL}", f"{EVIL}/form")
        assert v.kind == ViolationKind.DATA_ENTRY

    def test_plain_text_passes_through(self):
        assert self.enforcer.resolve_text("LAX", f"{EVIL}/")[0] == "LAX"

    def test_contains_grant(self):
        assert self.enforcer.contains_grant(EMAIL.upper())
        assert not self.enforcer.contains_grant("{{email}}")


class TestActionChecks:
    def setup_method(self):
        self.enforcer = MandateEnforcer(_mandate())

    def test_navigate_without_scheme_is_checked_as_https(self):
        action = Action(action=ActionType.NAVIGATE, url="evil.example/prize")
        v = self.enforcer.check_action(action, f"{SHOP}/")
        assert v.kind == ViolationKind.NAVIGATION

    def test_typing_placeholder_on_wrong_page(self):
        action = Action(action=ActionType.TYPE, element_id=3, text="{{email}}")
        assert self.enforcer.check_action(action, f"{EVIL}/").kind == ViolationKind.DATA_ENTRY
        assert self.enforcer.check_action(action, f"{SHOP}/") is None

    def test_precheck_violations_are_recorded_but_not_drained(self):
        # The orchestrator reports pre-check violations itself; drain() only
        # returns ones raised inside the browser, so nothing is reported twice.
        action = Action(action=ActionType.NAVIGATE, url=f"{EVIL}/")
        self.enforcer.check_action(action, f"{SHOP}/")
        assert len(self.enforcer.violations) == 1
        assert self.enforcer.drain() == []

    def test_check_page_backstop(self):
        assert self.enforcer.check_page(f"{SHOP}/") is None
        assert self.enforcer.check_page("chrome-error://chromewebdata/") is None
        assert self.enforcer.check_page(f"{EVIL}/").kind == ViolationKind.NAVIGATION


class TestWebSocketChecks:
    def test_socket_outside_mandate_blocked(self):
        enforcer = MandateEnforcer(_mandate())
        assert enforcer.check_websocket("wss://evil.example/ws").kind == ViolationKind.WEBSOCKET
        assert enforcer.check_websocket("wss://www.united.com/ws") is None

    def test_message_leaking_grant_blocked(self):
        grant = DataGrant(name="email", value=EMAIL, origins=["https://checkout.united.com"])
        enforcer = MandateEnforcer(_mandate(data=[grant]))
        msg = json.dumps({"email": EMAIL})
        leak = enforcer.check_websocket_message("wss://www.united.com/ws", msg)
        assert leak.kind == ViolationKind.DATA_LEAK
        assert enforcer.check_websocket_message("wss://checkout.united.com/ws", msg) is None


class TestPendingViolations:
    async def test_wait_returns_as_soon_as_a_violation_is_queued(self):
        enforcer = MandateEnforcer(_mandate())
        violation = enforcer.check_request(f"{EVIL}/", "GET", True)
        asyncio.get_running_loop().call_later(0.02, enforcer.record, violation, True)
        start = time.monotonic()
        await enforcer.wait_for_pending(timeout=2.0)
        assert time.monotonic() - start < 1.0
        assert enforcer.drain() == [violation]

    async def test_wait_times_out_quietly(self):
        enforcer = MandateEnforcer(_mandate())
        await enforcer.wait_for_pending(timeout=0.05)
        assert enforcer.drain() == []
