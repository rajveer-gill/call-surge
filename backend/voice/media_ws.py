"""Twilio Media Streams WebSocket bridged to Deepgram live transcription."""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Any, Optional

import websockets
from fastapi import WebSocket, WebSocketDisconnect

from observability import voice_debug, voice_info, voice_transcript
from voice.twilio_call import safe_twilio_call_update
from voice.deepgram_bridge import (
    DEEPGRAM_MODEL,
    connect_deepgram_listen,
    parse_deepgram_transcript_message,
)
from voice.media_token import token_stream_generation, verify_pending_media_stream_token
from voice.stt_config import deepgram_max_frame_bytes, media_stream_max_sec, utterance_finalize_debounce_ms
from voice.twilio_fallback_twiml import gather_process_speech_twiml
from voice.twiml_stt import got_it_respond_twiml
from voice.twilio_media import parse_twilio_media_message, twilio_media_payload_bytes, twilio_start_meta
from voice.utterance import apply_caller_utterance

_log = logging.getLogger("nuvatra")

# Longest a single turn may be held open by speech that keeps arriving. Ongoing words
# extend the debounce (see _UtteranceCollector.on_partial) so a caller is not cut off
# mid-sentence, and this is the backstop on that: someone who talks without pausing, or a
# television behind them, must not keep the receptionist listening indefinitely. Twenty
# seconds is far longer than any real booking sentence and far shorter than a caller would
# tolerate waiting.
MAX_UTTERANCE_HOLD_SEC = 20.0


def _resolve_stream_base_url(row: dict[str, Any], call_sid: str) -> tuple[str, str]:
    """
    Resolve HTTPS base URL for REST updates during a media stream.
    Returns (base_url, source) where source is session|env|none (for logs only).
    """
    session_base = (row.get("twilio_public_base_url") or "").strip()
    if session_base:
        return session_base, "session"
    try:
        import main as m

        env_base = (m._public_base_url() or "").strip()
        if env_base:
            voice_info("media_ws_base_url_env_fallback", call_sid=call_sid)
            return env_base, "env"
    except Exception:
        pass
    return "", "none"


