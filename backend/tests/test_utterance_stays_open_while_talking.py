"""A caller who is still talking must not have their turn ended under them.

From a test call on 2026-09-03, one sentence committed after four words:

    00:55:49  caller_said  "Hi. This is Raj."

He was mid-way through "...I'd like to book a shampoo and haircut with Terrance on
Thursday at 11AM" when the turn closed.

The debounce restarted only on FINAL segments. Interim results stream continuously while
someone speaks and were ignored by the timer, so the window really meant "commit N ms
after the last final", not "commit after N ms of silence". A caller talking for longer
than the debounce without Deepgram happening to emit a final got cut off.

That also inverted the endpointing setting, which is what made it visible: raising
endpointing to 800ms made Deepgram emit finals LESS often, so the clock ran out mid
sentence and callers were cut off SOONER. Two attempts to fix this by moving timers made
it worse, because both were downstream of the thing that was actually wrong.

This is the turn-taking path on every call, so both directions are pinned here: speech
holds the turn open, and silence still ends it promptly.
"""

import asyncio

import pytest

from voice.media_ws import MAX_UTTERANCE_HOLD_SEC, _UtteranceCollector


class _Sock:
    def __init__(self):
        self.closed = False

    async def close(self):
        self.closed = True


def _collector(debounce_sec=0.05):
    c = _UtteranceCollector(
        call_sid="CAtest",
        base_url="https://example.test",
        debounce_sec=debounce_sec,
        twilio_client=None,
        websocket=_Sock(),
    )
    c.committed_text = None

    async def _capture():
        if c._committed:
            return
        c._committed = True
        c._cancel_debounce()
        c.committed_text = c.transcript()[0]

    c.commit_now = _capture  # don't touch Twilio or the turn pipeline
    return c


@pytest.mark.asyncio
async def test_ongoing_speech_holds_the_turn_open():
    """The live failure: a final lands, the caller keeps going, the rest must survive."""
    c = _collector(debounce_sec=0.05)
    c.on_final_segment("Hi. This is Raj.", 0.9)
    for words in (
        "I'd like",
        "I'd like to book a shampoo",
        "I'd like to book a shampoo and haircut with Terrance",
        "I'd like to book a shampoo and haircut with Terrance on Thursday at 11AM",
    ):
        await asyncio.sleep(0.03)  # still inside the debounce, still talking
        c.on_partial(words, 0.9)
    assert c.committed_text is None, "cut off while the caller was still speaking"
    await asyncio.sleep(0.12)
    assert c.committed_text is not None
    assert "Thursday at 11AM" in c.committed_text


@pytest.mark.asyncio
async def test_silence_still_ends_the_turn():
    c = _collector(debounce_sec=0.05)
    c.on_final_segment("Book me for Friday", 0.9)
    await asyncio.sleep(0.15)
    assert c.committed_text == "Book me for Friday"


@pytest.mark.asyncio
async def test_a_repeated_interim_does_not_hold_the_line_forever():
    """Deepgram repeats the same interim while it is still deciding. That is not speech."""
    c = _collector(debounce_sec=0.05)
    c.on_partial("uh", 0.5)
    for _ in range(6):
        await asyncio.sleep(0.02)
        c.on_partial("uh", 0.5)  # unchanged — must not extend
    await asyncio.sleep(0.12)
    assert c.committed_text == "uh"


@pytest.mark.asyncio
async def test_speech_with_no_final_at_all_still_commits():
    """Some turns never produce a final; transcript() falls back to the last interim."""
    c = _collector(debounce_sec=0.05)
    c.on_partial("just a haircut please", 0.8)
    await asyncio.sleep(0.15)
    assert c.committed_text == "just a haircut please"


@pytest.mark.asyncio
async def test_a_caller_who_never_pauses_is_eventually_answered(monkeypatch):
    """The backstop. Endless speech must not keep the receptionist listening forever.

    The cap is shortened rather than the clock faked: media_ws reads time.monotonic, which
    is the same clock asyncio schedules on, so patching it stops the event loop dead.
    """
    import voice.media_ws as mw

    monkeypatch.setattr(mw, "MAX_UTTERANCE_HOLD_SEC", 0.2)
    c = _collector(debounce_sec=0.05)
    for i in range(40):
        c.on_partial(f"and another thing number {i}", 0.9)
        if c.committed_text is not None:
            break
        await asyncio.sleep(0.02)  # never a gap long enough to end the turn
    await asyncio.sleep(0.12)
    assert c.committed_text is not None, "held open past the cap"


def test_the_cap_is_long_enough_for_a_real_sentence():
    assert MAX_UTTERANCE_HOLD_SEC >= 10


@pytest.mark.asyncio
async def test_nothing_arrives_after_the_turn_is_committed():
    c = _collector(debounce_sec=0.05)
    c.on_final_segment("Friday at two", 0.9)
    await asyncio.sleep(0.15)
    assert c.committed_text == "Friday at two"
    c.on_partial("please ignore me", 0.9)
    c.on_final_segment("and me", 0.9)
    await asyncio.sleep(0.1)
    assert c.committed_text == "Friday at two"
