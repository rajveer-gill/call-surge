"""Group membership: owner > manager > viewer, and the rules that keep it honest.

The two that matter most are not in the original ask:

1. A manager may not create an owner. Without that, a manager mints an owner they
   control and outranks everyone, which makes "a manager cannot remove the owner"
   decorative.
2. The last owner cannot be removed or demoted by anyone, including themselves.
   Otherwise a group locks itself out of its own account and only we can fix it.
"""
import pytest
from fastapi import HTTPException

import database
import routers.org as org


# --- the ordering the whole model rests on ---------------------------------

def test_roles_are_ordered_weakest_first():
    assert database.ORG_ROLES == ("viewer", "manager", "owner")
    assert database.org_role_rank("owner") > database.org_role_rank("manager")
    assert database.org_role_rank("manager") > database.org_role_rank("viewer")


@pytest.mark.parametrize("value", [None, "", "  ", "admin", "Owner ", "OWNER"])
def test_unknown_or_missing_role_never_outranks(value):
    """A typo or a NULL must read as the weakest role, never the strongest."""
    if (value or "").strip().lower() == "owner":
        assert database.org_role_at_least(value, "owner")
        return
    assert database.org_role_rank(value) == 0
    assert not database.org_role_at_least(value, "manager")


# --- who may act on whom ---------------------------------------------------

def _no_owners_left(monkeypatch, n=1):
    monkeypatch.setattr(database, "db_org_owner_count", lambda _o: n)


def test_manager_cannot_remove_an_owner(monkeypatch):
    _no_owners_left(monkeypatch, 3)
    with pytest.raises(HTTPException) as ei:
        org._guard_target("manager", "owner", "org_1")
    assert ei.value.status_code == 403
    assert "owner" in str(ei.value.detail).lower()


def test_manager_cannot_mint_an_owner(monkeypatch):
    """The escalation this model would otherwise allow."""
    _no_owners_left(monkeypatch, 3)
    with pytest.raises(HTTPException) as ei:
        org._guard_target("manager", None, "org_1", granting="owner")
    assert ei.value.status_code == 403


def test_manager_may_manage_managers_and_viewers(monkeypatch):
    _no_owners_left(monkeypatch, 3)
    org._guard_target("manager", "manager", "org_1")
    org._guard_target("manager", "viewer", "org_1")
    org._guard_target("manager", None, "org_1", granting="manager")


def test_owner_may_change_another_owner_while_others_remain(monkeypatch):
    _no_owners_left(monkeypatch, 2)
    org._guard_target("owner", "owner", "org_1")


def test_last_owner_cannot_be_removed_even_by_themselves(monkeypatch):
    _no_owners_left(monkeypatch, 1)
    with pytest.raises(HTTPException) as ei:
        org._guard_target("owner", "owner", "org_1")
    assert ei.value.status_code == 409
    assert "only owner" in str(ei.value.detail).lower()


def test_last_owner_cannot_be_demoted(monkeypatch):
    _no_owners_left(monkeypatch, 1)
    with pytest.raises(HTTPException) as ei:
        org._guard_target("owner", "owner", "org_1", granting="manager")
    assert ei.value.status_code == 409


def test_promoting_the_last_owner_to_owner_is_not_blocked(monkeypatch):
    """Re-granting the same role must not trip the last-owner guard — it removes
    nobody, and failing here would make the row uneditable."""
    _no_owners_left(monkeypatch, 1)
    org._guard_target("owner", "owner", "org_1", granting="owner")


def test_owner_count_failure_blocks_rather_than_permits(monkeypatch):
    """A transient DB error must not read as 'no owners to protect'."""
    def _boom(_o):
        raise database.DatabaseUnavailable("pool exhausted")

    monkeypatch.setattr(database, "db_org_owner_count", _boom)
    with pytest.raises(database.DatabaseUnavailable):
        org._guard_target("owner", "owner", "org_1")


# --- which group a request is about ----------------------------------------

def test_non_member_is_refused(monkeypatch):
    monkeypatch.setattr(database, "db_org_memberships_org_wide", lambda _u: [])
    with pytest.raises(HTTPException) as ei:
        org._resolve_org_for_user("u1", None)
    assert ei.value.status_code == 403


def test_single_group_is_inferred(monkeypatch):
    monkeypatch.setattr(database, "db_org_memberships_org_wide",
                        lambda _u: [{"org_id": "org_1"}])
    assert org._resolve_org_for_user("u1", None) == "org_1"


def test_multiple_groups_must_be_named(monkeypatch):
    """Guessing would be a way to act on the wrong company's account."""
    monkeypatch.setattr(database, "db_org_memberships_org_wide",
                        lambda _u: [{"org_id": "org_1"}, {"org_id": "org_2"}])
    with pytest.raises(HTTPException) as ei:
        org._resolve_org_for_user("u1", None)
    assert ei.value.status_code == 400
    assert org._resolve_org_for_user("u1", "org_2") == "org_2"


