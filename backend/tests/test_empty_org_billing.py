"""A group with no stores must stop billing.

sync_org_subscription_quantity floored quantity at max(1, store_count), so a group
that removed its last location went on paying for a store it did not have. That is
how a group reached 0 stores and $150/month.
"""
import pytest
from stripe import StripeObject

import stripe
import routers.billing as billing


def _obj(**f):
    o = StripeObject()
    for k, v in f.items():
        setattr(o, k, v)
    return o


@pytest.fixture(autouse=True)
def _stripe_ready(monkeypatch):
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_dummy")
    monkeypatch.setattr(billing.runtime, "USE_DB", True, raising=False)


def _org(monkeypatch, stores):
    monkeypatch.setattr(billing.database, "db_org_get_by_id",
                        lambda _i: {"id": "o1", "stripe_subscription_id": "sub_1"})
    monkeypatch.setattr(billing.database, "db_org_store_count", lambda _i: stores)


def test_last_store_removed_schedules_cancellation(monkeypatch):
    _org(monkeypatch, 0)
    monkeypatch.setattr(stripe.Subscription, "retrieve",
                        lambda *a, **k: _obj(status="active", cancel_at_period_end=False))
    seen = {}
    monkeypatch.setattr(stripe.Subscription, "modify",
                        lambda sub_id, **kw: seen.update(sub=sub_id, **kw) or _obj(id=sub_id))
    out = billing.sync_org_subscription_quantity("o1")
    assert out["quantity"] == 0
    assert out["cancel_scheduled"] is True
    # At period end, not immediately: they paid for this period, and an immediate
    # cancel raises refund questions nobody asked for.
    assert seen["cancel_at_period_end"] is True


def test_it_does_not_bill_for_a_phantom_store(monkeypatch):
    """The old floor. Quantity must never be forced to 1 when there are 0 stores."""
    _org(monkeypatch, 0)
    monkeypatch.setattr(stripe.Subscription, "retrieve",
                        lambda *a, **k: _obj(status="active", cancel_at_period_end=False))
    seen = {}
    monkeypatch.setattr(stripe.Subscription, "modify",
                        lambda sub_id, **kw: seen.update(**kw) or _obj(id=sub_id))
    billing.sync_org_subscription_quantity("o1")
    assert "items" not in seen


def test_already_scheduled_is_left_alone(monkeypatch):
    _org(monkeypatch, 0)
    monkeypatch.setattr(stripe.Subscription, "retrieve",
                        lambda *a, **k: _obj(status="active", cancel_at_period_end=True))

    def _boom(*a, **k):
        raise AssertionError("must not re-modify an already-scheduled cancellation")

    monkeypatch.setattr(stripe.Subscription, "modify", _boom)
    assert billing.sync_org_subscription_quantity("o1")["cancel_scheduled"] is True


def test_adding_a_store_back_resumes_billing(monkeypatch):
    """Otherwise they have stores again and the subscription still dies."""
    _org(monkeypatch, 2)
    monkeypatch.setattr(stripe.Subscription, "retrieve",
                        lambda *a, **k: _obj(
                            status="active",
                            cancel_at_period_end=True,
                            items=_obj(data=[_obj(id="si_1", quantity=0,
                                                  price=_obj(id="price_1"))]),
                        ))
    calls = []
    monkeypatch.setattr(stripe.Subscription, "modify",
                        lambda sub_id, **kw: calls.append(kw) or _obj(id=sub_id))
    billing.sync_org_subscription_quantity("o1")
    assert any(c.get("cancel_at_period_end") is False for c in calls), calls
    assert any(c.get("items") for c in calls), calls


def test_cancel_for_deletion_is_immediate(monkeypatch):
    """The group is about to stop existing — there is nothing left to un-cancel."""
    monkeypatch.setattr(stripe.Subscription, "retrieve",
                        lambda *a, **k: _obj(status="active"))
    seen = {}
    monkeypatch.setattr(stripe.Subscription, "cancel",
                        lambda sub_id, **kw: seen.update(sub=sub_id) or _obj(id=sub_id))
    out = billing.cancel_org_subscription("o1", "sub_1")
    assert out["ok"] is True
    assert seen["sub"] == "sub_1"


def test_already_cancelled_counts_as_success(monkeypatch):
    """The goal is "no longer billing", not "I was the one who stopped it"."""
    monkeypatch.setattr(stripe.Subscription, "retrieve",
                        lambda *a, **k: _obj(status="canceled"))

    def _boom(*a, **k):
        raise AssertionError("must not cancel an already-cancelled subscription")

    monkeypatch.setattr(stripe.Subscription, "cancel", _boom)
    assert billing.cancel_org_subscription("o1", "sub_1")["ok"] is True


def test_a_failed_cancel_is_reported_not_swallowed(monkeypatch):
    """The caller deletes the group next; it must not proceed on a failed cancel."""
    monkeypatch.setattr(stripe.Subscription, "retrieve",
                        lambda *a, **k: _obj(status="active"))

    def _fail(*a, **k):
        raise RuntimeError("stripe down")

    monkeypatch.setattr(stripe.Subscription, "cancel", _fail)
    out = billing.cancel_org_subscription("o1", "sub_1")
    assert out["ok"] is False
    assert "stripe down" in out["error"]
