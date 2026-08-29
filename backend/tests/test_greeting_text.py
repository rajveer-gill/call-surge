"""Phone greeting text: placeholders, receptionist prepend, recording disclosure order."""

from __future__ import annotations

import main
import voice_service


def test_greeting_prepends_receptionist_name(monkeypatch):
    monkeypatch.setattr(
        main,
        "get_business_info",
        lambda: {
            "name": "Test Spa",
            "receptionist_name": "Ava",
            "greeting": "Thank you for calling {business_name}. How can I help?",
        },
    )
    monkeypatch.setattr(voice_service, "_call_recording_enabled_for_tenant", lambda _t: False)
    monkeypatch.setattr(voice_service, "_tenant_for_call_recording", lambda: None)
    text = main.get_greeting_text()
    assert text.startswith("Hi, I'm Ava.")
    assert "Test Spa" in text


def test_greeting_respects_custom_receptionist_placeholder(monkeypatch):
    monkeypatch.setattr(
        main,
        "get_business_info",
        lambda: {
            "name": "Test Spa",
            "receptionist_name": "Ava",
            "greeting": "Hi, this is {receptionist_name} at {business_name}.",
        },
    )
    monkeypatch.setattr(voice_service, "_call_recording_enabled_for_tenant", lambda _t: False)
    monkeypatch.setattr(voice_service, "_tenant_for_call_recording", lambda: None)
    text = main.get_greeting_text()
    assert text.startswith("Hi, this is Ava at Test Spa.")
    assert "Hi, I'm Ava" not in text


def test_user_custom_greeting_template(monkeypatch):
    monkeypatch.setattr(
        main,
        "get_business_info",
        lambda: {
            "name": "Call Surge Demo",
            "receptionist_name": "Jordan",
            "greeting": "Thank you for calling {business_name}. I am {receptionist_name}. What is up?",
        },
    )
    monkeypatch.setattr(voice_service, "_call_recording_enabled_for_tenant", lambda _t: False)
    monkeypatch.setattr(voice_service, "_tenant_for_call_recording", lambda: None)
    payload = main.build_phone_greeting_payload(main.get_business_info(), None)
    assert payload["main_greeting"] == (
        "Thank you for calling Call Surge Demo. I am Jordan. What is up?"
    )
    assert payload["used_default_template"] is False
    assert payload["prepended_receptionist"] is False


def test_recording_disclosure_comes_before_the_closing_question(monkeypatch):
    """The caller answers the question, so nothing may be spoken after it.

    Lana's callers started talking on "How can I help you today?" and talked straight
    over the disclosure that used to follow it.
    """
    monkeypatch.setattr(
        main,
        "get_business_info",
        lambda: {
            "name": "Test Spa",
            "receptionist_name": "Ava",
            "greeting": "Thank you for calling {business_name}. What is up?",
        },
    )
    monkeypatch.setattr(voice_service, "_call_recording_enabled_for_tenant", lambda _t: True)
    monkeypatch.setattr(voice_service, "_tenant_for_call_recording", lambda: {"client_id": "test"})
    text = main.get_greeting_text()
    assert text.endswith("What is up?")
    assert text.index("recorded") < text.index("What is up?")
    # The salon still introduces itself before any boilerplate.
    assert text.index("Test Spa") < text.index("recorded")


def test_disclosure_stays_last_when_greeting_has_no_closing_question(monkeypatch):
    monkeypatch.setattr(
        main,
        "get_business_info",
        lambda: {
            "name": "Test Spa",
            "receptionist_name": "Ava",
            "greeting": "Thank you for calling {business_name}.",
        },
    )
    monkeypatch.setattr(voice_service, "_call_recording_enabled_for_tenant", lambda _t: True)
    monkeypatch.setattr(voice_service, "_tenant_for_call_recording", lambda: {"client_id": "test"})
    text = main.get_greeting_text()
    assert text.endswith(main.RECORDING_DISCLOSURE_TEXT)


def test_disclosure_stays_last_when_greeting_is_only_a_question(monkeypatch):
    """No lead-in to sit in front of, so the call must not open on boilerplate."""
    monkeypatch.setattr(
        main,
        "get_business_info",
        lambda: {"name": "Test Spa", "receptionist_name": "", "greeting": "What is up?"},
    )
    monkeypatch.setattr(voice_service, "_call_recording_enabled_for_tenant", lambda _t: True)
    monkeypatch.setattr(voice_service, "_tenant_for_call_recording", lambda: {"client_id": "test"})
    text = main.get_greeting_text()
    assert text.startswith("What is up?")
    assert text.endswith(main.RECORDING_DISCLOSURE_TEXT)


def test_business_name_from_tenant_when_config_name_empty(monkeypatch):
    monkeypatch.setattr(
        main,
        "get_business_info",
        lambda: {
            "name": "",
            "receptionist_name": "Ava",
            "greeting": "Thank you for calling {business_name}.",
        },
    )
    tenant = {"name": "Admin Tenant Name", "client_id": "test-spa"}
    payload = main.build_phone_greeting_payload(main.get_business_info(), tenant)
    assert payload["placeholders"]["business_name"] == "Admin Tenant Name"
    assert "Admin Tenant Name" in payload["main_greeting"]
