"""The receptionist must not ask for something the caller has already said.

Two failures from Lana's test calls, 2026-08-28:

    "I asked to book a haircut for a day that I knew would not be available. When I
     changed days, it had to ask what service I wanted again."

    "I got stuck in a loop of the AI asking what stylist I wanted when I had already
     said. This will cause a lot of frustration for our customers."

Both are the same shape: the answer is in the transcript, and the code asks anyway.
So the service and the stylist are read back out of what the caller said — with the
same matchers the booking validator uses — and, as a last resort, the stylist question
is capped so a caller can never be trapped answering it forever.
"""

import conversation_service as cs

BIZ = {
    "name": "Gill Salons",
    "services": [
        {"id": "svc_cut", "name": "Haircut", "duration_minutes": 30},
        {"id": "svc_color", "name": "All Over Color", "duration_minutes": 90},
    ],
    "staff": [
        {"id": "st_t", "name": "Terrance", "service_ids": []},
        {"id": "st_m", "name": "Melissa", "service_ids": []},
    ],
}


# --- reading the stylist back out of what they said ----------------------------


def test_a_named_stylist_resolves_to_the_roster():
    assert cs._staff_id_from_spoken_text("I'd like Terrance please", BIZ) == "st_t"


def test_a_misheard_name_still_resolves():
    """The roster spells it Terrance; the transcript came back "Terence"."""
    assert cs._staff_id_from_spoken_text("and with Terence.", BIZ) == "st_t"


def test_the_last_stylist_named_wins():
    """A caller who changes their mind names the new stylist last."""
    said = "I wanted Melissa. Actually, can I have Terrance instead?"
    assert cs._staff_id_from_spoken_text(said, BIZ) == "st_t"


def test_nobody_named_resolves_to_nobody():
    assert cs._staff_id_from_spoken_text("a haircut tomorrow at 2", BIZ) is None
    assert cs._staff_id_from_spoken_text("", BIZ) is None


def test_a_stylist_with_no_roster_id_cannot_be_resolved():
    """There is nothing to write into staff_id, so the caller is asked rather than
    silently booked with nobody."""
    biz = {"staff": [{"name": "Terrance"}]}
    assert cs._staff_id_from_spoken_text("with Terrance", biz) is None


# --- the loop breaker ----------------------------------------------------------


def _asked(n):
    return [
        {"role": "assistant", "content": "Which stylist would you like to see?"}
        for _ in range(n)
    ]


def test_asking_twice_is_enough():
    assert cs._stylist_asked_too_many_times(_asked(1)) is False
    assert cs._stylist_asked_too_many_times(_asked(2)) is True


def test_the_caller_answering_is_not_an_ask():
    history = _asked(1) + [{"role": "user", "content": "which stylist? Terrance."}]
    assert cs._stylist_asked_too_many_times(history) is False


# --- the validator ------------------------------------------------------------


def _booking(**over):
    b = {
        "name": "Lana",
        "phone": "+14155550123",
        "email": "",
        "date": "2099-07-09",
        "time": "14:00",
        "reason": "Haircut",
        "staff": "",
    }
    b.update(over)
    return b


def test_a_stylist_named_out_loud_is_not_asked_for_again(monkeypatch):
    """The model left the staff field empty, but the caller said the name — twice now.
    Reading it from the transcript is what stops the loop."""
    monkeypatch.setattr(cs.config_service, "get_business_info", lambda: BIZ)
    history = [
        {"role": "user", "content": "a haircut with Terrance"},
        {"role": "assistant", "content": "Which stylist would you like to see?"},
        {"role": "user", "content": "Terrance."},
    ]
    ok, msg, staff_id, service = cs._validate_booking_requirements(
        _booking(), BIZ, conversation_history=history
    )
    assert ok is True, msg
    assert staff_id == "st_t"
    assert service == "Haircut"


def test_the_resolved_stylist_is_written_back_into_the_booking(monkeypatch):
    """The appointment is created from the booking dict, which re-resolves the stylist
    from field 7. Passing the check without filling it in would file the request with no
    stylist — the caller said a name and the salon would never see it."""
    monkeypatch.setattr(cs.config_service, "get_business_info", lambda: BIZ)
    booking = _booking(staff="")
    history = [{"role": "user", "content": "a haircut with Terence"}]
    ok, _, _, _ = cs._validate_booking_requirements(
        booking, BIZ, conversation_history=history
    )
    assert ok is True
    assert booking["staff"] == "Terrance"
    assert cs.resolve_staff_id_from_booking_fragment(booking["staff"]) == "st_t"


def test_a_misheard_name_in_the_staff_field_still_resolves(monkeypatch):
    monkeypatch.setattr(cs.config_service, "get_business_info", lambda: BIZ)
    assert cs.resolve_staff_id_from_booking_fragment("Terence") == "st_t"
    assert cs.resolve_staff_id_from_booking_fragment("Priya") is None


def test_a_caller_who_has_said_nothing_is_still_asked(monkeypatch):
    monkeypatch.setattr(cs.config_service, "get_business_info", lambda: BIZ)
    history = [{"role": "user", "content": "a haircut on Thursday at 2"}]
    ok, msg, _, _ = cs._validate_booking_requirements(
        _booking(), BIZ, conversation_history=history
    )
    assert ok is False
    assert "which stylist" in (msg or "").lower()


