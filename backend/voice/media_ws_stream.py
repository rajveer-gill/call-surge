"""Option C: bidirectional Twilio Media Stream — one persistent stream for the whole call.

Unlike the batch path (per-turn <Connect><Stream> for STT only, AI reply via <Play> of a
fully-synthesized mp3), this keeps a single stream open and sends the AI reply back as
outbound mulaw/8000 frames over the same socket, so first audio starts in ~hundreds of ms
and the caller can barge in (we send `clear` to flush Twilio's buffer and stop the stream).

Crucially it REUSES the existing brain untouched:
  - apply_caller_utterance(): records the turn, handles forward/limits/language, schedules
    generate_response_async, and sets runtime.call_store.response_status.
  - generate_response_async(): produces `ai_text` with ALL booking/directive/SMS side
    effects (the moat). We just stream that text instead of turning it into a <Play> URL.

Gated by config_service.voice_streaming_enabled() (VOICE_STREAMING_TTS); off = untouched.
"""
from __future__ import annotations

import asyncio
import base64
import json
import logging
import threading
import time
from typing import Any, Optional

import websockets
from fastapi import WebSocket
from starlette.websockets import WebSocketDisconnect

import config_service
import runtime
from observability import voice_info, voice_transcript, voice_warning
from voice.deepgram_bridge import (
    DEEPGRAM_MODEL,
    connect_deepgram_listen,
    parse_deepgram_transcript_message,
)
from voice.media_token import token_stream_generation, verify_pending_media_stream_token
from voice.stt_config import utterance_finalize_debounce_ms
from voice.streaming_tts import stream_tts_ulaw_frames
from voice.twilio_call import safe_twilio_call_update
from voice.twilio_media import parse_twilio_media_message, twilio_media_payload_bytes, twilio_start_meta
from voice.utterance import apply_caller_utterance

_log = logging.getLogger("nuvatra")

_FRAME_SEC = 0.02  # 20 ms of audio per mulaw frame.
# Send frames this far AHEAD of their play time so Twilio always has a cushion buffered
# (prevents underrun/choppiness from send jitter). Barge-in still stops instantly because a
# `clear` flushes Twilio's buffer regardless of how far ahead we've sent.
_SEND_LEAD_SEC = 0.6
_REPLY_WAIT_SEC = 25.0  # max wait for the brain to produce ai_text before giving up the turn.
_HANDSHAKE_SEC = 25.0
# Half-duplex: stop feeding caller audio to STT while the AI is speaking (+ this guard after
# playback ends) so the AI's own voice — echoed back on speakerphone / the inbound track —
# isn't transcribed as caller speech (which caused false "barge-in" self-interruptions).
# True barge-in while speaking needs acoustic echo cancellation (future work).
_LISTEN_GUARD_SEC = 0.5
# Deepgram closes a listen stream after ~10s with no audio. While the AI is speaking we gate
# caller audio (half-duplex), so a reply longer than that window would let Deepgram idle out —
# and the next caller frame would hit a dead socket. Send a KeepAlive on this cadence during
# the gap so the stream stays open through long replies. See bidi_deepgram_keepalive.
_DG_KEEPALIVE_SEC = 5.0


