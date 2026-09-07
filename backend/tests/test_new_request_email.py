"""The shop is told a request is waiting, instead of having to watch the dashboard.

Satya, in the 2026-08-27 call: does the salon get a notification, or do they have to keep
the portal open? It was never answered, and the answer was "keep the portal open" — a
request arrived, appeared on a page nobody was looking at, and nothing else happened.

That is survivable while four people are testing. With call forwarding live it is the
whole product failing quietly: a customer rings, the receptionist takes the booking
perfectly, and no one at the salon ever finds out.

Two things this must never do. It must not fail a booking — the request is already
written by the time we get here, and a shop with no email in Settings, or a deployment
with no mail transport, simply gets nothing. And it must not be sent on the call path:
it runs moments before the caller hears their confirmation, so an SMTP handshake there
would be dead air on the phone.
"""

from unittest.mock import patch

import pytest

import email_notify


BIZ = {
    "name": "19765 Gig Harbor",
    "public_name": "Gig Harbor Hair Masters",
    "email": "salon@example.test",
    "staff": [{"id": "st_t", "name": "Terrance"}],
}

APT = {
    "id": 230,
    "name": "Bob",
    "phone": "+12535550102",
    "date": "2026-09-10",
    "time": "09:00",
    "reason": "Full Highlight",
    "client_id": "lana-s-store",
}


# --- what the shop actually reads ---------------------------------------------


def test_the_subject_carries_who_and_when():
    """Read on a phone between clients — it has to work without opening anything."""
    subject, _html, _text = email_notify.format_new_request_email(
        business_name="Gig Harbor Hair Masters",
        customer_name="Bob",
        customer_phone="+12535550102",
        date="Thursday, September 10",
        time_ampm="9:00 AM",
        service="Full Highlight",
    )
    assert "Bob" in subject
    assert "September 10" in subject
    assert "9:00 AM" in subject


def test_the_body_carries_the_number_to_ring():
    """A request exists to be called back. The phone number is the point."""
    _s, html, text = email_notify.format_new_request_email(
        business_name="Gig Harbor Hair Masters",
        customer_name="Bob",
        customer_phone="+12535550102",
        date="Thursday, September 10",
        time_ampm="9:00 AM",
        service="Full Highlight",
        stylist="Terrance",
        dashboard_url="https://call-surge.com/dashboard",
    )
    for expected in ("+12535550102", "Full Highlight", "Terrance", "Thursday, September 10"):
        assert expected in html, expected
        assert expected in text, expected
    assert "https://call-surge.com/dashboard" in html


def test_it_says_nothing_is_booked_yet():
    """The whole product promise. It must not read like a confirmed appointment."""
    _s, html, text = email_notify.format_new_request_email(
        business_name="Gig Harbor Hair Masters",
        customer_name="Bob",
        customer_phone="",
        date="Thursday, September 10",
        time_ampm="9:00 AM",
    )
    assert "Nothing is booked yet" in html
    assert "Nothing is booked yet" in text


def test_a_missing_phone_is_stated_not_blank():
    _s, html, _t = email_notify.format_new_request_email(
        business_name="B", customer_name="Bob", customer_phone="",
        date="Thursday, September 10", time_ampm="9:00 AM",
    )
    assert "Not provided" in html


def test_the_empty_service_sentinel_is_not_shown():
    """"—" is the internal marker for no service; it means nothing to a salon."""
    _s, html, text = email_notify.format_new_request_email(
        business_name="B", customer_name="Bob", customer_phone="+1",
        date="Thursday, September 10", time_ampm="9:00 AM", service="—",
    )
    assert "Service" not in html, "the placeholder was rendered as a real service"
    assert "Service" not in text
    # A real service still appears.
    _s2, html2, _t2 = email_notify.format_new_request_email(
        business_name="B", customer_name="Bob", customer_phone="+1",
        date="Thursday, September 10", time_ampm="9:00 AM", service="Full Highlight",
    )
    assert "Service" in html2 and "Full Highlight" in html2


# --- never break a booking ----------------------------------------------------


def test_a_store_with_no_email_gets_nothing():
    with patch.object(email_notify, "send_appointment_email") as send:
        assert email_notify.notify_store_of_request(
            to="", business_name="B", customer_name="Bob", customer_phone="+1",
            date="Thursday, September 10", time_ampm="9:00 AM",
        ) is False
        send.assert_not_called()


def test_a_broken_transport_is_swallowed():
    with patch.object(email_notify, "send_appointment_email", side_effect=RuntimeError("resend down")):
        assert email_notify.notify_store_of_request(
            to="salon@example.test", business_name="B", customer_name="Bob",
            customer_phone="+1", date="Thursday, September 10", time_ampm="9:00 AM",
        ) is False


# --- replies reach a person ---------------------------------------------------


def test_reply_to_is_set_from_env(monkeypatch):
    """The sending domain has no inbox. Without this, replying bounces."""
    monkeypatch.setenv("APPOINTMENT_EMAIL_REPLY_TO", "raj.gill@nuvatrahq.com")
    monkeypatch.setenv("APPOINTMENT_EMAIL_FROM", "Call Surge <appointments@mail.call-surge.com>")
    monkeypatch.setenv("RESEND_API_KEY", "re_test")
    seen: dict = {}

    class _Resp:
        status_code = 200

    def _post(_url, headers=None, json=None, timeout=None):
        seen.update(json or {})
        return _Resp()

    import httpx

    monkeypatch.setattr(httpx, "post", _post)
    assert email_notify.send_appointment_email(
        "salon@example.test", subject="s", html_body="<p>h</p>"
    ) is True
    assert seen.get("reply_to") == ["raj.gill@nuvatrahq.com"]
    assert seen.get("from") == "Call Surge <appointments@mail.call-surge.com>"


def test_no_reply_to_configured_sends_without_the_field(monkeypatch):
    monkeypatch.delenv("APPOINTMENT_EMAIL_REPLY_TO", raising=False)
    monkeypatch.setenv("APPOINTMENT_EMAIL_FROM", "a@b.test")
    monkeypatch.setenv("RESEND_API_KEY", "re_test")
    seen: dict = {}

    class _Resp:
        status_code = 200

    import httpx

    monkeypatch.setattr(
        httpx, "post", lambda _u, headers=None, json=None, timeout=None: (seen.update(json or {}), _Resp())[1]
    )
    email_notify.send_appointment_email("salon@example.test", subject="s", html_body="<p>h</p>")
    assert "reply_to" not in seen


# --- the call path ------------------------------------------------------------


def test_the_send_happens_off_the_call_path(monkeypatch):
    """It runs mid-call. A slow mail server must not become dead air on the phone."""
    import conversation_service as cs

    started: list = []
    monkeypatch.setattr(
        cs.threading, "Thread", lambda *a, **k: type(
            "T", (), {"start": lambda _s: started.append(k.get("daemon"))}
        )()
    )
    cs._email_store_about_request(APT, BIZ, "st_t")
    assert started == [True], "must be a daemon thread, not an inline send"


def test_no_email_configured_starts_no_thread(monkeypatch):
    import conversation_service as cs

    started: list = []
    monkeypatch.setattr(
        cs.threading, "Thread", lambda *a, **k: type(
            "T", (), {"start": lambda _s: started.append(1)}
        )()
    )
    cs._email_store_about_request(APT, {**BIZ, "email": ""}, "st_t")
    assert started == []


def test_the_date_is_readable_not_iso():
    import conversation_service as cs

    assert cs.staff_schedule_friendly_date("2026-09-10").startswith("Thursday")
    assert cs.staff_schedule_friendly_date("nonsense") == "nonsense"
