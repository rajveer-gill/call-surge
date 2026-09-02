"""The availability rule must tell the model what to do when the stylist IS free.

Two live calls, 2026-09-02, against a roster reading `Terrance=thu,fri,sun`:

    caller: "...with Terrance on Thursday at 2PM."
    Ava:    "Terrance doesn't work on Thursdays. He is available on Friday and Sunday."

    caller: "...with Terrance on Friday at 2PM."
    Ava:    "Terrance doesn't work on Fridays. He is available on Thursday, Friday,
             and Sunday."

Both days are on his line. The second reply refuses and offers the same day in one
breath, which gives the mechanism away: the model was not reading the roster and
deciding, it was filling in a refusal template the prompt had shown it —

    "if a stylist works Monday, Wednesday, Friday and the caller asks for Thursday,
     respond that they don't work Thursdays and offer Monday, Wednesday, or Friday"

— swapping the caller's day into "doesn't work <day>s". Every instruction and both
examples in that block described turning someone away; nothing described the case
where the stylist is free, which is almost every call.

Refusing a day the stylist works turns away a paying customer and leaves
`appointment_created=False`, indistinguishable from a caller who changed their mind.
So the rule now has two branches, the available one first, and the examples show a
booking going through.
"""

from prompts.receptionist import build_system_prompt

# Lana's roster as it stood when the calls above were made.
BIZ = {
    "name": "Gig Harbor Hair Masters",
    "hours": "Tue-Fri 9am-6pm",
    "services": [{"id": "svc_cut", "name": "Shampoo & Haircut", "price": 35}],
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


def _availability_block(prompt: str) -> str:
    marker = "Stylist availability"
    assert marker in prompt, "no per-stylist availability block in prompt"
    return prompt[prompt.index(marker) :]


def test_the_available_case_is_stated_and_comes_first():
    """The common case — the stylist works that day — needs its own branch."""
    block = _availability_block(build_system_prompt(business_info=BIZ))
    assert "AVAILABLE" in block
    # Available before unavailable: the model should read the proceed path first.
    assert block.index("they are AVAILABLE") < block.index("they are unavailable")
    # And it must be told to say nothing at all about the schedule when free.
    assert "NOTHING about their schedule" in block


def test_it_forbids_refusing_a_day_the_stylist_works():
    block = _availability_block(build_system_prompt(business_info=BIZ))
    assert "NEVER tell a caller a stylist does not work on a day that IS listed" in block
    # The self-check that would have caught both live replies before they were spoken.
    assert "re-read their line first" in block


def test_the_old_refusal_only_example_is_gone():
    """That example is what the model echoed, plural and all."""
    block = _availability_block(build_system_prompt(business_info=BIZ))
    assert "respond that they don't work Thursdays" not in block


def test_the_examples_include_a_booking_that_goes_through():
    block = _availability_block(build_system_prompt(business_info=BIZ))
    assert "book it and never mention their schedule" in block


def test_each_stylist_still_carries_their_own_days():
    """The per-stylist lines are the data the branches are decided against."""
    block = _availability_block(build_system_prompt(business_info=BIZ))
    terrance = next(l for l in block.splitlines() if l.strip().startswith("• Terrance:"))
    melissa = next(l for l in block.splitlines() if l.strip().startswith("• Melissa:"))
    for day in ("Thursday", "Friday", "Sunday"):
        assert day in terrance
    assert "Thursday" not in melissa
    for day in ("Tuesday", "Wednesday", "Saturday"):
        assert day in melissa


def test_an_unrestricted_roster_adds_no_availability_block():
    """Nobody restricted means nothing to enforce — and no refusal language in play."""
    biz = {
        **BIZ,
        "staff": [{"id": "st_a", "name": "Ada", "service_ids": []}],
    }
    assert "Stylist availability" not in build_system_prompt(business_info=biz)
