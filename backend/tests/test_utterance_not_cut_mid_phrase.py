"""A caller is not cut off on a word that cannot end a sentence.

Raj's test call, 2026-09-09. He said one sentence, in one breath:

    "Hi, I'd like to book a shampoo and haircut on Friday at 11AM with Terrance."

What the receptionist heard:

    05:41:25  caller_said  "Hi. I'd like to book a shampoo and haircut on Friday at 11AM with"
    05:41:26  ai_said      "...Which stylist would you like for your shampoo and haircut?"
    05:41:41  bidi_reply_spoken       frames=687          <- 15 seconds of her talking
    05:41:42  caller_said  "Terrence."                    <- his own tail, handed back 16s later
    05:41:43  ai_said      "Terrance isn't available on Friday."
    05:41:59  call_end     appointment_created=False      <- he hung up

Deepgram endpoints on breath, not grammar. He took a beat before the name and the turn was
committed on "with" — a preposition, which cannot be the last word of a sentence, so the
turn was demonstrably not over. She then asked for the stylist he had just named, and his
own trailing word came back as a new turn a quarter of a minute later.

The rule here is narrow on purpose: only a closed list of function words, and only two
extra debounce windows. Being wrong costs a few hundred milliseconds; the bug it replaces
cost a booking.
"""
import asyncio

import pytest

from voice.media_ws import _MAX_DANGLING_EXTENSIONS, _UtteranceCollector


DEBOUNCE = 0.01


def _collector(committed: list):
    c = _UtteranceCollector(
        call_sid="CAtest",
        base_url="https://example.test",
        debounce_sec=DEBOUNCE,
        twilio_client=None,
        websocket=None,  # type: ignore[arg-type]
    )

    async def _fake_commit() -> None:
        c._committed = True
        committed.append(c.transcript()[0])

    c.commit_now = _fake_commit  # type: ignore[method-assign]
    return c


async def _settle(seconds: float) -> None:
    """Let the debounce task run without depending on wall-clock precision."""
    await asyncio.sleep(seconds)


@pytest.mark.asyncio
async def test_the_call_that_prompted_this(monkeypatch):
    """His exact transcript: the tail must join the same turn, not become the next one."""
    done: list = []
    c = _collector(done)
    c.on_final_segment("Hi. I'd like to book a shampoo and haircut on Friday at 11AM with", 0.9)
    # One debounce elapses with nothing further — previously this committed on "with".
    await _settle(DEBOUNCE * 1.6)
    assert done == [], "committed on a preposition"
    # He finishes the sentence inside the extra window he has now been given.
    c.on_partial("Terrance.", 0.9)
    await _settle(DEBOUNCE * 3)
    assert done == [
        "Hi. I'd like to book a shampoo and haircut on Friday at 11AM with Terrance."
    ]


@pytest.mark.asyncio
async def test_a_finished_sentence_is_not_delayed():
    """The common case must be untouched — this runs on every turn of every call."""
    done: list = []
    c = _collector(done)
    c.on_final_segment("I'd like to book a haircut.", 0.9)
    await _settle(DEBOUNCE * 1.6)
    assert done == ["I'd like to book a haircut."]


@pytest.mark.asyncio
async def test_a_caller_who_really_does_trail_off_is_still_answered():
    """Silence after "with" must not hang the line. Bounded, then committed."""
    done: list = []
    c = _collector(done)
    c.on_final_segment("I want to book with", 0.9)
    await _settle(DEBOUNCE * (_MAX_DANGLING_EXTENSIONS + 3))
    assert done == ["I want to book with"]


@pytest.mark.asyncio
async def test_each_extension_is_re_evaluated():
    """Two dangling pauses in a row still resolve once the sentence completes."""
    done: list = []
    c = _collector(done)
    c.on_final_segment("Book me in for", 0.9)
    await _settle(DEBOUNCE * 1.6)
    assert done == []
    c.on_partial("a", 0.9)  # still dangling — an article
    await _settle(DEBOUNCE * 1.6)
    assert done == []
    c.on_partial("a trim.", 0.9)
    await _settle(DEBOUNCE * 3)
    assert done == ["Book me in for a trim."]


@pytest.mark.asyncio
async def test_trailing_punctuation_does_not_hide_the_dangling_word():
    """Deepgram sometimes punctuates its guess. "with." is still mid-phrase."""
    done: list = []
    c = _collector(done)
    c.on_final_segment("a haircut with.", 0.9)
    await _settle(DEBOUNCE * 1.6)
    assert done == []


@pytest.mark.asyncio
async def test_a_name_ending_in_a_dangling_word_is_not_matched():
    """Only the LAST word counts. "Terrance" ends the sentence even though "with" is in it."""
    done: list = []
    c = _collector(done)
    c.on_final_segment("a haircut with Terrance", 0.9)
    await _settle(DEBOUNCE * 1.6)
    assert done == ["a haircut with Terrance"]


@pytest.mark.asyncio
async def test_one_word_answers_still_commit_immediately():
    """"Yes." and "Terrance." are whole turns. Delaying these would be felt on every call."""
    for word in ("Yes.", "Terrance.", "Sure.", "Tomorrow."):
        done: list = []
        c = _collector(done)
        c.on_final_segment(word, 0.9)
        await _settle(DEBOUNCE * 1.6)
        assert done == [word], f"{word} was held"
