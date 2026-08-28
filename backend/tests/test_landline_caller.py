"""What happens when someone calls from a landline.

Lana's last question, 2026-08-28: "What happens if someone calls from a landline?"

Everything after the booking is a text message: the confirmation, the details, and (for
internal stores) the YES that reserves the slot. Twilio rejects the send with error
21614 — the number cannot receive SMS — and the old path retried it three times, waited
out two backoff sleeps in the middle of a live call, and then told the caller the same
thing it tells anyone whose text failed for a moment.

A landline is not a transient failure. It fails immediately, the caller is told the
truth, and the shop gets a message telling them to call back.
"""

import sms_service


class _NotAMobile(Exception):
    """Shaped like twilio.base.exceptions.TwilioRestException."""

    def __init__(self, code=21614):
        super().__init__("The 'To' number is not a valid mobile number")
        self.code = code


class _Transient(Exception):
    def __init__(self):
        super().__init__("connection reset")
        self.code = 30001


class _FakeMessages:
    def __init__(self, error):
        self.error = error
        self.attempts = 0

    def create(self, **kwargs):
        self.attempts += 1
        raise self.error


class _FakeClient:
    def __init__(self, error):
        self.messages = _FakeMessages(error)


def _wire(monkeypatch, error):
    client = _FakeClient(error)
    monkeypatch.setattr(sms_service.runtime, "twilio_client", client, raising=False)
    monkeypatch.setattr(sms_service.runtime, "USE_DB", False, raising=False)
    monkeypatch.setattr(sms_service.deps, "audit_log", lambda *a, **k: None)
    monkeypatch.setattr(sms_service.time, "sleep", lambda *_: None)
    return client


def test_a_landline_fails_once_and_says_why(monkeypatch):
    client = _wire(monkeypatch, _NotAMobile())
    detail: dict = {}
    ok = sms_service.send_sms(
        "+14155550101", "hi", from_override="+15550001111", detail_out=detail
    )
    assert ok is False
    assert detail["not_textable"] is True
    assert detail["error_code"] == 21614
    assert client.messages.attempts == 1, "retrying cannot make a landline textable"


def test_a_real_hiccup_is_still_retried(monkeypatch):
    client = _wire(monkeypatch, _Transient())
    detail: dict = {}
    ok = sms_service.send_sms(
        "+14155550101", "hi", from_override="+15550001111", detail_out=detail
    )
    assert ok is False
    assert client.messages.attempts == 3
    assert detail.get("not_textable") is None


def test_callers_without_a_detail_dict_are_unaffected(monkeypatch):
    """Every other send_sms caller passes no detail_out and must keep working."""
    _wire(monkeypatch, _NotAMobile())
    assert sms_service.send_sms("+14155550101", "hi", from_override="+15550001111") is False


def test_every_undeliverable_code_is_covered():
    for code in (21211, 21408, 21612, 21614):
        assert code in sms_service.NON_TEXTABLE_ERROR_CODES
