"""Every way of saying "it's done" has to be caught, not just the ones we thought of.

A caller on go-live day, 2026-09-09, from a number that was neither ours nor Lana's:

    18:36:04  ai   "The request for today at 3:00 PM will be passed on to the salon."
    18:36:17  ai   "Great, I'll put in a request for today at 3:00 PM..."
    18:36:34  ai   "I've noted your request ... today at 3:00 PM."
    18:37:18  ai   "I'll include that number for the confirmation. Your request for
                    today at 3:00 PM will be passed on to the salon."
    18:37:43  call_end  appointment_created=False

Four minutes. A service, a time, and a second phone number to be reached on. No BOOKING
line was ever emitted, so the salon had nothing, and the caller hung up believing they
had a 3 PM appointment request.

`91817ec` added a guard for exactly this and it did not fire: its verb list was
put in / sent / submitted / filed / placed / entered / logged, and the model said
"noted". The underlying reason the booking was never filed is that the receptionist
never asked for a name — but this guard exists precisely so a missed field cannot become
a false promise, and it has to hold whatever words the model reaches for.

The list is now deliberately generous. A false positive costs one turn of double-checking;
a miss costs a customer who believes they have an appointment.
"""
import conversation_service as cs


def test_the_sentence_that_got_through():
    assert cs._ai_implies_committed_booking(
        "I've noted your request for a Shampoo, Haircut & Blow Dry today at 3:00 PM."
    ) is True


def test_the_words_it_already_caught_still_catch():
    for said in (
        "I've put in your request for Thursday.",
        "I have sent your request to the salon.",
        "We've submitted your request.",
        "Your request has been filed.",
        "Your request is in.",
    ):
        assert cs._ai_implies_committed_booking(said) is True, said


def test_the_neighbours_of_noted():
    for verb, said in (
        ("recorded", "I've recorded your request for 3 PM."),
        ("saved", "I have saved your request."),
        ("added", "I've added your request for today."),
        ("taken down", "I've taken down your request."),
        ("written down", "I have written down your request."),
        ("passed on", "Your request has been passed on to the salon."),
        ("got", "I've got your request for a blow dry at 3."),
    ):
        assert cs._ai_implies_committed_booking(said) is True, verb


def test_saying_what_will_happen_is_still_allowed():
    """Future tense is a promise about the next step, not a claim it is already done —
    and it is what the receptionist legitimately says while still collecting details."""
    for said in (
        "I'll put in a request for today at 3:00 PM.",
        "The request will be passed on to the salon.",
        "I'm putting in a request for you rather than booking it.",
        "Just so you know, the salon will confirm the time with you.",
    ):
        assert cs._ai_implies_committed_booking(said) is False, said


def test_ordinary_replies_are_untouched():
    for said in (
        "Which service would you like?",
        "Terrance is free at 9:00 AM, 2:00 PM, or 5:00 PM.",
        "Can I have your name for the request?",
        "",
    ):
        assert cs._ai_implies_committed_booking(said) is False, said
