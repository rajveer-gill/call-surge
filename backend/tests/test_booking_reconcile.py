"""Unit tests for the end-of-call booking reconciliation safety net.

reconcile_booking_at_call_end() runs when a call ends with no appointment created but the
transcript shows the caller agreed to a booking (e.g. the model never emitted the BOOKING:
marker, or the caller hung up mid-turn). It must:
  - create the appointment + send the confirmation SMS when the booking validates, and
  - NEVER book past the stylist/shop schedule backstop (e.g. a stylist on a day off).
"""
import conversation_service as cs


def _agreed_history():
    return [
        {"role": "user", "content": "I'd like to book a haircut with Mia"},
        {"role": "assistant", "content": "Sure, what day works?"},
        {"role": "user", "content": "Monday at 10 please"},
    ]


def _patch_common(monkeypatch):
    monkeypatch.setattr(
        cs.config_service,
        "get_business_info",
        lambda: {
            "staff": [{"id": "s1", "name": "Mia", "working_days": ["mon", "wed", "fri"]}],
            "services": [{"id": "svc1", "name": "Cut"}],
        },
    )
    monkeypatch.setattr(
        cs.config_service, "staff_roster_ready_for_booking", lambda *a, **k: True
    )
    monkeypatch.setattr(cs.database, "set_request_client_id", lambda *a, **k: None)


def test_reconcile_books_and_texts_valid_agreed_booking(monkeypatch):
    _patch_common(monkeypatch)
    monkeypatch.setattr(
        cs,
        "_extract_booking_line_from_conversation",
        lambda *a, **k: {"name": "Sam", "date": "2026-07-06", "time": "10:00", "reason": "Cut", "staff": "Mia"},
    )
    monkeypatch.setattr(
        cs,
        "_validate_booking_requirements",
        lambda booking, conversation_history=None: (True, None, "s1", "Cut"),
    )
    calls = {}

    def _fake_create(booking, **k):
        calls["created"] = True
        return {"id": 42, "name": "Sam", "date": "2026-07-06", "time": "10:00", "reason": "Cut", "phone": "+15551234567"}

    monkeypatch.setattr(cs, "_create_appointment_from_booking", _fake_create)

    def _fake_sms(apt, call_data, cid, call_sid):
        calls["sms_apt_id"] = apt.get("id")
        return "texted"

    monkeypatch.setattr(cs, "_send_booking_confirmation_sms", _fake_sms)

    call_data = {
        "client_id": "test",
        "from_number": "+15551234567",
        "conversation_history": _agreed_history(),
    }
    assert cs.reconcile_booking_at_call_end(call_data, "CA1") is True
    assert call_data["appointment_created"] is True
    assert calls.get("created") is True
    assert calls.get("sms_apt_id") == 42


def test_reconcile_refuses_off_day_and_does_not_book(monkeypatch):
    _patch_common(monkeypatch)
    # Caller agreed to Thursday with Mia, who only works Mon/Wed/Fri: the backstop rejects it.
    monkeypatch.setattr(
        cs,
        "_extract_booking_line_from_conversation",
        lambda *a, **k: {"name": "Sam", "date": "2026-07-09", "time": "10:00", "reason": "Cut", "staff": "Mia"},
    )
    monkeypatch.setattr(
        cs,
        "_validate_booking_requirements",
        lambda booking, conversation_history=None: (False, "Mia doesn't work on Thursday.", "s1", "Cut"),
    )

    def _must_not_create(*a, **k):
        raise AssertionError("must not create an appointment for an off-day booking")

    def _must_not_sms(*a, **k):
        raise AssertionError("must not send a confirmation SMS when rejected")

    monkeypatch.setattr(cs, "_create_appointment_from_booking", _must_not_create)
    monkeypatch.setattr(cs, "_send_booking_confirmation_sms", _must_not_sms)

    call_data = {
        "client_id": "test",
        "from_number": "+15551234567",
        "conversation_history": _agreed_history(),
    }
    assert cs.reconcile_booking_at_call_end(call_data, "CA2") is False
    assert not call_data.get("appointment_created")


