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
# E5. The first version used 880 Hz, which on a phone earpiece reads as a smoke-alarm
# beep; dropping a major third is noticeably warmer and still clear of speech.
_TONE_HZ = 659.25
_TONE_SEC = 0.26
_AMPLITUDE = 0.16  # quiet; it plays every turn

# A pure sine is a beep. What makes something sound like a struck bell rather than an
# electronic tone is a couple of quiet overtones above the fundamental, so the ear hears
# an object being hit instead of an oscillator being switched on. The octave and the
# twelfth are the two strongest partials in most real chimes.
#
# All three fit inside the telephone band (300-3400 Hz): 659, 1319, 1978.
_PARTIALS = ((1.0, 1.00), (2.0, 0.30), (3.0, 0.11))

# And a bell decays, it does not stop. A flat tone with a fade-out at each end is the
# other half of why the first version sounded mechanical.
_ATTACK_SEC = 0.006  # fast enough to sound struck, slow enough not to click
_DECAY_TAU = 0.075  # exponential time constant; ~3.5 tau over the tone's length


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
    attack = max(1, int(_SAMPLE_RATE * _ATTACK_SEC))
    raw: list[float] = []
    for i in range(total):
        t = i / _SAMPLE_RATE
        # Struck, then decaying — a bell, not a switch being held down.
        if i < attack:
            env = i / attack
        else:
            env = math.exp(-(t - _ATTACK_SEC) / _DECAY_TAU)
        value = sum(
            gain * math.sin(2.0 * math.pi * _TONE_HZ * mult * t)
            for mult, gain in _PARTIALS
        )
        raw.append(value * env)
    # Normalise on the summed partials rather than the fundamental, or adding overtones
    # would quietly make the chime louder than the amplitude we chose.
    peak = max((abs(v) for v in raw), default=0.0) or 1.0
    pcm = [int((v / peak) * _AMPLITUDE * 32767) for v in raw]
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
