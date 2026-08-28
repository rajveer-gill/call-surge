"""Renaming in Settings must reach the row every other screen reads.

A business name lives in two places: the config, which drives the receptionist,
and tenants.name, which the store list, the org dashboard and admin read. Only
the creation path ever wrote both. Renaming from Settings wrote the config alone,
so a store created as "Lana's Store" kept that label on every management screen
no matter what Settings said — the rename looked saved and half of it was.
"""
import pytest
from fastapi.testclient import TestClient

import config_service
import database
import runtime
from main import app, require_tenant

TENANT = {
    "id": "test-tenant-id",
    "client_id": "test-spa",
    "name": "Lana's Store",
    "plan": "pro",
    "subscription_status": "active",
    "twilio_phone_number": "+15550001111",
}


@pytest.fixture
def client(monkeypatch, tmp_path):
    monkeypatch.delenv("CLERK_JWKS_URL", raising=False)
    monkeypatch.setattr("config_service.PROJECT_ROOT", tmp_path)
    monkeypatch.setattr("main.PROJECT_ROOT", tmp_path)
    monkeypatch.setenv("CLIENT_ID", "test-spa")
    monkeypatch.setattr(runtime, "USE_DB", True)
    # USE_DB routes config through Postgres, which the suite has no access to. The
    # rename is what is under test, so hold the config layer still around it.
    store = {"business_name": TENANT["name"], "client_id": "test-spa"}
    monkeypatch.setattr(config_service, "_read_raw_client_config", lambda _cid: dict(store))
    monkeypatch.setattr(config_service, "save_raw_client_config",
                        lambda _cid, data: store.update(data) or True)
    app.dependency_overrides[require_tenant] = lambda: dict(TENANT)
    yield TestClient(app)
    app.dependency_overrides.pop(require_tenant, None)


@pytest.fixture
def renames(monkeypatch):
    calls = []
    monkeypatch.setattr(database, "db_tenant_set_name",
                        lambda tid, name: calls.append((tid, name)) or True)
    return calls


def test_rename_updates_the_tenant_row(client, renames):
    r = client.patch("/api/business-info", json={"name": "19765 Gig Harbor"})
    assert r.status_code == 200, r.text
    assert renames == [("test-tenant-id", "19765 Gig Harbor")]


def test_unchanged_name_does_not_write(client, renames):
    """Saving Settings without touching the name must not churn the row."""
    r = client.patch("/api/business-info", json={"name": "Lana's Store"})
    assert r.status_code == 200, r.text
    assert renames == []


def test_editing_something_else_does_not_rename(client, renames):
    r = client.patch("/api/business-info", json={"hours": "Mon-Fri 9-5"})
    assert r.status_code == 200, r.text
    assert renames == []


def test_a_failed_rename_does_not_fail_the_save(client, monkeypatch):
    """The config write already succeeded. Reporting failure would make them retry
    a save that partly landed."""
    monkeypatch.setattr(database, "db_tenant_set_name",
                        lambda *a: (_ for _ in ()).throw(RuntimeError("db down")))
    r = client.patch("/api/business-info", json={"name": "19765 Gig Harbor"})
    assert r.status_code == 200, r.text