def test_cannot_name_a_group_you_do_not_oversee(monkeypatch):
    monkeypatch.setattr(database, "db_org_memberships_org_wide",
                        lambda _u: [{"org_id": "org_1"}])
    with pytest.raises(HTTPException) as ei:
        org._resolve_org_for_user("u1", "org_someone_else")
    assert ei.value.status_code == 403


# --- the minimum-role gate --------------------------------------------------

class _Req:
    method = "POST"
    headers: dict = {}
    client = None
    url = type("U", (), {"path": "/api/org/members"})()


def test_viewer_cannot_manage_members(monkeypatch):
    monkeypatch.setattr(database, "db_org_member_role", lambda _u, _o: "viewer")
    monkeypatch.setattr(org.deps, "audit_log", lambda *a, **k: None)
    with pytest.raises(HTTPException) as ei:
        org._actor_role("u1", "org_1", "manager", _Req())
    assert ei.value.status_code == 403


def test_a_store_scoped_manager_is_not_a_group_member(monkeypatch):
    """db_org_member_role filters tenant_id IS NULL, so someone invited to one store
    returns None here and is refused group-level actions."""
    monkeypatch.setattr(database, "db_org_member_role", lambda _u, _o: None)
    monkeypatch.setattr(org.deps, "audit_log", lambda *a, **k: None)
    with pytest.raises(HTTPException) as ei:
        org._actor_role("u1", "org_1", "manager", _Req())
    assert ei.value.status_code == 403


# --- exactly one owner, and how it moves -----------------------------------

def test_role_endpoint_refuses_to_mint_a_second_owner(monkeypatch):
    """Promoting to owner would leave two, and "the owner" stops meaning anything."""
    monkeypatch.setattr(org.runtime, "USE_DB", True, raising=False)
    monkeypatch.setattr(database, "db_org_memberships_org_wide", lambda _u: [{"org_id": "o1"}])
    monkeypatch.setattr(database, "db_org_member_role", lambda _u, _o: "owner")
    monkeypatch.setattr(org.deps, "audit_log", lambda *a, **k: None)
    with pytest.raises(HTTPException) as ei:
        org.update_org_member_role(
            "u_target", org.OrgMemberRoleUpdate(role="owner"), _Req(), user_id="u_owner"
        )
    assert ei.value.status_code == 400
    assert "transfer" in str(ei.value.detail).lower()


def test_cannot_invite_straight_to_owner(monkeypatch):
    monkeypatch.setattr(org.runtime, "USE_DB", True, raising=False)
    monkeypatch.setattr(database, "db_org_memberships_org_wide", lambda _u: [{"org_id": "o1"}])
    monkeypatch.setattr(database, "db_org_member_role", lambda _u, _o: "owner")
    monkeypatch.setattr(org.deps, "audit_log", lambda *a, **k: None)
    with pytest.raises(HTTPException) as ei:
        org.invite_org_member(
            org.OrgMemberInvite(email="new@example.com", role="owner"), _Req(), user_id="u_owner"
        )
    assert ei.value.status_code == 400


def test_transfer_requires_the_target_to_already_be_in_the_group(monkeypatch):
    """Handing the business to an address that has not accepted an invite would be a
    way to lose it."""
    monkeypatch.setattr(org.runtime, "USE_DB", True, raising=False)
    monkeypatch.setattr(database, "db_org_memberships_org_wide", lambda _u: [{"org_id": "o1"}])
    monkeypatch.setattr(database, "db_org_member_role",
                        lambda u, _o: "owner" if u == "u_owner" else None)
    monkeypatch.setattr(org.deps, "audit_log", lambda *a, **k: None)
    with pytest.raises(HTTPException) as ei:
        org.transfer_org_ownership(
            "u_stranger", org.OrgOwnershipTransfer(), _Req(), user_id="u_owner"
        )
    assert ei.value.status_code == 404


def test_only_an_owner_may_transfer(monkeypatch):
    monkeypatch.setattr(org.runtime, "USE_DB", True, raising=False)
    monkeypatch.setattr(database, "db_org_memberships_org_wide", lambda _u: [{"org_id": "o1"}])
    monkeypatch.setattr(database, "db_org_member_role", lambda _u, _o: "manager")
    monkeypatch.setattr(org.deps, "audit_log", lambda *a, **k: None)
    with pytest.raises(HTTPException) as ei:
        org.transfer_org_ownership(
            "u_target", org.OrgOwnershipTransfer(), _Req(), user_id="u_manager"
        )
    assert ei.value.status_code == 403


