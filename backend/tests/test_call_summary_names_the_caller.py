"""The dashboard summary must not call our own receptionist the caller.

From Gig Harbor's Call Analytics on go-live day, 2026-09-09:

    "Ava called to request a shampoo and haircut appointment with Melissa at Gig..."
    "The caller, Ava, initiated the call but did not specify a clear intent or purpose for..."

Ava answered those calls. She is the AI receptionist, and she opens every call with
"Hi, I'm Ava" — so in a Whisper transcript, which has no speaker labels, hers is the only
name present and the summariser reasonably concluded it was the customer's.

The Summary column is the one thing the salon reads to decide whether a call needs
following up, so naming the wrong person in it is worse than saying nothing. The fix is to
tell the summariser which voice is ours, since the transcript genuinely cannot.
"""
import voice_service as vs


def test_the_receptionist_is_named_as_the_one_answering():
    p = vs._call_summary_system_prompt("Ava", "Gig Harbor Hair Masters")
    assert "Ava" in p
    assert "ANSWERS the phone" in p
    assert "Ava is never the caller" in p


def test_the_business_name_is_used():
    p = vs._call_summary_system_prompt("Ava", "Gig Harbor Hair Masters")
    assert "Gig Harbor Hair Masters" in p


def test_it_still_asks_for_what_the_dashboard_needs():
    """The original instructions have to survive the fix — length, intent, no invention."""
    p = vs._call_summary_system_prompt("Ava", "Gig Harbor Hair Masters")
    assert "2-4 clear sentences" in p
    assert "caller intent" in p
    assert "do not invent" in p


def test_a_shop_that_never_named_its_receptionist_still_gets_a_usable_prompt():
    """receptionist_name is optional in Settings, so this runs with it blank."""
    p = vs._call_summary_system_prompt("", "")
    assert "the AI receptionist" in p
    assert "the business" in p
    # No stray empty quotes or dangling names where the values would have gone.
    assert "  " not in p


def test_whitespace_only_settings_are_treated_as_unset():
    p = vs._call_summary_system_prompt("   ", "\t")
    assert "the AI receptionist" in p
    assert "the business" in p
