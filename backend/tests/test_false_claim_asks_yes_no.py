"""Catching a false claim must not cost the caller everything they just said.

Raj, 2026-09-09, after giving service, stylist, day and time across four turns:

    22:32:32  him  "Let's do Thursday at 03:00."
    22:32:33  ai   "Thank you! I've put in a request for a "Shampoo & Haircut" on
                    Thursday, September 10th, at 3:00 PM. The salon will confirm..."
    22:32:34  ai   "I haven't locked anything in just yet—I want to make sure I've got it
                    right. Can you confirm the service, day, and time you'd like?"
    22:32:50  call_end  appointment_created=False

The guard was right: no BOOKING line was emitted, so nothing had been filed and the claim
was false. But its replacement discarded a sentence that had every detail correct and
asked him to supply all of it again — "asked for info I already gave it", which is the
same complaint Lana has raised about repeated questions.

The details are reused instead, so the caller answers yes or no. That "yes" is the turn
where the model finally emits BOOKING.
"""
import conversation_service as cs

REAL = (
    'Thank you! I\'ve put in a request for a "Shampoo & Haircut" on Thursday, '
    "September 10th, at 3:00 PM. The salon will confirm the time with you."
)


def test_his_reply_becomes_a_yes_no():
    out = cs._confirmation_instead_of_false_claim(REAL)
    assert "Shampoo & Haircut" in out
    assert "Thursday, September 10th, at 3:00 PM" in out
    assert "Is that correct?" in out
    # And it must no longer claim the thing that had not happened.
    assert "put in a request" not in out
    # Nor ask him to say it all again.
    assert "confirm the service, day, and time you'd like" not in out


def test_only_the_first_sentence_of_detail_is_read_back():
    """"The salon will confirm the time with you" is not part of the booking."""
    out = cs._confirmation_instead_of_false_claim(REAL)
    assert "The salon will confirm" not in out


def test_other_phrasings_of_the_claim_are_handled():
    for said in (
        "I've noted your request for a haircut with Melissa on Friday at 2 PM.",
        "I have sent your request for a Shampoo & Haircut tomorrow at 10 AM.",
        "We've submitted your request for a blow dry on Saturday at 1 PM.",
    ):
        out = cs._confirmation_instead_of_false_claim(said)
        assert "Is that correct?" in out, said
        assert "let me make sure I have it right" in out, said


def test_a_claim_with_no_usable_detail_falls_back():
    """A clumsy generic question beats a confident wrong one."""
    for thin in (
        "I've put in your request.",
        "Your request has been sent.",
        "I've noted your request. Bye!",
    ):
        assert cs._confirmation_instead_of_false_claim(thin) == cs._GENERIC_CONFIRM_ASK, thin


def test_an_overlong_detail_falls_back():
    rambling = "I've put in a request for " + ("a very long service name " * 12) + "."
    assert cs._confirmation_instead_of_false_claim(rambling) == cs._GENERIC_CONFIRM_ASK


def test_text_with_no_claim_at_all_falls_back():
    assert cs._confirmation_instead_of_false_claim("What day works for you?") == (
        cs._GENERIC_CONFIRM_ASK
    )
    assert cs._confirmation_instead_of_false_claim("") == cs._GENERIC_CONFIRM_ASK


def test_the_guard_still_fires_on_the_claim():
    """The rewrite is downstream of detection; detection itself is unchanged."""
    assert cs._ai_implies_committed_booking(REAL) is True
