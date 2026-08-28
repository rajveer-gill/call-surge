"""A request must never sound like a booked appointment.

From Lana, 2026-08-28: "It does not make it clear enough that this is an appointment
request and not an actual appointment. Can we make it clearer that this is not an
actual appointment?"

Request mode already refused to say "booked" — but only after the fact. The caller went
through the whole conversation, gave a day and a time, and only at the end heard that
someone would confirm it. So the AI now says what it is doing BEFORE it takes the day,
and every later mention says request.
"""

import booking_service
import conversation_service as cs
from prompts.receptionist import build_system_prompt

REQUEST_BIZ = {
    "name": "HairMasters Olympia",
    "hours": "Mon-Sat 9 AM - 7 PM",
    "booking_mode": "external",
    "booking_provider_name": "Zenoti",
    "services": [{"id": "svc", "name": "Haircut", "duration_minutes": 30}],
    "staff": [{"id": "s1", "name": "Terrance", "service_ids": []}],
}


def test_the_ai_is_told_to_say_it_is_a_request_before_taking_the_day():
    p = build_system_prompt(business_info=REQUEST_BIZ, include_booked_slots=True)
    low = p.lower()
    assert "the first time you start taking day/time details" in low
    assert "before you ask for the day" in low
    assert "never \"your appointment\"" in low


def test_an_internal_store_is_not_given_the_request_wording():
    """Every other customer books for real and must not start apologising for it."""
    internal = {**REQUEST_BIZ, "booking_mode": "internal"}
    p = build_system_prompt(business_info=internal, include_booked_slots=True)
    assert "REQUEST ONLY" not in p


def test_the_spoken_confirmation_says_request_not_appointment():
    said = cs.post_booking_spoken_confirmation("pending_review", "texted")
    assert "request, not a confirmed appointment" in said
    assert "booked" not in said.lower()


def test_every_request_outcome_says_it_is_a_request():
    for outcome in ("texted", "sms_failed", "no_phone"):
        said = cs.post_booking_spoken_confirmation("pending_review", outcome).lower()
        assert "request" in said, outcome


def test_the_text_leads_with_not_booked_yet():
    apt = {
        "name": "Lana",
        "phone": "+14155550101",
        "date": "2026-09-02",
        "time": "14:00",
        "reason": "Haircut",
        "status": "pending_review",
    }
    msg = booking_service._format_appointment_details_confirmation_sms(apt)
    first_line = msg.splitlines()[0]
    assert "APPOINTMENT REQUEST" in first_line
    assert "nothing is booked yet" in first_line
    assert "YES or CONFIRM" not in msg, "there is no slot for the customer to reserve"


def test_the_internal_confirmation_text_is_unchanged():
    apt = {
        "name": "Lana",
        "phone": "+14155550101",
        "date": "2026-09-02",
        "time": "14:00",
        "reason": "Haircut",
        "status": "pending_customer",
    }
    msg = booking_service._format_appointment_details_confirmation_sms(apt)
    assert "NOT locked in until you text back YES" in msg
    assert "APPOINTMENT REQUEST" not in msg
