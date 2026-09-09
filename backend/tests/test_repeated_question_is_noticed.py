"""Saying the identical sentence twice in a row is counted, not assumed away.

Lana Anderberg, go-live morning 2026-09-09:

    "the receptionist is still repeating questions that have been answered, most often
     asking for the date after the caller has already said it"

Her 10:07 call:

    17:08:46  ai    "Could you please confirm the date you'd like for the
                     Shampoo & Haircut with Melissa at 1:00 PM?"
    17:08:55  Lana  "Shampoo and haircut."
    17:08:56  ai    "Could you please confirm the date you'd like...?"
    17:09:03  Lana  "A shampoo and haircut."
    17:09:05  ai    "Could you please confirm the date you'd like...?"

Three times, word for word. She was a turn behind from talking over it, so she kept
answering the previous question — and it kept replying with the same words.

The prompt now forbids this. Prompt rules alone have a poor record here: three rewrites
failed to stop invented stylist availability before the answer was computed in code. So
the occurrences are counted. If `ai_repeated_question` keeps showing up in production,
the rule is not holding and this needs enforcing in code instead.
"""
import conversation_service as cs

DATE_Q = "Could you please confirm the date you'd like for the Shampoo & Haircut with Melissa at 1:00 PM?"


def test_her_call_would_now_be_flagged():
    call = {}
    assert cs._note_repeated_question(call, DATE_Q) is False, "first ask is not a repeat"
    assert cs._note_repeated_question(call, DATE_Q) is True, "second, word for word"
    assert cs._note_repeated_question(call, DATE_Q) is True, "third"


def test_moving_the_conversation_on_is_not_a_repeat():
    call = {}
    cs._note_repeated_question(call, DATE_Q)
    assert cs._note_repeated_question(call, "Great. And your name for the request?") is False


def test_rephrasing_the_same_question_is_not_a_repeat():
    """This is exactly what the prompt now asks for, so it must not be flagged."""
    call = {}
    cs._note_repeated_question(call, DATE_Q)
    assert (
        cs._note_repeated_question(
            call, "Sorry, which day did you want — today, or later this week?"
        )
        is False
    )


def test_punctuation_and_case_do_not_hide_a_repeat():
    call = {}
    cs._note_repeated_question(call, "Which day would you like?")
    assert cs._note_repeated_question(call, "which day would you like") is True


def test_acknowledgements_are_left_alone():
    """"Got it." and "One moment." recur legitimately turn after turn. Only questions
    count — length is the wrong test, since "Which day would you like?" is five words
    and is squarely the bug."""
    for phrase in ("Got it.", "Sure!", "One moment.", "Of course, happy to help."):
        call = {}
        cs._note_repeated_question(call, phrase)
        assert cs._note_repeated_question(call, phrase) is False, phrase


def test_a_short_question_still_counts():
    call = {}
    cs._note_repeated_question(call, "What time?")
    assert cs._note_repeated_question(call, "What time?") is True


def test_an_empty_reply_is_not_a_repeat():
    call = {}
    cs._note_repeated_question(call, "")
    assert cs._note_repeated_question(call, "") is False


def test_each_call_is_tracked_separately():
    """State lives on call_data, so one caller's turn cannot flag another's."""
    a, b = {}, {}
    cs._note_repeated_question(a, DATE_Q)
    assert cs._note_repeated_question(b, DATE_Q) is False


def test_returning_to_a_question_later_is_not_flagged():
    """Only back-to-back repetition is the tell. Coming back to it after the caller has
    said something else is a normal conversation."""
    call = {}
    cs._note_repeated_question(call, DATE_Q)
    cs._note_repeated_question(call, "And which stylist would you like for that?")
    assert cs._note_repeated_question(call, DATE_Q) is False
