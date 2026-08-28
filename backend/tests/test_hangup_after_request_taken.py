"""The call ends once the caller has been told their details are on the way.

From Lana's first test call, 2026-08-28: "After it tells me that I will get a text with
the details, it does not disconnect the call. It continues to repeat itself, and I
received 5 text messages."

There was no path that ended a call. Every reply set up another listen, so after the
request was taken the AI stayed on the line with nothing left to do — re-emitting
BOOKING lines and re-texting whatever came back.
"""

import pytest

import conversation_service as cs
import config_service
import deps
import voice_service
from fastapi.testclient import TestClient


def test_the_goodbye_comes_with_the_confirmation_and_ends_the_call(monkeypatch):
    """As soon as the text is on its way there is nothing left to say. An "anything else
    before I let you go?" turn was tried here and taken back out: it is one more turn for
    the model to talk into, and the complaint being fixed is that it kept talking."""
    monkeypatch.setattr(
        cs.config_service, "get_business_info", lambda: {"name": "Gill Salons"}
    )
    call_data: dict = {}
    out = cs._close_call_after_booking(call_data, "I've sent your request to the salon.")
    assert call_data["end_call_after_reply"] is True
    assert out.startswith("I've sent your request to the salon.")
    assert "Goodbye" in out
    assert "Gill Salons" in out
    assert "anything else" not in out.lower()


def test_a_store_can_keep_the_line_open(monkeypatch):
    monkeypatch.setenv("VOICE_END_CALL_AFTER_BOOKING", "0")
    monkeypatch.setattr(cs.config_service, "get_business_info", lambda: {"name": "Salon"})
    call_data: dict = {}
    out = cs._close_call_after_booking(call_data, "I've texted you the details.")
    assert call_data == {}
    assert out == "I've texted you the details."


def test_a_completed_booking_ends_the_call_on_that_turn(monkeypatch):
    """End to end through the turn pipeline: the BOOKING line lands, the confirmation and
    goodbye are spoken together, and the status the TwiML layer reads says to hang up."""
    import asyncio
    from datetime import timedelta
    from unittest.mock import MagicMock

    import business_hours
    import database
    import main
    import runtime
    import sms_service

    future = (business_hours.business_local_now({}) + timedelta(days=5)).date().isoformat()
    monkeypatch.setattr("runtime.USE_DB", True)
    monkeypatch.setattr(config_service, "staff_roster_ready_for_booking", lambda info=None: True)
    monkeypatch.setattr(
        config_service,
        "get_business_info",
        lambda: {"name": "Gill Salons", "staff": [], "services": [], "forwarding_phone": ""},
    )
    monkeypatch.setattr(
        cs,
        "_create_appointment_from_booking",
        lambda booking, client_id_override=None, reserve_slot_immediately=True, **kw: {
            "id": 77,
            "name": "Lana",
            "date": future,
            "time": "15:00",
            "phone": "+15551234567",
            "status": "pending_review",
        },
    )
    monkeypatch.setattr(sms_service, "send_sms", lambda *a, **k: True)
    monkeypatch.setattr(database, "db_sms_session_upsert", lambda *a, **k: None)
    monkeypatch.setattr(database, "db_sms_consent_record", lambda *a, **k: None)
    monkeypatch.setattr(main.client.chat.completions, "create", MagicMock())
    main.client.chat.completions.create.return_value = MagicMock(
        choices=[
            MagicMock(
                message=MagicMock(
                    content=f"BOOKING: Lana|+15551234567||{future}|3 PM|Haircut|"
                )
            )
        ]
    )

    call_sid = "CAdddddddddddddddddddddddddddddddd"
    call_data = {
        "client_id": "salon-test",
        "from_number": "+15551234567",
        "to_number": "+15559876543",
        "conversation_history": [{"role": "user", "content": "book a haircut tomorrow at 3"}],
    }
    main.active_calls[call_sid] = call_data
    asyncio.run(
        main.generate_response_async(call_sid, call_data, "English", "https://api.example.com")
    )
    status = runtime.call_store.response_status.get(call_sid, {})
    assert status.get("status") == "ready"
    assert status.get("end_call") is True
    spoken = status.get("ai_text") or ""
    assert "texted you the details" in spoken
    assert "Goodbye" in spoken


@pytest.fixture
def respond_client(monkeypatch):
    from voice.call_session_store import MemoryCallSessionStore, reset_call_session_store_for_tests

    reset_call_session_store_for_tests(MemoryCallSessionStore())
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://voice.example.test")
    import main

    monkeypatch.setattr(voice_service, "_voice_stt_use_deepgram", lambda: False)
    monkeypatch.setattr(deps, "_validate_twilio_webhook", lambda _r, _d: True)
    monkeypatch.setattr(config_service, "get_tts_voice", lambda: "fable")
    monkeypatch.setattr(config_service, "get_business_info", lambda: {"forwarding_phone": ""})
    return TestClient(main.app)


def _ready_call(main, call_sid, **status):
    main.active_calls[call_sid] = {
        "client_id": "default",
        "conversation_history": [],
        "detected_language": "English",
        "twilio_public_base_url": "https://voice.example.test",
        "media_stream_gen": 0,
    }
    main.response_status[call_sid] = {
        "status": "ready",
        "audio_url": "https://voice.example.test/api/phone/tts-audio?text=bye",
        **status,
    }


def test_the_reply_plays_and_then_the_call_hangs_up(respond_client):
    import main

    call_sid = "CAbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
    _ready_call(main, call_sid, end_call=True)
    body = respond_client.post("/api/phone/respond", data={"CallSid": call_sid}).text
    assert "<Play>" in body
    assert "<Hangup" in body
    assert "<Gather" not in body, "never listen for another turn after the goodbye"
    assert "Still there" not in body


def test_an_ordinary_reply_still_listens(respond_client):
    """The hangup must fire only on the turn that closes the call."""
    import main

    call_sid = "CAcccccccccccccccccccccccccccccccc"
    _ready_call(main, call_sid)
    body = respond_client.post("/api/phone/respond", data={"CallSid": call_sid}).text
    assert "<Play>" in body
    assert "<Hangup" not in body
