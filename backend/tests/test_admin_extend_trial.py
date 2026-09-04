"""Admin extend-trial must restore `trialing` status, not just push the date.

Regression guard for the bug where a tenant that was charged after the trial expired
(subscription_status="active") — or refunded/canceled — kept getting gated to starter-tier
features after an admin "extend trial", because get_plan_limits only grants full pro-tier
trial access when subscription_status == "trialing".
"""
from datetime import datetime, timezone

import pytest

import database
import deps
import runtime
from routers import admin


def test_extend_trial_sets_status_trialing(monkeypatch):
    """Extending a free trial writes subscription_status='trialing' + a future trial_ends_at
    in a single update, so the tenant gets the full (pro-tier) trial experience again."""
    calls = {}

    monkeypatch.setattr(runtime, "USE_DB", True, raising=False)
    # A tenant that already paid (charged after the trial lapsed): status is "active", plan "starter".
    monkeypatch.setattr(
        database,
        "db_tenant_get_by_id",
        lambda tid: {
            "id": tid,
            "client_id": "gills-salons",
            "plan": "starter",
            "subscription_status": "active",
            "trial_ends_at": None,
        },
    )

    def fake_update(tenant_id, **kwargs):
        calls["tenant_id"] = tenant_id
        calls.update(kwargs)
        return True

    monkeypatch.setattr(database, "db_tenant_update_subscription", fake_update)
    # The extend-trial path must NOT use the date-only helper (that's what left status stale).
    monkeypatch.setattr(
        database,
        "db_tenant_extend_trial",
        lambda *a, **k: pytest.fail("extend_trial_months must set status, not push the date only"),
    )
    monkeypatch.setattr(deps, "audit_log", lambda *a, **k: None)

    req = admin.BillingExemptUpdate(extend_trial_months=2)
    result = admin.admin_tenant_billing_exempt(
        tenant_id="t1", req=req, request=None, admin_user_id="admin1"
    )

    assert result["success"] is True
    assert result["subscription_status"] == "trialing"
    assert calls["tenant_id"] == "t1"
    assert calls["subscription_status"] == "trialing"
    assert calls["trial_ends_at"] > datetime.now(timezone.utc)


def test_extend_trial_grants_full_pro_access():
    """End-to-end contract: once status is 'trialing' with a future end date, get_plan_limits
    unlocks the pro-tier features that were locked at starter."""
    from plans import get_plan_limits

    future = datetime.now(timezone.utc).replace(year=datetime.now(timezone.utc).year + 1)
    tenant = {
        "plan": "starter",
        "subscription_status": "trialing",
        "trial_ends_at": future,
    }
    limits = get_plan_limits(tenant)
    assert limits["is_trial"] is True
    # Features that are False at starter must be unlocked during an active trial.
    assert limits["has_messages"] is True
    assert limits["has_lead_capture"] is True
    assert limits["has_call_recording"] is True
    assert limits["has_export"] is True


# ---------------------------------------------------------------------------
# Org-billed stores: the subscription lives on the org row, not the store's.
#
# Regression guard for the 2026-09-03 charge: "extend trial" on a store in a group
# reported "No Stripe subscription — nothing was billing them", left the group's
# Stripe trial untouched, and the group was invoiced $150 at the original trial end.
# ---------------------------------------------------------------------------

ORG_ID = "096ba47e-80f8-49d0-9b7e-ee84cde684c9"


def _org_store(tid):
    """A store paid for by its group: no subscription of its own, ever."""
    return {
        "id": tid,
        "client_id": "lana-s-store",
        "org_id": ORG_ID,
        "plan": "starter",
        "subscription_status": "incomplete",
        "stripe_subscription_id": None,
        "trial_ends_at": None,
    }


class _FakeStripe:
    """Stands in for the `stripe` module; records Subscription.modify calls."""

    def __init__(self):
        self.api_key = None
        self.modified = []
        outer = self

        class Subscription:
            @staticmethod
            def modify(sub_id, **kwargs):
                outer.modified.append((sub_id, kwargs))
                return {"id": sub_id, "status": "trialing"}

        self.Subscription = Subscription


@pytest.fixture
def fake_stripe(monkeypatch):
    import sys

    fake = _FakeStripe()
    monkeypatch.setitem(sys.modules, "stripe", fake)
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_fake")
    return fake


def test_defer_billing_uses_the_org_subscription_when_store_has_none(monkeypatch, fake_stripe):
    """The group's subscription is what bills the store, so that is what gets deferred."""
    monkeypatch.setattr(
        database,
        "db_org_get_by_id",
        lambda oid: {"id": oid, "stripe_subscription_id": "sub_org_123", "subscription_status": "active"},
    )
    until = datetime(2026, 10, 3, tzinfo=timezone.utc)

    result = admin._defer_stripe_billing(_org_store("t1"), until)

    assert result["applied"] is True
    assert result["scope"] == "org"
    assert fake_stripe.modified == [
        ("sub_org_123", {"trial_end": int(until.timestamp()), "proration_behavior": "none"})
    ]


