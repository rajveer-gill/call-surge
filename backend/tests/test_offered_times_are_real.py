"""A time the receptionist offers must be a time the booking check will accept.

Lana Anderberg, testing on go-live morning, 2026-09-09:

    16:51:02  ai    "Terrance is working today, but the 10:45 AM time isn't available.
                     How about 12:00 PM or 1:00 PM instead?"
    16:51:55  Lana  "12:00 works."
    16:51:58  ai    "Terrance already has an appointment at 12:00 PM..."   (substituted)
    16:52:36  Lana  "Then why did you tell me 12:00 was available?"
    16:54:05  Lana  "Why did you offer me a 12:00?"
    16:54:14  call_end  appointment_created=False  duration_sec=229

Both halves were working as designed. The roster fact told the model Terrance works that
day; the slot check refused a booking on a taken slot. Nothing in between told the model
which times were FREE, so asked for an alternative it invented one, and the slot check
refused the time it had just offered — six times, for three and a half minutes.

Two things are asserted here: the fact handed to the model lists only genuinely free
times, and the refusal names real ones instead of asking the caller to guess again.
"""
import conversation_service as cs

BIZ = {
    "name": "Gig Harbor Hair Masters",
    "booking_mode": "external",
    "hours": "Monday-Sunday: 9:00 AM - 5:00 PM",
    "services": [{"id": "svc_cut", "name": "Shampoo & Haircut", "duration_minutes": 30}],
    "staff": [{"id": "st_t", "name": "Terrance", "service_ids": []}],
}
TERRANCE = BIZ["staff"][0]
DAY = "2099-07-08"  # a Wednesday, far enough out that nothing else is on the calendar


def _slots(taken):
    def is_slot_available(date, time, duration_minutes=30, staff_id=None):
        return (date, time) not in taken

    return is_slot_available


def _asked_for_terrance(monkeypatch):
    """Stand in for the date parser, which reads "Wednesday" and "tomorrow" rather than
    an ISO string. It has its own tests; what is under test here is which times the note
    names once a stylist and a day have been established."""
    monkeypatch.setattr(cs, "_requested_date_from_spoken_text", lambda *a, **k: DAY)
    return [
        {
            "role": "user",
            # "book" carries the booking intent the note gates on; without it the note
            # correctly stays silent, because there is no booking to protect.
            "content": "I would like to book a shampoo and haircut with Terrance at 12",
        }
    ]


def test_only_genuinely_free_times_come_back(monkeypatch):
    monkeypatch.setattr(cs.config_service, "get_business_info", lambda: BIZ)
    monkeypatch.setattr(
        cs.booking_service,
        "is_slot_available",
        _slots({(DAY, "12:00"), (DAY, "13:00")}),
    )
    free = cs.staff_free_times(TERRANCE, DAY, BIZ)
    assert "12:00" not in free, "offered the slot Lana was refused"
    assert "13:00" not in free
    assert "11:00" in free and "14:00" in free


def test_the_fact_never_names_a_taken_time(monkeypatch):
    """The exact shape of her call: 12:00 taken, so 12:00 must not be offered."""
    monkeypatch.setattr(cs.config_service, "get_business_info", lambda: BIZ)
    monkeypatch.setattr(cs.booking_service, "is_slot_available", _slots({(DAY, "12:00")}))
    note = cs.stylist_free_times_note(_asked_for_terrance(monkeypatch), BIZ)
    assert note is not None
    assert "12:00 PM" not in note
    assert "Terrance is free at" in note


def test_a_fully_booked_day_offers_nothing(monkeypatch):
    """Silence beats a guess — offering a time here is exactly the bug."""
    monkeypatch.setattr(cs.config_service, "get_business_info", lambda: BIZ)
    monkeypatch.setattr(cs.booking_service, "is_slot_available", lambda *a, **k: False)
    note = cs.stylist_free_times_note(_asked_for_terrance(monkeypatch), BIZ)
    assert note is not None
    assert "NO free times" in note
    assert "is free at" not in note


def test_an_unreadable_calendar_offers_nothing(monkeypatch):
    """A calendar that raises must not become a day that looks wide open."""
    monkeypatch.setattr(cs.config_service, "get_business_info", lambda: BIZ)

    def boom(*a, **k):
        raise RuntimeError("calendar down")

    monkeypatch.setattr(cs.booking_service, "is_slot_available", boom)
    assert cs.staff_free_times(TERRANCE, DAY, BIZ) == []


