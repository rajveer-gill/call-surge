"""A visit can be more than one service.

From Lana's test calls, 2026-08-28:

    "I tried to add a highlight service, and it said it would, but it did not."
    "I then called to book a haircut and all-over color with Terrance to test that it
     can book two services at the same time…"

The service matcher returned the FIRST menu entry it found in the reason field and
stopped. "Haircut and All Over Color" resolved to "Haircut", the canonical name was
written back over the reason, and the second service was gone — from the request the
salon receives, from the confirmation text, and from the length of the booking.
"""

import booking_service as bs
import sms_appointment_updates as sau

BIZ = {
    "services": [
        {"id": "svc_cut", "name": "Haircut", "price": 45, "duration_minutes": 30},
        {"id": "svc_color", "name": "All Over Color", "price": 90, "duration_minutes": 90},
        {"id": "svc_hl", "name": "Highlight", "price": 40, "duration_minutes": 45},
        {
            "id": "svc_cond",
            "name": "Deep Conditioner",
            "price": 15,
            "duration_minutes": 0,
            "is_addon": True,
        },
    ],
    "staff": [{"id": "st_t", "name": "Terrance", "service_ids": []}],
}


def test_both_services_survive_the_matcher():
    names, required = bs.normalize_service_choices_for_booking(
        "Haircut and All Over Color", BIZ
    )
    assert names == ["Haircut", "All Over Color"]
    assert required is True


def test_the_order_the_caller_said_them_in_is_kept():
    names, _ = bs.normalize_service_choices_for_booking("All Over Color + Haircut", BIZ)
    assert names == ["All Over Color", "Haircut"]


def test_a_longer_name_wins_over_a_substring_of_it():
    """"Color" must not eat the text that belongs to "All Over Color" and leave the
    caller booked for a service they never asked for."""
    names, _ = bs.normalize_service_choices_for_booking("All Over Color", BIZ)
    assert names == ["All Over Color"]


def test_one_service_still_behaves_exactly_as_before():
    assert bs._normalize_service_choice_for_booking("Haircut", BIZ) == ("Haircut", True)
    assert bs._normalize_service_choice_for_booking("", BIZ) == (None, True)
    assert bs._normalize_service_choice_for_booking("—", BIZ) == (None, True)


def test_a_fragment_of_a_menu_name_still_matches():
    """The caller says "a color"; the menu says "All Over Color"."""
    names, _ = bs.normalize_service_choices_for_booking("color", BIZ)
    assert names == ["All Over Color"]


def test_a_shop_with_no_menu_is_untouched():
    names, required = bs.normalize_service_choices_for_booking("trim", {"services": []})
    assert names == ["trim"]
    assert required is False


def test_the_add_on_is_never_the_primary_service():
    """"A deep conditioner and a haircut" is a haircut appointment with a conditioner on
    it, whichever order it was said in — the stylist and the slot follow the haircut."""
    assert bs.primary_service_name(["Deep Conditioner", "Haircut"], BIZ) == "Haircut"


def test_the_block_is_as_long_as_everything_booked():
    """30 minutes for a cut and a colour is how a stylist's day gets double-booked."""
    assert bs._service_duration_minutes_for_reason("Haircut", BIZ) == 30
    assert bs._service_duration_minutes_for_reason("Haircut + All Over Color", BIZ) == 120


def test_a_zero_minute_add_on_adds_no_time():
    assert bs._service_duration_minutes_for_reason("Haircut + Deep Conditioner", BIZ) == 30


def test_the_reason_field_reads_as_both_services():
    assert bs.format_service_choices(["Haircut", "Highlight"]) == "Haircut + Highlight"


# --- adding a service to a request that already exists -------------------------


def test_adding_a_service_keeps_the_one_they_already_have():
    assert sau.merge_added_service("Haircut", "Highlight") == "Haircut + Highlight"


def test_adding_a_service_they_already_have_changes_nothing():
    assert sau.merge_added_service("Haircut + Highlight", "Highlight") is None
    assert sau.merge_added_service("Haircut", "haircut") is None


def test_an_add_request_is_recognised_from_how_people_actually_say_it():
    for said in (
        "can you add a highlight",
        "I'd also like a highlight",
        "and a highlight as well",
        "throw in a highlight too",
    ):
        assert sau.parse_service_addition(
            said, current_service="Haircut", known_services=["Haircut", "Highlight"]
        ) == "Highlight", said


def test_a_plain_swap_is_not_read_as_an_addition():
    """"Make it a highlight instead" replaces the haircut. Merging it would book the
    caller for two services they never asked for."""
    assert (
        sau.parse_service_addition(
            "actually make it a highlight instead",
            current_service="Haircut",
            known_services=["Haircut", "Highlight"],
        )
        is None
    )
    assert (
        sau.parse_service_from_sms(
            "actually make it a highlight instead",
            current_service="Haircut",
            known_services=["Haircut", "Highlight"],
        )
        == "Highlight"
    )


def test_a_bare_and_is_not_an_addition_marker():
    """"A cut and colour" states the whole booking; reading it as an addition to an
    existing cut would double it."""
    assert sau.text_requests_additional_service("a cut and colour please") is False
