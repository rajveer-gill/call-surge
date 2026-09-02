"""Regression tests for receptionist system prompt (booking rules, time format, language)."""

import pytest

from prompts.receptionist import (
    appointment_focus_guidance,
    build_system_prompt,
    caller_message_suggests_pricing,
    format_service_catalog_for_prompt,
    latest_user_message,
)


@pytest.fixture
def minimal_business():
    return {
        "name": "Test Spa",
        "hours": "9-5",
        "services": ["Massage"],
        "staff": [{"name": "Jamie"}],
        "business_type": "spa",
    }


def test_prompt_contains_booking_token_format(minimal_business):
    p = build_system_prompt(
        business_info=minimal_business,
        include_booked_slots=True,
        booked_slots_prompt_text="Booked slots (do not double-book): 2026-04-28 at 1:00 PM.",
    )
    assert "BOOKING:" in p
    assert "name|phone|email|date|time|reason" in p
    assert "NEVER a stylist" in p or "Never put a stylist name in field 1" in p
    assert "Do NOT ask for email" in p


def test_prompt_includes_time_off_and_closures(monkeypatch):
    # Pin "today" so upcoming-date filtering is deterministic.
    from datetime import datetime
    import business_hours

    monkeypatch.setattr(business_hours, "business_local_now", lambda info=None, now=None: datetime(2026, 7, 1, 12, 0))
    biz = {
        "name": "Salon",
        "hours": "9-5",
        "services": [{"id": "s1", "name": "Cut"}],
        "staff": [{"id": "1", "name": "Jake", "time_off": ["2026-07-03", "2026-07-04"]}],
        "closures": ["2026-07-06"],
    }
    p = build_system_prompt(business_info=biz, include_booked_slots=True)
    assert "Jake: OFF (not available) on Jul 3–4" in p
    assert "SHOP CLOSED" in p
    assert "Jul 6" in p


def test_prompt_includes_stylist_working_days():
    # The AI must know which days a stylist works and not book them on off days.
    biz = {
        "name": "Salon",
        "hours": "9-5",
        "services": [{"id": "s1", "name": "Cut"}],
        "staff": [{"id": "1", "name": "Jake", "working_days": ["mon", "tue", "wed"]}],
    }
    p = build_system_prompt(business_info=biz, include_booked_slots=True)
    assert "Jake: works Monday, Tuesday, Wednesday" in p
    low = p.lower()
    # Hardened: the AI must refuse an off-day request and must not falsely confirm it.
    # Wording moved into the case-(b) branch when the rule gained an available branch
    # (see test_prompt_stylist_available_days); the constraint asserted here is the same.
    assert "do not book them" in low
    assert "isn't available then" in low
    # Per-stylist emphasis: never apply one stylist's schedule to another.
    assert "never apply one stylist" in low


def test_prompt_unrestricted_stylist_listed_explicitly_when_another_is_restricted():
    # Regression: a stylist with NO working_days was told they were only available on ANOTHER
    # stylist's days. Every stylist must get their own explicit line so days can't be misapplied.
    biz = {
        "name": "Salon",
        "hours": "9-5",
        "services": [{"id": "s1", "name": "Cut"}],
        "staff": [
            {"id": "1", "name": "Jake", "working_days": ["mon", "wed", "fri"]},
            {"id": "2", "name": "Tom"},  # no schedule → any day
        ],
    }
    p = build_system_prompt(business_info=biz, include_booked_slots=True)
    assert "Jake: works Monday, Wednesday, Friday" in p
    # Tom must be spelled out as unrestricted, not left to inherit Jake's days — and firmly, so
    # the model doesn't invent a day-off (a real failure: it told a caller Tom was off Mondays).
    assert "Tom: works EVERY day the shop is open" in p
    assert "never tell the caller Tom is off on a day the shop is open" in p


