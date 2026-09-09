"""Telling the caller apart from our own voice coming back off a speakerphone.

This is the whole risk in barge-in. Say yes too easily and the receptionist interrupts
herself on every speakerphone call, which is worse than the bug it fixes. Say no too
easily and nothing changes.

The case it exists for — Raj's call, 2026-09-09 06:36. He started talking during the
greeting, which is what everyone who has ever phoned a business does:

    06:36:19  caller_said  "Hi. I like to book"          <- 1s after the greeting ended
    06:36:29  caller_said  "Hi. I'd to book a haircut."  <- 0s after the next reply ended
    06:36:40  caller_said  "A haircut."                  <- 1s
    06:36:48  caller_said  "Shampoo and haircut."        <- 0s
    06:36:49  ai_said      "...preferred stylist, or is anyone fine?"  (asked twice)
    06:36:57  call_end     appointment_created=False

Every turn sat in the buffer, so he answered the previous question all the way down.
"""
import pytest

from voice.barge_detector import (
    BARGE_MIN_WORDS,
    barge_in_enabled,
    looks_like_caller,
)

GREETING = "Hi, I'm Ava. Thank you for calling Gig Harbor Hair Masters. How can I help you today?"
STYLIST_Q = "Sure, a Shampoo & Haircut. Do you have a preferred stylist, or is anyone fine?"


@pytest.mark.parametrize(
    "heard",
    [
        "Hi I'd like to book a shampoo and haircut",  # his opening line, over the greeting
        "I like to book",
        "a haircut on Friday",
        "yeah Terrance please",
        "can I get an appointment",
    ],
)
def test_a_caller_talking_over_the_greeting_is_heard(heard):
    assert looks_like_caller(heard, GREETING) is True


@pytest.mark.parametrize(
    "echo",
    [
        # Our own greeting coming back off a handset, in the fragments Deepgram emits.
        "Thank you for calling Gig Harbor",
        "how can I help you today",
        "Hi I'm Ava thank you for calling",
        "calling Gig Harbor Hair Masters",
    ],
)
def test_our_own_greeting_echoing_back_is_not(echo):
    assert looks_like_caller(echo, GREETING) is False


@pytest.mark.parametrize(
    "answered_in_our_words",
    [
        "preferred stylist",
        "anyone is fine",          # echoes "or is anyone fine" almost exactly
        "preferred stylist Terrance",  # one word of their own in three
    ],
)
def test_a_caller_answering_in_our_own_words_does_not_barge(answered_in_our_words):
    """A deliberate miss, and the direction the threshold is biased in.

    These are real callers answering — but they are answering using the words we just
    said, so we cannot separate them from a speakerphone repeating us. They do not barge.

    The cost is that this one turn plays to the end: today's behaviour exactly, with their
    words still buffered and still arriving whole. The cost of the opposite bias is the
    receptionist interrupting herself whenever a handset echoes her, which spoils every
    call rather than one turn. Missing a barge is recoverable; a false one is not.
    """
    assert looks_like_caller(answered_in_our_words, STYLIST_Q) is False


def test_a_caller_using_their_own_words_does_barge():
    """The common shape of a real answer: mostly words we did not just say."""
    for said in ("Terrance please", "uh yeah Terrance", "I'd like Terrance", "book me Friday"):
        assert looks_like_caller(said, STYLIST_Q) is True, said


def test_a_single_word_never_stops_the_reply():
    """A cough, an "mm-hm", one echoed word. Too little to cut somebody off mid-sentence."""
    for word in ("yeah", "hi", "um", "haircut"):
        assert looks_like_caller(word, GREETING) is False
    assert BARGE_MIN_WORDS >= 2


def test_silence_and_junk_are_ignored():
    for junk in ("", "   ", ".", "?!"):
        assert looks_like_caller(junk, GREETING) is False


def test_not_knowing_our_own_script_never_barges():
    """If we can't say what we're saying, we can't rule out echo — so we don't cut."""
    assert looks_like_caller("I'd like to book a haircut", "") is False


def test_the_kill_switch(monkeypatch):
    assert barge_in_enabled() is True
    for off in ("0", "false", "no", "off", "OFF"):
        monkeypatch.setenv("VOICE_BARGE_IN", off)
        assert barge_in_enabled() is False, off
    monkeypatch.setenv("VOICE_BARGE_IN", "1")
    assert barge_in_enabled() is True


def test_case_and_punctuation_do_not_change_the_answer():
    assert looks_like_caller("I'D LIKE TO BOOK!!", GREETING) is True
    assert looks_like_caller("How can I help you today?", GREETING) is False
