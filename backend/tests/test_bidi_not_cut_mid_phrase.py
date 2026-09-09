"""The streaming path must not commit a turn on a word a sentence cannot end on.

Raj, 2026-09-09, asking to be put through to the salon. Cut twice in one call:

    21:13:54  caller_said  "I like to be"
    21:13:55  ai_said      "It sounds like you're considering scheduling an appointment..."
    21:14:07  caller_said  "No. I'd like to"
    21:14:08  ai_said      "If you have something specific in mind, feel free to let me know!"
    21:14:20  caller_said  "No. I'd like to be transferred to this alone."
    21:14:20  forward_decision | matched_keyword=transferred
    21:14:23  outcome=forwarded

Neither fragment carried the word that triggers a transfer, so the model answered him
twice before the third attempt got through. He also heard it as the turn chime firing too
early — correctly: the chime plays at the commit, so a commit that lands mid-sentence is
heard as being cut off.

An identical guard exists in media_ws.py for the batch collector. Production runs the
BIDI path, so a fix applied only there would have changed nothing — these tests exist to
pin the behaviour to the path that actually serves calls.
"""
import asyncio

import pytest

from voice.media_ws_stream import _BidiSession, _MAX_DANGLING_EXTENSIONS


def _session(debounce: float = 0.01) -> _BidiSession:
    s = _BidiSession.__new__(_BidiSession)
    s.call_sid = "CAtest"
    s.debounce_sec = debounce
    s._finals, s._interim, s._conf = [], "", 0.0
    s._commit_task = None
    s._last_spoken_text = ""
    s.utterance_q = asyncio.Queue()
    return s


@pytest.mark.parametrize(
    "fragment",
    [
        "I like to be",            # his first cut
        "No. I'd like to",         # his second
        "a haircut with",
        "book me in for a",
        "can I speak to the",
    ],
)
def test_a_fragment_that_cannot_end_a_sentence_is_held(fragment):
    s = _session()
    s._finals = [fragment]
    assert s._ends_mid_phrase() is True


@pytest.mark.parametrize(
    "whole",
    [
        "No. I'd like to be transferred to this alone.",  # the one that got through
        "I'd like to book a shampoo and haircut",
        "Terrance",
        "Saturday at three o'clock",
        "anyone is fine",
    ],
)
def test_a_finished_sentence_is_not_held(whole):
    s = _session()
    s._finals = [whole]
    assert s._ends_mid_phrase() is False


def test_only_the_last_word_counts():
    """"with" appears mid-sentence in plenty of complete utterances."""
    s = _session()
    s._finals = ["a haircut with Terrance"]
    assert s._ends_mid_phrase() is False


def test_the_trailing_interim_is_considered_when_there_are_no_finals():
    s = _session()
    s._interim = "I'd like to be"
    assert s._ends_mid_phrase() is True


def test_nothing_heard_is_not_mid_phrase():
    s = _session()
    assert s._ends_mid_phrase() is False


@pytest.mark.asyncio
async def test_the_tail_joins_the_same_turn():
    """His call, end to end: the fragment waits, the rest arrives, one utterance."""
    s = _session(debounce=0.01)
    s._finals = ["I like to be"]
    s._commit_task = asyncio.create_task(s._debounced_commit())
    await asyncio.sleep(0.016)  # one debounce: previously committed here
    assert s.utterance_q.empty(), "committed on 'be'"
    s._finals = ["I like to be transferred to the salon."]
    await asyncio.sleep(0.05)
    text, _conf = await asyncio.wait_for(s.utterance_q.get(), timeout=0.5)
    assert text == "I like to be transferred to the salon."


@pytest.mark.asyncio
async def test_a_caller_who_really_stops_on_a_function_word_is_still_answered():
    """Silence after "to" must not hang the line. Bounded, then committed."""
    s = _session(debounce=0.01)
    s._finals = ["No. I'd like to"]
    s._commit_task = asyncio.create_task(s._debounced_commit())
    text, _conf = await asyncio.wait_for(s.utterance_q.get(), timeout=1.0)
    assert text == "No. I'd like to"


@pytest.mark.asyncio
async def test_a_complete_sentence_is_not_delayed():
    """The common case runs on every turn of every call and must be untouched."""
    s = _session(debounce=0.01)
    s._finals = ["I'd like to book a haircut."]
    s._commit_task = asyncio.create_task(s._debounced_commit())
    text, _conf = await asyncio.wait_for(s.utterance_q.get(), timeout=0.1)
    assert text == "I'd like to book a haircut."


def test_the_extension_is_bounded():
    assert _MAX_DANGLING_EXTENSIONS == 2


@pytest.mark.parametrize(
    "ending",
    [
        "Saturday at 10 AM",     # "am" the time, not "am" the verb
        "Thursday at 11AM",      # and with the digits attached, which the tokenizer drops
        "Thank you",             # said at the end of most calls
        "No",
        "Yes",
        "That's it",
        "I'm done",
        "anyone is fine",
    ],
)
def test_ordinary_ways_of_finishing_a_turn_are_not_held(ending):
    """The list is function words, but plenty of function words end real turns.

    "…on Thursday at 11AM" was held open by an earlier version of this list, because the
    word tokenizer drops digits and the remaining "AM" collided with the verb "am". A time
    is not an unfinished sentence, and neither is "thank you".
    """
    s = _session()
    s._finals = [ending]
    assert s._ends_mid_phrase() is False, ending
