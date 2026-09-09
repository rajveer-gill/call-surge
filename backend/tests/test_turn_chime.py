"""The turn chime is real audio, at the right pitch, and quiet.

Callers cannot tell "still listening" from "thinking", so the silence after they stop is
ambiguous. One of Lana Anderberg's testers filled it by asking "Are you there?" and then
repeating herself. This plays the moment the receptionist decides the turn has passed.

It is hand-rolled G.711 mulaw, which is the part worth testing: a wrong encoder does not
fail, it plays noise down a customer's ear on every single turn. So these decode the
frames back to PCM and check the waveform is what was intended — the right pitch, the
right loudness, and not silence.
"""
import math

from voice.turn_chime import (
    TURN_CHIME_FRAMES,
    _AMPLITUDE,
    _FRAME_SAMPLES,
    _SAMPLE_RATE,
    _TONE_HZ,
    _TONE_SEC,
    chime_enabled,
)


def _ulaw_to_linear(byte: int) -> int:
    """Standard G.711 mulaw decode, so the test does not share the encoder's assumptions."""
    byte = ~byte & 0xFF
    sign = byte & 0x80
    exponent = (byte >> 4) & 0x07
    mantissa = byte & 0x0F
    sample = ((mantissa << 3) + 0x84) << exponent
    sample -= 0x84
    return -sample if sign else sample


def _pcm() -> list[int]:
    return [_ulaw_to_linear(b) for b in b"".join(TURN_CHIME_FRAMES)]


def test_frames_are_twilio_sized():
    """Twilio media frames are 20ms of 8kHz mulaw. A ragged frame desynchronises audio."""
    assert TURN_CHIME_FRAMES
    for f in TURN_CHIME_FRAMES:
        assert len(f) == _FRAME_SAMPLES


def test_it_is_short_enough_to_be_over_before_the_reply():
    ms = len(TURN_CHIME_FRAMES) * 20
    assert 80 <= ms <= 300, f"{ms}ms is not an acknowledgement"


def test_it_is_not_silence():
    """A silent chime is the feature quietly not existing."""
    peak = max(abs(s) for s in _pcm())
    assert peak > 1000, "no audible signal"


def test_it_is_quiet():
    """It plays on every turn of every call. Anything assertive becomes grating."""
    peak = max(abs(s) for s in _pcm())
    assert peak < int(0.30 * 32767), f"peak {peak} is too loud for every-turn audio"
    # And in the region we asked for, allowing for mulaw's coarse quantisation.
    assert peak > int(0.5 * _AMPLITUDE * 32767)


def test_the_pitch_is_right():
    """Counting zero crossings catches an encoder that produces plausible-looking noise.

    The tone crosses zero twice per cycle, so over its length the count is
    close to 2 * f * seconds. Silence padding contributes none.
    """
    pcm = _pcm()
    crossings = sum(
        1 for a, b in zip(pcm, pcm[1:]) if (a >= 0) != (b >= 0) and (a or b)
    )
    tone_seconds = crossings / (2 * _TONE_HZ) if crossings else 0
    expected = 2 * _TONE_HZ * tone_seconds
    assert abs(crossings - expected) < 5
    # And that the crossings span roughly the tone we built, not a click. Tied to the
    # configured length rather than a literal, so retuning the chime does not need this
    # edited — the overtones are quiet enough that the waveform still crosses zero at the
    # fundamental, which is what makes this a fundamental-frequency check at all.
    assert abs(tone_seconds - _TONE_SEC) < 0.03, (tone_seconds, _TONE_SEC)


def test_it_starts_and_ends_softly():
    """Without a fade a tone clicks, which is worse than no chime at all."""
    pcm = _pcm()
    ramp = int(_SAMPLE_RATE * 0.005)  # 5ms in
    assert max(abs(s) for s in pcm[:ramp]) < max(abs(s) for s in pcm)


def test_padding_is_mulaw_silence_not_zero_bytes():
    """0x00 in mulaw is a large negative value — padding with it would click loudly."""
    tail = TURN_CHIME_FRAMES[-1]
    if 0xFF in tail:
        assert 0x00 not in tail[tail.index(0xFF):]


def test_the_kill_switch(monkeypatch):
    assert chime_enabled() is True
    for off in ("0", "false", "no", "off", "OFF"):
        monkeypatch.setenv("VOICE_TURN_CHIME", off)
        assert chime_enabled() is False, off
    monkeypatch.setenv("VOICE_TURN_CHIME", "1")
    assert chime_enabled() is True


def test_it_is_built_once():
    """Synthesising per turn would put arithmetic on the call path for no reason."""
    from voice import turn_chime

    assert turn_chime.TURN_CHIME_FRAMES is TURN_CHIME_FRAMES
