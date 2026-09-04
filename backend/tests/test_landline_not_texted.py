"""Never promise a text to a number that cannot receive one.

Lana, 2026-09-04:

    "We had a stylist call from the salon, and it treated the call just like a cell
     phone and sent a text."

From that call — the confirmation looked perfect on the way out, and was dead on
arrival:

    17:02:50  outbound_twilio_ok            segments=3
    17:02:50  post_booking_confirmation_sms success=True  not_textable=False
    ...and one second later, from Twilio:
              status=undelivered  error_code=30005  "Unknown destination handset"

Twilio does not reject a landline when the message is created. It accepts it, hands back
a SID, and fails asynchronously — so NON_TEXTABLE_ERROR_CODES, which only ever sees the
synchronous exception, could not catch a real landline and `booking_caller_not_textable`
had never once fired in production. The caller was told "I've texted you the details"
and got nothing.

Asking Twilio the line type beforehand is the only way to know in time to say something
true while the caller is still on the phone. The booking itself is unaffected: the
request is filed exactly as it would be, and the salon has the number that called.

The dangerous direction here is the opposite one — withholding a confirmation from a
working mobile — so anything short of a confident "landline" sends.
"""

from unittest.mock import MagicMock

import pytest

import runtime
import sms_service
from conversation_service import post_booking_spoken_confirmation


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    sms_service._line_type_cache.clear()
    monkeypatch.setattr(runtime, "USE_DB", False)
    monkeypatch.setattr(sms_service.deps, "audit_log", lambda *a, **k: None)
    monkeypatch.delenv("VOICE_SMS_LINE_TYPE_LOOKUP", raising=False)
    yield
    sms_service._line_type_cache.clear()


def _twilio(monkeypatch, line_type=None, lookup_raises=False):
    client = MagicMock()
    client.messages.create.return_value = MagicMock(sid="SMtest123")
    if lookup_raises:
        client.lookups.v2.phone_numbers.side_effect = RuntimeError("lookup down")
    else:
        fetched = MagicMock()
        fetched.line_type_intelligence = {"type": line_type} if line_type else {}
        client.lookups.v2.phone_numbers.return_value.fetch.return_value = fetched
    monkeypatch.setattr(runtime, "twilio_client", client)
    return client


def _send(detail=None):
    return sms_service.send_sms(
        "+12538478707", "your appointment details", from_override="+18595550187",
        detail_out=detail if detail is not None else {},
    )


# --- the landline -------------------------------------------------------------


def test_a_landline_is_not_texted(monkeypatch):
    client = _twilio(monkeypatch, line_type="landline")
    detail: dict = {}
    assert _send(detail) is False
    assert detail["not_textable"] is True
    assert detail["line_type"] == "landline"
    client.messages.create.assert_not_called()


def test_the_caller_is_told_the_truth_instead():
    """The wording already existed and had never once been reached in production."""
    said = post_booking_spoken_confirmation("pending_review", "not_textable")
    assert "can't receive texts" in said
    assert "they'll call you on this number" in said


# --- everyone else still gets their text --------------------------------------


def test_a_mobile_is_texted(monkeypatch):
    client = _twilio(monkeypatch, line_type="mobile")
    assert _send() is True
    client.messages.create.assert_called_once()


def test_voip_is_texted(monkeypatch):
    """Google Voice and most softphones receive SMS perfectly well."""
    for t in ("voip", "nonFixedVoip", "fixedVoip"):
        sms_service._line_type_cache.clear()
        client = _twilio(monkeypatch, line_type=t)
        assert _send() is True, t
        client.messages.create.assert_called_once()


def test_an_unknown_line_type_is_texted(monkeypatch):
    client = _twilio(monkeypatch, line_type=None)
    assert _send() is True
    client.messages.create.assert_called_once()


def test_a_broken_lookup_still_texts(monkeypatch):
    """Twilio Lookup being down must never silence a confirmation to a real mobile."""
    client = _twilio(monkeypatch, lookup_raises=True)
    assert _send() is True
    client.messages.create.assert_called_once()


# --- cost and control ---------------------------------------------------------


def test_a_number_is_looked_up_once(monkeypatch):
    """Line type never changes, and a salon's callers repeat."""
    client = _twilio(monkeypatch, line_type="mobile")
    _send()
    _send()
    _send()
    assert client.lookups.v2.phone_numbers.call_count == 1
    assert client.messages.create.call_count == 3


def test_the_lookup_can_be_switched_off(monkeypatch):
    monkeypatch.setenv("VOICE_SMS_LINE_TYPE_LOOKUP", "0")
    client = _twilio(monkeypatch, line_type="landline")
    assert _send() is True, "disabled means behave exactly as before"
    client.lookups.v2.phone_numbers.assert_not_called()
    client.messages.create.assert_called_once()


def test_it_is_on_by_default():
    assert sms_service.line_type_lookup_enabled() is True