def test_reconcile_noop_when_no_booking_extracted(monkeypatch):
    _patch_common(monkeypatch)
    monkeypatch.setattr(cs, "_extract_booking_line_from_conversation", lambda *a, **k: None)

    def _must_not_create(*a, **k):
        raise AssertionError("must not create when nothing was extracted")

    monkeypatch.setattr(cs, "_create_appointment_from_booking", _must_not_create)

    call_data = {
        "client_id": "test",
        "from_number": "+15551234567",
        "conversation_history": _agreed_history(),
    }
    assert cs.reconcile_booking_at_call_end(call_data, "CA3") is False


def test_reconcile_noop_when_conversation_not_a_booking(monkeypatch):
    _patch_common(monkeypatch)

    def _must_not_extract(*a, **k):
        raise AssertionError("must not extract when the conversation isn't a booking")

    monkeypatch.setattr(cs, "_extract_booking_line_from_conversation", _must_not_extract)

    call_data = {
        "client_id": "test",
        "from_number": "+15551234567",
        "conversation_history": [{"role": "user", "content": "What are your hours today?"}],
    }
    assert cs.reconcile_booking_at_call_end(call_data, "CA4") is False


def test_reconcile_noop_when_already_booked(monkeypatch):
    def _must_not_extract(*a, **k):
        raise AssertionError("must not run when an appointment already exists")

    monkeypatch.setattr(cs, "_extract_booking_line_from_conversation", _must_not_extract)

    call_data = {
        "appointment_created": True,
        "conversation_history": _agreed_history(),
    }
    assert cs.reconcile_booking_at_call_end(call_data, "CA5") is False


import sms_appointment_updates as _sau


def _wire_voice_change(monkeypatch, *, existing, updater):
    monkeypatch.setattr(cs.runtime, "USE_DB", True)
    # The live lookup has to return BOTH unconfirmed statuses. The first version of this
    # fixture stubbed db_appointments_get_pending_by_phone, which in production returns
    # only pending_review rows, while the code then demanded pending_customer — so these
    # tests passed against a path that could never run on a real call.
    monkeypatch.setattr(
        cs.database, "db_appointments_get_by_phone_for_sms",
        lambda phone, client_id=None: (dict(existing) if existing else None),
    )
    monkeypatch.setattr(
        cs.config_service, "get_business_info",
        lambda: {"staff": [{"id": "s1", "name": "Tom"}], "services": [{"id": "svc1", "name": "Short Cut"}]},
    )
    monkeypatch.setattr(cs.config_service, "_normalize_service_entries", lambda s: s)
    monkeypatch.setattr(_sau, "apply_sms_appointment_detail_updates_from_bodies", updater)
    monkeypatch.setattr(cs.database, "db_appointments_update", lambda *a, **k: None)
    monkeypatch.setattr(cs.database, "db_appointments_get_by_id", lambda *a, **k: None)
    monkeypatch.setattr(cs.caller_memory, "update_caller_memory", lambda *a, **k: None)
    monkeypatch.setattr(cs.database, "db_appointments_update_active_name_by_phone", lambda *a, **k: 0)
    monkeypatch.setattr(cs.booking_service, "_reconcile_sms_appointment_slot_after_detail_change", lambda apt: None)
    sent = {}
    monkeypatch.setattr(
        cs, "_send_booking_confirmation_sms",
        lambda apt, cd, cid, sid, **kw: sent.setdefault("text", "Perfect — I've texted your updated confirmation. Text YES to confirm."),
    )
    return sent


def _voice_call_data():
    return {
        "appointment_created": True,
        "client_id": "test",
        "from_number": "+15551234567",
        "conversation_history": [{"role": "user", "content": "actually let's do 3 PM"}],
    }


def test_voice_change_applies_and_texts(monkeypatch):
    sent = _wire_voice_change(
        monkeypatch,
        existing={"id": 5, "status": "pending_customer", "date": "2026-07-06", "time": "14:00", "reason": "Short Cut", "staff_id": "s1"},
        updater=lambda bodies, apt, **k: ({**apt, "time": "15:00"}, ["time"]),
    )
    out = cs._apply_voice_detail_change_if_pending(_voice_call_data(), "CA1")
    assert out and "confirmation" in out.lower()  # spoke the fresh confirmation
    assert sent.get("text")  # updated text sent immediately (mid-call)


