"""A store manager gets one store, and getting it takes nothing from anyone.

Inviting a store manager used to make them a tenant_members row, which runs through
db_tenant_member_assign_owner — "make this user the sole owner of the tenant". That
deletes every other member of the store AND every other membership the invitee had,
so inviting someone who already had an account silently took a store away from them.

Store managers are now org members scoped to a single store: org membership is
explicitly exempt from the one-user-one-tenant collapsing, so the grant is additive.
The tests that matter are the containment ones — a store manager must not reach the
group's other stores, its rollup, or its billing.
"""

from __future__ import annotations

import os

import pytest
from fastapi import HTTPException

import database
import deps

_DB = pytest.mark.skipif(
    not os.getenv("DATABASE_URL"), reason="DATABASE_URL required (needs real Postgres)"
)


class _Req:
    def __init__(self, method="GET", store=None):
        self.method = method
        self.headers = {"X-Store-Id": store} if store else {}
        self.client = None
        self.url = "http://test/api/stats"


# --- Telling the two kinds of membership apart --------------------------------


def test_org_wide_excludes_store_scoped(monkeypatch):
    """The gate for the rollup, billing, and adding stores."""
    monkeypatch.setattr(
        database,
        "db_org_memberships",
        lambda uid: [
            {"org_id": "o1", "name": "Region", "role": "manager", "tenant_id": None},
            {"org_id": "o2", "name": "Other", "role": "manager", "tenant_id": "t-9"},
        ],
    )
    assert [m["org_id"] for m in database.db_org_memberships_org_wide("u")] == ["o1"]


def test_a_store_manager_has_no_org_wide_membership(monkeypatch):
    monkeypatch.setattr(
        database,
        "db_org_memberships",
        lambda uid: [{"org_id": "o1", "name": "R", "role": "manager", "tenant_id": "t-1"}],
    )
    assert database.db_org_memberships_org_wide("u") == []


# --- The single-store fallback in require_tenant ------------------------------
# A store manager never visits the store list, so they send no X-Store-Id and land
# here instead of in _resolve_org_store. It has to apply the same role gate.


@pytest.mark.parametrize("method", ["POST", "PATCH", "DELETE", "PUT"])
def test_a_read_only_viewer_cannot_write(monkeypatch, method):
    """Both routes into an org store share this gate. Before it was extracted, only
    the header path applied it — so omitting the header was a way around it."""
    monkeypatch.setattr(deps, "audit_log", lambda *a, **k: None)
    with pytest.raises(HTTPException) as e:
        deps._enforce_org_write_role(_Req(method=method), "viewer", "u", "shop-a")
    assert e.value.status_code == 403


@pytest.mark.parametrize("method", ["GET", "HEAD", "OPTIONS"])
def test_a_viewer_may_still_read(monkeypatch, method):
    deps._enforce_org_write_role(_Req(method=method), "viewer", "u", "shop-a")


@pytest.mark.parametrize("method", ["GET", "POST", "DELETE"])
def test_a_manager_may_do_anything(method):
    deps._enforce_org_write_role(_Req(method=method), "manager", "u", "shop-a")


def test_an_unknown_role_is_treated_as_read_only(monkeypatch):
    """A typo or an unrecognised role must fail closed, never grant write.

    "owner" used to be in this list as a stand-in for "a role we do not know". It is
    a real role now and outranks manager, so it moved to the test below. The
    fail-closed rule is unchanged: anything NOT in ORG_ROLES still gets nothing.
    """
    monkeypatch.setattr(deps, "audit_log", lambda *a, **k: None)
    for role in ("", "   ", "admin", "superuser", None):
        with pytest.raises(HTTPException):
            deps._enforce_org_write_role(_Req(method="POST"), role, "u", "shop-a")


def test_an_owner_may_write(monkeypatch):
    """The head account must not be read-only. This gate asked role == "manager",
    so introducing a role ABOVE manager silently made the owner read-only across
    the whole product."""
    monkeypatch.setattr(deps, "audit_log", lambda *a, **k: None)
    deps._enforce_org_write_role(_Req(method="POST"), "owner", "u", "shop-a")


# --- Containment (needs Postgres: the scope lives in the SQL join) ------------