def test_defer_billing_reports_no_subscription_only_when_org_has_none_either(monkeypatch, fake_stripe):
    monkeypatch.setattr(
        database, "db_org_get_by_id", lambda oid: {"id": oid, "stripe_subscription_id": None}
    )
    result = admin._defer_stripe_billing(_org_store("t1"), datetime.now(timezone.utc))
    assert result == {"applied": False, "reason": "no_subscription", "scope": "org", "error": None}
    assert fake_stripe.modified == []


def test_extend_trial_on_org_store_moves_stripe_and_mirrors_onto_the_org(monkeypatch, fake_stripe):
    """End to end through the endpoint: the store row, the org row, and Stripe all agree."""
    tenant_calls, org_calls = {}, {}

    monkeypatch.setattr(runtime, "USE_DB", True, raising=False)
    monkeypatch.setattr(database, "db_tenant_get_by_id", _org_store)
    monkeypatch.setattr(
        database,
        "db_org_get_by_id",
        lambda oid: {"id": oid, "stripe_subscription_id": "sub_org_123", "subscription_status": "active"},
    )

    def fake_tenant_update(tenant_id, **kwargs):
        tenant_calls["tenant_id"] = tenant_id
        tenant_calls.update(kwargs)
        return True

    def fake_org_update(org_id, **kwargs):
        org_calls["org_id"] = org_id
        org_calls.update({k: v for k, v in kwargs.items() if v is not None})
        return True

    monkeypatch.setattr(database, "db_tenant_update_subscription", fake_tenant_update)
    monkeypatch.setattr(database, "db_org_update_subscription", fake_org_update)
    monkeypatch.setattr(deps, "audit_log", lambda *a, **k: None)

    result = admin.admin_tenant_billing_exempt(
        tenant_id="t1",
        req=admin.BillingExemptUpdate(extend_trial_months=1),
        request=None,
        admin_user_id="admin1",
    )

    assert result["success"] is True
    assert result["stripe"] == {"applied": True, "reason": "trial_end_set", "scope": "org", "error": None}
    new_ends = tenant_calls["trial_ends_at"]
    assert new_ends > datetime.now(timezone.utc)
    # The org row — where access and the dashboard's trial countdown are read from
    # for a group-paid store — carries the same grant.
    assert org_calls == {
        "org_id": ORG_ID,
        "subscription_status": "trialing",
        "trial_ends_at": new_ends,
    }
    # And Stripe's trial_end moved on the group's subscription, to the same date.
    assert fake_stripe.modified == [
        ("sub_org_123", {"trial_end": int(new_ends.timestamp()), "proration_behavior": "none"})
    ]


def test_exemption_on_org_store_mirrors_exempt_until_onto_the_org(monkeypatch, fake_stripe):
    org_calls = {}
    monkeypatch.setattr(runtime, "USE_DB", True, raising=False)
    monkeypatch.setattr(database, "db_tenant_get_by_id", _org_store)
    monkeypatch.setattr(
        database, "db_org_get_by_id", lambda oid: {"id": oid, "stripe_subscription_id": "sub_org_123"}
    )
    monkeypatch.setattr(database, "db_tenant_set_billing_exempt", lambda *a, **k: True)
    monkeypatch.setattr(database, "db_tenant_extend_trial", lambda *a, **k: True)

    def fake_org_update(org_id, **kwargs):
        org_calls.update({k: v for k, v in kwargs.items() if v is not None})
        return True

    monkeypatch.setattr(database, "db_org_update_subscription", fake_org_update)
    monkeypatch.setattr(deps, "audit_log", lambda *a, **k: None)

    result = admin.admin_tenant_billing_exempt(
        tenant_id="t1",
        req=admin.BillingExemptUpdate(extend_months=1),
        request=None,
        admin_user_id="admin1",
    )

    assert result["success"] is True
    assert result["stripe"]["applied"] is True and result["stripe"]["scope"] == "org"
    assert "billing_exempt_until" in org_calls
    assert "subscription_status" not in org_calls  # an exemption never rewrites status
    assert fake_stripe.modified[0][0] == "sub_org_123"


def test_stripe_status_reads_the_org_subscription_for_a_group_store(monkeypatch, fake_stripe):
    """The admin 'Check Stripe' button must not say 'no subscription' about a store the
    group is paying for."""
    monkeypatch.setattr(runtime, "USE_DB", True, raising=False)
    monkeypatch.setattr(database, "db_tenant_get_by_id", _org_store)
    monkeypatch.setattr(
        database,
        "db_org_get_by_id",
        lambda oid: {"id": oid, "stripe_subscription_id": "sub_org_123", "subscription_status": "active"},
    )

    class _Sub:
        status = "active"
        cancel_at_period_end = False
        current_period_end = 1_790_000_000
        trial_end = None

    fake_stripe.Subscription.retrieve = staticmethod(lambda sid: _Sub())

    out = admin.admin_tenant_stripe_status(tenant_id="t1", admin_user_id="admin1")

    assert out["has_subscription"] is True
    assert out["scope"] == "org"
    assert out["subscription_id"] == "sub_org_123"
    # Compared against the org's own copy, not the store's perpetual 'incomplete'.
    assert out["ours"] == "active"
    assert out["in_sync"] is True
