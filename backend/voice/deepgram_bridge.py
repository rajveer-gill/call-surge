"""Deepgram live listen WebSocket helpers (Nova-3, 8kHz mu-law from Twilio)."""

from __future__ import annotations

import json
from typing import Any, Optional

import websockets

from voice.stt_config import deepgram_api_key, deepgram_endpointing_ms

# Twilio PSTN inbound is typically mu-law 8k mono — Deepgram accepts this encoding directly.
DEEPGRAM_MODEL = "nova-3"


def deepgram_listen_query() -> str:
    """Built per connection so endpointing can be tuned by env without a deploy.

    endpointing is how long Deepgram waits in silence before calling the utterance done.
    It decides where a caller's sentence is cut, and everything downstream — including
    utterance_finalize_debounce_ms — only sees what it has already decided.
    """
    return (
        f"model={DEEPGRAM_MODEL}"
        "&encoding=mulaw"
        "&sample_rate=8000"
        "&channels=1"
        f"&endpointing={deepgram_endpointing_ms()}"
        "&smart_format=true"
        "&interim_results=true"
    )


def deepgram_listen_uri() -> str:
    return f"wss://api.deepgram.com/v1/listen?{deepgram_listen_query()}"


def parse_deepgram_transcript_message(text: str) -> Optional[tuple[str, bool, float]]:
    """
    Return (transcript, is_final, confidence) from a Deepgram JSON message, or None if not a transcript.
    """
    try:
        d: Any = json.loads(text)
    except json.JSONDecodeError:
        return None
    if not isinstance(d, dict):
        return None
    ch = d.get("channel")
    if not isinstance(ch, dict):
        return None
    alts = ch.get("alternatives")
    if not isinstance(alts, list) or not alts:
        return None
    a0 = alts[0]
    if not isinstance(a0, dict):
        return None
    t = (a0.get("transcript") or "").strip()
    conf_raw = a0.get("confidence")
    try:
        conf = float(conf_raw) if conf_raw is not None else 0.0
    except (TypeError, ValueError):
        conf = 0.0
    is_final = bool(d.get("is_final") or d.get("speech_final"))
    return t, is_final, conf


async def connect_deepgram_listen() -> Any:
    """Return an open websockets client connection (caller must close)."""
    key = deepgram_api_key()
    if not key:
        raise RuntimeError("DEEPGRAM_API_KEY is not set")
    uri = deepgram_listen_uri()
    return await websockets.connect(
        uri,
        extra_headers=[("Authorization", f"Token {key}")],
        max_size=None,
    )
