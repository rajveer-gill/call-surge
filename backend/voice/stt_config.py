"""STT / media stream tuning from environment."""

from __future__ import annotations

import os
from typing import Optional


def voice_stt_provider() -> str:
    v = (os.getenv("VOICE_STT_PROVIDER") or "twilio").strip().lower()
    return "deepgram" if v == "deepgram" else "twilio"


def deepgram_env_block_reason() -> Optional[str]:
    """
    If VOICE_STT_PROVIDER=deepgram but env cannot satisfy the media bridge, return a short reason.
    Returns None when env does not request deepgram, or when env prerequisites are satisfied.
    """
    if voice_stt_provider() != "deepgram":
        return None
    if not deepgram_api_key():
        return "missing_DEEPGRAM_API_KEY"
    if not media_stream_signing_secret():
        return "missing_MEDIA_STREAM_SIGNING_SECRET_and_TWILIO_AUTH_TOKEN"
    return None


def deepgram_api_key() -> str:
    return (os.getenv("DEEPGRAM_API_KEY") or "").strip()


def media_stream_signing_secret() -> str:
    return (
        (os.getenv("MEDIA_STREAM_SIGNING_SECRET") or "").strip()
        or (os.getenv("TWILIO_AUTH_TOKEN") or "").strip()
    )


def media_stream_max_sec() -> float:
    try:
        return float(os.getenv("VOICE_MEDIA_STREAM_MAX_SEC", "30"))
    except ValueError:
        return 30.0


def utterance_finalize_debounce_ms() -> int:
    # Silence to wait after the caller stops before we commit the utterance and play "got it,
    # one moment". Higher = the caller can pause mid-sentence without being cut off (less
    # rushed); too high and the AI feels slow to answer. Tune per shop via the env var.
    try:
        return int(os.getenv("VOICE_DEEPGRAM_FINAL_DEBOUNCE_MS", "800"))
    except ValueError:
        return 800


def deepgram_endpointing_ms() -> int:
    """Silence (ms) after which DEEPGRAM itself declares the utterance finished.

    This is the one that actually decides where a caller's sentence ends, and it sits
    UPSTREAM of utterance_finalize_debounce_ms — by the time our debounce is consulted,
    Deepgram has already cut the transcript and sent it. Raising ours while this stayed at
    300 changed nothing, which is how it was found:

        00:04:22  "Hi. This is Raj. I'd like to book a shampoo and a haircut"
        00:04:32  "with Terrence on Thursday at 11AM."

    One sentence, split at the breath before "with", answered as two turns — so the
    receptionist asked which stylist he wanted while the answer was still in flight. That
    is what Lana reported as it only taking the first part of what she said.

    300ms is shorter than an ordinary pause mid-sentence. Kept as the default so nothing
    changes for anyone without a deliberate env set; raise it per environment and revert
    instantly if replies start feeling slow, since every extra millisecond here is silence
    the caller waits through before the AI answers.
    """
    try:
        return int(os.getenv("VOICE_DEEPGRAM_ENDPOINTING_MS", "300"))
    except ValueError:
        return 300


def deepgram_max_frame_bytes() -> int:
    try:
        return int(os.getenv("VOICE_DEEPGRAM_MAX_FRAME_BYTES", "8192"))
    except ValueError:
        return 8192


def http_to_ws_base(base_url: str) -> str:
    """Turn https://host into wss://host for Twilio Media Streams."""
    b = (base_url or "").strip().rstrip("/")
    if b.startswith("https://"):
        return "wss://" + b[len("https://") :]
    if b.startswith("http://"):
        return "ws://" + b[len("http://") :]
    return b
