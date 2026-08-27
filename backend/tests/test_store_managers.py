"""Store managers: list them, remove them, and never move one silently.

Inviting a store manager shipped with no way to see who was on a store or take
them off. A standing invitation is access to that store's calls, messages and
customers, and withdrawing it meant asking us.

org_members is PRIMARY KEY (clerk_user_id, org_id) — one row per person per group.
So inviting someone who already manages another store in the same group upserts
that row and MOVES them: they lose the first store, with no error and nothing on
screen. Silent data loss is worse than a refusal.
"""
import pytest
from fastapi import HTTPException

import clerk_service
import database
import routers.org as org


class _Req:
    method = "POST"
    headers: dict = {}
    client = None
    url = type("U", (), {"path": "/api/org/stores/x/managers"})()


STORE = {"id": "11111111-1111-1111-1111-111111111111", "client_id": "shop-a",
         "org_id": "99999999-9999-9999-9999-999999999999", "name": "Shop A"}
OTHER = {"id": "22222222-2222-2222-2222-222222222222", "client_id": "shop-b",
         "org_id": STORE["org_id"], "name": "Shop B"}


def _as_manager(monkeypatch, role="manager"):
    monkeypatch.setattr(org.runtime, "USE_DB", True, raising=False)
    monkeypatch.setattr(database, "db_org_store_for_user",
                        lambda _u, _r: {"tenant": STORE, "role": role})
    monkeypatch.setattr(org.deps, "audit_log", lambda *a, **k: None)


def test_viewer_cannot_remove_a_store_manager(monkeypatch):
    _as_manager(monkeypatch, role="viewer")
    with pytest.raises(HTTPException) as ei:
        org.remove_store_manager("shop-a", "user_x", _Req(), user_id="u1")
    assert ei.value.status_code == 403


def test_removing_someone_who_is_not_on_this_store_is_404(monkeypatch):
    _as_manager(monkeypatch)
    monkeypatch.setattr(database, "db_org_member_scope", lambda _u, _o: None)
    with pytest.raises(HTTPException) as ei:
        org.remove_store_manager("shop-a", "user_x", _Req(), user_id="u1")
    assert ei.value.status_code == 404


def test_cannot_strip_a_whole_group_person_from_one_store_screen(monkeypatch):
    """Their row covers every store, so deleting it here would revoke the whole
    group — far more than this button appears to offer."""
    _as_manager(monkeypatch)
    monkeypatch.setattr(database, "db_org_member_scope",
                        lambda _u, _o: {"role": "manager", "tenant_id": None})
    with pytest.raises(HTTPException) as ei:
        org.remove_store_manager("shop-a", "user_x", _Req(), user_id="u1")
    assert ei.value.status_code in (404, 409)


def test_removing_a_store_manager_works(monkeypatch):
    _as_manager(monkeypatch)
    monkeypatch.setattr(database, "db_org_member_scope",
                        lambda _u, _o: {"role": "manager", "tenant_id": STORE["id"]})
    seen = {}
    monkeypatch.setattr(database, "db_org_member_remove",
                        lambda u, o: seen.update(user=u, org=o) or True)
    out = org.remove_store_manager("shop-a", "user_x", _Req(), user_id="u1")
    assert out["ok"] is True
    assert seen == {"user": "user_x", "org": STORE["org_id"]}


def test_inviting_someone_who_manages_another_store_is_refused(monkeypatch):
    """The silent move. Without this they lose Shop B and nothing says so."""
    _as_manager(monkeypatch)
    monkeypatch.setattr(clerk_service, "clerk_user_id_for_email", lambda _e: "user_x")
    monkeypatch.setattr(database, "db_org_member_scope",
                        lambda _u, _o: {"role": "manager", "tenant_id": OTHER["id"]})
    monkeypatch.setattr(database, "db_tenant_get_by_id", lambda _i: OTHER)
    with pytest.raises(HTTPException) as ei:
        org.invite_store_manager(
            "shop-a", org.InviteStoreManagerRequest(email="m@example.net"),
            _Req(), user_id="u1",
        )
    assert ei.value.status_code == 409
    # Name the store they are on, or the person inviting cannot act on the refusal.
    assert "Shop B" in str(ei.value.detail)


def test_reinviting_to_the_SAME_store_is_allowed(monkeypatch):
    """Re-sending an invite to the store they already manage changes nothing and
    must not be mistaken for a move."""
    _as_manager(monkeypatch)
    monkeypatch.setattr(clerk_service, "clerk_user_id_for_email", lambda _e: "user_x")
    monkeypatch.setattr(database, "db_org_member_scope",
                        lambda _u, _o: {"role": "manager", "tenant_id": STORE["id"]})
    monkeypatch.setattr(clerk_service, "_clerk_invite_email_to_org",
                        lambda *a, **k: {"user_added": True, "invite_sent": False,
                                         "pending_invite_stored": False, "clerk_error": None})
    out = org.invite_store_manager(
        "shop-a", org.InviteStoreManagerRequest(email="m@example.net"),
        _Req(), user_id="u1",
    )
    assert out.get("ok") is not False


def test_a_brand_new_person_is_not_blocked(monkeypatch):
    """No Clerk account yet means no existing row to move."""
    _as_manager(monkeypatch)
    monkeypatch.setattr(clerk_service, "clerk_user_id_for_email", lambda _e: None)
    monkeypatch.setattr(clerk_service, "_clerk_invite_email_to_org",
                        lambda *a, **k: {"user_added": False, "invite_sent": True,
                                         "pending_invite_stored": True, "clerk_error": None})
    out = org.invite_store_manager(
        "shop-a", org.InviteStoreManagerRequest(email="new@example.net"),
        _Req(), user_id="u1",
    )
    assert out.get("ok") is not False
