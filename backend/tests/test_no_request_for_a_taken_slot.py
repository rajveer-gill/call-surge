"""A request is never filed for a slot we already know is taken.

Lana, 2026-09-01:

    "I copied the appointments from Zenoti, and they populated in the appointments
     section. When I called to make an appointment, I specifically asked for a time
     that is already booked with Melissa. It still allowed me to request that exact
     time with Melissa."

Her call, from the logs:

    22:13:44  ai_said   Melissa is not available at 5 PM today, but she does have
                        openings earlier today at 9 AM, 10 AM ... Would any of those
                        times work for you?
    22:13:45  BOOKING   2026-09-01 | 17:00 | Shampoo & Haircut | Melissa
    22:13:45  booking_created_request apt_id=214 time=17:00
    22:13:45  [call ends on the confirmation]

The model declined the slot in prose and emitted a BOOKING line for that same slot on
the same turn. The prompt knew the slot was taken — that is where the refusal came
from — but no code path checked, so the request was filed and she was hung up on in
the middle of the question she had just been asked.

The slot check that would have caught it sat behind `if not external:`, and her store
is external (Zenoti). Validating here instead means the rejection speaks the reason and
leaves the call open.
"""

import conversation_service as cs

BIZ = {
    "name": "Gig Harbor Hair Masters",
    "booking_mode": "external",  # Zenoti — the real calendar lives elsewhere
    "services": [{"id": "svc_cut", "name": "Shampoo & Haircut", "duration_minutes": 30}],
    "staff": [
        {"id": "st_m", "name": "Melissa", "service_ids": []},
        {"id": "st_t", "name": "Terrance", "service_ids": []},
    ],
}


def _booking(**over):
    b = {
        "name": "Lana",
        "phone": "+12535550142",
        "email": "",
        "date": "2099-07-09",
        "time": "17:00",
        "reason": "Shampoo & Haircut",
        "staff": "Melissa",
    }
    b.update(over)
    return b


def _said(stylist="Melissa"):
    """The stylist gate wants the caller to have named them out loud, not just the
    BOOKING field — so every case here supplies the turn where they did."""
    return [{"role": "user", "content": f"a shampoo and haircut with {stylist} at five"}]


def _slots(taken):
    """Stand in for the calendar: `taken` is the set of (date, time) already booked."""

    def is_slot_available(date, time, duration_minutes=30, staff_id=None):
        return (date, time) not in taken

    return is_slot_available


def test_a_slot_we_know_is_taken_is_refused(monkeypatch):
    monkeypatch.setattr(cs.config_service, "get_business_info", lambda: BIZ)
    monkeypatch.setattr(
        cs.booking_service, "is_slot_available", _slots({("2099-07-09", "17:00")})
    )
    ok, msg, staff_id, _ = cs._validate_booking_requirements(_booking(), BIZ, conversation_history=_said())
    assert ok is False
    assert staff_id == "st_m"
    # The caller is told who, when, and what to do next — not just "no".
    assert "Melissa" in msg
    assert "5:00 PM" in msg
    assert "another time" in msg


def test_the_same_slot_with_a_free_stylist_is_allowed(monkeypatch):
    """The collision is per stylist. Terrance at 5 PM is a different calendar."""
    monkeypatch.setattr(cs.config_service, "get_business_info", lambda: BIZ)

    def is_slot_available(date, time, duration_minutes=30, staff_id=None):
        return staff_id != "st_m"

    monkeypatch.setattr(cs.booking_service, "is_slot_available", is_slot_available)
    ok, msg, staff_id, _ = cs._validate_booking_requirements(
        _booking(staff="Terrance"), BIZ, conversation_history=_said("Terrance")
    )
    assert ok is True, msg
    assert staff_id == "st_t"


def test_a_free_slot_still_books(monkeypatch):
    monkeypatch.setattr(cs.config_service, "get_business_info", lambda: BIZ)
    monkeypatch.setattr(cs.booking_service, "is_slot_available", _slots(set()))
    ok, msg, _, _ = cs._validate_booking_requirements(
        _booking(time="16:00"), BIZ, conversation_history=_said()
    )
    assert ok is True, msg


def test_a_store_that_imports_nothing_is_unaffected(monkeypatch):
    """An empty calendar must never refuse anyone.

    External stores hold only what they chose to import, so absent data has to mean
    "no blocker" — otherwise turning the check on would start refusing times that were
    genuinely free for every store that does not import.
    """
    monkeypatch.setattr(cs.config_service, "get_business_info", lambda: BIZ)
    monkeypatch.setattr(cs.booking_service, "is_slot_available", _slots(set()))
    for hour in ("09:00", "12:00", "17:00"):
        ok, msg, _, _ = cs._validate_booking_requirements(
            _booking(time=hour), BIZ, conversation_history=_said()
        )
        assert ok is True, f"{hour}: {msg}"


def test_it_applies_to_internal_stores_too(monkeypatch):
    internal = {**BIZ, "booking_mode": "internal"}
    monkeypatch.setattr(cs.config_service, "get_business_info", lambda: internal)
    monkeypatch.setattr(
        cs.booking_service, "is_slot_available", _slots({("2099-07-09", "17:00")})
    )
    ok, _, _, _ = cs._validate_booking_requirements(
        _booking(), internal, conversation_history=_said()
    )
    assert ok is False


def test_no_stylist_named_is_left_alone(monkeypatch):
    """"Anyone's fine" spans every calendar; one stylist being busy blocks nothing."""
    monkeypatch.setattr(cs.config_service, "get_business_info", lambda: BIZ)
    called = []

    def is_slot_available(date, time, duration_minutes=30, staff_id=None):
        called.append(staff_id)
        return False

    monkeypatch.setattr(cs.booking_service, "is_slot_available", is_slot_available)
    cs._validate_booking_requirements(_booking(staff=""), BIZ)
    # Whatever else the validator decides, it must not have consulted one stylist's
    # calendar to answer a request that named none.
    assert called == []


def test_a_booking_with_no_time_is_not_checked(monkeypatch):
    monkeypatch.setattr(cs.config_service, "get_business_info", lambda: BIZ)

    def boom(*a, **k):  # pragma: no cover - must not run
        raise AssertionError("no time means nothing to check")

    monkeypatch.setattr(cs.booking_service, "is_slot_available", boom)
    ok, msg, _, _ = cs._validate_booking_requirements(
        _booking(time=""), BIZ, conversation_history=_said()
    )
    assert ok is True, msg
