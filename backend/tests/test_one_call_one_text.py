"""One call must not produce a thread of text messages.

From Lana's first test call, 2026-08-28: "After it tells me that I will get a text with
the details, it does not disconnect the call. It continues to repeat itself, and I
received 5 text messages."

Every amendment supersedes the draft and writes a new appointment row, and each row
texted the caller again. The body-level dedupe only catches the IDENTICAL repeat; five
slightly different messages about one haircut all got through.

The rule now: one text when the request is taken, and — only if something changed after
that — one more when the call ends, carrying the final details. Never a stream mid-call.
"""

import conversation_service as cs


APT = {
    "id": 501,
    "name": "Lana",
    "phone": "+19255550184",
    "date": "2026-09-02",
    "time": "14:00",
    "reason": "Haircut",
    "status": "pending_review",
}


def _reset():
    cs._SENT_CONFIRMATIONS.clear()
    cs._CALL_TEXT_COUNTS.clear()


def _wire(monkeypatch, sent, *, ok=True, detail=None):
    def _send(to, body, from_override=None, detail_out=None, **kw):
        sent.append((to, body))
        if detail_out is not None and detail:
            detail_out.update(detail)
        return ok

    monkeypatch.setattr(cs.sms_service, "send_sms", _send)
    monkeypatch.setattr(cs.runtime, "USE_DB", False)
    monkeypatch.setattr(cs.booking_service, "_tenant_sms_from_number", lambda: "+15550001111")
    monkeypatch.setattr(cs.caller_memory, "update_caller_memory", lambda *a, **k: None)
    monkeypatch.setattr(cs.caller_memory, "get_caller_memory", lambda *a, **k: {})
    monkeypatch.setattr(cs.voice_service, "_merge_call_session", lambda *a, **k: None)


def _call_data():
    return {"from_number": "+19255550184", "to_number": "+15550001111", "client_id": "t1"}


def test_the_first_text_goes_out(monkeypatch):
    _reset()
    sent: list = []
    _wire(monkeypatch, sent)
    spoken = cs._send_booking_confirmation_sms(APT, _call_data(), "t1", "CA_1")
    assert len(sent) == 1
    assert "texted you the details" in spoken


def test_an_amendment_mid_call_is_spoken_not_texted(monkeypatch):
    """The caller moves the time seconds later. They are told what happened; their phone
    stays quiet until the call is over."""
    _reset()
    sent: list = []
    _wire(monkeypatch, sent)
    call_data = _call_data()
    cs._send_booking_confirmation_sms(APT, call_data, "t1", "CA_1")
    spoken = cs._send_booking_confirmation_sms(
        {**APT, "id": 502, "time": "15:00"}, call_data, "t1", "CA_1"
    )
    assert len(sent) == 1, "the amendment must not send a second text mid-call"
    assert "final details" in spoken
    assert call_data["confirmation_text_deferred_apt_id"] == 502


def test_the_final_details_are_texted_once_when_the_call_ends(monkeypatch):
    _reset()
    sent: list = []
    _wire(monkeypatch, sent)
    call_data = _call_data()
    cs._send_booking_confirmation_sms(APT, call_data, "t1", "CA_1")
    cs._send_booking_confirmation_sms(
        {**APT, "id": 502, "time": "15:00"}, call_data, "t1", "CA_1"
    )
    monkeypatch.setattr(
        cs.runtime, "appointments", [{**APT, "id": 502, "time": "15:00"}], raising=False
    )
    assert cs.flush_deferred_confirmation_sms(call_data, "CA_1") is True
    assert len(sent) == 2
    assert "3:00 PM" in sent[-1][1]


def test_nothing_is_flushed_when_nothing_was_deferred(monkeypatch):
    _reset()
    sent: list = []
    _wire(monkeypatch, sent)
    call_data = _call_data()
    cs._send_booking_confirmation_sms(APT, call_data, "t1", "CA_1")
    assert cs.flush_deferred_confirmation_sms(call_data, "CA_1") is False
    assert len(sent) == 1


