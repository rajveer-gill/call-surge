"""Monthly overage billing cron: confirms Stripe is charged the right amounts
for voice and SMS overage, per-channel, idempotently."""

from unittest.mock import MagicMock

import database
import runtime
from routers import cron


def _setup(
    monkeypatch,
    *,
    usage,
    caps,
    processed=False,
    status="active",
    customer="cus_1",
    billed=(),
    fail_channel=None,
    mark_fails=(),
):
    """Wire the cron's collaborators to fakes.

    billed:       channels already invoiced for this month (per-channel guard).
    fail_channel: 'voice'/'sms' — make that channel's Stripe create raise.
    mark_fails:   channels whose marker write should report failure.

    Returns (items, inserted, marked): the Stripe create kwargs, the tenant-wide
    processed markers, and the per-channel markers written.
    """
    monkeypatch.setattr(cron, "_verify_cron_secret", lambda request: True)
    monkeypatch.setattr("runtime.USE_DB", True)
    monkeypatch.setattr(cron, "STRIPE_AVAILABLE", True)
    monkeypatch.setenv("STRIPE_SECRET_KEY", "sk_test_x")
    monkeypatch.setattr(cron, "get_plan_limits", lambda t: caps)
    monkeypatch.setattr(database, "db_cron_run_start", lambda name: 1)
    monkeypatch.setattr(database, "db_cron_run_finish", lambda *a, **k: None)
    monkeypatch.setattr(
        database, "db_tenant_list_all",
        lambda: [{"client_id": "salon", "subscription_status": status, "stripe_customer_id": customer}],
    )
    monkeypatch.setattr(database, "db_overage_processed_exists", lambda cid, m: processed)
    inserted = []
    monkeypatch.setattr(database, "db_overage_processed_insert", lambda cid, m: inserted.append((cid, m)))
    monkeypatch.setattr(database, "db_usage_get", lambda cid, m: usage)
    monkeypatch.setattr(database, "db_overage_items_billed", lambda cid, m: set(billed))
    marked = []

    def _mark(cid, month, channel):
        if channel in mark_fails:
            return False
        marked.append(channel)
        return True

    monkeypatch.setattr(database, "db_overage_item_mark_billed", _mark)
    items = []

    def _create(**kw):
        if fail_channel and fail_channel in kw.get("idempotency_key", ""):
            raise RuntimeError("stripe boom")
        items.append(kw)

    fake_stripe = MagicMock()
    fake_stripe.InvoiceItem.create.side_effect = _create
    monkeypatch.setattr(cron, "stripe", fake_stripe)
    return items, inserted, marked


def test_bills_voice_and_sms_overage(monkeypatch):
    # 600 min used vs 500 cap = 100 over @ $0.15 = $15.00 = 1500c
    # 120 texts vs 100 cap = 20 over @ $0.05 = $1.00 = 100c
    items, inserted, marked = _setup(
        monkeypatch,
        usage={"voice_minutes": 600, "sms_count": 120},
        caps={"minutes_cap": 500, "sms_cap": 100},
    )
    res = cron.cron_process_overage(MagicMock())
    assert res["invoices_created"] == 2
    amounts = sorted(i["amount"] for i in items)
    assert amounts == [100, 1500]
    assert all(i["currency"] == "usd" and i["customer"] == "cus_1" for i in items)
    descriptions = " ".join(i["description"] for i in items)
    assert "minutes" in descriptions and "texts" in descriptions
    assert sorted(marked) == ["sms", "voice"]
    assert inserted == [("salon", inserted[0][1])]  # processed marker written


def test_bills_only_the_channel_that_is_over(monkeypatch):
    # voice under cap, sms over
    items, _, marked = _setup(
        monkeypatch,
        usage={"voice_minutes": 400, "sms_count": 130},
        caps={"minutes_cap": 500, "sms_cap": 100},
    )
    res = cron.cron_process_overage(MagicMock())
    assert res["invoices_created"] == 1
    assert items[0]["amount"] == 150  # 30 texts * $0.05
    assert "texts" in items[0]["description"]
    assert marked == ["sms"]


