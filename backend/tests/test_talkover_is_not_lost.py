"""What a caller says over the AI is kept, not thrown away.

Lana's booking call on 2026-09-04 ran 3m53s. Her side of it, as the system heard it:

    "Oh,"          "highlight."          "Different stylist."

She was not being terse. From the recording she was talking over Ava rather than waiting,
and while the AI is speaking her microphone was not fed to Deepgram at all — so the start
of every sentence was dropped and only the tail that landed after the reply survived. The
receptionist then answered the fragment, and she repeated herself.

`interrupted=True` has never appeared once in production across 116 replies, because
_barge_in() is unreachable: there is no live transcript while speaking to trigger it.

The audio is now held and handed over the moment the reply ends. The AI still finishes its
sentence — this is not barge-in — but nothing the caller said goes missing.

Keeping the audio brings back the reason it was dropped: on speakerphone the AI's own voice
returns down the line and would be fed to the brain as if the caller had said it. A flushed
transcript that is mostly our own last sentence is discarded, with a floor on length so a
real "yes" is never mistaken for echo.
"""

import asyncio
from unittest.mock import MagicMock

import pytest

import voice.media_ws_stream as mws
from voice.media_ws_stream import (
    _ECHO_MIN_WORDS,
    _MAX_TALKOVER_FRAMES,
    _MAX_TALKOVER_SEC,
    _BidiSession,
)


def _session():
    s = _BidiSession.__new__(_BidiSession)
    s.call_sid = "CAtest"
    s.dg_ws = None
    s.speaking = False
    s._resume_listen_at = 0.0
    s._closing = False
    s._last_dg_activity = 0.0
    s._talkover = []
    s._last_spoken_text = ""
    s._finals = []
    s._interim = ""
    s._conf = 0.0
    s._commit_task = None
    s.debounce_sec = 0.01
    s.utterance_q = asyncio.Queue()
    return s


# --- the audio is kept ---------------------------------------------------------


@pytest.mark.asyncio
async def test_talkover_is_handed_to_deepgram_when_the_reply_ends():
    s = _session()
    sent: list[bytes] = []
    dg = MagicMock()

    async def send(b):
        sent.append(b)

    dg.send = send
    s.dg_ws = dg
    s._talkover = [b"aaa", b"bbb", b"ccc"]
    await s._flush_talkover()
    assert sent == [b"aaa", b"bbb", b"ccc"], "the caller's words were dropped"
    assert s._talkover == [], "buffer must not replay on the next turn"


@pytest.mark.asyncio
async def test_a_flush_with_no_socket_does_not_raise():
    s = _session()
    s._talkover = [b"aaa"]
    await s._flush_talkover()  # dg_ws is None — a caller mid-reconnect
    assert s._talkover == []


@pytest.mark.asyncio
async def test_a_failing_socket_does_not_kill_the_call():
    s = _session()
    dg = MagicMock()

    async def boom(_b):
        raise RuntimeError("deepgram gone")

    dg.send = boom
    s.dg_ws = dg
    s._talkover = [b"aaa"]
    await s._flush_talkover()  # logged, not raised


def test_the_buffer_is_bounded():
    """A television behind the caller must not grow this without limit."""
    assert _MAX_TALKOVER_FRAMES == int(_MAX_TALKOVER_SEC / mws._FRAME_SEC)
    assert 500 <= _MAX_TALKOVER_FRAMES <= 2000  # ~10-40s of audio


# --- the AI's own voice is not mistaken for the caller -------------------------


def test_our_own_sentence_coming_back_is_discarded():
    s = _session()
    s._last_spoken_text = (
        "We offer several types of highlights: mini foil, full highlight, "
        "partial highlight, cap highlights, or balayage. Which one would you like?"
    )
    assert s._looks_like_our_own_echo(
        "we offer several types of highlights mini foil full highlight"
    )


def test_a_real_answer_is_kept():
    s = _session()
    s._last_spoken_text = (
        "What day and time would you like to request for your full highlight with Melissa?"
    )
    for said in (
        "September 11 at 12PM",
        "Different stylist",
        "Can I get Terrance instead on Thursday",
        "actually make it a haircut and a blow dry",
    ):
        assert not s._looks_like_our_own_echo(said), said


def test_a_short_reply_is_never_treated_as_echo():
    """"yes" sits inside almost anything we just said. Losing it costs a booking."""
    s = _session()
    s._last_spoken_text = "Would you like Tuesday at two, yes or no?"
    for said in ("yes", "no", "Tuesday", "yes please"):
        assert not s._looks_like_our_own_echo(said), said
    assert len("yes please".split()) < _ECHO_MIN_WORDS


def test_nothing_spoken_yet_means_nothing_is_echo():
    s = _session()
    s._last_spoken_text = ""
    assert not s._looks_like_our_own_echo("I'd like to book a haircut with Terrance")


@pytest.mark.asyncio
async def test_an_echoed_transcript_never_reaches_the_brain():
    s = _session()
    s._last_spoken_text = "Which stylist would you prefer for your full highlight today?"
    s._finals = ["which stylist would you prefer for your full highlight"]
    await s._debounced_commit()
    assert s.utterance_q.empty(), "the AI answered its own voice"


@pytest.mark.asyncio
async def test_a_real_utterance_reaches_the_brain():
    s = _session()
    s._last_spoken_text = "Which stylist would you prefer for your full highlight today?"
    s._finals = ["September 10 at 9AM with Terrance please"]
    await s._debounced_commit()
    text, _conf = s.utterance_q.get_nowait()
    assert text == "September 10 at 9AM with Terrance please"
