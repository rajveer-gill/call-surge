"""Decides one thing: has the caller started talking while the AI is speaking?

Why this is a second Deepgram stream rather than the one we already have
-----------------------------------------------------------------------
The main stream is deliberately deaf while the AI speaks (`_LISTEN_GUARD_SEC` and the
half-duplex gate in media_ws_stream), because on speakerphone the AI's own voice comes
back down the line and would be transcribed as the caller. Caller audio is buffered
instead and handed over whole when the reply ends.

That buffering is why nothing is lost today, and it must keep working exactly as it does.
So this detector never touches the transcript path: it gets a copy of the same frames, on
its own connection, and its only output is a single boolean handed to a callback. If it
is wrong, the AI stops talking when it shouldn't — a bad turn. If it were wired into the
main stream instead, being wrong would put the AI's own words into the caller's mouth,
which is a bad call and possibly a bad booking.

What it costs: a second concurrent Deepgram stream for the fraction of the call the AI is
speaking. Fine for one store; worth revisiting before this runs across fifty.

Why a transcript rather than energy
-----------------------------------
Loudness cannot separate the caller from the AI's echo without real echo cancellation —
on speakerphone the loudest thing on the line IS the AI. Words can: we know exactly what
the AI is currently saying, so a transcript that does NOT look like that script is the
caller. That is the same reasoning as `_looks_like_our_own_echo`, applied live.

From Raj's test call, 2026-09-09 06:36 — a caller who simply started talking during the
greeting, which is what everyone who has ever phoned a business does:

    06:36:19  caller_said  "Hi. I like to book"           <- 1s after the greeting ended
    06:36:29  caller_said  "Hi. I'd to book a haircut."   <- 0s after the next reply ended
    06:36:40  caller_said  "A haircut."                   <- 1s
    06:36:48  caller_said  "Shampoo and haircut."         <- 0s
    06:36:49  ai_said      "...Do you have a preferred stylist, or is anyone fine?"
                           (the identical question she had just asked)
    06:36:57  call_end     appointment_created=False

Every one of his turns spent the call in the buffer, so he was answering the previous
question all the way down and she never caught up.
"""

from __future__ import annotations

import asyncio
import os
import re
from typing import Any, Callable, Optional

from observability import voice_info, voice_warning
from voice.deepgram_bridge import connect_deepgram_listen, parse_deepgram_transcript_message

# Words needed before we will stop the AI mid-sentence. Two is enough to be a real
# utterance and short enough to react in about a second; one word invites a cough, a
# "mm-hm", or a single echoed word cutting the reply off.
BARGE_MIN_WORDS = 2
# Share of those words that must NOT appear in what the AI is currently saying. Echo
# scores near zero here by definition; a caller talking about their own booking scores
# near one. Set well above the noise rather than at a hair trigger, because the cost of
# firing wrongly is the AI interrupting itself.
BARGE_MIN_NOVEL = 0.5
# Nothing may barge in the first moments of a reply: the tail of the caller's own previous
# sentence can still be in flight, and cutting the reply on it would stop the AI answering
# the very thing it was asked.
BARGE_MIN_INTO_REPLY_SEC = 0.75


def barge_in_enabled() -> bool:
    """On unless explicitly disabled. The kill switch is an env var so this can be turned
    off with a deploy and no code change — Render only picks up env on deploy either way,
    but a flag says what happened where a revert only says someone panicked."""
    return (os.getenv("VOICE_BARGE_IN", "1") or "").strip().lower() not in (
        "0",
        "false",
        "no",
        "off",
    )


def looks_like_caller(text: str, ai_script: str) -> bool:
    """True when this transcript is somebody other than the AI reading its own reply."""
    words = re.findall(r"[a-z0-9']+", (text or "").lower())
    if len(words) < BARGE_MIN_WORDS:
        return False
    mine = set(re.findall(r"[a-z0-9']+", (ai_script or "").lower()))
    if not mine:
        # We don't know what we're saying, so we cannot rule out echo. Staying silent here
        # means the reply plays out — today's behaviour, which is safe.
        return False
    novel = sum(1 for w in words if w not in mine) / len(words)
    return novel >= BARGE_MIN_NOVEL


class BargeDetector:
    """One Deepgram stream, open for the duration of a single spoken reply.

    Every method swallows its own failures: a detector that cannot connect, or dies
    mid-reply, must leave the call behaving exactly as it did before this existed.
    """

    def __init__(self, *, call_sid: str, on_barge: Callable[[], Any]) -> None:
        self.call_sid = call_sid
        self._on_barge = on_barge
        self._ws: Optional[Any] = None
        self._reader: Optional[asyncio.Task[None]] = None
        self._script = ""
        self._started_at = 0.0
        self._fired = False
        self._stopped = False

    async def start(self, script: str) -> None:
        """Open the stream for a reply whose text is `script`. Never raises."""
        self._script = script or ""
        self._fired = False
        self._stopped = False
        self._started_at = asyncio.get_running_loop().time()
        try:
            self._ws = await connect_deepgram_listen()
        except Exception as e:
            # No detector this turn. The reply plays to the end, the caller's audio is
            # still buffered and still arrives whole — i.e. exactly the old behaviour.
            voice_warning(
                "barge_detector_connect_failed", call_sid=self.call_sid, error=str(e)[:120]
            )
            self._ws = None
            return
        self._reader = asyncio.create_task(self._read())

    async def feed(self, frame: bytes) -> None:
        if not self._ws or self._fired or self._stopped or not frame:
            return
        try:
            await self._ws.send(frame)
        except Exception:
            # A dead detector is not a dead call. Drop it and let the reply finish.
            self._stopped = True

    async def _read(self) -> None:
        ws = self._ws
        if ws is None:
            return
        try:
            async for msg in ws:
                if self._fired or self._stopped:
                    return
                parsed = parse_deepgram_transcript_message(
                    msg if isinstance(msg, str) else msg.decode("utf-8", "ignore")
                )
                if not parsed:
                    continue
                text, _is_final, _conf = parsed
                if not (text or "").strip():
                    continue
                loop = asyncio.get_running_loop()
                if loop.time() - self._started_at < BARGE_MIN_INTO_REPLY_SEC:
                    continue
                if not looks_like_caller(text, self._script):
                    continue
                self._fired = True
                voice_info(
                    "barge_detected",
                    call_sid=self.call_sid,
                    heard_words=len(re.findall(r"[a-z0-9']+", text.lower())),
                    into_reply_sec=round(loop.time() - self._started_at, 2),
                )
                try:
                    res = self._on_barge()
                    if asyncio.iscoroutine(res):
                        await res
                except Exception:
                    voice_warning("barge_callback_failed", call_sid=self.call_sid)
                return
        except asyncio.CancelledError:
            raise
        except Exception:
            # Connection dropped mid-reply. Nothing to do: no barge this turn.
            return

    async def stop(self) -> None:
        self._stopped = True
        if self._reader and not self._reader.done():
            self._reader.cancel()
        self._reader = None
        ws, self._ws = self._ws, None
        if ws is not None:
            try:
                await ws.close()
            except Exception:
                pass