def test_no_charge_when_within_caps(monkeypatch):
    items, inserted, _ = _setup(
        monkeypatch,
        usage={"voice_minutes": 100, "sms_count": 10},
        caps={"minutes_cap": 500, "sms_cap": 100},
    )
    res = cron.cron_process_overage(MagicMock())
    assert res["invoices_created"] == 0
    assert items == []
    assert len(inserted) == 1  # still marks processed so we don't recheck


def test_idempotent_skip_when_already_processed(monkeypatch):
    items, _, _ = _setup(
        monkeypatch,
        usage={"voice_minutes": 999, "sms_count": 999},
        caps={"minutes_cap": 500, "sms_cap": 100},
        processed=True,
    )
    res = cron.cron_process_overage(MagicMock())
    assert res["invoices_created"] == 0
    assert items == []


def test_skips_non_active_subscriptions(monkeypatch):
    items, _, _ = _setup(
        monkeypatch,
        usage={"voice_minutes": 999, "sms_count": 999},
        caps={"minutes_cap": 500, "sms_cap": 100},
        status="trialing",
    )
    res = cron.cron_process_overage(MagicMock())
    assert res["invoices_created"] == 0
    assert items == []


# --- Partial-failure recovery -------------------------------------------------
# The bug these cover: voice billed, SMS raised, no marker written at all, so the
# next run charged the customer for voice a second time.


def test_sms_failure_still_records_the_voice_charge(monkeypatch):
    items, inserted, marked = _setup(
        monkeypatch,
        usage={"voice_minutes": 600, "sms_count": 120},
        caps={"minutes_cap": 500, "sms_cap": 100},
        fail_channel="sms",
    )
    res = cron.cron_process_overage(MagicMock())
    assert res["invoices_created"] == 1
    assert res["errors"] == 1
    assert [i["amount"] for i in items] == [1500]  # voice went through
    assert marked == ["voice"]  # and is recorded, so a retry won't repeat it
    assert inserted == []  # tenant is not "done" — SMS still owes


def test_retry_after_partial_failure_bills_only_the_missing_channel(monkeypatch):
    # Same tenant/month as above, re-run with voice already recorded.
    items, inserted, marked = _setup(
        monkeypatch,
        usage={"voice_minutes": 600, "sms_count": 120},
        caps={"minutes_cap": 500, "sms_cap": 100},
        billed=("voice",),
    )
    res = cron.cron_process_overage(MagicMock())
    assert res["invoices_created"] == 1
    assert res["errors"] == 0
    assert [i["amount"] for i in items] == [100]  # SMS only — voice not re-billed
    assert marked == ["sms"]
    assert len(inserted) == 1  # now the tenant is complete


def test_voice_failure_does_not_block_sms(monkeypatch):
    items, inserted, marked = _setup(
        monkeypatch,
        usage={"voice_minutes": 600, "sms_count": 120},
        caps={"minutes_cap": 500, "sms_cap": 100},
        fail_channel="voice",
    )
    res = cron.cron_process_overage(MagicMock())
    assert res["invoices_created"] == 1
    assert res["errors"] == 1
    assert [i["amount"] for i in items] == [100]
    assert marked == ["sms"]
    assert inserted == []


def test_each_channel_gets_a_distinct_idempotency_key(monkeypatch):
    items, _, _ = _setup(
        monkeypatch,
        usage={"voice_minutes": 600, "sms_count": 120},
        caps={"minutes_cap": 500, "sms_cap": 100},
    )
    cron.cron_process_overage(MagicMock())
    keys = sorted(i["idempotency_key"] for i in items)
    assert len(set(keys)) == 2
    assert all(k.startswith("overage:salon:") for k in keys)
    assert keys[0].endswith(":sms") and keys[1].endswith(":voice")


def test_marker_write_failure_is_reported_and_withholds_processed(monkeypatch):
    # Charged in Stripe but the guard didn't persist — the next run would re-bill,
    # so the run must not report clean.
    items, inserted, marked = _setup(
        monkeypatch,
        usage={"voice_minutes": 600, "sms_count": 120},
        caps={"minutes_cap": 500, "sms_cap": 100},
        mark_fails=("voice",),
    )
    res = cron.cron_process_overage(MagicMock())
    assert res["invoices_created"] == 2
    assert res["errors"] == 1
    assert marked == ["sms"]
    assert inserted == []
