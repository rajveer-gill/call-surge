"""A caller hanging up mid-sentence must not raise.

Every such call on production produced:

    RuntimeError: Cannot call "send" once a close message has been sent.
      media_ws_stream.py:154 in _speak -> await self._send_media(fr)

Six of them in one evening — a full traceback for the most ordinary thing a caller
can do. Nothing was broken for the caller, who had already gone, but an un-awaited
task exception per hangup buries the errors that do matter, and the speak loop died
wherever it happened to be instead of finishing tidily.
"""
import asyncio

import pytest

from voice.media_ws_stream import _BidiSession


class _ClosedWS:
    """A socket Twilio has already closed, which is what a hangup leaves behind."""

    def __init__(self):
        self.sends = 0

    async def send_text(self, _payload):
        self.sends += 1
        raise RuntimeError('Cannot call "send" once a close message has been sent.')


class _BrokenWS:
    async def send_text(self, _payload):
        raise RuntimeError("some other failure entirely")


def _session(ws):
    s = _BidiSession.__new__(_BidiSession)
    s.ws = ws
    s.call_sid = "CA_test"
    s.stream_sid = "MZ_test"
    s._closing = False
    return s


def test_sending_after_hangup_does_not_raise():
    s = _session(_ClosedWS())
    asyncio.run(s._send({"event": "media"}))  # must not raise
    assert s._closing is True


def test_it_stops_sending_rather_than_failing_per_frame():
    """The loop checks _closing, so one refused frame ends the stream instead of
    raising once for every remaining frame of the sentence."""
    ws = _ClosedWS()
    s = _session(ws)

    async def _many():
        for _ in range(5):
            await s._send({"event": "media"})

    asyncio.run(_many())
    assert ws.sends == 1, "should stop trying after the socket reports closed"


def test_a_real_failure_still_raises():
    """Only the socket being gone is tolerated. Swallowing everything here would
    hide genuine faults behind a caller who happened to hang up."""
    s = _session(_BrokenWS())
    with pytest.raises(RuntimeError, match="some other failure"):
        asyncio.run(s._send({"event": "media"}))


def test_nothing_is_sent_once_closing():
    ws = _ClosedWS()
    s = _session(ws)
    s._closing = True
    asyncio.run(s._send({"event": "media"}))
    assert ws.sends == 0
