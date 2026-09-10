"""Answering "what's your name?" with a stylist's name is not asking for that stylist.

A real customer, 2026-09-10, 176 seconds into a booking:

    15:56:43  ai   "...I'll put in a request for you today at 4 PM with any available
                    stylist. What's your name?"
    15:56:55  her  "Melissa."
    15:56:56  ai   "Melissa isn't available today, but she works on Tuesday, Wednesday,
                    and Saturday. Would any of those days work?"
    15:57:07  her  "Melissa."
    15:57:08  ai   "Melissa isn't available today. She works on Tuesday, Wednesday..."
    15:57:10  her  "No. My name is Melissa."
    15:57:12  ai   "Thanks, Melissa. For the Shampoo & Haircut, Melissa isn't available
                    today. Would you like Tuesday, Wednesday, or Saturday...?"
    15:57:29  ai   BOOKING: Melissa| | |2026-09-10|4 PM|Shampoo & Haircut|Terrance

The booking came out right — her name in field 1, Terrance as the stylist. The two and a
half minutes to get there did not. The stylist extractors scan the caller's words for any
roster name with no notion of what question was just asked, so her answer read as a
stylist request, and the AVAILABILITY FACT then asserted it as authoritative — past her
own correction, twice.

Lana Anderberg the same morning: "It is still repeating the same questions too often. This
is going to get people very frustrated."

The guard is narrow on purpose. Someone answering both at once — "Melissa, and can I have
Terrance at 3" — is still naming a stylist and must keep working.
"""
import conversation_service as cs

BIZ = {
    "name": "Gig Harbor Hair Masters",
    "hours": "Monday-Sunday: 9:00 AM - 5:00 PM",
    "services": [{"id": "svc", "name": "Shampoo & Haircut", "duration_minutes": 30}],
    "staff": [
        {"id": "s1", "name": "Melissa", "service_ids": [], "working_days": ["tue", "wed", "sat"]},
        {"id": "s2", "name": "Terrance", "service_ids": [], "working_days": ["wed", "thu", "sun"]},
    ],
}

ASKED_NAME = {"role": "assistant", "content": "What's your name?"}


def _history(*users_and_assistants):
    return list(users_and_assistants)


def test_her_answer_is_not_read_as_a_stylist_request():
    h = _history(
        {"role": "user", "content": "I'd like to schedule a haircut"},
        ASKED_NAME,
        {"role": "user", "content": "Melissa."},
    )
    assert cs._caller_was_giving_their_own_name(h, BIZ) is True


def test_her_correction_is_not_either():
    h = _history(ASKED_NAME, {"role": "user", "content": "No. My name is Melissa."})
    assert cs._caller_was_giving_their_own_name(h, BIZ) is True


def test_no_availability_fact_is_injected_for_her_name(monkeypatch):
    """The fact is what stated it as authoritative and overrode the model."""
    monkeypatch.setattr(cs.config_service, "get_business_info", lambda: BIZ)
    monkeypatch.setattr(cs, "_requested_date_from_spoken_text", lambda *a, **k: "2099-07-09")
    h = _history(
        {"role": "user", "content": "I would like to book a shampoo and haircut today"},
        ASKED_NAME,
        {"role": "user", "content": "Melissa."},
    )
    assert cs.stylist_day_availability_note(h, BIZ) is None
    assert cs.stylist_free_times_note(h, BIZ) is None


def test_naming_a_stylist_normally_still_works(monkeypatch):
    """The whole point of the fact. Nothing here answers a name question."""
    monkeypatch.setattr(cs.config_service, "get_business_info", lambda: BIZ)
    monkeypatch.setattr(cs, "_requested_date_from_spoken_text", lambda *a, **k: "2099-07-09")
    h = _history(
        {"role": "user", "content": "I would like to book a shampoo and haircut"},
        {"role": "assistant", "content": "Which stylist would you like?"},
        {"role": "user", "content": "Melissa please"},
    )
    assert cs._caller_was_giving_their_own_name(h, BIZ) is False
    assert cs.stylist_day_availability_note(h, BIZ) is not None


def test_answering_both_at_once_still_names_the_stylist():
    """"Melissa, and can I have Terrance at 3" is doing two things in one breath."""
    h = _history(
        ASKED_NAME,
        {"role": "user", "content": "Melissa, and can I have Terrance at 3 on Saturday"},
    )
    assert cs._caller_was_giving_their_own_name(h, BIZ) is False


def test_a_name_that_is_not_on_the_roster_is_irrelevant():
    """Only a collision with a stylist name can cause this confusion."""
    h = _history(ASKED_NAME, {"role": "user", "content": "Dev."})
    assert cs._caller_was_giving_their_own_name(h, BIZ) is False


def test_the_guard_needs_the_question_to_be_recent():
    """A stylist named several turns after the name question is a real request."""
    h = _history(
        ASKED_NAME,
        {"role": "user", "content": "Dev."},
        {"role": "assistant", "content": "Which day works for you?"},
        {"role": "user", "content": "Melissa"},
    )
    assert cs._caller_was_giving_their_own_name(h, BIZ) is False


def test_an_empty_or_missing_turn_is_safe():
    assert cs._caller_was_giving_their_own_name([], BIZ) is False
    assert cs._caller_was_giving_their_own_name([ASKED_NAME], BIZ) is False
