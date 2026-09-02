"""The model is told whether the stylist works that day. It does not work it out.

Against a roster reading Terrance=thu,fri,sun, on three separate calls:

    "Terrance doesn't work on Thursdays. He is available on Friday and Sunday."
    "Terrance doesn't work on Fridays. He is available on Thursday, Friday, and Sunday."
    "Terrance doesn't work on Sundays. He is available on Thursday, Friday, and Sunday."

Every one of those days is on his line, and the last two refuse a day while offering it
in the same breath — the tell that this is a refusal being completed rather than a
roster being read. Three prompt rewrites did not stop it.

A wrong refusal is the most expensive thing this system does. It turns away a paying
customer and records appointment_created=False, which in the dashboard is
indistinguishable from a caller who changed their mind, so nobody ever finds out.

staff_unavailable_message has always known the answer — it is the backstop that keeps a
BOOKING off a day the stylist is off. The gap was that it only runs when a BOOKING line
exists, and a refusal never emits one. So the verdict is computed before the reply and
handed over as a fact.
"""

from datetime import date, timedelta

import conversation_service as cs

BIZ = {
    "name": "Gig Harbor Hair Masters",
    "hours": "Tue–Fri: 9:00 AM – 6:00 PM\nSaturday: 9:00 AM – 5:00 PM\nSunday: 11:00 AM – 4:00 PM",
    "services": [{"id": "svc_cut", "name": "Shampoo & Haircut"}],
    "staff": [
        {
            "id": "st_t",
            "name": "Terrance",
            "service_ids": [],
            "working_days": ["thu", "fri", "sun"],
        },
        {
            "id": "st_m",
            "name": "Melissa",
            "service_ids": [],
            "working_days": ["tue", "wed", "sat"],
        },
    ],
}


def _turn(said: str):
    # The note only builds on a call that looks like a booking, which is what
    # _conversation_suggests_booking gates on — so every case opens the way the real
    # transcripts do ("I'd like to book a shampoo and haircut with Terrence on Friday").
    return [{"role": "user", "content": f"I'd like to book an appointment, {said}"}]


def _weekday_name(offset_from_today: int = 0) -> str:
    return (date.today() + timedelta(days=offset_from_today)).strftime("%A")


def _note(said: str):
    return cs.stylist_day_availability_note(_turn(said), BIZ)


# --- the days he works --------------------------------------------------------


def test_a_day_he_works_is_stated_as_available():
    for day in ("Thursday", "Friday", "Sunday"):
        note = _note(f"a shampoo and haircut with Terrance on {day} at 2pm")
        assert note, day
        assert "DOES work" in note, day
        assert day in note
        assert "do not list" in note.lower()


def test_it_forbids_the_exact_sentence_that_lost_the_customer():
    note = _note("a shampoo and haircut with Terrance on Sunday at noon")
    assert "Do NOT tell the caller Terrance is unavailable" in note


# --- the days he does not ------------------------------------------------------


def test_a_day_he_is_off_is_stated_as_unavailable():
    for day in ("Monday", "Tuesday", "Wednesday", "Saturday"):
        note = _note(f"a shampoo and haircut with Terrance on {day} at 2pm")
        assert note, day
        assert "does NOT work" in note, day
        assert "Do not book that day" in note


def test_each_stylist_gets_their_own_verdict():
    """Melissa works Tuesday and Terrance does not — the same day, opposite answers."""
    tue_t = _note("with Terrance on Tuesday at 2pm")
    tue_m = _note("with Melissa on Tuesday at 2pm")
    assert "does NOT work" in tue_t
    assert "DOES work" in tue_m


# --- reading the caller --------------------------------------------------------


def test_the_last_day_named_wins():
    """A caller who changes their mind names the new day second."""
    note = _note("with Terrance on Tuesday. Actually, make it Friday instead.")
    assert "DOES work" in note
    assert "Friday" in note


def test_the_last_stylist_named_wins():
    note = _note("with Melissa on Friday. Sorry, I meant Terrance.")
    assert "Terrance" in note
    assert "DOES work" in note  # Terrance works Friday; Melissa does not


def test_today_and_tomorrow_resolve():
    for word, offset in (("today", 0), ("tomorrow", 1)):
        note = _note(f"a haircut with Terrance {word}")
        # Only asserts it resolved to the right weekday; which verdict depends on the day.
        if note:
            assert _weekday_name(offset) in note, word


def test_no_day_named_says_nothing():
    assert _note("I'd like a haircut with Terrance") is None


def test_no_stylist_named_says_nothing():
    """"Anyone's fine" has no single schedule to rule on."""
    assert _note("I'd like a haircut on Friday at 2pm") is None


def test_a_stylist_not_on_the_roster_says_nothing():
    assert _note("a haircut with Priya on Friday") is None


def test_bare_conversation_says_nothing():
    assert cs.stylist_day_availability_note([], BIZ) is None
    assert cs.stylist_day_availability_note(None, BIZ) is None


def test_sat_the_verb_is_not_saturday():
    """Full weekday names only — "sat" is an ordinary word in a salon transcript."""
    note = _note("I sat in the chair last time with Terrance")
    assert note is None
