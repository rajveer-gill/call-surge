"""The address Settings saves has to be the address a booking emails.

Lana Anderberg's first question about go-live, 2026-09-03:

    "Is there any way for a notification sound to happen when a request goes to the
     dashboard?"

...which came out of Satya's older one: does anyone get told, or must someone watch the
page? We answered it with an email per request and a `notification_email` field in
Settings. The field was declared on the PATCH model, echoed back in the save log's
`fields` list, and never copied into the config — so Settings accepted an address, showed
it saved, reloaded it from a value that was never stored, and every booking still logged:

    booking_created_request        apt_id=232
    new_request_email_no_recipient apt_id=232

Nothing failed loudly. The request still reached the dashboard, and a blank field is a
legitimate state meaning "don't email anyone", so silence looked exactly like a shop that
had chosen silence.

It survived review because the tests covered `notify_store_of_request(to=...)` — the leaf,
handed a recipient directly. Nothing exercised save-then-read, which is the only place the
bug lived. These tests go through the endpoint and read back through the same accessor the
booking path uses.
"""
import pytest
from fastapi.testclient import TestClient

import config_service
import conversation_service as cs
import runtime
from main import app, require_tenant

TENANT = {
    "id": "test-tenant-id",
    "client_id": "test-salon",
    "name": "Gig Harbor Hair Masters",
    "plan": "pro",
    "subscription_status": "active",
    "twilio_phone_number": "+15550001111",
}


@pytest.fixture
def store(monkeypatch, tmp_path):
    """The config as the endpoint sees it — a dict standing in for the Postgres row."""
    monkeypatch.delenv("CLERK_JWKS_URL", raising=False)
    monkeypatch.setattr("config_service.PROJECT_ROOT", tmp_path)
    monkeypatch.setattr("main.PROJECT_ROOT", tmp_path)
    monkeypatch.setenv("CLIENT_ID", "test-salon")
    monkeypatch.setattr(runtime, "USE_DB", False)
    data = {"business_name": TENANT["name"], "client_id": "test-salon"}
    monkeypatch.setattr(config_service, "_read_raw_client_config", lambda _cid: dict(data))
    monkeypatch.setattr(
        config_service, "save_raw_client_config",
        lambda _cid, new: data.clear() or data.update(new) or True,
    )
    return data


@pytest.fixture
def client(store):
    app.dependency_overrides[require_tenant] = lambda: dict(TENANT)
    yield TestClient(app)
    app.dependency_overrides.pop(require_tenant, None)


def test_the_address_is_actually_stored(client, store):
    r = client.patch("/api/business-info", json={"notification_email": "desk@salon.com"})
    assert r.status_code == 200, r.text
    # The bug: this key was absent entirely, because nothing copied it out of the model.
    assert store.get("notification_email") == "desk@salon.com"


def test_it_comes_back_through_the_accessor_the_booking_path_uses(client):
    """`get_business_info` is what conversation_service reads when a request is filed."""
    client.patch("/api/business-info", json={"notification_email": "desk@salon.com"})
    assert config_service.get_business_info().get("notification_email") == "desk@salon.com"


def test_a_saved_address_actually_receives_the_booking_email(client, monkeypatch):
    """End to end: PATCH the field, then file a request and see who gets mailed.

    This is the assertion that would have caught it. Everything before this one can pass
    on a value that was written but never read back by the code that matters.
    """
    client.patch(
        "/api/business-info",
        json={"notification_email": "desk@salon.com, manager@salon.com"},
    )
    import email_notify

    sent: list = []
    monkeypatch.setattr(
        email_notify, "notify_store_of_request",
        lambda **kw: sent.append(kw.get("to")) or True,
    )
    # The send runs on a daemon thread so an SMTP handshake never becomes dead air on the
    # call. Run it inline here instead of sleeping, so the assertion cannot race it.
    monkeypatch.setattr(
        cs.threading, "Thread",
        lambda target, daemon=False: type("Inline", (), {"start": staticmethod(target)})(),
    )
    cs._email_store_about_request(
        {"name": "Raj", "phone": "+12535550142", "date": "2026-09-10",
         "time": "14:00", "reason": "Shampoo & Haircut"},
        config_service.get_business_info(),
    )
    assert sent == ["desk@salon.com", "manager@salon.com"]


def test_multiple_addresses_survive_the_round_trip(client, store):
    client.patch(
        "/api/business-info",
        json={"notification_email": "desk@salon.com, owner@salon.com"},
    )
    assert store["notification_email"] == "desk@salon.com, owner@salon.com"


def test_a_shop_can_turn_notifications_back_off(client, store):
    """Empty must clear it. The UI sends "" rather than omitting the key for this reason:
    an omitted key is indistinguishable from "leave it alone", so a shop that deleted
    every address would have kept being emailed with no way to stop."""
    client.patch("/api/business-info", json={"notification_email": "desk@salon.com"})
    assert store["notification_email"] == "desk@salon.com"
    r = client.patch("/api/business-info", json={"notification_email": ""})
    assert r.status_code == 200, r.text
    assert store["notification_email"] == ""


def test_an_unrelated_save_leaves_the_address_alone(client, store):
    """Settings sends the whole form on every save; editing the greeting must not wipe it."""
    client.patch("/api/business-info", json={"notification_email": "desk@salon.com"})
    client.patch("/api/business-info", json={"greeting": "Thanks for calling!"})
    assert store["notification_email"] == "desk@salon.com"


def test_surrounding_whitespace_is_trimmed(client, store):
    """People paste addresses out of Outlook with a leading space."""
    client.patch("/api/business-info", json={"notification_email": "  desk@salon.com  "})
    assert store["notification_email"] == "desk@salon.com"
