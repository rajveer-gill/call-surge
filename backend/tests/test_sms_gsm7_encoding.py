"""One stray typographic character doubles the cost of every text the platform sends.

SMS packs GSM-7 at 160 characters per segment (153 once a message spans several). A
single character outside that alphabet flips the WHOLE message to UCS-2, which holds 70
(67 each). Carriers bill per segment, and the A2P daily cap counts segments too, so the
em-dash that used to sit in the booking confirmation was charging 7 segments for a
418-character message that should cost 3.

These tests pin the fix at the send boundary, where tenant-entered text (a service or
business name pasted out of a word processor) meets our own copy.
"""

from unittest.mock import MagicMock

import pytest

import booking_service
import runtime
import sms_service


def _is_gsm7(text: str) -> bool:
    return all(ch in sms_service.GSM7_CHARS for ch in text)


@pytest.fixture
def mock_twilio(monkeypatch):
    client = MagicMock()
    client.messages.create.return_value = MagicMock(sid="SMtest123")
    monkeypatch.setattr(runtime, "twilio_client", client)
    monkeypatch.setattr(runtime, "USE_DB", False)
    monkeypatch.setattr(sms_service.deps, "audit_log", lambda *a, **k: None)
    return client


def test_typographic_punctuation_becomes_plain():
    raw = "Here’s your slot — confirm it… “thanks”"
    out = sms_service.to_gsm7_safe(raw)
    assert out == "Here's your slot - confirm it... \"thanks\""
    assert _is_gsm7(out)


def test_non_latin_text_is_left_alone():
    """The point is to kill lookalikes, not to mangle real content to save a segment."""
    raw = "预约已收到"
    assert sms_service.to_gsm7_safe(raw) == raw


def test_segment_count_matches_the_carrier_math():
    assert sms_service.sms_segment_count("a" * 160) == 1
    assert sms_service.sms_segment_count("a" * 161) == 2
    assert sms_service.sms_segment_count("a" * 306) == 2
    assert sms_service.sms_segment_count("") == 0
    # One non-GSM character re-prices the entire body: 71 characters is a single GSM-7
    # segment and two UCS-2 ones (the UCS-2 single-segment limit being 70).
    assert sms_service.sms_segment_count("a" * 71) == 1
    assert sms_service.sms_segment_count("a" * 70 + "—") == 2
    # And at the length of a real booking confirmation, 3 segments versus 7.
    assert sms_service.sms_segment_count("a" * 418) == 3
    assert sms_service.sms_segment_count("a" * 417 + "—") == 7


def test_request_confirmation_body_is_gsm7():
    """Lana's flow. This is the body that was billing 7 segments instead of 3."""
    apt = {
        "name": "Raj",
        "phone": "+19255550195",
        "date": "2026-09-03",
        "time": "14:00",
        "reason": "Shampoo & Haircut + All-over color",
        "status": "pending_review",
    }
    body = sms_service.to_gsm7_safe(
        booking_service._format_appointment_details_confirmation_sms(apt)
    )
    assert _is_gsm7(body), [ch for ch in body if ch not in sms_service.GSM7_CHARS]
    assert sms_service.sms_segment_count(body) <= 3
    assert "APPOINTMENT REQUEST" in body


@pytest.mark.parametrize("status", ["pending_review", "pending_customer", "confirmed"])
def test_every_confirmation_variant_is_gsm7(status):
    apt = {"name": "Raj", "date": "2026-09-03", "time": "14:00", "status": status}
    body = sms_service.to_gsm7_safe(
        booking_service._format_appointment_details_confirmation_sms(apt)
    )
    assert _is_gsm7(body), [ch for ch in body if ch not in sms_service.GSM7_CHARS]


def test_empty_service_sentinel_does_not_reach_the_wire(mock_twilio):
    """The no-service marker is still an em-dash internally, by design.

    Lines elsewhere in booking_service compare reason against it, so changing the
    sentinel would be a data migration. Normalizing at the boundary is what keeps it
    off the wire.
    """
    apt = {"name": "Raj", "date": "2026-09-03", "time": "14:00", "status": "pending_review"}
    built = booking_service._format_appointment_details_confirmation_sms(apt)
    assert "—" in built  # sentinel survives internally
    sms_service.send_sms("+14255551234", built, from_override="+14782150212")
    sent = mock_twilio.messages.create.call_args.kwargs["body"]
    assert "—" not in sent
    assert _is_gsm7(sent)


def test_send_sms_normalizes_tenant_text(mock_twilio):
    """A business name pasted out of a word processor must not re-price the message."""
    sms_service.send_sms(
        "+14255551234",
        "Thanks for booking with Gig Harbor — Hair Masters’ team",
        from_override="+14782150212",
    )
    sent = mock_twilio.messages.create.call_args.kwargs["body"]
    assert sent == "Thanks for booking with Gig Harbor - Hair Masters' team"
    assert _is_gsm7(sent)
