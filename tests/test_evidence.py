"""Tests for evidence checks: how code decides a sub-goal is really done."""

from __future__ import annotations

from typing import Optional

import pytest
from pydantic import ValidationError

from web_lobster.core.values import ExtractedValue, ValueType
from web_lobster.verify.evidence import (
    EvidenceContext,
    RequestCheck,
    TextCheck,
    UrlCheck,
    ValueCheck,
    evaluate,
    parse_url_pattern,
    url_matches,
)
from web_lobster.verify.network import NetworkEvent, NetworkRecorder

SHOP = "https://www.united.com"


def _event(seq: int, method: str, url: str, status: Optional[int], failure: Optional[str] = None) -> NetworkEvent:
    return NetworkEvent(seq=seq, method=method, url=url, status=status, failure=failure)


def _context(**overrides) -> EvidenceContext:
    fields = dict(page_url=f"{SHOP}/confirmation/ABC123?ref=mail", page_text="", events=[], values={})
    fields.update(overrides)
    return EvidenceContext(**fields)


def _value(name: str, type_: ValueType, value) -> ExtractedValue:
    return ExtractedValue(name=name, type=type_, value=value, origin=SHOP)


class TestUrlPatterns:
    def test_path_glob_covers_path_and_query(self):
        assert url_matches(f"{SHOP}/confirmation/ABC123?ref=x", f"{SHOP}/confirmation/*")
        assert not url_matches(f"{SHOP}/checkout", f"{SHOP}/confirmation/*")

    def test_no_path_means_any_path(self):
        assert url_matches(f"{SHOP}/anything?q=1", SHOP)

    def test_root_path_is_exact(self):
        assert url_matches(f"{SHOP}/", f"{SHOP}/")
        assert not url_matches(f"{SHOP}/x", f"{SHOP}/")

    def test_host_cannot_match_lookalikes(self):
        assert not url_matches("https://www.united.com.evil.example/confirmation/1", f"{SHOP}/confirmation/*")
        assert url_matches("https://checkout.united.com/done", "https://*.united.com/done")
        assert not url_matches("https://evilunited.com/done", "https://*.united.com/done")

    @pytest.mark.parametrize("pattern", [
        "www.united.com/confirmation/*",
        "https://*united.com/*",
        "https://www.united.com*/x",
        "ftp://united.com/",
    ])
    def test_invalid_patterns_rejected(self, pattern):
        with pytest.raises(ValueError):
            parse_url_pattern(pattern)


class TestUrlCheck:
    def test_passes_on_matching_page(self):
        assert evaluate(UrlCheck(pattern=f"{SHOP}/confirmation/*"), _context()).passed

    def test_fails_elsewhere(self):
        result = evaluate(UrlCheck(pattern=f"{SHOP}/confirmation/*"), _context(page_url=f"{SHOP}/checkout"))
        assert not result.passed
        assert result.type == "url"

    def test_an_error_page_is_not_the_page(self):
        # A guessed URL lands on a 404 that matches the pattern it guessed:
        # live, saucedemo.com/login.html doesn't exist, and the check passed.
        result = evaluate(UrlCheck(pattern=f"{SHOP}/confirmation/*"), _context(page_status=404))
        assert not result.passed
        assert "answered 404" in result.detail

    def test_an_unknown_status_still_passes(self):
        # Nothing was navigated during this sub-goal, so there's nothing to hold
        # against the page.
        assert evaluate(UrlCheck(pattern=f"{SHOP}/confirmation/*"), _context(page_status=None)).passed
        assert evaluate(UrlCheck(pattern=f"{SHOP}/confirmation/*"), _context(page_status=200)).passed


