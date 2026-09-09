"""A caller on a line that cannot receive texts is never promised one.

Lana Anderberg, 2026-09-09, third time she has raised the texting question:

    "Call at 11:33 we had a stylist call from a salon, we have landline phones that go
     through Verizon. AI does tell her she will receive a text."

The Sept 4 fix (`24cdebb`) checks the line type inside send_sms, which runs on the one
turn a BOOKING marker is emitted. That call never produced a booking — the model simply
answered "How will they confirm the time?" with "by sending you a text message". No send,
so no check, so no guard. The line type had never been looked up on that call at all;
`caller_line_type` had never appeared in production logs in the five days since.

So the knowledge has to reach the model, not just the sender. The lookup is warmed off
the call path — it is a network round trip and the voice turn is already one un-streamed
LLM call — which means the answer arrives from about the second turn onward, and "unknown"
has to behave exactly as it does everywhere else: say nothing, send anyway.
"""
import sms_service


def _clear():
    sms_service._line_type_cache.clear()


def test_a_looked_up_landline_is_known_non_textable():
    _clear()
    sms_service._line_type_cache["+13605550140"] = "landline"
    assert sms_service.known_non_textable("+13605550140") is True


def test_a_mobile_is_not():
    _clear()
    sms_service._line_type_cache["+12535550142"] = "mobile"
    assert sms_service.known_non_textable("+12535550142") is False


def test_voip_stays_textable():
    """Google Voice and softphones are voip and do receive texts. Refusing them would be
    a worse bug than the one this fixes — see _NON_TEXTABLE_LINE_TYPES."""
    _clear()
    for kind in ("voip", "nonfixedvoip", "fixedvoip"):
        sms_service._line_type_cache["+15105550177"] = kind
        assert sms_service.known_non_textable("+15105550177") is False, kind


def test_a_number_not_yet_looked_up_is_not_treated_as_non_textable():
    """The whole point of warming off the call path: turn one may not have the answer,
    and unknown must mean "say nothing special", never "warn them"."""
    _clear()
    assert sms_service.known_non_textable("+13605550140") is False


def test_a_failed_lookup_is_not_treated_as_non_textable():
    """phone_line_type caches None on failure. Unknown means send, as everywhere else."""
    _clear()
    sms_service._line_type_cache["+13605550140"] = None
    assert sms_service.known_non_textable("+13605550140") is False


def test_blank_numbers_are_safe():
    _clear()
    for num in ("", "   ", None):
        assert sms_service.known_non_textable(num) is False


def test_prefetch_does_not_look_up_twice(monkeypatch):
    """One network call per number. A salon's callers are mostly repeat callers."""
    _clear()
    calls = []
    monkeypatch.setattr(sms_service, "phone_line_type", lambda n: calls.append(n))
    sms_service._line_type_cache["+12535550142"] = "mobile"
    sms_service.prefetch_line_type("+12535550142")
    assert calls == [], "looked up a number already in the cache"


def test_prefetch_is_off_when_the_lookup_is_disabled(monkeypatch):
    _clear()
    calls = []
    monkeypatch.setattr(sms_service, "phone_line_type", lambda n: calls.append(n))
    monkeypatch.setenv("VOICE_SMS_LINE_TYPE_LOOKUP", "0")
    sms_service.prefetch_line_type("+13605550140")
    assert calls == []


def test_prefetch_never_raises_on_a_blank_number():
    _clear()
    sms_service.prefetch_line_type("")
    sms_service.prefetch_line_type(None)