def test_transfer_demotes_the_previous_owner(monkeypatch):
    monkeypatch.setattr(org.runtime, "USE_DB", True, raising=False)
    monkeypatch.setattr(database, "db_org_memberships_org_wide", lambda _u: [{"org_id": "o1"}])
    monkeypatch.setattr(database, "db_org_member_role",
                        lambda u, _o: "owner" if u == "u_owner" else "manager")
    monkeypatch.setattr(org.deps, "audit_log", lambda *a, **k: None)
    seen = {}
    monkeypatch.setattr(database, "db_org_transfer_ownership",
                        lambda o, f, t: seen.update(org_id=o, frm=f, to=t) or True)
    out = org.transfer_org_ownership(
        "u_target", org.OrgOwnershipTransfer(), _Req(), user_id="u_owner"
    )
    assert seen == {"org_id": "o1", "frm": "u_owner", "to": "u_target"}
    assert out["owner"] == "u_target"
    # Stated plainly, because the caller is giving away their own access.
    assert out["you_are_now"] == "manager"


# --- the invitation has to be redeemable ------------------------------------

def test_invite_redirects_to_sign_up_not_a_protected_page(monkeypatch):
    """A Clerk ticket can only be redeemed by the sign-up flow.

    This pointed at /dashboard/stores, so an invitee with no account was bounced to
    sign-in and told "new sign ups are currently restricted". The invite was real,
    the email arrived, and it could not be used.
    """
    import clerk_service

    sent = {}
    monkeypatch.setenv("CLERK_SECRET_KEY", "sk_test_dummy")
    monkeypatch.setenv("FRONTEND_URL", "https://staging.example")
    monkeypatch.setattr(clerk_service, "_clerk_user_ids_for_email", lambda *a, **k: [])
    monkeypatch.setattr(clerk_service.database, "db_org_invite_upsert", lambda *a, **k: True)
    monkeypatch.setattr(clerk_service.deps, "_admin_access_log", lambda *a, **k: None)

    class _Resp:
        status_code = 200
        text = "{}"

    import httpx

    def _post(url, **kw):
        sent.update(kw.get("json") or {})
        return _Resp()

    monkeypatch.setattr(httpx, "post", _post)
    clerk_service._clerk_invite_email_to_org("new@example.net", "org_1", "manager")
    url = sent.get("redirect_url", "")
    assert "/sign-up" in url, url
    assert "/dashboard/stores?" not in url and not url.endswith("/dashboard/stores"), url
    assert "invited=1" in url, url


def test_store_scoped_invite_carries_the_store_destination(monkeypatch):
    import clerk_service

    sent = {}
    monkeypatch.setenv("CLERK_SECRET_KEY", "sk_test_dummy")
    monkeypatch.setenv("FRONTEND_URL", "https://staging.example")
    monkeypatch.setattr(clerk_service, "_clerk_user_ids_for_email", lambda *a, **k: [])
    monkeypatch.setattr(clerk_service.database, "db_org_invite_upsert", lambda *a, **k: True)
    monkeypatch.setattr(clerk_service.deps, "_admin_access_log", lambda *a, **k: None)

    class _Resp:
        status_code = 200
        text = "{}"

    import httpx

    monkeypatch.setattr(httpx, "post", lambda url, **kw: (sent.update(kw.get("json") or {}), _Resp())[1])
    clerk_service._clerk_invite_email_to_org("s@example.net", "org_1", "manager", tenant_id="t1")
    url = sent.get("redirect_url", "")
    assert "/sign-up" in url
    # A store manager has no rollup to land on.
    assert "%2Fdashboard" in url and "stores" not in url


def test_viewer_cannot_revoke_an_invite(monkeypatch):
    """Revoking is a membership change, so it needs the same floor as inviting.

    org_id is passed explicitly: called outside FastAPI, a Query(None) default is a
    Query object rather than None, which reads as "a group was named" and refuses
    before reaching the role check this test is about.
    """
    monkeypatch.setattr(org.runtime, "USE_DB", True, raising=False)
    monkeypatch.setattr(database, "db_org_memberships_org_wide", lambda _u: [{"org_id": "o1"}])
    monkeypatch.setattr(database, "db_org_member_role", lambda _u, _o: "viewer")
    monkeypatch.setattr(org.deps, "audit_log", lambda *a, **k: None)
    with pytest.raises(HTTPException) as ei:
        org.revoke_org_invite(_Req(), email="x@example.net", user_id="u1", org_id=None)
    assert ei.value.status_code == 403


