"""A short tone played down the line the moment the receptionist decides you've finished.

Callers have no way to tell the difference between "still listening" and "thinking", so
silence after they stop talking is ambiguous — one of Lana Anderberg's testers filled it
by asking "Are you there?" and then repeating herself. A person signals the handover with
a breath or an "mm-hm"; this is the phone equivalent, and it marks the exact instant the
turn passed rather than the moment the answer happens to be ready.

Deliberately a tone rather than words. "Got it, one moment" is about a second and a half
of speech to cover a reply that arrives in about one, on a call the customer already says
has too much dead air in it. This is 140ms.

Two soft notes would read as a doorbell; one short note reads as an acknowledgement, which
is what it is. Quiet on purpose — it plays on every turn, and anything assertive becomes
grating over a four-minute booking.

The caller can talk straight through it: barge-in stops the reply that follows, so a chime
heard while they were still mid-sentence costs them nothing.
"""

from __future__ import annotations

import math
import struct

# Twilio media streams are 8 kHz mulaw, same as the TTS frames alongside this.
_SAMPLE_RATE = 8000
_FRAME_SAMPLES = 160  # 20 ms per frame, matching _FRAME_SEC in media_ws_stream
_TONE_HZ = 880.0  # A5 — above speech, so it does not sound like a word
_TONE_SEC = 0.14
_AMPLITUDE = 0.16  # quiet; it plays every turn
_FADE_SEC = 0.02  # ramp both ends, or the tone clicks


def _linear_to_ulaw(sample: int) -> int:
    """One 16-bit PCM sample to 8-bit mulaw (G.711). Standard reference implementation."""
    BIAS = 0x84
    CLIP = 32635
    sign = 0x80 if sample < 0 else 0x00
    if sample < 0:
        sample = -sample
    if sample > CLIP:
        sample = CLIP
    sample += BIAS
    exponent = 7
    mask = 0x4000
    while exponent > 0 and not (sample & mask):
        exponent -= 1
        mask >>= 1
    mantissa = (sample >> (exponent + 3)) & 0x0F
    return ~(sign | (exponent << 4) | mantissa) & 0xFF


def _build_frames() -> list[bytes]:
    total = int(_SAMPLE_RATE * _TONE_SEC)
    fade = max(1, int(_SAMPLE_RATE * _FADE_SEC))
    pcm: list[int] = []
    for i in range(total):
        # Linear fade in and out so the tone starts and stops without a click.
        if i < fade:
            env = i / fade
        elif i > total - fade:
            env = max(0.0, (total - i) / fade)
        else:
            env = 1.0
        value = math.sin(2.0 * math.pi * _TONE_HZ * (i / _SAMPLE_RATE))
        pcm.append(int(value * env * _AMPLITUDE * 32767))
    ulaw = bytes(_linear_to_ulaw(s) for s in pcm)
    # Pad to a whole number of frames; mulaw silence is 0xFF, not 0x00.
    remainder = len(ulaw) % _FRAME_SAMPLES
    if remainder:
        ulaw += b"\xff" * (_FRAME_SAMPLES - remainder)
    return [
        ulaw[i : i + _FRAME_SAMPLES] for i in range(0, len(ulaw), _FRAME_SAMPLES)
    ]


# Built once at import: identical on every turn of every call, and synthesising it per
# turn would put arithmetic on the call path for no reason.
TURN_CHIME_FRAMES: list[bytes] = _build_frames()


def chime_enabled() -> bool:
    """On unless turned off. VOICE_TURN_CHIME=0 silences it without a code change."""
    import os

    return (os.getenv("VOICE_TURN_CHIME", "1") or "").strip().lower() not in (
        "0",
        "false",
        "no",
        "off",
    )