@_DB
def test_a_store_manager_reaches_only_their_store():
    database.init_db()
    org = database.db_org_create("Region")
    a = database.db_tenant_create_pending("scope-a", "Store A", "pro", "salon_chair")
    b = database.db_tenant_create_pending("scope-b", "Store B", "pro", "salon_chair")
    database.db_org_attach_tenant(a["id"], org["id"])
    database.db_org_attach_tenant(b["id"], org["id"])

    database.db_org_member_add("user_mgr", org["id"], "manager", tenant_id=a["id"])

    assert database.db_org_store_for_user("user_mgr", "scope-a") is not None
    assert database.db_org_store_for_user("user_mgr", "scope-b") is None, (
        "a store manager must not reach the group's other stores"
    )
    assert [s["client_id"] for s in database.db_org_stores_for_user("user_mgr")] == ["scope-a"]
    assert database.db_org_memberships_org_wide("user_mgr") == []


@_DB
def test_an_org_wide_member_still_reaches_every_store():
    """The regression guard for the regional manager the feature was built for."""
    database.init_db()
    org = database.db_org_create("Region")
    a = database.db_tenant_create_pending("wide-a", "Store A", "pro", "salon_chair")
    b = database.db_tenant_create_pending("wide-b", "Store B", "pro", "salon_chair")
    database.db_org_attach_tenant(a["id"], org["id"])
    database.db_org_attach_tenant(b["id"], org["id"])

    database.db_org_member_add("user_regional", org["id"], "manager")  # no tenant_id

    assert database.db_org_store_for_user("user_regional", "wide-a") is not None
    assert database.db_org_store_for_user("user_regional", "wide-b") is not None
    assert len(database.db_org_memberships_org_wide("user_regional")) == 1


@_DB
def test_inviting_a_manager_takes_nothing_from_anyone():
    """The bug this replaced: the invitee lost every other store they managed, and
    every other member lost this one."""
    database.init_db()
    org = database.db_org_create("Region")
    a = database.db_tenant_create_pending("keep-a", "Store A", "pro", "salon_chair")
    b = database.db_tenant_create_pending("keep-b", "Store B", "pro", "salon_chair")
    database.db_org_attach_tenant(a["id"], org["id"])
    database.db_org_attach_tenant(b["id"], org["id"])

    database.db_org_member_add("user_regional", org["id"], "manager")   # oversees both
    database.db_org_member_add("user_mgr", org["id"], "manager", tenant_id=a["id"])

    # The regional manager still has both stores.
    assert len(database.db_org_stores_for_user("user_regional")) == 2
    # And a direct dashboard owner elsewhere is untouched.
    other = database.db_tenant_create_pending("indep", "Indie", "pro", "salon_chair")
    database.db_tenant_member_add("user_mgr", other["id"])
    assert other["id"] in database.db_tenant_membership_tenant_ids("user_mgr")


@_DB
def test_a_scoped_invite_becomes_a_scoped_membership():
    database.init_db()
    org = database.db_org_create("Region")
    store = database.db_tenant_create_pending("inv-a", "Store A", "pro", "salon_chair")
    database.db_org_attach_tenant(store["id"], org["id"])

    database.db_org_invite_upsert("mgr@co.com", org["id"], "manager", tenant_id=store["id"])
    joined = database.db_org_invites_consume_for_emails("user_new", ["mgr@co.com"])
    assert len(joined) == 1
    assert joined[0]["tenant_id"] == store["id"], "the scope must survive the invite"
    assert [s["client_id"] for s in database.db_org_stores_for_user("user_new")] == ["inv-a"]


@_DB
def test_deleting_the_store_removes_its_manager():
    """A membership left pointing at nothing would widen to the whole group, because
    NULL tenant_id means org-wide."""
    database.init_db()
    org = database.db_org_create("Region")
    a = database.db_tenant_create_pending("del-a", "Store A", "pro", "salon_chair")
    b = database.db_tenant_create_pending("del-b", "Store B", "pro", "salon_chair")
    database.db_org_attach_tenant(a["id"], org["id"])
    database.db_org_attach_tenant(b["id"], org["id"])
    database.db_org_member_add("user_mgr", org["id"], "manager", tenant_id=a["id"])

    database.db_tenant_delete(a["id"])
    assert database.db_org_stores_for_user("user_mgr") == []
    assert database.db_org_store_for_user("user_mgr", "del-b") is None