def test_revoking_a_missing_invite_is_a_404_not_a_silent_ok(monkeypatch):
    """Reporting success for an invite that was never cancelled would leave a
    standing offer of access to every store in the group."""
    monkeypatch.setattr(org.runtime, "USE_DB", True, raising=False)
    monkeypatch.setattr(database, "db_org_memberships_org_wide", lambda _u: [{"org_id": "o1"}])
    monkeypatch.setattr(database, "db_org_member_role", lambda _u, _o: "manager")
    monkeypatch.setattr(database, "db_org_invite_delete", lambda _e, _o: False)
    monkeypatch.setattr(org.deps, "audit_log", lambda *a, **k: None)
    with pytest.raises(HTTPException) as ei:
        org.revoke_org_invite(_Req(), email="ghost@example.net", user_id="u1", org_id=None)
    assert ei.value.status_code == 404


def test_manager_can_revoke(monkeypatch):
    monkeypatch.setattr(org.runtime, "USE_DB", True, raising=False)
    monkeypatch.setattr(database, "db_org_memberships_org_wide", lambda _u: [{"org_id": "o1"}])
    monkeypatch.setattr(database, "db_org_member_role", lambda _u, _o: "manager")
    monkeypatch.setattr(database, "db_org_invite_delete", lambda _e, _o: True)
    monkeypatch.setattr(org.deps, "audit_log", lambda *a, **k: None)
    out = org.revoke_org_invite(_Req(), email="x@example.net", user_id="u1", org_id=None)
    assert out["ok"] is True


# --- cancelling must actually cancel ----------------------------------------

def test_cancel_revokes_the_clerk_invitation_too(monkeypatch):
    """Deleting our row alone left the emailed link working as a way in after
    access was withdrawn, and made the address un-invitable because Clerk still
    held a pending invite."""
    import clerk_service

    monkeypatch.setattr(org.runtime, "USE_DB", True, raising=False)
    monkeypatch.setattr(database, "db_org_memberships_org_wide", lambda _u: [{"org_id": "o1"}])
    monkeypatch.setattr(database, "db_org_member_role", lambda _u, _o: "manager")
    monkeypatch.setattr(database, "db_org_invite_delete", lambda _e, _o: True)
    monkeypatch.setattr(org.deps, "audit_log", lambda *a, **k: None)
    seen = {}
    monkeypatch.setattr(clerk_service, "revoke_clerk_invitations",
                        lambda e: seen.update(email=e) or {"revoked": 1, "error": None})
    out = org.revoke_org_invite(_Req(), email="x@example.net", user_id="u1", org_id=None)
    assert seen == {"email": "x@example.net"}
    assert out["link_revoked"] is True


def test_cancel_reports_when_the_link_could_not_be_killed(monkeypatch):
    """If Clerk was unreachable the emailed link may still work. Say so rather than
    implying the invitation is dead."""
    import clerk_service

    monkeypatch.setattr(org.runtime, "USE_DB", True, raising=False)
    monkeypatch.setattr(database, "db_org_memberships_org_wide", lambda _u: [{"org_id": "o1"}])
    monkeypatch.setattr(database, "db_org_member_role", lambda _u, _o: "manager")
    monkeypatch.setattr(database, "db_org_invite_delete", lambda _e, _o: True)
    monkeypatch.setattr(org.deps, "audit_log", lambda *a, **k: None)
    monkeypatch.setattr(clerk_service, "revoke_clerk_invitations",
                        lambda _e: {"revoked": 0, "error": "list 500"})
    out = org.revoke_org_invite(_Req(), email="x@example.net", user_id="u1", org_id=None)
    # Access is withdrawn on our side regardless — that must not fail on Clerk.
    assert out["ok"] is True
    assert out["link_revoked"] is False
    assert out["clerk_error"] == "list 500"


def test_self_check_reports_the_frontend_url_value_not_just_that_it_is_set(monkeypatch):
    """Invitation links are built from FRONTEND_URL. Set-but-wrong sends invitees to
    the other environment's app and a different Clerk instance, where the invite
    cannot be redeemed. A boolean cannot catch that."""
    import routers.admin as admin

    monkeypatch.setenv("FRONTEND_URL", "https://www.call-surge.com")
    monkeypatch.setattr(admin.runtime, "USE_DB", False, raising=False)
    out = admin.admin_ops_self_check(_="admin")
    assert out["frontend_url"] == "https://www.call-surge.com"
    assert out["frontend_url_ok"] is True


def test_self_check_flags_a_frontend_url_with_a_path(monkeypatch):
    import routers.admin as admin

    monkeypatch.setenv("FRONTEND_URL", "https://www.call-surge.com/dashboard")
    monkeypatch.setattr(admin.runtime, "USE_DB", False, raising=False)
    assert admin.admin_ops_self_check(_="admin")["frontend_url_ok"] is False
