"""One call must not send the same confirmation text three times.

From production, 2026-08-27, inside a single 140-second call:

    22:21:39  apt_id=193  -> +19259...84  body_len=336
    22:21:57  apt_id=194  -> +19259...84  body_len=336
    22:22:11  apt_id=195  -> +19259...84  body_len=336
              voice_booking_draft_superseded | count=1

A caller who amends anything mid-call supersedes the earlier draft and a new
appointment row is written. The confirmation was sent on every creation, so the
caller received the identical message three times in 32 seconds with no way to tell
it was one booking.

Keyed on the exact body: a genuine correction — different time, different stylist —
still goes out, because the caller needs to see what changed.
"""
import conversation_service as cs


def _reset():
    cs._SENT_CONFIRMATIONS.clear()


def test_the_same_text_twice_on_one_call_is_suppressed():
    _reset()
    body = "You're booked for Friday at 12:00 with Taylor."
    assert cs._confirmation_already_sent("CA_1", "+19255550184", body) is False
    assert cs._confirmation_already_sent("CA_1", "+19255550184", body) is True
    assert cs._confirmation_already_sent("CA_1", "+19255550184", body) is True


def test_a_real_correction_still_goes_out():
    """The caller moved the time — they have to be told."""
    _reset()
    assert cs._confirmation_already_sent("CA_1", "+19255550184", "Friday at 12:00") is False
    assert cs._confirmation_already_sent("CA_1", "+19255550184", "Friday at 13:00") is False


def test_a_second_call_is_not_suppressed():
    """Two bookings really are two bookings."""
    _reset()
    body = "You're booked for Friday at 12:00."
    assert cs._confirmation_already_sent("CA_1", "+19255550184", body) is False
    assert cs._confirmation_already_sent("CA_2", "+19255550184", body) is False


def test_a_different_caller_is_not_suppressed():
    _reset()
    body = "You're booked for Friday at 12:00."
    assert cs._confirmation_already_sent("CA_1", "+19255550184", body) is False
    assert cs._confirmation_already_sent("CA_1", "+19255550195", body) is False


def test_without_a_call_sid_nothing_is_suppressed():
    """We cannot tell a repeat from a separate booking, so we must not guess and
    swallow someone's only confirmation."""
    _reset()
    body = "You're booked."
    assert cs._confirmation_already_sent("", "+19255550184", body) is False
    assert cs._confirmation_already_sent("", "+19255550184", body) is False


def test_the_cache_cannot_grow_without_bound():
    _reset()
    for i in range(cs._SENT_CONFIRMATIONS_MAX + 20):
        cs._confirmation_already_sent(f"CA_{i}", "+19255550184", "body")
    assert len(cs._SENT_CONFIRMATIONS) <= cs._SENT_CONFIRMATIONS_MAX