def test_prompt_gives_weekday_date_reference(monkeypatch):
    # The AI kept calling an open Friday "closed" because it computed weekdays itself and got them
    # wrong. It must be handed an explicit weekday↔date table instead.
    from datetime import datetime
    import business_hours

    monkeypatch.setattr(business_hours, "business_local_now", lambda info=None, now=None: datetime(2026, 7, 3, 14, 0))
    biz = {
        "name": "Salon",
        "hours": "Monday–Friday: 9:00 AM – 5:00 PM, Saturday–Sunday: Closed",
        "services": [{"id": "s1", "name": "Cut"}],
        "staff": [{"name": "Tom"}],
    }
    p = build_system_prompt(business_info=biz, include_booked_slots=True)
    assert "DATE REFERENCE" in p
    assert "Friday 2026-07-03" in p  # today, named
    assert "Saturday 2026-07-04" in p  # tomorrow named, so it won't book the closed weekend


def test_prompt_instructs_reemit_booking_on_change():
    # A caller changing the service/stylist/time after booking must trigger a fresh BOOKING line,
    # otherwise the change isn't saved (regression: a service change didn't update the record).
    biz = {
        "name": "Salon",
        "hours": "9-5",
        "services": [{"id": "s1", "name": "Cut"}],
        "staff": [{"name": "Jake"}],
    }
    p = build_system_prompt(business_info=biz, include_booked_slots=True)
    low = p.lower()
    assert "changes any already-agreed detail" in low
    assert "new booking line" in low


def test_prompt_no_working_days_section_when_unset():
    biz = {
        "name": "Salon",
        "hours": "9-5",
        "services": [{"id": "s1", "name": "Cut"}],
        "staff": [{"id": "1", "name": "Jake"}],
    }
    p = build_system_prompt(business_info=biz, include_booked_slots=True)
    assert "working days" not in p.lower()


def test_prompt_only_listed_times_are_taken_no_invented_conflicts():
    # With one taken slot, the AI must treat every OTHER time/day as open and never
    # invent conflicts (it was telling callers an empty day's time was "taken").
    p = build_system_prompt(
        business_info={"name": "Salon", "hours": "9-5", "staff": [{"name": "Jake"}]},
        include_booked_slots=True,
        booked_slots_prompt_text="Booked (taken): 2026-06-24 at 2:00 PM.",
    )
    low = p.lower()
    assert "only the exact date-and-time entries listed above are taken" in low
    assert "never tell a caller a requested time is taken" in low


def test_prompt_forbids_inventing_services_when_none_configured():
    # No services configured: the AI must not invent/list services (it was making up
    # salon services like "haircuts, coloring" on a fresh tenant).
    biz = {"name": "Gills Salons", "hours": "9-5", "services": [], "staff": [{"name": "Jake"}], "business_type": "salon"}
    p = build_system_prompt(business_info=biz, include_booked_slots=True)
    low = p.lower()
    assert "not configured a service menu" in low
    assert "never invent" in low
    assert "don't have the service list" in low or "do not have the service list" in low


def test_prompt_uses_configured_service_menu_when_present(minimal_business):
    # With services present, the prompt should reference the configured menu, not the
    # "no service menu" guidance.
    p = build_system_prompt(business_info=minimal_business, include_booked_slots=True)
    assert "configured service menu" in p.lower()
    assert "not configured a service menu" not in p.lower()


def test_prompt_take_a_message_when_no_transfer_line(minimal_business):
    on = build_system_prompt(
        business_info={**minimal_business, "transfer_takes_message": True},
        include_booked_slots=False,
    )
    assert "NO LIVE TRANSFER LINE" in on
    assert "MESSAGE:" in on
    # We already have the caller's number from caller ID — the AI must not ask for it.
    assert "caller ID" in on
    assert "do not ask for their phone number" in on.lower()
    off = build_system_prompt(
        business_info={**minimal_business, "transfer_takes_message": False},
        include_booked_slots=False,
    )
    assert "NO LIVE TRANSFER LINE" not in off