def test_after_two_asks_the_request_goes_through_without_a_stylist(monkeypatch):
    """Whatever is stopping the answer from being read, a third ask will not fix it —
    and the caller cannot escape it. Take the request; the salon can assign someone."""
    monkeypatch.setattr(cs.config_service, "get_business_info", lambda: BIZ)
    history = [
        {"role": "user", "content": "a haircut please"},
        {"role": "assistant", "content": "Which stylist would you like to see?"},
        {"role": "user", "content": "I said, the one I always see"},
        {"role": "assistant", "content": "Which stylist would you like to see?"},
        {"role": "user", "content": "I already told you"},
    ]
    ok, msg, staff_id, _ = cs._validate_booking_requirements(
        _booking(), BIZ, conversation_history=history
    )
    assert ok is True, msg
    assert staff_id is None


def test_no_preference_still_books_with_nobody(monkeypatch):
    monkeypatch.setattr(cs.config_service, "get_business_info", lambda: BIZ)
    history = [{"role": "user", "content": "a haircut, anyone is fine"}]
    ok, msg, staff_id, _ = cs._validate_booking_requirements(
        _booking(), BIZ, conversation_history=history
    )
    assert ok is True, msg
    assert staff_id is None


def test_both_services_reach_the_reason_field(monkeypatch):
    monkeypatch.setattr(cs.config_service, "get_business_info", lambda: BIZ)
    history = [{"role": "user", "content": "a haircut and all over color with Terrance"}]
    ok, msg, staff_id, service = cs._validate_booking_requirements(
        _booking(reason="Haircut and All Over Color", staff="Terrance"),
        BIZ,
        conversation_history=history,
    )
    assert ok is True, msg
    assert service == "Haircut + All Over Color"
    assert staff_id == "st_t"


# --- what the model is told it already knows -----------------------------------


def test_changing_the_day_does_not_re_ask_the_service(monkeypatch):
    """Lana's first call: a haircut on a day that didn't work, then a different day, and
    the AI asked what service she wanted again. The model dropped the reason field; the
    transcript still had it."""
    monkeypatch.setattr(cs.config_service, "get_business_info", lambda: BIZ)
    history = [
        {"role": "user", "content": "I'd like a haircut with Terrance"},
        {"role": "assistant", "content": "Terrance doesn't work Wednesdays."},
        {"role": "user", "content": "Let's do Thursday then"},
    ]
    booking = _booking(reason="", staff="Terrance")
    ok, msg, _, service = cs._validate_booking_requirements(
        booking, BIZ, conversation_history=history
    )
    assert ok is True, msg
    assert service == "Haircut"
    assert booking["reason"] == "Haircut"


def test_a_price_question_earlier_does_not_get_added_to_the_booking(monkeypatch):
    """"How much is a haircut?" then "book me an all over color" is ONE service."""
    monkeypatch.setattr(cs.config_service, "get_business_info", lambda: BIZ)
    history = [
        {"role": "user", "content": "how much is a haircut?"},
        {"role": "assistant", "content": "A haircut is forty-five dollars."},
        {"role": "user", "content": "okay, book me an all over color with Terrance"},
    ]
    booking = _booking(reason="", staff="Terrance")
    ok, msg, _, service = cs._validate_booking_requirements(
        booking, BIZ, conversation_history=history
    )
    assert ok is True, msg
    assert service == "All Over Color"


def test_the_service_is_still_asked_for_when_they_never_said_one(monkeypatch):
    monkeypatch.setattr(cs.config_service, "get_business_info", lambda: BIZ)
    history = [{"role": "user", "content": "can I book something for Thursday at 2?"}]
    ok, msg, _, _ = cs._validate_booking_requirements(
        _booking(reason=""), BIZ, conversation_history=history
    )
    assert ok is False
    assert "service" in (msg or "").lower()


def test_the_recap_carries_the_service_and_stylist(monkeypatch):
    monkeypatch.setattr(cs.config_service, "get_business_info", lambda: BIZ)
    history = [
        {"role": "user", "content": "I'd like to book a haircut with Terrance"},
        {"role": "assistant", "content": "What day would you like?"},
        {"role": "user", "content": "Wednesday"},
        {"role": "assistant", "content": "Terrance doesn't work Wednesdays."},
        {"role": "user", "content": "Okay, how about Thursday then"},
    ]
    note = cs.booking_details_recap_note(history, BIZ)
    assert note is not None
    assert "Haircut" in note and "Terrance" in note
    assert "not ask for any of these again" in note.lower()
    assert "different day or time" in note.lower()


def test_no_recap_before_anyone_mentions_booking(monkeypatch):
    monkeypatch.setattr(cs.config_service, "get_business_info", lambda: BIZ)
    history = [{"role": "user", "content": "what time do you close?"}]
    assert cs.booking_details_recap_note(history, BIZ) is None


def test_no_preference_is_recapped_as_no_preference(monkeypatch):
    monkeypatch.setattr(cs.config_service, "get_business_info", lambda: BIZ)
    history = [{"role": "user", "content": "book a haircut, anyone is fine"}]
    note = cs.booking_details_recap_note(history, BIZ)
    assert note and "no preference" in note.lower()
