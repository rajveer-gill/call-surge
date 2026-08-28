"""The name on the account is not always the name to answer the phone with.

Gill Salons files each location by its store number — the account is called
"19765 Gig Harbor", because that is how their records, their reporting and their
franchisor refer to it. Callers know the shop as "Gig Harbor Hair Masters".

They had already worked around this by hand-typing the public name into the custom
greeting, which fixes the first eight words of the call and nothing after it: the
prompt, and so every later sentence the AI says, still carried the filing name.
public_name is the supported version of that workaround, and these tests cover the
surfaces the greeting workaround could not reach.
"""
import business_hours
import config_service
import voice_service
from prompts.receptionist import build_system_prompt

FILING = "19765 Gig Harbor"
PUBLIC = "Gig Harbor Hair Masters"


def _biz(public=True):
    info = {
        "name": FILING,
        "hours": "Mon 10-5, Tue-Fri 9-6, Sat 9-5",
        "services": [{"name": "Haircut", "price": 45, "duration_minutes": 30}],
        "staff": [{"id": "s1", "name": "Melissa"}],
    }
    if public:
        info["public_name"] = PUBLIC
    return info


def test_helper_prefers_the_public_name():
    assert config_service.customer_facing_name(_biz()) == PUBLIC


def test_helper_falls_back_to_the_account_name():
    """Every existing tenant has no public_name and must be untouched."""
    assert config_service.customer_facing_name(_biz(public=False)) == FILING
    assert config_service.customer_facing_name({"name": FILING, "public_name": ""}) == FILING
    assert config_service.customer_facing_name({"name": FILING, "public_name": "   "}) == FILING


def test_prompt_says_the_public_name_and_not_the_filing_name():
    """The greeting workaround stopped here. The prompt drives everything the AI
    says for the rest of the call, so the store number leaked into every later
    sentence that named the business."""
    prompt = build_system_prompt(business_info=_biz())
    assert PUBLIC in prompt
    assert FILING not in prompt, [l for l in prompt.split("\n") if FILING in l]


def test_prompt_unchanged_without_a_public_name():
    assert FILING in build_system_prompt(business_info=_biz(public=False))


def test_greeting_template_substitutes_the_public_name():
    assert voice_service._resolve_greeting_business_name(_biz()) == PUBLIC
    assert voice_service._resolve_greeting_business_name(_biz(public=False)) == FILING


def test_after_hours_lines_use_the_public_name():
    """Both closed-store lines read the name out loud.

    The first version of this test called a function that does not exist and
    passed without asserting anything, which is how same_day_after_hours_message
    was missed on the first pass.
    """
    import datetime as _dt

    msg = business_hours.same_day_after_hours_message(_biz())
    assert PUBLIC in msg and FILING not in msg
    assert FILING in business_hours.same_day_after_hours_message(_biz(public=False))

    info = _biz()
    info["hours"] = "Mon-Sun 9-5"
    block = business_hours.after_hours_prompt_block(info, _dt.datetime(2026, 8, 27, 23, 30))
    assert block is not None, "expected to be past closing at 23:30"
    assert PUBLIC in block and FILING not in block


def test_setting_survives_the_config_round_trip():
    """The seam that made quote_prices ship inert.

    _config_data_to_business_info is an explicit key-by-key mapping. A field the
    API stores but that mapping drops looks saved in the dashboard and never
    reaches the prompt. Go through the real mapping, not a hand-built dict.
    """
    stored = {"business_name": FILING, "public_name": PUBLIC}
    info = config_service._config_data_to_business_info(stored)
    assert info["public_name"] == PUBLIC
    assert config_service.customer_facing_name(info) == PUBLIC
    prompt = build_system_prompt(business_info=info)
    assert PUBLIC in prompt and FILING not in prompt


def test_round_trip_without_the_field_is_the_account_name():
    info = config_service._config_data_to_business_info({"business_name": FILING})
    assert config_service.customer_facing_name(info) == FILING


def test_helper_reads_raw_client_config_too():
    """The reminder and automation SMS paths hold the raw config, where the key is
    business_name. A helper that only understood business_info would have left the
    filing name in the text customers get the night before their appointment."""
    assert config_service.customer_facing_name(
        {"business_name": FILING, "public_name": PUBLIC}) == PUBLIC
    assert config_service.customer_facing_name({"business_name": FILING}) == FILING