class _UtteranceCollector:
    """Accumulate Deepgram finals + last interim; commit once after debounce."""

    def __init__(
        self,
        *,
        call_sid: str,
        base_url: str,
        debounce_sec: float,
        twilio_client: Any,
        websocket: WebSocket,
    ) -> None:
        self.call_sid = call_sid
        self.base_url = base_url
        self.debounce_sec = debounce_sec
        self.twilio_client = twilio_client
        self.websocket = websocket
        self.final_segments: list[str] = []
        self.last_interim = ""
        self.last_confidence = 0.0
        self._debounce_task: Optional[asyncio.Task[None]] = None
        self._committed = False
        # Ceiling on how long ongoing speech may hold the turn open. Without it a caller
        # who never pauses — or a room with a television in it — keeps the window alive
        # forever and the AI never answers.
        self._first_scheduled_at: Optional[float] = None

    def _cancel_debounce(self) -> None:
        if self._debounce_task and not self._debounce_task.done():
            self._debounce_task.cancel()
        self._debounce_task = None

    def on_partial(self, text: str, confidence: float) -> None:
        """Words still arriving. Keep the turn open — the caller has not finished.

        This used to update the text and leave the timer alone, which made the debounce
        mean "commit N ms after the last FINAL" rather than "commit after N ms of
        silence". Interim results stream continuously while someone is talking and were
        all ignored, so a caller was cut off whenever they spoke for longer than the
        debounce without Deepgram happening to emit a final:

            00:55:49  caller_said  "Hi. This is Raj."

        — the rest of that sentence was still being spoken. It also inverted the
        endpointing setting: raising it made Deepgram emit finals LESS often, which left
        the clock running out mid-sentence and cut callers off sooner, not later.

        Only a changed transcript extends the window. Deepgram repeats the same interim
        while it is still deciding, and treating those as fresh speech would hold the
        line open through silence.
        """
        if self._committed:
            return
        if text:
            changed = text != self.last_interim
            self.last_interim = text
            self.last_confidence = max(self.last_confidence, confidence)
            if changed:
                self._schedule_commit()

    def on_final_segment(self, text: str, confidence: float) -> None:
        if self._committed:
            return
        t = (text or "").strip()
        if t:
            self.final_segments.append(t)
            # This final IS the interim it grew out of. Clearing keeps last_interim
            # meaning "speech since the last final", so transcript() can append it
            # without repeating words already committed to a segment.
            self.last_interim = ""
            self.last_confidence = max(self.last_confidence, confidence)
            self._schedule_commit()
        elif self.final_segments or (self.last_interim or "").strip():
            # Empty final but we still have text to flush (e.g. endpointing after interim).
            self._schedule_commit()

    def _schedule_commit(self) -> None:
        if self._committed:
            return
        now = time.monotonic()
        if self._first_scheduled_at is None:
            self._first_scheduled_at = now
        elif now - self._first_scheduled_at >= MAX_UTTERANCE_HOLD_SEC:
            # Held open long enough. Take what we have rather than listen forever.
            voice_info("utterance_hold_capped", call_sid=self.call_sid)
            self._cancel_debounce()
            self._debounce_task = asyncio.create_task(self.commit_now())
            return
        self._cancel_debounce()
        self._debounce_task = asyncio.create_task(self._debounced_commit())

    async def _debounced_commit(self) -> None:
        try:
            await asyncio.sleep(self.debounce_sec)
            await self.commit_now()
        except asyncio.CancelledError:
            return

    def transcript(self) -> tuple[str, float]:
        """Everything heard this turn: the finals, plus whatever has arrived since the last one.

        The trailing interim used to be dropped whenever any final existed, so holding the
        turn open for a caller still speaking gained nothing — the words they were saying
        were collected and then thrown away at commit. "Hi. This is Raj." was committed
        while "I'd like to book a shampoo and haircut with Terrance on Thursday at 11AM"
        sat in last_interim.

        on_final_segment clears last_interim, so it only ever holds speech that no final
        has superseded and the two cannot duplicate each other.
        """
        parts = [*self.final_segments]
        tail = (self.last_interim or "").strip()
        if tail:
            parts.append(tail)
        return " ".join(p for p in parts if p).strip(), self.last_confidence

    async def commit_now(self) -> None:
        if self._committed:
            return
        self._committed = True
        self._cancel_debounce()
        text, conf = self.transcript()
        voice_info(
            "utterance_final",
            call_sid=self.call_sid,
            transcript_len=len(text),
            confidence=conf,
        )
        voice_transcript("caller_said", call_sid=self.call_sid, text=text)
        if not (text or "").strip():
            # Deepgram often sends an empty final on stream end; do not replace live TwiML or
            # hit Gather/process-speech with silence — let Twilio continue to play/got-it/respond.
            voice_info("utterance_commit_skipped_empty", call_sid=self.call_sid)
            try:
                await self.websocket.close()
            except Exception:
                pass
            return
        try:
            result = await apply_caller_utterance(self.call_sid, text, conf, self.base_url)
            if self.twilio_client:
                if result.mode == "replace_call_twiml" and result.replacement_twiml:
                    await safe_twilio_call_update(
                        self.twilio_client,
                        self.call_sid,
                        result.replacement_twiml,
                        op="replace_twiml",
                    )
                elif result.mode == "tail_play_respond":
                    # Interrupt queued TwiML after </Connect> (Still there? / second stream).
                    xml = got_it_respond_twiml(self.call_sid, self.base_url)
                    await safe_twilio_call_update(
                        self.twilio_client,
                        self.call_sid,
                        xml,
                        op="got_it_respond",
                    )
        except Exception:
            _log.exception("apply_caller_utterance_failed call_sid=%s", self.call_sid)
        try:
            await self.websocket.close()
        except Exception:
            pass


