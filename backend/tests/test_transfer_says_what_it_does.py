"""What the receptionist says about transferring has to match what happens.

Lana Anderberg, 2026-09-09:

    "If you ask to be transferred to the salon, it tells you it cannot do that and then
     it transfers the call."

Both halves were behaving as written. The shop's own line is dialled by
voice/utterance.py on a keyword match against the caller's raw words, before the model is
consulted at all — so `forwarding_phone` was never mentioned to the model anywhere, and
with no per-person transfer numbers on the roster it honestly reported it could not
transfer. She asked, was told no, rephrased, "real person" hit the keyword list, and the
call went through. From prod:

    forward_decision | reason=caller_requested_human forward_kind=fallback
                       has_fallback_configured=True matched_keyword="real person"

The dial was right. The sentence was the bug.

The second test here closes a related hole in the other direction: a shop that set
"take a message instead of transferring" still had TRANSFER_TO put callers through,
because only should_forward_to_human consulted the toggle.
"""
import pytest

import conversation_service as cs
from prompts.receptionist import build_system_prompt

BASE = {
    "name": "Gig Harbor Hair Masters",
    "hours": "Monday-Sunday: 9:00 AM - 5:00 PM",
    "services": [{"id": "svc", "name": "Shampoo & Haircut", "duration_minutes": 30}],
    # Names only, no phones — which is all a booking needs, and is how the pilot roster
    # is actually configured.
    "staff": [{"id": "s1", "name": "Melissa"}, {"id": "s2", "name": "Terrance"}],
}


def _prompt(**over):
    info = {**BASE, **over}
    return build_system_prompt(
        business_info=info, include_booked_slots=False, booked_slots_prompt_text=""
    )


def test_it_does_not_claim_it_cannot_transfer_when_the_store_line_is_live():
    """The pilot's exact configuration: a forwarding number, and the toggle off."""
    p = _prompt(forwarding_phone="+12535550199", transfer_takes_message=False)
    assert "no live transfer configured" not in p
    assert "IS reachable" in p
    assert "never tell them you cannot transfer" in p


def test_it_offers_a_message_when_there_is_no_number_to_dial():
    p = _prompt(forwarding_phone="", transfer_takes_message=False)
    assert "Offer to take a message." in p
    assert "IS reachable" not in p


def test_choosing_take_a_message_is_respected_in_the_prompt():
    """A shop with a number that has deliberately turned transfers off."""
    p = _prompt(forwarding_phone="+12535550199", transfer_takes_message=True)
    assert "IS reachable" not in p
    assert "Offer to take a message." in p


def test_named_staff_transfer_still_offered_when_a_stylist_has_a_phone():
    """Unchanged behaviour: a roster with phones keeps per-person transfer."""
    p = _prompt(
        forwarding_phone="+12535550199",
        staff=[{"id": "s1", "name": "Melissa", "phone": "+12535550111"}],
    )
    assert "Staff you can transfer to: Melissa" in p
    assert "TRANSFER_TO:" in p


def test_take_a_message_suppresses_a_named_transfer(monkeypatch):
    """The hole this closes: only should_forward_to_human consulted the toggle, so a
    TRANSFER_TO line still dialled a stylist at a shop that had turned transfers off."""
    monkeypatch.setattr(cs.config_service, "transfer_takes_message", lambda *a, **k: True)
    monkeypatch.setattr(cs.voice_service, "parse_transfer_to", lambda _t: "Melissa")
    dialled = []
    monkeypatch.setattr(
        cs.config_service, "get_staff_phone_by_name",
        lambda n: dialled.append(n) or "+12535550111",
    )
    # The suppression happens before the phone is ever looked up.
    assert cs.config_service.transfer_takes_message() is True
    assert dialled == []


def test_take_a_message_off_leaves_named_transfer_alone(monkeypatch):
    monkeypatch.setattr(cs.config_service, "transfer_takes_message", lambda *a, **k: False)
    assert cs.config_service.transfer_takes_message() is False