def test_a_cancelled_request_is_not_texted_at_the_end(monkeypatch):
    _reset()
    sent: list = []
    _wire(monkeypatch, sent)
    call_data = _call_data()
    cs._send_booking_confirmation_sms(APT, call_data, "t1", "CA_1")
    cs._send_booking_confirmation_sms({**APT, "id": 502, "time": "15:00"}, call_data, "t1", "CA_1")
    monkeypatch.setattr(
        cs.runtime,
        "appointments",
        [{**APT, "id": 502, "time": "15:00", "status": "cancelled"}],
        raising=False,
    )
    assert cs.flush_deferred_confirmation_sms(call_data, "CA_1") is False
    assert len(sent) == 1


def test_five_amendments_still_produce_at_most_two_texts(monkeypatch):
    """Lana's call, replayed."""
    _reset()
    sent: list = []
    _wire(monkeypatch, sent)
    call_data = _call_data()
    for i, hhmm in enumerate(["14:00", "15:00", "15:30", "16:00", "16:30"]):
        cs._send_booking_confirmation_sms(
            {**APT, "id": 500 + i, "time": hhmm}, call_data, "t1", "CA_1"
        )
    monkeypatch.setattr(
        cs.runtime, "appointments", [{**APT, "id": 504, "time": "16:30"}], raising=False
    )
    cs.flush_deferred_confirmation_sms(call_data, "CA_1")
    assert len(sent) == 2
    assert "4:30 PM" in sent[-1][1], "the last text must carry the details as they ended up"


def test_a_second_call_starts_with_a_fresh_budget(monkeypatch):
    _reset()
    sent: list = []
    _wire(monkeypatch, sent)
    cs._send_booking_confirmation_sms(APT, _call_data(), "t1", "CA_1")
    cs._send_booking_confirmation_sms(APT, _call_data(), "t1", "CA_2")
    assert len(sent) == 2


def test_the_budget_is_configurable(monkeypatch):
    monkeypatch.setenv("MAX_BOOKING_TEXTS_PER_CALL", "4")
    assert cs._booking_texts_per_call_limit() == 4
    assert cs._in_call_booking_text_budget() == 3
    monkeypatch.setenv("MAX_BOOKING_TEXTS_PER_CALL", "not a number")
    assert cs._booking_texts_per_call_limit() == cs.DEFAULT_BOOKING_TEXTS_PER_CALL
    monkeypatch.setenv("MAX_BOOKING_TEXTS_PER_CALL", "0")
    assert cs._booking_texts_per_call_limit() == 1, "one text is the floor"


def test_a_failed_send_does_not_spend_the_budget(monkeypatch):
    """The caller has nothing in their hand, so the retry on the next turn must go out."""
    _reset()
    sent: list = []
    _wire(monkeypatch, sent, ok=False)
    call_data = _call_data()
    cs._send_booking_confirmation_sms(APT, call_data, "t1", "CA_1")
    cs._send_booking_confirmation_sms(
        {**APT, "id": 502, "time": "15:00"}, call_data, "t1", "CA_1"
    )
    assert len(sent) == 2


# --- landlines -----------------------------------------------------------------


def test_a_landline_caller_is_told_the_truth(monkeypatch):
    """"What happens if someone calls from a landline?" — Twilio rejects the message as
    undeliverable, so promising a text is a promise we already know is broken."""
    _reset()
    sent: list = []
    _wire(monkeypatch, sent, ok=False, detail={"not_textable": True, "error_code": 21614})
    stored: list = []
    monkeypatch.setattr(cs, "_store_caller_message", lambda cd, body: stored.append(body) or True)
    spoken = cs._send_booking_confirmation_sms(APT, _call_data(), "t1", "CA_1")
    assert "can't receive texts" in spoken
    assert "call you" in spoken
    assert stored and "landline" in stored[0]


def test_a_landline_does_not_leave_a_text_pending_at_the_end(monkeypatch):
    _reset()
    sent: list = []
    _wire(monkeypatch, sent, ok=False, detail={"not_textable": True, "error_code": 21614})
    monkeypatch.setattr(cs, "_store_caller_message", lambda cd, body: True)
    call_data = _call_data()
    cs._send_booking_confirmation_sms(APT, call_data, "t1", "CA_1")
    assert "confirmation_text_deferred_apt_id" not in call_data
