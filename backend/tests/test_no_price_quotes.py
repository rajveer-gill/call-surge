"""Some businesses do not quote prices over the phone.

Lana Anderberg, West Coast Regional Director at Gill Salons: "We do not list prices
online, and we do not give price quotes over the phone. We can give starting prices,
can we make sure the AI does not give price quotes?"

The guarantee is not "the model was told not to say prices". It is that the prompt
contains no prices to say. A rule can be worn down by a caller who pushes; a number
the model was never given cannot be produced. These tests assert the absence of the
numbers, not the presence of the instruction.
"""
import re

import config_service
from prompts.receptionist import build_system_prompt, format_service_catalog_for_prompt

CATALOG = [
    {"name": "Shampoo & Haircut", "price": 45, "duration_minutes": 30},
    {"name": "Full Highlight", "price": 180.5, "duration_minutes": 120},
    {"name": "Additional Color", "price": 25, "duration_minutes": 0, "is_addon": True},
]

# Any currency-shaped number. Deliberately broad: the point is that nothing
# price-like survives, not that one particular format was stripped.
MONEY = re.compile(r"\$\s?\d|\b\d+\.\d{2}\b|\b(?:45|180|25)\s*(?:dollars|usd)\b", re.I)


def _biz(quote_prices=None):
    info = {
        "name": "Gill Salons Gig Harbor",
        "services": CATALOG,
        "staff": [{"id": "s1", "name": "Melissa"}],
        "hours": "Mon 10-5, Tue-Fri 9-6, Sat 9-5",
    }
    if quote_prices is not None:
        info["quote_prices"] = quote_prices
    return info


def test_catalog_block_carries_no_amounts_when_quoting_is_off():
    block = format_service_catalog_for_prompt(CATALOG, quote_prices=False)
    assert not MONEY.search(block), block
    # The service NAMES must survive — the AI still has to book them.
    assert "Shampoo & Haircut" in block
    assert "Full Highlight" in block


def test_catalog_block_keeps_amounts_when_quoting_is_on():
    block = format_service_catalog_for_prompt(CATALOG, quote_prices=True)
    assert MONEY.search(block), block


def test_full_prompt_contains_no_price_when_quoting_is_off():
    """The whole assembled prompt, not just the catalog block — a price leaking
    back in from another section would defeat the point."""
    prompt = build_system_prompt(business_info=_biz(quote_prices=False))
    assert not MONEY.search(prompt), [l for l in prompt.split("\n") if MONEY.search(l)]


def test_prompt_tells_it_what_to_say_instead():
    """Withholding the numbers is not enough on its own: with no guidance the model
    improvises, and 'I don't know' loses the caller. It must offer a way forward."""
    prompt = build_system_prompt(business_info=_biz(quote_prices=False)).lower()
    assert "does not quote prices" in prompt
    assert "book" in prompt


def test_no_contradictory_instruction_survives():
    """The shared focus block used to say 'never say you do not know if prices are
    listed'. Left in place it would argue with the new rule, and contradictory
    instructions are how the earlier request-mode regression happened."""
    prompt = build_system_prompt(business_info=_biz(quote_prices=False))
    assert "never say you do not know if prices are listed" not in prompt.lower()
    assert "answer using the dollar amounts above" not in prompt.lower()


def test_default_is_unchanged_for_everyone_else():
    """Absent setting must behave exactly as before — this ships to every existing
    tenant, none of whom asked for it."""
    assert config_service.quotes_prices({}) is True
    assert config_service.quotes_prices({"quote_prices": None}) is True
    prompt = build_system_prompt(business_info=_biz())
    assert MONEY.search(prompt)


def test_toggle_reads_falsey_values_as_off():
    assert config_service.quotes_prices({"quote_prices": False}) is False
    assert config_service.quotes_prices({"quote_prices": 0}) is False