def test_prompt_disambiguates_booking_intent_while_taking_a_message(minimal_business):
    # A message that sounds like a booking ("I want to book an appointment") must not silently
    # switch to booking or silently be recorded — the AI asks which the caller meant first.
    p = build_system_prompt(business_info=minimal_business, include_booked_slots=False).lower()
    assert "just leave it as a message" in p
    assert "right now" in p


def test_prompt_requires_12_hour_spoken_times(minimal_business):
    p = build_system_prompt(
        business_info=minimal_business,
        include_booked_slots=True,
        booked_slots_prompt_text="x",
    )
    assert "12-hour format with AM/PM" in p
    assert "24-hour/military" in p or "13:00" in p


def test_prompt_repeat_caller_memory(minimal_business):
    p = build_system_prompt(
        business_info=minimal_business,
        caller_memory={"name": "Alex", "call_count": 2, "last_reason": "booking"},
        include_booked_slots=False,
    )
    assert "REPEAT CALLER" in p
    assert "Alex" in p


def test_prompt_staff_transfer_instruction(minimal_business):
    minimal_business["staff"] = [{"id": "s1", "name": "Jamie", "phone": "+15551110001"}]
    minimal_business["transfer_targets"] = [{"staff_id": "s1", "phone": "+15551110001"}]
    p = build_system_prompt(business_info=minimal_business, include_booked_slots=False)
    assert "TRANSFER_TO:" in p
    assert "Jamie" in p


def test_prompt_staff_notes_reference_block(minimal_business):
    minimal_business["staff"] = [
        {"name": "Jamie", "notes": "Best for massage bookings."},
    ]
    p = build_system_prompt(business_info=minimal_business, include_booked_slots=False)
    assert "Business-entered facts about staff" in p
    assert "Jamie" in p
    assert "massage" in p.lower()


def test_prompt_includes_receptionist_name(minimal_business):
    minimal_business["receptionist_name"] = "Ava"
    p = build_system_prompt(business_info=minimal_business, include_booked_slots=False)
    assert "Your name is Ava" in p


def test_prompt_non_english_locks_language(minimal_business):
    p = build_system_prompt(
        business_info=minimal_business,
        detected_language="Spanish",
        include_booked_slots=False,
    )
    assert "Spanish" in p
    assert "MUST respond ONLY in Spanish" in p


def test_prompt_no_services_skips_service_questions():
    biz = {
        "name": "Test Spa",
        "hours": "9-5",
        "services": [],
        "staff": [{"name": "Jamie"}],
    }
    p = build_system_prompt(
        business_info=biz,
        include_booked_slots=True,
        booked_slots_prompt_text="",
    )
    assert "NOT configured a service menu" in p
    assert "Do NOT ask callers to pick a service" in p
    assert "- Services:" not in p.split("You can help with:")[1].split("staff")[0]


def test_prompt_staff_linked_services(minimal_business):
    minimal_business["services"] = [
        {"id": "svc-a", "name": "Haircut", "price": 40, "duration_minutes": 30},
        {"id": "svc-b", "name": "Color", "price": 80, "duration_minutes": 90},
    ]
    minimal_business["staff"] = [
        {"name": "Jamie", "service_ids": ["svc-a"]},
        {"name": "Alex", "service_ids": []},
    ]
    p = build_system_prompt(business_info=minimal_business, include_booked_slots=False)
    assert "Staff and which services they provide" in p
    assert "Jamie: Haircut" in p
    assert "Alex: any listed service" in p


def test_prompt_multi_staff_asks_service_before_stylist():
    biz = {
        "name": "Test Salon",
        "services": [{"id": "s1", "name": "Haircut", "price": 30, "duration_minutes": 30}],
        "staff": [{"name": "Jamie"}, {"name": "Alex"}],
    }
    p = build_system_prompt(business_info=biz, include_booked_slots=True, booked_slots_prompt_text="")
    assert "Multiple team members" in p
    # Service-first: ask the service, then suggest stylists who provide it.
    assert "Ask which SERVICE they want FIRST" in p
    assert "do not ask for the stylist before the service" in p