class _BidiSession:
    def __init__(self, websocket: WebSocket, twilio_client: Any) -> None:
        self.ws = websocket
        self.twilio_client = twilio_client
        self.call_sid: Optional[str] = None
        self.stream_sid: Optional[str] = None
        self.base_url = ""
        self.voice = "fable"
        self.speed = 1.0
        self._call_data: dict = {}
        self.dg_ws: Any = None
        self._dg_task: "Optional[asyncio.Task[None]]" = None
        self._last_dg_activity = 0.0  # monotonic time of last send to Deepgram (audio or KeepAlive)
        self.speaking = False
        self.interrupt = asyncio.Event()
        self._reply_mark: Optional[asyncio.Event] = None
        self._barge_cleared = False
        self._resume_listen_at = 0.0  # monotonic time when STT may resume after speaking
        self._closing = False
        self.utterance_q: "asyncio.Queue[tuple[str, float]]" = asyncio.Queue()
        self.debounce_sec = utterance_finalize_debounce_ms() / 1000.0
        # utterance accumulation
        self._finals: list[str] = []
        self._interim = ""
        self._conf = 0.0
        self._commit_task: Optional[asyncio.Task[None]] = None

    # ---- outbound websocket messages (Twilio bidirectional protocol) ----
    async def _send(self, obj: dict) -> None:
        """Send one Twilio frame, tolerating a caller who has already hung up.

        The caller hanging up mid-sentence closes the socket while _speak is still
        streaming frames, and every one of those calls raised
        RuntimeError('Cannot call "send" once a close message has been sent') out of
        an un-awaited task — a full traceback per hangup, for the most ordinary
        thing a caller can do. Nothing was broken for the caller, who had already
        gone, but the noise buries real errors and the speak loop died wherever it
        happened to be rather than finishing tidily.

        Marks the session closing so the loop stops rather than raising per frame.
        """
        if self._closing:
            return
        try:
            await self.ws.send_text(json.dumps(obj))
        except (RuntimeError, WebSocketDisconnect) as e:
            # Only the socket being gone. Anything else is a real fault and must
            # keep raising rather than vanishing into a swallowed exception.
            if isinstance(e, RuntimeError) and "close message" not in str(e):
                raise
            if not self._closing:
                self._closing = True
                voice_info("bidi_send_after_close", call_sid=self.call_sid)

    async def _send_media(self, frame: bytes) -> None:
        await self._send({
            "event": "media",
            "streamSid": self.stream_sid,
            "media": {"payload": base64.b64encode(frame).decode("ascii")},
        })

    async def _send_clear(self) -> None:
        await self._send({"event": "clear", "streamSid": self.stream_sid})

    async def _send_mark(self, name: str) -> None:
        await self._send({"event": "mark", "streamSid": self.stream_sid, "mark": {"name": name}})

    async def _close(self) -> None:
        try:
            await self.ws.close()
        except Exception:
            pass

    # ---- speaking (stream TTS frames out, interruptible) ----
    async def _speak(self, text: str) -> None:
        text = (text or "").strip()
        if not text or self._closing:
            return
        self.speaking = True
        self.interrupt.clear()
        self._barge_cleared = False
        self._reply_mark = asyncio.Event()
        # Drop any half-accumulated transcript so echo captured at the edge of the last turn
        # can't commit as a phantom utterance.
        self._finals, self._interim, self._conf = [], "", 0.0
        if self._commit_task and not self._commit_task.done():
            self._commit_task.cancel()
        loop = asyncio.get_running_loop()
        frame_q: "asyncio.Queue[Optional[bytes]]" = asyncio.Queue()

        def producer() -> None:
            try:
                for fr in stream_tts_ulaw_frames(text, self.voice, model="tts-1", speed=self.speed):
                    if self.interrupt.is_set():
                        break
                    loop.call_soon_threadsafe(frame_q.put_nowait, fr)
            except Exception:
                _log.exception("bidi_tts_producer_failed call_sid=%s", self.call_sid)
            finally:
                loop.call_soon_threadsafe(frame_q.put_nowait, None)

        threading.Thread(target=producer, daemon=True).start()
        start = loop.time()
        sent = 0
        interrupted = False
        while True:
            fr = await frame_q.get()
            if fr is None:
                break
            if self.interrupt.is_set():
                interrupted = True
                break
            if self._closing:
                # They hung up. Stop streaming into a socket that is gone.
                self.speaking = False
                voice_info(
                    "bidi_reply_abandoned", call_sid=self.call_sid, frames=sent,
                    reason="caller_hung_up",
                )
                return
            await self._send_media(fr)
            sent += 1
            # Pace against an absolute clock (not a per-frame sleep, which drifts slow): send
            # frame `sent` up to _SEND_LEAD_SEC before its play time, so Twilio keeps a cushion
            # and never underruns. If we've fallen behind (target<=now) we don't sleep — catch up.
            target = start + sent * _FRAME_SEC - _SEND_LEAD_SEC
            delay = target - loop.time()
            if delay > 0:
                await asyncio.sleep(delay)
        if interrupted:
            if not self._barge_cleared:
                await self._send_clear()
            self.speaking = False
            self._resume_listen_at = loop.time() + _LISTEN_GUARD_SEC
            voice_info("bidi_reply_spoken", call_sid=self.call_sid, frames=sent, interrupted=True)
            return
        # All frames sent, but Twilio may still be playing the buffered tail. Mark the end and
        # keep `speaking` True (STT stays gated) until Twilio echoes the mark or the remaining
        # audio would have finished — so we don't transcribe the tail as caller speech.
        await self._send_mark("reply_end")
        remaining = max(0.0, sent * _FRAME_SEC - (loop.time() - start))
        try:
            await asyncio.wait_for(self._reply_mark.wait(), timeout=remaining + 2.0)
        except asyncio.TimeoutError:
            pass
        self.speaking = False
        self._resume_listen_at = loop.time() + _LISTEN_GUARD_SEC
        voice_info("bidi_reply_spoken", call_sid=self.call_sid, frames=sent, interrupted=False)

    async def _barge_in(self) -> None:
        """Caller spoke while we were talking: flush Twilio's buffer and stop the stream."""
        if self.speaking and not self._barge_cleared:
            self._barge_cleared = True
            self.interrupt.set()
            await self._send_clear()
            voice_info("bidi_barge_in", call_sid=self.call_sid)

    # ---- utterance accumulation + debounced commit ----
    def _on_transcript(self, text: str, is_final: bool, conf: float) -> None:
        # STT is gated while speaking (half-duplex), so transcripts here are caller speech,
        # not the AI's own echo.
        t = (text or "").strip()
        if not t:
            if is_final and (self._finals or self._interim):
                self._schedule_commit()
            return
        if is_final:
            self._finals.append(t)
            self._conf = max(self._conf, conf)
            self._schedule_commit()
        else:
            self._interim = t
            self._conf = max(self._conf, conf)

    def _schedule_commit(self) -> None:
        if self._commit_task and not self._commit_task.done():
            self._commit_task.cancel()
        self._commit_task = asyncio.create_task(self._debounced_commit())

    async def _debounced_commit(self) -> None:
        try:
            await asyncio.sleep(self.debounce_sec)
        except asyncio.CancelledError:
            return
        text = " ".join(self._finals).strip() or self._interim.strip()
        conf = self._conf
        self._finals, self._interim, self._conf = [], "", 0.0
        if text:
            await self.utterance_q.put((text, conf))

    # ---- turn: reuse the existing brain, then stream the reply ----
    async def _await_reply(self) -> Optional[str]:
        deadline = time.monotonic() + _REPLY_WAIT_SEC
        while time.monotonic() < deadline:
            st = runtime.call_store.response_status.get(self.call_sid or "", {})
            status = st.get("status")
            if status == "ready":
                return (st.get("ai_text") or "").strip()
            if status == "forward":
                return None
            await asyncio.sleep(0.05)
        return ""

    async def _run_turn(self, text: str, conf: float) -> None:
        voice_transcript("caller_said", call_sid=self.call_sid, text=text)
        result = await apply_caller_utterance(self.call_sid or "", text, conf, self.base_url)
        # Forward / limits / lost-session / language-record all come back as a full TwiML doc:
        # REST-replace the call with it (that supersedes the <Connect> stream and ends the WS).
        if result.mode == "replace_call_twiml" and result.replacement_twiml:
            await safe_twilio_call_update(
                self.twilio_client, self.call_sid, result.replacement_twiml, op="bidi_replace_twiml"
            )
            self._closing = True
            await self._close()
            return
        ai_text = await self._await_reply()
        st = runtime.call_store.response_status.get(self.call_sid or "", {})
        if st.get("status") == "forward":
            fp = st.get("forwarding_phone")
            runtime.call_store.response_status.pop(self.call_sid or "", None)
            if fp:
                import main as m
                xml = str(m.forward_call_to_business(fp, self.base_url, "English"))
                await safe_twilio_call_update(self.twilio_client, self.call_sid, xml, op="bidi_forward")
            self._closing = True
            await self._close()
            return
        end_call = bool(st.get("end_call"))
        runtime.call_store.response_status.pop(self.call_sid or "", None)
        if ai_text:
            await self._speak(ai_text)
        if end_call:
            # The request is taken and the goodbye has been spoken. Replacing the call's
            # TwiML supersedes the <Connect> stream, which ends the call cleanly.
            voice_info("bidi_end_call_after_booking", call_sid=self.call_sid)
            await safe_twilio_call_update(
                self.twilio_client,
                self.call_sid,
                '<?xml version="1.0" encoding="UTF-8"?><Response><Hangup/></Response>',
                op="bidi_hangup_after_booking",
            )
            self._closing = True
            await self._close()

    async def _drive_turns(self) -> None:
        while not self._closing:
            text, conf = await self.utterance_q.get()
            if self._closing:
                return
            try:
                await self._run_turn(text, conf)
            except Exception:
                _log.exception("bidi_run_turn_failed call_sid=%s", self.call_sid)

    async def _pump_deepgram(self) -> None:
        try:
            async for message in self.dg_ws:
                if not isinstance(message, str):
                    continue
                parsed = parse_deepgram_transcript_message(message)
                if not parsed:
                    continue
                t, is_final, conf = parsed
                self._on_transcript(t, is_final, conf)
        except websockets.exceptions.ConnectionClosed:
            pass
        except Exception:
            _log.exception("bidi_pump_deepgram_failed call_sid=%s", self.call_sid)

    async def _keepalive_deepgram(self) -> None:
        """Keep the Deepgram stream alive while we're not sending caller audio (e.g. during a
        long AI reply under the half-duplex gate), so it doesn't idle-close mid-call."""
        loop = asyncio.get_running_loop()
        if not self.dg_ws or loop.time() - self._last_dg_activity < _DG_KEEPALIVE_SEC:
            return
        self._last_dg_activity = loop.time()
        try:
            await self.dg_ws.send(json.dumps({"type": "KeepAlive"}))
        except Exception:
            # A failed KeepAlive means the socket is already gone; the audio-send path will
            # log it and reconnect on the next caller frame.
            pass

    async def _reconnect_deepgram(self) -> bool:
        """Deepgram send failed (usually an idle-closed socket). Reconnect and restart the pump
        instead of silently dropping the call. Returns True if the stream is usable again."""
        try:
            await self.dg_ws.close()
        except Exception:
            pass
        try:
            self.dg_ws = await connect_deepgram_listen()
        except Exception as e:
            voice_warning("bidi_deepgram_reconnect_failed", call_sid=self.call_sid, detail=str(e)[:200])
            return False
        if self._dg_task is not None:
            self._dg_task.cancel()
        self._dg_task = asyncio.create_task(self._pump_deepgram())
        self._last_dg_activity = asyncio.get_running_loop().time()
        voice_info("bidi_deepgram_reconnected", call_sid=self.call_sid)
        return True

    # ---- handshake, greeting, main loop ----
    async def run(self) -> None:
        await self.ws.accept()
        voice_info("bidi_ws_open", path="/api/phone/media-stream")
        if not await self._handshake():
            await self._close()
            return
        # Tenant context for get_business_info() in this asyncio task.
        import database
        cid = str((self._call_data or {}).get("client_id") or "").strip()
        if cid:
            database.set_request_client_id(cid)
        try:
            self.dg_ws = await connect_deepgram_listen()
            voice_info("deepgram_connect_ok", call_sid=self.call_sid, model=DEEPGRAM_MODEL)
        except Exception as e:
            voice_warning("bidi_deepgram_connect_failed", call_sid=self.call_sid, detail=str(e)[:200])
            await self._close()
            return

        self._dg_task = asyncio.create_task(self._pump_deepgram())
        turn_task = asyncio.create_task(self._drive_turns())
        # Greeting first (interruptible like any reply).
        try:
            import voice_service
            payload = voice_service.build_phone_greeting_payload(config_service.get_business_info())
            self.voice = payload.get("voice") or self.voice
            asyncio.create_task(self._speak(payload.get("spoken_text") or ""))
        except Exception:
            _log.exception("bidi_greeting_failed call_sid=%s", self.call_sid)

        try:
            await self._inbound_loop()
        finally:
            self._closing = True
            for tk in (self._dg_task, turn_task):
                if tk is not None:
                    tk.cancel()
            try:
                await self.dg_ws.close()
            except Exception:
                pass
            await self._close()

    async def _handshake(self) -> bool:
        deadline = time.monotonic() + _HANDSHAKE_SEC
        while time.monotonic() < deadline:
            try:
                raw = await asyncio.wait_for(self.ws.receive(), timeout=5.0)
            except asyncio.TimeoutError:
                continue
            if raw.get("type") == "websocket.disconnect":
                return False
            text = raw.get("text")
            if not isinstance(text, str):
                continue
            ev = parse_twilio_media_message(text)
            if not ev:
                continue
            if ev.get("event") == "start":
                return self._accept_start(ev)
            # ignore 'connected' and any pre-start media
        return False

    def _accept_start(self, ev: dict) -> bool:
        import main as m
        call_sid, stream_sid, cp = twilio_start_meta(ev)
        token = (cp or {}).get("token") or ""
        self.call_sid = call_sid
        self.stream_sid = stream_sid
        row = m.active_calls.get(call_sid or "") or {}
        max_gen = runtime.call_store.get_media_stream_max_gen(call_sid or "")
        if max_gen < 1:
            max_gen = int(row.get("media_stream_gen") or 0)
        voice_info(
            "bidi_ws_handshake",
            call_sid=call_sid or "",
            stream_sid_present=bool(stream_sid),
            has_token=bool(token),
            max_gen=max_gen,
            token_gen=token_stream_generation(token),
        )
        if not call_sid or not stream_sid or not token or max_gen < 1:
            return False
        if not verify_pending_media_stream_token(token, call_sid, max_issued_generation=max_gen):
            voice_warning("bidi_ws_invalid_token", call_sid=call_sid)
            return False
        base = (row.get("twilio_public_base_url") or "").strip()
        if not base:
            try:
                base = (m._public_base_url() or "").strip()
            except Exception:
                base = ""
        if not base:
            voice_warning("bidi_ws_missing_base_url", call_sid=call_sid)
            return False
        self.base_url = base
        self._call_data = row
        self.speed = config_service.get_tts_speed()
        voice_info("bidi_stream_start", call_sid=call_sid)
        return True

    async def _inbound_loop(self) -> None:
        while not self._closing:
            try:
                raw = await asyncio.wait_for(self.ws.receive(), timeout=30.0)
            except asyncio.TimeoutError:
                continue
            if raw.get("type") == "websocket.disconnect":
                voice_info("bidi_ws_close", reason="client_disconnect", call_sid=self.call_sid)
                return
            text = raw.get("text")
            if not isinstance(text, str):
                continue
            ev = parse_twilio_media_message(text)
            if not ev:
                continue
            kind = ev.get("event")
            if kind == "media":
                # Half-duplex gate: don't feed caller audio to STT while the AI is speaking
                # (or during the brief guard after), so the AI's echoed voice isn't transcribed.
                # While gated we send Deepgram a KeepAlive so a long reply can't idle-close it.
                if self.speaking or asyncio.get_running_loop().time() < self._resume_listen_at:
                    await self._keepalive_deepgram()
                    continue
                payload = twilio_media_payload_bytes(ev)
                if payload and self.dg_ws:
                    try:
                        await self.dg_ws.send(payload)
                        self._last_dg_activity = asyncio.get_running_loop().time()
                    except Exception:
                        # Don't silently drop the call: log and try to reconnect Deepgram once.
                        voice_warning("bidi_deepgram_send_failed", call_sid=self.call_sid)
                        if not await self._reconnect_deepgram():
                            voice_info("bidi_ws_close", reason="deepgram_lost", call_sid=self.call_sid)
                            return
            elif kind == "mark":
                name = (ev.get("mark") or {}).get("name")
                if name == "reply_end" and self._reply_mark is not None:
                    self._reply_mark.set()
            elif kind == "stop":
                voice_info("bidi_ws_close", reason="twilio_stop", call_sid=self.call_sid)
                return


async def handle_bidirectional_media(websocket: WebSocket, twilio_client: Any) -> None:
    session = _BidiSession(websocket, twilio_client)
    try:
        await session.run()
    except Exception:
        _log.exception("bidi_media_unhandled")
        try:
            await websocket.close()
        except Exception:
            pass
