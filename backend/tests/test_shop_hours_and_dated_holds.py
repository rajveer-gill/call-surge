"""Two ways the receptionist got a date/time wrong, both from the same root:
the model was left to work something out that the code already knows.

Puneet, 2026-09-02:

    "I made an appointment on Sunday at 10AM (we open at 11AM), do we also want to
     block out those hours."

The validator checked whether the shop had already closed for TODAY, whether the date
was a declared closure, and the stylist's own days and hours — but never whether the
requested time was inside the shop's opening hours for that weekday. Terrance's own
hours allowed 10 AM, so it went through an hour before the doors open.

And from a test call the same afternoon, with one hold in the system (Terrance,
Thursday 2026-09-03 at 2:00 PM):

    caller: "...with Terrance on Friday at 2PM."
    Ava:    "Terrance is not available at 2 PM on Friday."

Friday was free. The hold was Thursday's. The prompt listed it as a bare `2026-09-03`
and the model had to derive the weekday itself, which it got wrong — so the holds are
now labelled with their day.

The refusal path is the dangerous one here, so the hours check only ever fires on a
weekday whose hours were genuinely read off the tenant's text. Unset or unparseable
hours fall back to Mon-Fri 9-5 weekend-closed, and enforcing that would refuse every
weekend caller at any store that left the field blank.
"""

import conversation_service as cs
from business_hours import day_slot_for_date_explicit, parse_hours_to_weekly
from booking_service import _weekday_label_for_date
import datetime

# Gig Harbor's real hours, en-dash and Tue-Fri range exactly as stored.
HOURS = (
    "Monday: 10:00 AM – 5:00 PM\n"
    "Tue–Fri: 9:00 AM – 6:00 PM\n"
    "Saturday: 9:00 AM – 5:00 PM\n"
    "Sunday: 11:00 AM – 4:00 PM"
)

BIZ = {
    "name": "Gig Harbor Hair Masters",
    "booking_mode": "external",
    "hours": HOURS,
    "services": [{"id": "svc_cut", "name": "Shampoo & Haircut", "duration_minutes": 30}],
    "staff": [
        {
            "id": "st_t",
            "name": "Terrance",
            "service_ids": [],
            "working_days": ["thu", "fri", "sun"],
        }
    ],
}

SUNDAY = "2099-07-12"  # a Sunday
FRIDAY = "2099-07-10"


def _booking(**over):
    b = {
        "name": "Puneet",
        "phone": "+19259971684",
        "email": "",
        "date": SUNDAY,
        "time": "10:00",
        "reason": "Shampoo & Haircut",
        "staff": "Terrance",
    }
    b.update(over)
    return b


def _said():
    return [{"role": "user", "content": "a shampoo and haircut with Terrance"}]


def _free(monkeypatch):
    monkeypatch.setattr(
        cs.booking_service, "is_slot_available", lambda *a, **k: True
    )


# --- the shop's opening hours -------------------------------------------------


def test_before_opening_is_refused(monkeypatch):
    """Puneet's booking. 10 AM on a Sunday the salon opens at 11."""
    monkeypatch.setattr(cs.config_service, "get_business_info", lambda: BIZ)
    _free(monkeypatch)
    ok, msg, _, _ = cs._validate_booking_requirements(
        _booking(), BIZ, conversation_history=_said()
    )
    assert ok is False
    assert "Sunday" in msg
    assert "11:00 AM" in msg and "4:00 PM" in msg  # told when they CAN come
    assert "10:00 AM" in msg


def test_after_closing_is_refused(monkeypatch):
    monkeypatch.setattr(cs.config_service, "get_business_info", lambda: BIZ)
    _free(monkeypatch)
    ok, msg, _, _ = cs._validate_booking_requirements(
        _booking(time="17:00"), BIZ, conversation_history=_said()
    )
    assert ok is False, "5 PM is past Sunday's 4 PM close"


