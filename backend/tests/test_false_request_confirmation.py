"""Never tell a caller their request is in when nothing was filed.

2026-09-02, a live test call. No BOOKING line was emitted on any turn
(voice_booking_intent_no_marker fired on turns 2, 3 and 4), call_end_state recorded
appointment_created=False, and the caller was told:

    "Great! I've put in your request for a Shampoo & Haircut with Terrance on
     September 6th at 10:00 AM. The salon will confirm the time with you soon."

Nothing existed. This is worse than any refusal: a caller who is turned away knows to
ring someone else, and a caller who is told they are in the book simply doesn't.

_ai_implies_committed_booking already existed to catch precisely this, and missed it,
because every phrase it knew was BOOKING vocabulary — booked, all set, scheduled, put
you down. Request mode does not use that vocabulary; we taught it not to, because Lana
asked for it to be unmistakable that this is a request. So the guard had a hole shaped
exactly like our own wording change.

Tense is the whole distinction here. "I've put in your request" claims a thing that does
not exist. "I'm putting in a request for you rather than booking it" opens nearly every
reply the receptionist makes and is correct every time.
"""

import conversation_service as cs


def _fires(text: str) -> bool:
    return cs._ai_implies_committed_booking(text)


# --- the live failure ---------------------------------------------------------


def test_the_call_that_lied():
    assert _fires(
        "Great! I've put in your request for a Shampoo & Haircut with Terrance on "
        "September 6th at 10:00 AM. The salon will confirm the time with you soon."
    )


# --- other ways to claim a request exists -------------------------------------


def test_completed_request_claims_fire():
    for said in (
        "I've put in your request.",
        "I have put in your request for Thursday.",
        "We've put in the request for you.",
        "I've sent your request to the salon.",
        "I've submitted your request.",
        "I have filed your request.",
        "I've already put in your request.",
        "Your request is in.",
        "Your request has been sent to the salon.",
        "Your request has been submitted.",
    ):
        assert _fires(said), said


# --- the disclaimer that must never fire --------------------------------------


def test_the_standing_disclaimer_never_fires():
    """This opens nearly every reply. Firing here would break every call."""
    assert not _fires(
        "Just so you know, I'm putting in a request for you rather than booking it; "
        "the salon will confirm the time with you."
    )


def test_promises_about_what_happens_next_never_fire():
    """Said before the BOOKING line exists, which is exactly when they should be said."""
    for said in (
        "Thank you! Please share your name and I'll put in the request for today at 4:00 PM.",
        "I'll pass your request to the salon and they'll confirm the time for tomorrow at 2 PM.",
        "I'm putting in a request for you rather than booking it; the salon will confirm it.",
        "I will put in your request once I have your name.",
        "Would you like me to put in a request for that time?",
        "Melissa is available on Tuesday. I'll put in a request for 11:00 AM.",
    ):
        assert not _fires(said), said


def test_asking_for_details_never_fires():
    for said in (
        "What day and time would you like to come in?",
        "Which stylist would you prefer for your full highlight?",
        "Terrance doesn't work on Tuesdays. He is available on Thursday, Friday, or Sunday.",
        "",
    ):
        assert not _fires(said), said


# --- the booking vocabulary still works ---------------------------------------


def test_the_original_booking_claims_still_fire():
    for said in (
        "You're all set for Thursday at 2 PM.",
        "I've booked you in with Melissa.",
        "I've got you down for Friday.",
        "Perfect, I've got everything I need. We'll send a text to confirm your "
        "appointment for a long cut with Andrew on Tuesday.",
    ):
        assert _fires(said), said