async def handle_phone_media_websocket(websocket: WebSocket, twilio_client: Any) -> None:
    await websocket.accept()
    voice_info("media_ws_open", path="/api/phone/media")
    dg_ws: Any = None
    collector: Optional[_UtteranceCollector] = None
    call_sid: Optional[str] = None
    base_url = ""
    max_sec = media_stream_max_sec()
    debounce_sec = utterance_finalize_debounce_ms() / 1000.0
    stream_deadline: Optional[float] = None
    audio_prebuffer = bytearray()
    max_pre = 512 * 1024

    async def fail_open_gather(reason: str) -> None:
        voice_info("deepgram_connect_fail", reason=reason, call_sid=call_sid or "")
        if twilio_client and call_sid and base_url:
            xml = gather_process_speech_twiml(call_sid, base_url)
            await safe_twilio_call_update(
                twilio_client,
                call_sid,
                xml,
                op="deepgram_fail_open_gather",
            )
        try:
            await websocket.close()
        except Exception:
            pass

    handshake_deadline = time.monotonic() + 25.0
    try:
        while time.monotonic() < handshake_deadline:
            left = handshake_deadline - time.monotonic()
            if left <= 0:
                break
            try:
                raw_msg = await asyncio.wait_for(websocket.receive(), timeout=min(left, 5.0))
            except asyncio.TimeoutError:
                continue
            mtype = raw_msg.get("type")
            if mtype == "websocket.disconnect":
                voice_info("media_ws_close", reason="client_disconnect", call_sid=call_sid or "")
                return
            if mtype != "websocket.receive":
                continue
            text = raw_msg.get("text")
            if not isinstance(text, str):
                continue
            ev = parse_twilio_media_message(text)
            if not ev:
                voice_debug("frame_parse_errors", detail="invalid_json")
                continue
            kind = ev.get("event")
            if kind == "connected":
                voice_info("twilio_stream_connected")
                continue
            if kind == "media" and dg_ws is None:
                payload = twilio_media_payload_bytes(ev, max_b64_len=deepgram_max_frame_bytes() * 2)
                if payload and len(audio_prebuffer) + len(payload) <= max_pre:
                    audio_prebuffer.extend(payload)
                continue
            if kind == "start":
                cs, _ss, cp = twilio_start_meta(ev)
                call_sid = cs
                token = (cp or {}).get("token") or ""
                import main as m
                import runtime

                row = m.active_calls.get(call_sid) or {}
                max_gen = runtime.call_store.get_media_stream_max_gen(call_sid)
                if max_gen < 1:
                    max_gen = int(row.get("media_stream_gen") or 0)
                tok_gen = token_stream_generation(token)
                session_has_base = bool((row.get("twilio_public_base_url") or "").strip())
                voice_info(
                    "media_ws_handshake",
                    call_sid=call_sid,
                    max_gen=max_gen,
                    token_gen=tok_gen,
                    has_token=bool(token),
                    session_has_base_url=session_has_base,
                    session_exists=bool(row),
                )
                voice_debug(
                    "media_ws_handshake_detail",
                    call_sid=call_sid,
                    session_keys=sorted(row.keys()) if row else [],
                )
                if not call_sid or not token or not max_gen:
                    voice_info(
                        "media_ws_close",
                        reason="invalid_token",
                        call_sid=call_sid or "",
                        detail="missing_token_or_gen",
                    )
                    await websocket.close(code=4401)
                    return
                if not verify_pending_media_stream_token(
                    token, call_sid, max_issued_generation=max_gen
                ):
                    voice_info(
                        "media_ws_close",
                        reason="invalid_token",
                        call_sid=call_sid,
                        token_gen=tok_gen,
                        max_issued_gen=max_gen,
                    )
                    await websocket.close(code=4401)
                    return
                base_url, base_source = _resolve_stream_base_url(row, call_sid)
                if not base_url:
                    voice_info(
                        "media_ws_close",
                        reason="missing_base_url",
                        call_sid=call_sid,
                        session_has_base_url=session_has_base,
                    )
                    await websocket.close(code=4400)
                    return
                if base_source == "env" and not session_has_base:
                    runtime.call_store.merge_session(
                        call_sid, {"twilio_public_base_url": base_url}
                    )
                voice_info(
                    "twilio_stream_start",
                    call_sid=call_sid,
                    base_url_source=base_source,
                )
                stream_deadline = time.monotonic() + max_sec
                try:
                    dg_ws = await connect_deepgram_listen()
                    voice_info("deepgram_connect_ok", call_sid=call_sid, model=DEEPGRAM_MODEL)
                except Exception as e:
                    await fail_open_gather(str(e))
                    return
                collector = _UtteranceCollector(
                    call_sid=call_sid,
                    base_url=base_url,
                    debounce_sec=debounce_sec,
                    twilio_client=twilio_client,
                    websocket=websocket,
                )
                if audio_prebuffer:
                    await dg_ws.send(bytes(audio_prebuffer))
                    audio_prebuffer.clear()
                break
            voice_debug("twilio_unknown_event", event=str(kind))

        if not dg_ws or not collector or not call_sid:
            voice_info("media_ws_close", reason="handshake_timeout_or_invalid", call_sid=call_sid or "")
            try:
                await websocket.close(code=4408)
            except Exception:
                pass
            return

        async def pump_dg() -> None:
            assert dg_ws is not None
            assert collector is not None
            try:
                async for message in dg_ws:
                    if isinstance(message, bytes):
                        continue
                    if not isinstance(message, str):
                        continue
                    parsed = parse_deepgram_transcript_message(message)
                    if not parsed:
                        continue
                    t, is_final, conf = parsed
                    if is_final:
                        collector.on_final_segment(t, conf)
                    else:
                        collector.on_partial(t, conf)
            except websockets.exceptions.ConnectionClosed:
                voice_debug("deepgram_ws_closed", call_sid=call_sid)
            except Exception:
                _log.exception("pump_dg_failed call_sid=%s", call_sid)

        dg_task = asyncio.create_task(pump_dg())

        try:
            while True:
                if stream_deadline is not None:
                    remaining = stream_deadline - time.monotonic()
                    if remaining <= 0:
                        voice_info("twilio_stream_stop", call_sid=call_sid, reason="max_duration")
                        await collector.commit_now()
                        break
                    timeout = min(remaining, 1.0)
                else:
                    timeout = 30.0
                try:
                    raw_msg = await asyncio.wait_for(websocket.receive(), timeout=timeout)
                except asyncio.TimeoutError:
                    continue
                mtype = raw_msg.get("type")
                if mtype == "websocket.disconnect":
                    voice_info("media_ws_close", reason="client_disconnect", call_sid=call_sid)
                    await collector.commit_now()
                    break
                if mtype != "websocket.receive":
                    continue
                wtext = raw_msg.get("text")
                if not isinstance(wtext, str):
                    continue
                ev = parse_twilio_media_message(wtext)
                if not ev:
                    continue
                kind = ev.get("event")
                if kind == "media":
                    payload = twilio_media_payload_bytes(ev)
                    if payload and dg_ws:
                        await dg_ws.send(payload)
                elif kind == "stop":
                    voice_info("twilio_stream_stop", call_sid=call_sid, reason="twilio_stop")
                    await collector.commit_now()
                    break
        finally:
            if collector and not collector._committed:
                voice_info("twilio_stream_stop", call_sid=call_sid, reason="cleanup_pending")
                await collector.commit_now()
            dg_task.cancel()
            try:
                await dg_task
            except asyncio.CancelledError:
                pass
            try:
                await dg_ws.close()
            except Exception:
                pass
    except WebSocketDisconnect:
        voice_info("media_ws_close", reason="websocket_disconnect", call_sid=call_sid or "")
        if collector and not collector._committed:
            await collector.commit_now()
    except Exception:
        _log.exception("media_ws_unhandled")
        if collector and not collector._committed:
            try:
                await collector.commit_now()
            except Exception:
                pass
        try:
            await websocket.close()
        except Exception:
            pass