def test_inside_opening_hours_is_allowed(monkeypatch):
    monkeypatch.setattr(cs.config_service, "get_business_info", lambda: BIZ)
    _free(monkeypatch)
    ok, msg, _, _ = cs._validate_booking_requirements(
        _booking(time="11:30"), BIZ, conversation_history=_said()
    )
    assert ok is True, msg


def test_opening_time_itself_is_allowed(monkeypatch):
    """11:00 on the dot is open, not "outside hours"."""
    monkeypatch.setattr(cs.config_service, "get_business_info", lambda: BIZ)
    _free(monkeypatch)
    ok, msg, _, _ = cs._validate_booking_requirements(
        _booking(time="11:00"), BIZ, conversation_history=_said()
    )
    assert ok is True, msg


def test_a_weekday_with_different_hours_uses_its_own(monkeypatch):
    """Friday opens at 9, so 10 AM is fine there even though it is not on Sunday."""
    monkeypatch.setattr(cs.config_service, "get_business_info", lambda: BIZ)
    _free(monkeypatch)
    ok, msg, _, _ = cs._validate_booking_requirements(
        _booking(date=FRIDAY, time="10:00"), BIZ, conversation_history=_said()
    )
    assert ok is True, msg


def test_a_day_the_shop_is_closed_is_refused(monkeypatch):
    biz = {**BIZ, "hours": HOURS + "\nSunday: Closed"}
    monkeypatch.setattr(cs.config_service, "get_business_info", lambda: biz)
    _free(monkeypatch)
    ok, msg, _, _ = cs._validate_booking_requirements(
        _booking(time="12:00"), biz, conversation_history=_said()
    )
    assert ok is False
    assert "closed on Sunday" in msg


# --- never refuse on hours we only guessed ------------------------------------


def test_unset_hours_refuse_nobody(monkeypatch):
    """The fallback closes the weekend. Acting on it would lose every weekend caller."""
    biz = {**BIZ, "hours": ""}
    monkeypatch.setattr(cs.config_service, "get_business_info", lambda: biz)
    _free(monkeypatch)
    for t in ("07:00", "10:00", "21:00"):
        ok, msg, _, _ = cs._validate_booking_requirements(
            _booking(time=t), biz, conversation_history=_said()
        )
        assert ok is True, f"{t}: {msg}"


def test_unparseable_hours_refuse_nobody(monkeypatch):
    biz = {**BIZ, "hours": "call us lol"}
    monkeypatch.setattr(cs.config_service, "get_business_info", lambda: biz)
    _free(monkeypatch)
    ok, msg, _, _ = cs._validate_booking_requirements(
        _booking(time="08:00"), biz, conversation_history=_said()
    )
    assert ok is True, msg


def test_bare_times_without_days_are_not_treated_as_explicit():
    """"9am to 5pm" says nothing about WHICH days — Mon-Fri is our assumption."""
    _, explicit = day_slot_for_date_explicit(
        {"hours": "9am to 5pm"}, datetime.date(2099, 7, 12)
    )
    assert explicit is False


def test_the_parsed_schedule_is_unchanged_for_existing_callers():
    """parse_hours_to_weekly still returns exactly what it used to."""
    w = parse_hours_to_weekly(HOURS)
    assert (w[6].open, w[6].close) == ("11:00", "16:00")  # Sunday
    assert (w[0].open, w[0].close) == ("10:00", "17:00")  # Monday
    assert (w[3].open, w[3].close) == ("09:00", "18:00")  # Thursday


# --- holds carry their weekday ------------------------------------------------


def test_a_hold_names_its_weekday():
    assert _weekday_label_for_date("2026-09-03") == "Thursday "
    assert _weekday_label_for_date("2026-09-04") == "Friday "


def test_an_unreadable_date_degrades_quietly():
    for bad in ("", "junk", None, "2026-13-45"):
        assert _weekday_label_for_date(bad) == ""