class TestTextCheckSeesFields:
    def test_text_typed_into_a_field_counts(self):
        # inner_text skips input values, so "I entered New York" could never be
        # proven by page text. Live run 27 spent a step budget on that check.
        result = evaluate(TextCheck(contains="New York"), _context(page_fields=["New York"]))
        assert result.passed
        assert result.detail == "found in a field on the page"

    def test_still_fails_when_nothing_holds_it(self):
        assert not evaluate(TextCheck(contains="New York"), _context(page_fields=["Tokyo"])).passed

    def test_a_date_matches_however_the_site_writes_it(self):
        # A plan asks for 2026-10-15; Google Flights answers "Thu, Oct 15".
        for held, rendering in (("Thu, Oct 15", "Oct 15"), ("15 October 2026", "15 Oct"),
                                ("departing 10/15/2026", "10/15/2026")):
            result = evaluate(TextCheck(contains="2026-10-15"), _context(page_fields=[held]))
            assert result.passed, held
            assert rendering in result.detail

    def test_another_day_is_still_another_day(self):
        assert not evaluate(TextCheck(contains="2026-10-16"), _context(page_fields=["Thu, Oct 15"])).passed
        assert not evaluate(TextCheck(contains="2026-11-15"), _context(page_fields=["Thu, Oct 15"])).passed


class TestRequestCheck:
    def test_needs_method_url_and_success_status(self):
        check = RequestCheck(method="POST", url=f"{SHOP}/api/*")
        events = [
            _event(1, "GET", f"{SHOP}/api/book", 200),
            _event(2, "POST", "https://evil.example/api/book", 200),
            _event(3, "POST", f"{SHOP}/api/book", 500),
        ]
        result = evaluate(check, _context(events=events))
        assert not result.passed and "500" in result.detail

        events.append(_event(4, "POST", f"{SHOP}/api/book", 303))
        result = evaluate(check, _context(events=events))
        assert result.passed and "303" in result.detail

    def test_blocked_request_does_not_count(self):
        events = [_event(1, "POST", f"{SHOP}/api/book", None, "net::ERR_BLOCKED_BY_CLIENT")]
        result = evaluate(RequestCheck(url=f"{SHOP}/api/book"), _context(events=events))
        assert not result.passed
        assert "ERR_BLOCKED_BY_CLIENT" in result.detail

    def test_nothing_sent(self):
        result = evaluate(RequestCheck(url=f"{SHOP}/api/book"), _context())
        assert result.detail == "no matching request was sent"

    def test_custom_status_range(self):
        check = RequestCheck(url=f"{SHOP}/api/book", status_min=201, status_max=201)
        assert not evaluate(check, _context(events=[_event(1, "POST", f"{SHOP}/api/book", 200)])).passed
        assert evaluate(check, _context(events=[_event(1, "POST", f"{SHOP}/api/book", 201)])).passed


class TestTextCheck:
    def test_ignores_case_and_whitespace(self):
        context = _context(page_text="Your  booking\n is CONFIRMED.")
        assert evaluate(TextCheck(contains="booking is confirmed"), context).passed
        assert not evaluate(TextCheck(contains="payment received"), context).passed


class TestValueCheck:
    @pytest.mark.parametrize("op, expected, passed", [
        ("<=", 400, False),
        ("<", 413, True),
        ("==", 412, True),
        ("!=", 412, False),
        (">", 399.5, True),
    ])
    def test_numbers(self, op, expected, passed):
        context = _context(values={"total": _value("total", ValueType.NUMBER, 412.0)})
        assert evaluate(ValueCheck(name="total", op=op, value=expected), context).passed is passed

    def test_iso_dates_compare_in_order(self):
        context = _context(values={"depart": _value("depart", ValueType.DATE, "2026-12-15")})
        assert evaluate(ValueCheck(name="depart", op="<", value="2027-01-01"), context).passed

    def test_booleans_only_support_equality(self):
        context = _context(values={"nonstop": _value("nonstop", ValueType.BOOLEAN, True)})
        assert evaluate(ValueCheck(name="nonstop", op="==", value=True), context).passed
        assert not evaluate(ValueCheck(name="nonstop", op=">", value=False), context).passed

    def test_a_null_check_asks_only_whether_the_value_was_read(self):
        # What a step whose whole job is reading can prove, instead of falling
        # back to a model's opinion (live run 28 came back right but unverified).
        context = _context(values={"price": _value("price", ValueType.NUMBER, 29.99)})
        read = evaluate(ValueCheck(name="price", op="!=", value=None), context)
        assert read.passed and read.detail == "{{$price}} was read"
        assert read.description == "{{$price}} was read"
        assert not evaluate(ValueCheck(name="gone", op="!=", value=None), context).passed

    def test_a_null_check_needs_an_equality_operator(self):
        with pytest.raises(ValidationError):
            ValueCheck(name="price", op="<", value=None)

    def test_missing_or_mismatched_values_fail(self):
        context = _context(values={"total": _value("total", ValueType.NUMBER, 412.0)})
        assert evaluate(ValueCheck(name="gone", op="==", value=1), context).detail == "{{$gone}} was not read"
        assert not evaluate(ValueCheck(name="total", op="==", value="412"), context).passed
        assert not evaluate(ValueCheck(name="total", op="==", value=True), context).passed


