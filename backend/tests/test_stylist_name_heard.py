"""A mis-heard stylist name must not restart the question.

From a real call on production, 2026-08-27:

    caller_said            "and with Terence."
    voice_booking_line_parsed   stylist_captured=Terrance   <- resolved correctly
    voice_booking_not_created   reason=missing_required_booking_fields
    ai_spoken              "Before I lock this in, which stylist would you like?"

The roster spells the stylist "Terrance". The caller said "Terrence" and the
transcript came back "Terence". The guard that checks whether a stylist was named
used an exact word match, decided nobody had been named, and asked again — so the
caller repeated the name, it failed identically, and the call ended without a
booking after 71 seconds.

The failing field was the STYLIST check while name, date and time were all fine,
which is why it read as "asking for things already confirmed".
"""
import pytest

import conversation_service as cs

ROSTER = [{"id": "s1", "name": "Melissa"}, {"id": "s2", "name": "Terrance"},
          {"id": "s3", "name": "Taylor"}, {"id": "s4", "name": "Sisi"}]
BIZ = {"staff": ROSTER}


@pytest.mark.parametrize("said", [
    "and with Terence.",      # what the transcript actually carried
    "Terrence.",              # the other common spelling
    "Terrance",               # exact
    "I'd like Terence please",
])
def test_a_misheard_stylist_still_counts_as_chosen(said):
    assert cs._caller_indicated_stylist_choice(said, BIZ) is True


@pytest.mark.parametrize("said", [
    "Tyler",                  # a different name, not a mis-hearing of Taylor
    "yeah.",                  # the caller's actual reply when re-asked
    "book a haircut",
    "tomorrow at 1pm",
    "",
])
def test_things_that_are_not_a_stylist_choice(said):
    assert cs._caller_indicated_stylist_choice(said, BIZ) is False


def test_prefix_guard_separates_mishearing_from_a_different_name():
    """Distance alone also matches Tyler against Taylor, who are different people.
    Speech errors land in the middle and end of a name, not the start."""
    assert cs._name_said_aloud("terrance", "terence") is True
    assert cs._name_said_aloud("taylor", "tyler") is False


def test_short_names_are_left_to_exact_matching():
    """Two characters of slack on a short name matches almost anything."""
    assert cs._name_said_aloud("ann", "and") is False
    assert cs._name_said_aloud("jo", "go") is False


def test_no_preference_still_works():
    assert cs._caller_indicated_stylist_choice("anyone is fine", BIZ) is True