def test_voice_change_returns_rejection(monkeypatch):
    def updater(bodies, apt, **k):
        ro = k.get("rejection_out")
        if ro is not None:
            ro["message"] = "Tom isn't available that day. Want another day?"
            ro["reason"] = "unavailable"
        return (apt, [])

    sent = _wire_voice_change(
        monkeypatch,
        existing={"id": 5, "status": "pending_customer", "date": "2026-07-06", "time": "14:00", "reason": "Short Cut", "staff_id": "s1"},
        updater=updater,
    )
    out = cs._apply_voice_detail_change_if_pending(_voice_call_data(), "CA2")
    assert out == "Tom isn't available that day. Want another day?"  # truthful refusal spoken
    assert "text" not in sent  # no confirmation sent for a refused change


def test_voice_change_noop_returns_none(monkeypatch):
    sent = _wire_voice_change(
        monkeypatch,
        existing={"id": 5, "status": "pending_customer", "date": "2026-07-06", "time": "14:00", "reason": "Short Cut", "staff_id": "s1"},
        updater=lambda bodies, apt, **k: (apt, []),
    )
    assert cs._apply_voice_detail_change_if_pending(_voice_call_data(), "CA3") is None
    assert "text" not in sent  # don't re-text when nothing changed


def test_voice_change_skips_confirmed(monkeypatch):
    """Internally, pending_review means the customer already texted YES and the store is
    holding the slot. Nothing said on a later call rewrites that."""
    called = {"n": 0}

    def updater(bodies, apt, **k):
        called["n"] += 1
        return (apt, ["time"])

    _wire_voice_change(
        monkeypatch,
        existing={"id": 5, "status": "pending_review", "date": "2026-07-06", "time": "14:00", "reason": "Short Cut", "staff_id": "s1"},
        updater=updater,
    )
    monkeypatch.setattr(cs.config_service, "is_external_booking", lambda *a, **k: False)
    assert cs._apply_voice_detail_change_if_pending(_voice_call_data(), "CA4") is None
    assert called["n"] == 0  # never touch an already-confirmed appointment


def test_voice_change_applies_to_a_request_mode_row(monkeypatch):
    """In request mode pending_review is where every request STARTS — the salon hasn't
    looked at it yet. Treating it as confirmed is why "actually, make it Thursday" never
    reached the salon on Lana's test call."""
    sent = _wire_voice_change(
        monkeypatch,
        existing={"id": 5, "status": "pending_review", "date": "2026-07-06", "time": "14:00", "reason": "Short Cut", "staff_id": "s1"},
        updater=lambda bodies, apt, **k: ({**apt, "date": "2026-07-09"}, ["date"]),
    )
    monkeypatch.setattr(cs.config_service, "is_external_booking", lambda *a, **k: True)
    out = cs._apply_voice_detail_change_if_pending(_voice_call_data(), "CA5")
    assert out and sent.get("text")


def test_voice_change_ignores_a_question_mentioning_a_stylist(monkeypatch):
    # A caller with a pending Tom booking asking "does Andrew work Tuesdays?" must NOT silently
    # switch the appointment to Andrew — the change handler only fires on an explicit request.
    called = {"n": 0}

    def updater(bodies, apt, **k):
        called["n"] += 1
        return ({**apt, "staff_id": "s9"}, ["staff_id"])

    _wire_voice_change(
        monkeypatch,
        existing={"id": 5, "status": "pending_customer", "date": "2026-07-06", "time": "14:00", "reason": "Short Cut", "staff_id": "s1"},
        updater=updater,
    )
    cd = _voice_call_data()
    cd["conversation_history"] = [{"role": "user", "content": "Does Andrew work Tuesdays?"}]
    assert cs._apply_voice_detail_change_if_pending(cd, "CAq") is None
    assert called["n"] == 0  # updater never invoked — no cue word, treated as a question