class TestCheckModels:
    def test_method_normalised_and_validated(self):
        assert RequestCheck(method="post", url=f"{SHOP}/").method == "POST"
        with pytest.raises(ValidationError):
            RequestCheck(method="FETCH", url=f"{SHOP}/")

    def test_status_range_validated(self):
        with pytest.raises(ValidationError):
            RequestCheck(url=f"{SHOP}/", status_min=400, status_max=200)

    def test_patterns_validated(self):
        with pytest.raises(ValidationError):
            UrlCheck(pattern="https://*united.com/*")
        with pytest.raises(ValidationError):
            RequestCheck(url="united.com/api")

    def test_text_and_value_fields_validated(self):
        with pytest.raises(ValidationError):
            TextCheck(contains="")
        with pytest.raises(ValidationError):
            ValueCheck(name="total price", op="==", value=1)
        with pytest.raises(ValidationError):
            ValueCheck(name="total", op="~", value=1)

    def test_descriptions_use_only_declared_content(self):
        assert RequestCheck(url=f"{SHOP}/api/*").describe() == f"POST {SHOP}/api/* answered 200-399"
        assert ValueCheck(name="total", op="<=", value=400).describe() == "{{$total}} <= 400"


class TestNetworkRecorder:
    def test_mark_and_since(self):
        recorder = NetworkRecorder()
        recorder.record("GET", f"{SHOP}/", 200)
        mark = recorder.mark()
        recorder.record("post", f"{SHOP}/api/book", 201)
        [event] = recorder.since(mark)
        assert event.method == "POST" and event.is_write and event.status == 201

    def test_redacts_urls_and_failures(self):
        recorder = NetworkRecorder(redact=lambda text: text.replace("secret", "{{token}}"))
        event = recorder.record("GET", f"{SHOP}/?t=secret", None, failure="blocked secret")
        assert "secret" not in event.url and "secret" not in event.failure

    def test_capacity(self):
        recorder = NetworkRecorder(capacity=3)
        for _ in range(5):
            recorder.record("GET", f"{SHOP}/", 200)
        assert [event.seq for event in recorder.since(0)] == [3, 4, 5]

    def test_in_flight_request_keeps_it_unsettled(self):
        recorder = NetworkRecorder()
        request = _FakeRequest("POST", f"{SHOP}/api/book")
        recorder._on_request(request)
        assert not recorder.settled(quiet_seconds=0)

        recorder._on_response(_FakeResponse(request, 303))
        recorder._on_request_done(request)
        assert recorder.settled(quiet_seconds=0)
        assert not recorder.settled(quiet_seconds=60)  # activity just happened

    def test_failed_request_is_logged_and_settles(self):
        recorder = NetworkRecorder()
        request = _FakeRequest("POST", f"{SHOP}/api/book", failure="net::ERR_FAILED")
        recorder._on_request(request)
        recorder._on_request_failed(request)
        [event] = recorder.since(0)
        assert event.status is None and event.failure == "net::ERR_FAILED"
        assert recorder.settled(quiet_seconds=0)


class _FakeRequest:
    def __init__(self, method: str, url: str, resource_type: str = "document", failure: Optional[str] = None):
        self.method, self.url, self.resource_type, self.failure = method, url, resource_type, failure


class _FakeResponse:
    def __init__(self, request: _FakeRequest, status: int):
        self.request, self.status = request, status
