"""A customer really can be called Melissa.

Lana Anderberg, 2026-09-09:

    "It can't distinguish a customer with the same name as our stylists, it gets confused
     and books with the stylist with that name and won't take their name."

Field 1 of a BOOKING line was erased whenever it matched anyone on the roster. A booking
cannot be filed without a name, so a caller who happened to share a stylist's first name
could never book — not "sometimes got confused", but structurally could not complete a
call. On a four-stylist roster that is a real slice of the town.

The guard was written for the model copying the stylist into the caller slot, which is a
genuine and repeated mistake. That case is still caught by comparing field 1 against
field 7. What is no longer assumed is that any roster name in field 1 must be the model's
error rather than the caller's actual name.
"""
import conversation_service as cs

BIZ = {
    "name": "Gig Harbor Hair Masters",
    "services": [{"id": "svc", "name": "Shampoo & Haircut", "duration_minutes": 30}],
    "staff": [
        {"id": "s1", "name": "Melissa", "service_ids": []},
        {"id": "s2", "name": "Terrance", "service_ids": []},
    ],
}


def _apply(booking, caller_memory=None):
    b = dict(booking)
    cs._apply_booking_customer_name(b, caller_memory=caller_memory, info=BIZ)
    return b


def test_a_customer_called_melissa_keeps_her_name():
    """Booking with a different stylist. Nothing here suggests a mix-up."""
    b = _apply({"name": "Melissa", "staff": "Terrance"})
    assert b["name"] == "Melissa"


def test_a_customer_called_melissa_with_no_stylist_keeps_her_name():
    b = _apply({"name": "Melissa", "staff": ""})
    assert b["name"] == "Melissa"


def test_the_stylist_copied_into_the_caller_slot_is_still_caught():
    """The mistake the guard exists for: field 1 and field 7 are the same person."""
    b = _apply({"name": "Melissa", "staff": "Melissa"})
    assert b["name"] == ""


def test_a_known_caller_beats_a_roster_name_in_field_one():
    """We know this caller is Dev, so Melissa in field 1 is the model's error."""
    b = _apply({"name": "Melissa", "staff": ""}, caller_memory={"name": "Dev"})
    assert b["name"] == "Dev"


def test_a_known_caller_still_wins_when_the_stylist_matches():
    b = _apply({"name": "Melissa", "staff": "Melissa"}, caller_memory={"name": "Dev"})
    assert b["name"] == "Dev"


def test_an_ordinary_name_is_untouched():
    b = _apply({"name": "Raj", "staff": "Terrance"})
    assert b["name"] == "Raj"


def test_a_missing_name_is_filled_from_memory():
    b = _apply({"name": "", "staff": "Terrance"}, caller_memory={"name": "Dev"})
    assert b["name"] == "Dev"


def test_a_stylist_name_in_memory_does_not_become_the_caller():
    """Unchanged: caller-memory holding a roster name is not a usable caller name, so it
    must not be substituted in."""
    b = _apply({"name": "", "staff": "Terrance"}, caller_memory={"name": "Melissa"})
    assert b["name"] == ""