def test_a_stylist_who_is_off_that_day_offers_nothing(monkeypatch):
    monkeypatch.setattr(cs.config_service, "get_business_info", lambda: BIZ)
    monkeypatch.setattr(cs.booking_service, "is_slot_available", _slots(set()))
    off = {**TERRANCE, "time_off": [DAY]}
    assert cs.staff_free_times(off, DAY, BIZ) == []


def test_the_spoken_list_stays_short(monkeypatch):
    """A wide-open day is a recital if we read all of it out."""
    monkeypatch.setattr(cs.config_service, "get_business_info", lambda: BIZ)
    monkeypatch.setattr(cs.booking_service, "is_slot_available", _slots(set()))
    note = cs.stylist_free_times_note(_asked_for_terrance(monkeypatch), BIZ)
    assert note.count(" AM") + note.count(" PM") <= cs._MAX_OFFERED_TIMES


def test_the_refusal_names_real_times_rather_than_asking_again(monkeypatch):
    """"Would you like another time?" is what sent her round the loop."""
    monkeypatch.setattr(cs.config_service, "get_business_info", lambda: BIZ)
    monkeypatch.setattr(cs.booking_service, "is_slot_available", _slots({(DAY, "12:00")}))
    ok, msg, _, _ = cs._validate_booking_requirements(
        {
            "name": "Lana",
            "phone": "+12535550142",
            "email": "",
            "date": DAY,
            "time": "12:00",
            "reason": "Shampoo & Haircut",
            "staff": "Terrance",
        },
        BIZ,
        conversation_history=[
            {"role": "user", "content": "a shampoo and haircut with Terrance at 12"}
        ],
    )
    assert ok is False
    assert "already has an appointment at 12:00 PM" in msg
    assert "is free at" in msg
    # And it must not suggest the very slot it just refused.
    assert msg.count("12:00 PM") == 1


def test_the_refusal_falls_back_when_there_is_nothing_to_offer(monkeypatch):
    monkeypatch.setattr(cs.config_service, "get_business_info", lambda: BIZ)
    monkeypatch.setattr(cs.booking_service, "is_slot_available", lambda *a, **k: False)
    ok, msg, _, _ = cs._validate_booking_requirements(
        {
            "name": "Lana",
            "phone": "+12535550142",
            "email": "",
            "date": DAY,
            "time": "12:00",
            "reason": "Shampoo & Haircut",
            "staff": "Terrance",
        },
        BIZ,
        conversation_history=[
            {"role": "user", "content": "a shampoo and haircut with Terrance at 12"}
        ],
    )
    assert ok is False
    assert "another day, or a different stylist" in msg
    assert "is free at" not in msg


def test_a_time_that_has_already_passed_is_not_free(monkeypatch):
    """Lana, 2026-09-09: "Offered past times as available ie 9 am at 11:04 am."

    The shop's opening hours describe the whole day. A slot earlier than now has not
    got free, it has gone — and offering it sends the caller to a time they cannot take.
    Introduced by the free-times fix itself, which read the hours and forgot the clock.
    """
    from datetime import datetime
    from zoneinfo import ZoneInfo

    today = "2099-07-08"
    at_1104 = datetime(2099, 7, 8, 11, 4, tzinfo=ZoneInfo("America/Los_Angeles"))
    monkeypatch.setattr(cs, "business_local_now", lambda _biz=None: at_1104)
    monkeypatch.setattr(cs.config_service, "get_business_info", lambda: BIZ)
    monkeypatch.setattr(cs.booking_service, "is_slot_available", _slots(set()))

    free = cs.staff_free_times(TERRANCE, today, BIZ)
    assert "09:00" not in free, "offered a time three hours in the past"
    assert "11:00" not in free, "offered a time four minutes in the past"
    assert "14:00" in free, "the rest of the day is still bookable"


def test_a_future_day_is_bookable_from_opening(monkeypatch):
    """The cutoff is only about today; tomorrow's 9 AM is perfectly real."""
    from datetime import datetime
    from zoneinfo import ZoneInfo

    at_1104 = datetime(2099, 7, 8, 11, 4, tzinfo=ZoneInfo("America/Los_Angeles"))
    monkeypatch.setattr(cs, "business_local_now", lambda _biz=None: at_1104)
    monkeypatch.setattr(cs.config_service, "get_business_info", lambda: BIZ)
    monkeypatch.setattr(cs.booking_service, "is_slot_available", _slots(set()))

    free = cs.staff_free_times(TERRANCE, "2099-07-09", BIZ)
    assert "09:00" in free
