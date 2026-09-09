"""Answering a menu is not the same as hearing ourselves back.

A caller on go-live day, 2026-09-09, four minutes into a call that produced no booking:

    18:34:17  ai   'Great! We offer "Shampoo & Haircut" or "Shampoo, Haircut & Blow Dry."
                    Which one would you prefer?'
    18:34:20  echo_discarded | DISCARDED: "Shampoo, haircut, and blow dry."
    18:34:46  her  "Are you there?"          <- 26 seconds of silence
    18:34:47  ai   asks the identical question again
    18:35:11  her  "Blow dry."               <- she gave up and shortened it

She answered correctly and the echo guard ate it, because a caller picking an option off
a menu says the menu back: her words were ~100% ours, which is the entire test. The guard
was inverted exactly where it was most likely to fire.

Overlap on its own cannot separate the two. What can is that echo is an acoustic copy —
our words, our order, nothing inserted. She said "and" where we wrote "&", which no
recording of us would do. So a discard now also requires the transcript to be an unbroken
run of what we just said.

This case was found because `3e0a248` started logging the discarded text the night
before, on the grounds that the guard "only exists because we started buffering talkover,
and if it has a case I have not found, it silently eats callers."
"""
from voice.media_ws_stream import _BidiSession, _is_contiguous_run

GREETING = (
    "Hi, I'm Ava. Thank you for calling Gig Harbor Hair Masters. "
    "This call may be recorded for quality and training. How can I help you today?"
)
MENU = (
    'Great! We offer "Shampoo & Haircut" or "Shampoo, Haircut & Blow Dry." '
    "Which one would you prefer?"
)


def _session(last_spoken: str) -> _BidiSession:
    s = _BidiSession.__new__(_BidiSession)
    s._last_spoken_text = last_spoken
    return s


def test_her_answer_survives():
    assert _session(MENU)._looks_like_our_own_echo("Shampoo, haircut, and blow dry.") is False


def test_other_ways_of_picking_from_the_menu_survive():
    for said in (
        "I'd like the shampoo haircut and blow dry",
        "shampoo haircut blow dry and style please",
        "can I get a shampoo and haircut",
        "the shampoo, haircut and blow dry one",
    ):
        assert _session(MENU)._looks_like_our_own_echo(said) is False, said


def test_real_echo_of_the_greeting_is_still_caught():
    """The case the guard exists for: our own voice off a speakerphone."""
    for echoed in (
        "this call may be recorded for quality",
        "thank you for calling gig harbor hair masters",
        "how can I help you today",
        "this call may be",
    ):
        assert _session(GREETING)._looks_like_our_own_echo(echoed) is True, echoed


def test_echo_of_the_menu_itself_is_still_caught():
    assert _session(MENU)._looks_like_our_own_echo("which one would you prefer") is True


def test_a_short_reply_is_never_echo():
    """Unchanged: "yes, Tuesday" inside a sentence we just said must reach the brain."""
    for said in ("yes", "blow dry", "one moment", "shampoo haircut"):
        assert _session(MENU)._looks_like_our_own_echo(said) is False, said


def test_unrelated_speech_is_never_echo():
    assert _session(MENU)._looks_like_our_own_echo("I need to reschedule my Tuesday") is False


def test_nothing_spoken_yet_means_nothing_to_echo():
    assert _session("")._looks_like_our_own_echo("shampoo haircut and blow dry") is False


class TestContiguousRun:
    def test_finds_a_run(self):
        assert _is_contiguous_run(["b", "c"], ["a", "b", "c", "d"]) is True

    def test_rejects_an_inserted_word(self):
        # Exactly her case: "and" between words we said next to each other.
        assert _is_contiguous_run(["b", "and", "c"], ["a", "b", "c", "d"]) is False

    def test_rejects_reordering(self):
        assert _is_contiguous_run(["c", "b"], ["a", "b", "c"]) is False

    def test_rejects_a_needle_longer_than_the_haystack(self):
        assert _is_contiguous_run(["a", "b", "c"], ["a", "b"]) is False

    def test_matches_a_later_occurrence(self):
        """The first word can appear before the real run — "shampoo" occurs twice in the
        menu, and the match must not be abandoned on the first near miss."""
        assert _is_contiguous_run(["b", "c"], ["b", "x", "b", "c"]) is True

    def test_empty_needle_is_not_a_run(self):
        assert _is_contiguous_run([], ["a"]) is False
