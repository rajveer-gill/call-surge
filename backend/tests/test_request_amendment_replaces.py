"""Changing the time mid-call must replace the request, not add a second one.

From a live call on production, 2026-08-28:

    02:34:42  booking_created_request  apt_id=197  time=15:00
    02:35:01  caller_said              "Actually, can we make it 4PM instead?"
    02:35:04  booking_created_request  apt_id=198  time=16:00
              (no supersede)

The salon was left holding two requests from the same caller for the same day, and
had to guess which one the customer meant. Superseding only matched the EXACT slot,
so moving the time — the most ordinary amendment there is — never replaced anything.

A request holds nothing and the caller is told it is unconfirmed, so replacing it
costs nobody a booking.
"""
import pytest

import conversation_service as cs


ROWS = []


@pytest.fixture(autouse=True)
def _wire(monkeypatch):
    monkeypatch.setattr(cs.runtime, "USE_DB", True, raising=False)
    monkeypatch.setattr(cs.database, "_client_id", lambda: "shop")
    monkeypatch.setattr(cs.booking_service, "_appointment_rows_for_calendar_merge", lambda: ROWS)
    monkeypatch.setattr(cs.booking_service, "_invalidate_booked_slots_cache", lambda: None)
    monkeypatch.setattr(cs.booking_service, "release_slot", lambda _i: None)
    cs.cancelled_ids = []
    monkeypatch.setattr(
        cs.database, "db_appointments_update",
        lambda aid, **kw: cs.cancelled_ids.append((aid, kw.get("status"))),
    )
    yield
    ROWS.clear()


def _request_row(aid, time, phone="+19255550195", source="receptionist"):
    return {"id": aid, "status": "pending_review", "date": "2026-08-28", "time": time,
            "phone": phone, "source": source, "staff_id": "s1"}


def test_moving_the_time_replaces_the_earlier_request():
    ROWS.append(_request_row(197, "15:00"))
    n = cs._supersede_pending_customer_drafts_for_slot(
        "2026-08-28", "16:00", "s1", client_id="shop", phone="+19255550195"
    )
    assert n == 1
    assert cs.cancelled_ids == [(197, "cancelled")]


def test_a_different_callers_request_is_untouched():
    """Two people can want the same afternoon."""
    ROWS.append(_request_row(197, "15:00", phone="+15550001111"))
    n = cs._supersede_pending_customer_drafts_for_slot(
        "2026-08-28", "16:00", "s1", client_id="shop", phone="+19255550195"
    )
    assert n == 0
    assert cs.cancelled_ids == []


def test_a_request_taken_another_way_is_untouched():
    """Only what the receptionist took on a call is ours to replace."""
    ROWS.append(_request_row(197, "15:00", source="dashboard"))
    n = cs._supersede_pending_customer_drafts_for_slot(
        "2026-08-28", "16:00", "s1", client_id="shop", phone="+19255550195"
    )
    assert n == 0


def test_another_day_is_untouched():
    row = _request_row(197, "15:00")
    row["date"] = "2026-08-29"
    ROWS.append(row)
    n = cs._supersede_pending_customer_drafts_for_slot(
        "2026-08-28", "16:00", "s1", client_id="shop", phone="+19255550195"
    )
    assert n == 0


def test_without_a_caller_phone_nothing_is_replaced():
    """We cannot tell whose request it is, and cancelling a stranger's is worse than
    leaving a duplicate."""
    ROWS.append(_request_row(197, "15:00"))
    n = cs._supersede_pending_customer_drafts_for_slot(
        "2026-08-28", "16:00", "s1", client_id="shop", phone=None
    )
    assert n == 0