def test_prompt_empty_slots_branch(minimal_business):
    p = build_system_prompt(
        business_info=minimal_business,
        include_booked_slots=True,
        booked_slots_prompt_text="",
    )
    assert "Booked slots: none" in p
    assert "ALL times are available" in p


def test_service_catalog_prompt_uses_natural_voice_guidance():
    block = format_service_catalog_for_prompt(
        [
            {"name": "Short Cut", "price": 30, "duration_minutes": 30},
            {"name": "Long Cut", "price": 50, "duration_minutes": 60},
        ]
    )
    assert '"Short Cut"' in block
    assert "VOICE:" in block
    assert "sound like a real receptionist" in block
    assert "($30.0" not in block
    assert "30 min)" not in block


def test_prompt_orients_caller_toward_booking(minimal_business):
    p = build_system_prompt(
        business_info=minimal_business,
        include_booked_slots=True,
        booked_slots_prompt_text="",
    )
    assert "PRIMARY GOAL" in p
    assert "book an appointment" in p.lower()
    assert "unrelated" in p.lower() or "off-topic" in p.lower() or "trivia" in p.lower()


def test_appointment_focus_guidance_sms_off_topic_redirect():
    g = appointment_focus_guidance("Test Spa", include_booked_slots=True, channel="sms")
    assert "book an appointment" in g.lower()
    assert "trivia" in g.lower() or "unrelated" in g.lower()


def test_prompt_services_not_robotic_price_list():
    biz = {
        "name": "Test Salon",
        "services": [
            {"id": "s1", "name": "Short Cut", "price": 30, "duration_minutes": 30},
            {"id": "s2", "name": "Long Cut", "price": 50, "duration_minutes": 60},
        ],
        "staff": [{"name": "Jamie"}],
    }
    p = build_system_prompt(business_info=biz, include_booked_slots=False)
    assert "Services menu" in p
    assert "VOICE:" in p
    assert "Short Cut ($30.0, 30 min)" not in p
    assert "Pricing:" in p
    assert "how much" in p.lower() or "cost" in p.lower()


def test_caller_message_suggests_pricing():
    assert caller_message_suggests_pricing("How much is a long cut?")
    assert caller_message_suggests_pricing("What's the price for short cut")
    assert not caller_message_suggests_pricing("Book me for tomorrow at 3")


def test_service_catalog_includes_price_answer_guidance():
    block = format_service_catalog_for_prompt(
        [{"name": "Long Cut", "price": 50, "duration_minutes": 45}]
    )
    assert "Never say you do not know" in block
    assert "$50" in block


def test_service_catalog_without_prices_configured():
    block = format_service_catalog_for_prompt(
        [{"name": "Long Cut", "price": 0, "duration_minutes": 45}]
    )
    assert "not configured" in block.lower() or "confirm" in block.lower()


def test_voice_booking_nudge_prioritizes_pricing_question(monkeypatch):
    from main import _voice_booking_nudge_message

    monkeypatch.setattr(
        "main.get_business_info",
        lambda: {
            "staff": [{"id": "j1", "name": "Jake"}],
            "services": [{"id": "s1", "name": "Long Cut", "price": 50, "duration_minutes": 45}],
        },
    )
    history = [
        {"role": "user", "content": "I want to book with Jake tomorrow"},
        {"role": "assistant", "content": "Sure"},
        {"role": "user", "content": "How much is a long cut?"},
    ]
    nudge = _voice_booking_nudge_message(history)
    assert nudge is not None
    assert "price" in nudge.lower()
    assert "not off-topic" in nudge.lower()
    assert "BOOKING:" not in nudge
