"""A request cannot be filed without a name, so the caller has to be asked for one.

2026-09-09, go-live day. A caller spent four minutes on the phone:

    18:35:11  her  "Blow dry."
    18:35:25  her  "Anyone is fine."
    18:36:04  her  "Today at 03:00."
    18:36:34  ai   "I've noted your request ... today at 3:00 PM."
    18:37:18  ai   "Your request for today at 3:00 PM will be passed on to the salon."
    18:37:43  call_end  appointment_created=False

Service, stylist and time, all collected. No name — nobody ever asked. Field 1 of a
BOOKING line is the caller's name and the validator will not accept the line without it,
so there was nothing to file, and the model filled the silence by saying it was done.

The nudge chain walked service, then stylist, then "you have enough details, output
BOOKING" — and never checked the one field that cannot be inferred from anything else.

The guard added the same day stops it CLAIMING the request was filed. This is what stops
it getting there.
"""
import conversation_service as cs

BIZ = {
    "name": "Gig Harbor Hair Masters",
    "hours": "Monday-Sunday: 9:00 AM - 5:00 PM",
    "services": [{"id": "svc", "name": "Shampoo & Haircut", "duration_minutes": 30}],
    "staff": [{"id": "s1", "name": "Melissa", "service_ids": []}],
}


def _history(*, asked_name: bool = False):
    """Her call: service, stylist and time all given, four user turns in."""
    h = [
        {"role": "user", "content": "I'd like to book an appointment please"},
        {"role": "assistant", "content": "Which service would you like?"},
        # Said exactly as the menu spells it: service_choice_resolved is a strict match,
        # so anything looser stops the nudge chain at the service step and never reaches
        # the branch under test here. (That strictness is its own bug — see the note in
        # test_service_choice_matching_is_too_strict.)
        {"role": "user", "content": "Shampoo & Haircut"},
        {"role": "assistant", "content": "Do you have a preferred stylist, or is anyone fine?"},
        {"role": "user", "content": "anyone is fine"},
        {"role": "assistant", "content": "What day and time would you like?"},
        {"role": "user", "content": "today at 3 PM"},
    ]
    if asked_name:
        h.append({"role": "assistant", "content": "May I have your name for the request?"})
    return h


def _nudge(history, caller_memory=None):
    return cs._voice_booking_nudge_message(history, BIZ, caller_memory=caller_memory)


def test_it_asks_for_the_name_instead_of_filing_nothing(monkeypatch):
    monkeypatch.setattr(cs.config_service, "get_business_info", lambda: BIZ)
    msg = _nudge(_history())
    assert msg is not None
    assert "NOT the caller's name" in msg
    assert "Ask ONE short question: their name" in msg
    # And it must not be told to file, nor to claim it has.
    assert "Do NOT output BOOKING yet" in msg
    assert "noted, passed on" in msg


def test_it_does_not_ask_twice_in_a_row(monkeypatch):
    """Being asked your name twice running is the other thing she complained about."""
    monkeypatch.setattr(cs.config_service, "get_business_info", lambda: BIZ)
    msg = _nudge(_history(asked_name=True))
    assert msg is None or "their name" not in msg


def test_a_repeat_caller_we_already_know_is_not_asked(monkeypatch):
    monkeypatch.setattr(cs.config_service, "get_business_info", lambda: BIZ)
    msg = _nudge(_history(), caller_memory={"name": "Dev"})
    assert msg is not None
    assert "Output BOOKING" in msg, "should go straight to filing"
    assert "their name" not in msg


def test_a_stylist_name_in_memory_is_not_the_caller(monkeypatch):
    """caller_memory can hold a roster name from a mis-parse. That is not a caller name,
    and treating it as one is how a booking ends up filed under the stylist."""
    monkeypatch.setattr(cs.config_service, "get_business_info", lambda: BIZ)
    msg = _nudge(_history(), caller_memory={"name": "Melissa"})
    assert "their name" in (msg or ""), "took a stylist for the caller"


def test_a_placeholder_in_memory_is_not_a_name(monkeypatch):
    monkeypatch.setattr(cs.config_service, "get_business_info", lambda: BIZ)
    for junk in ("", " ", "there", "caller", "customer", "guest", "D"):
        msg = _nudge(_history(), caller_memory={"name": junk})
        assert "their name" in (msg or ""), junk


def test_a_finished_booking_is_never_nudged(monkeypatch):
    """Unchanged guard: nudging after a booking lands creates a duplicate appointment
    and a duplicate confirmation text."""
    monkeypatch.setattr(cs.config_service, "get_business_info", lambda: BIZ)
    assert (
        cs._voice_booking_nudge_message(_history(), BIZ, appointment_created=True) is None
    )
