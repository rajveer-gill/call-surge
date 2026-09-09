"""Analytics & call-log reporting (plan-gated reads) + the call-recording proxy.

The recording proxy (/api/analytics/calls/{call_sid}/recording) streams MP3s from Twilio
via voice_service's SSRF-guarded fetch; the read routes report on the call log.
"""

from __future__ import annotations

import asyncio
import csv
import io
import json
import os
from datetime import datetime, timedelta, timezone
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Response

import config_service
import database
import deps
import runtime
import voice_service
from observability import system_info

try:
    from plans import get_plan_limits
except ImportError:  # pragma: no cover - plans module always present in practice
    get_plan_limits = None  # type: ignore

TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN")

router = APIRouter()


@router.get("/api/stats")
def get_stats(tenant: Optional[dict] = Depends(deps.require_active_subscription)):
    # Re-bind tenant context in the handler: a contextvar set inside the sync
    # require_tenant dependency does not survive into this sync endpoint (separate
    # threadpool context), so without this the reads fall back to the default
    # client_id and every tenant sees zeros. Same pattern as the appointments route.
    # DIAGNOSTIC: capture what the contextvar resolved to BEFORE we re-bind, to prove
    # whether the require_tenant dependency's client_id reached this sync handler.
    raw_cid_before_bind = database._client_id() if runtime.USE_DB else None
    cid = deps._bind_tenant_db_context(tenant)
    apts = database.db_appointments_get_all(client_id=cid) if runtime.USE_DB else runtime.appointments
    # Total Messages = all texts exchanged (in + out) across every SMS conversation.
    total_messages = database.db_sms_messages_total(cid) if runtime.USE_DB else len(runtime.messages)
    pending = len([a for a in apts if a.get("status") == "pending"])
    if runtime.USE_DB:
        diag = database.db_appointments_diagnostics(cid)
        system_info(
            "dashboard_stats_debug",
            tenant_client_id=cid,
            contextvar_before_bind=raw_cid_before_bind,
            bind_changed_cid=(raw_cid_before_bind != cid),
            appointment_count=len(apts),
            message_count=total_messages,
            appts_by_status=diag.get("by_status"),
            env_client_id=diag.get("env_client_id"),
            env_appointment_count=diag.get("env_client_id_appointment_count"),
            likely_client_id_mismatch=bool(diag.get("likely_mismatch")),
        )
    return {
        "total_appointments": len(apts),
        "total_messages": total_messages,
        "pending_appointments": pending,
    }


def _mark_utc(value) -> str:
    """Stamp a stored timestamp as UTC, because it is one and does not say so.

    Call starts are written with `datetime.now().isoformat()` (voice_service), which on
    Render — where the clock is UTC — produces "2026-09-09T17:18:06.123456" with no
    offset. The browser then does `new Date(...)` on that, and a bare ISO string with a
    time and no zone is parsed as LOCAL. So Gig Harbor read a 10:18 AM call as 5:18 PM,
    every row seven hours out, and a call anyone went looking for by time was not where
    they looked.

    The aggregation below has always assumed naive means UTC (`dt.replace(tzinfo=utc)`),
    so the hour and weekday charts were right the whole time — only the raw string handed
    to the browser was ambiguous. Saying so here fixes rows already in the database as
    well as new ones, which a change to the writer alone would not.
    """
    s = (value or "").strip()
    if not s:
        return s
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return s
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.isoformat()


def _with_utc_timestamps(entries: List[dict]) -> List[dict]:
    for e in entries:
        for key in ("start_iso", "end_iso"):
            if e.get(key):
                e[key] = _mark_utc(e[key])
    return entries


def _load_call_log(days: Optional[int] = None) -> List[dict]:
    """Load call log. If days set, filter by plan (DB only). Returns list of call entries (newest first)."""
    if runtime.USE_DB:
        return _with_utc_timestamps(database.db_call_log_load(limit=5000, days=days))
    data_dir = config_service.get_client_data_dir()
    if not data_dir:
        return []
    path = data_dir / "call_log.json"
    if not path.exists():
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            return _with_utc_timestamps(json.load(f))
    except Exception:
        return []


def _call_log_days(tenant: Optional[dict]) -> int:
    """Return call log retention days for plan."""
    return get_plan_limits(tenant).get("call_log_days", 30) if get_plan_limits else 9999


def _analytics_iso_week_bounds_utc(now: Optional[datetime] = None) -> tuple:
    """Current ISO week: Monday 00:00 UTC (inclusive) through next Monday 00:00 UTC (exclusive)."""
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)
    monday = now - timedelta(days=now.weekday())
    week_start = monday.replace(hour=0, minute=0, second=0, microsecond=0)
    week_end_excl = week_start + timedelta(days=7)
    return week_start, week_end_excl


def _weekday_sun_zero(dt: datetime) -> int:
    """Dashboard uses Sun=0..Sat=6; Python weekday is Mon=0..Sun=6."""
    return (dt.weekday() + 1) % 7


@router.get("/api/analytics/health")
def get_analytics_health(
    tenant: Optional[dict] = Depends(deps.require_tenant),
    _: None = Depends(deps.require_active_subscription),
):
    """7-day call health metrics for tenant dashboard (not plan-gated)."""
    raw_cid_before_bind = database._client_id() if runtime.USE_DB else None  # DIAGNOSTIC
    cid = deps._bind_tenant_db_context(tenant)  # contextvar from the dep doesn't reach this sync handler
    period_days = 7
    log = _load_call_log(days=period_days)
    if runtime.USE_DB:
        all_cid_calls = database.db_call_log_load(limit=5000, days=None)
        system_info(
            "dashboard_callhealth_debug",
            tenant_client_id=cid,
            contextvar_before_bind=raw_cid_before_bind,
            bind_changed_cid=(raw_cid_before_bind != cid),
            calls_last_7d=len(log),
            calls_all_time_this_tenant=len(all_cid_calls),
        )
    total = len(log)
    if total == 0:
        return {
            "period_days": period_days,
            "calls_total": 0,
            "forward_rate": 0.0,
            "error_rate": 0.0,
            "missed_rate": 0.0,
            "booking_completion_rate": 0.0,
            "avg_duration_sec": 0,
            "by_outcome": {},
        }
    by_outcome: dict[str, int] = {}
    durations: list[int] = []
    booking_signals = 0
    for entry in log:
        o = entry.get("outcome") or "unknown"
        by_outcome[o] = by_outcome.get(o, 0) + 1
        ds = entry.get("duration_sec")
        if ds is not None:
            try:
                durations.append(int(ds))
            except (TypeError, ValueError):
                pass
        if entry.get("call_summary") or entry.get("category") == "booking":
            booking_signals += 1
    forwarded = by_outcome.get("forwarded", 0)
    errors = by_outcome.get("error", 0)
    missed = by_outcome.get("missed", 0) + by_outcome.get("no_answer", 0)
    answered = by_outcome.get("answered_by_ai", 0)
    return {
        "period_days": period_days,
        "calls_total": total,
        "forward_rate": round(forwarded / total, 3),
        "error_rate": round(errors / total, 3),
        "missed_rate": round(missed / total, 3),
        "booking_completion_rate": (
            round(booking_signals / total, 3)
            if booking_signals
            else round(answered / total, 3)
        ),
        "avg_duration_sec": (
            round(sum(durations) / len(durations), 1) if durations else 0
        ),
        "by_outcome": by_outcome,
    }


@router.get("/api/analytics/summary")
def get_analytics_summary(
    tenant: Optional[dict] = Depends(deps.require_tenant),
    _: None = Depends(deps.require_active_subscription),
):
    """Pro: Peak call times, outcomes, total calls. Filtered by plan (call_log_days).
    by_day_of_week counts only the current ISO week (UTC); full history stays in DB/export.
    """
    deps._bind_tenant_db_context(tenant)  # contextvar from the dep doesn't reach this sync handler
    days = _call_log_days(tenant)
    log = _load_call_log(days=days)
    week_start, week_end_excl = _analytics_iso_week_bounds_utc()
    week_period = {
        "by_day_of_week_period_start": week_start.date().isoformat(),
        "by_day_of_week_period_end": (week_end_excl - timedelta(days=1))
        .date()
        .isoformat(),
        "by_day_of_week_timezone": "UTC",
    }
    if not log:
        return {
            "total_calls": 0,
            "by_outcome": {},
            "by_hour": {str(h): 0 for h in range(24)},
            "by_day_of_week": {str(d): 0 for d in range(7)},
            "client_id": database._client_id() or None,
            **week_period,
        }
    by_outcome = {}
    by_hour = {str(h): 0 for h in range(24)}
    by_day = {str(d): 0 for d in range(7)}
    for entry in log:
        o = entry.get("outcome") or "unknown"
        by_outcome[o] = by_outcome.get(o, 0) + 1
        start_iso = entry.get("start_iso")
        if start_iso:
            try:
                dt = datetime.fromisoformat(start_iso.replace("Z", "+00:00"))
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                by_hour[str(dt.hour)] = by_hour.get(str(dt.hour), 0) + 1
                if week_start <= dt < week_end_excl:
                    wd = _weekday_sun_zero(dt)
                    by_day[str(wd)] = by_day.get(str(wd), 0) + 1
            except Exception:
                pass
    return {
        "total_calls": len(log),
        "by_outcome": by_outcome,
        "by_hour": by_hour,
        "by_day_of_week": by_day,
        "client_id": database._client_id() or None,
        **week_period,
    }


@router.get("/api/analytics/calls")
def get_analytics_calls(
    limit: int = 50,
    outcome: Optional[str] = None,
    tenant: Optional[dict] = Depends(deps.require_tenant),
    _: None = Depends(deps.require_active_subscription),
):
    """Pro: Recent calls for dashboard. Filtered by plan (call_log_days). Optional filter by outcome."""
    deps._bind_tenant_db_context(tenant)  # contextvar from the dep doesn't reach this sync handler
    days = _call_log_days(tenant)
    log = _load_call_log(days=days)
    if outcome:
        log = [e for e in log if (e.get("outcome") or "") == outcome]
    return {"calls": log[:limit], "client_id": database._client_id() or None}


@router.get("/api/analytics/export")
def get_analytics_export(
    tenant: Optional[dict] = Depends(deps.require_tenant),
    _: None = Depends(deps.require_active_subscription),
):
    """Export call log as CSV. Growth/Pro only."""
    if (
        not tenant
        or not get_plan_limits
        or not get_plan_limits(tenant).get("has_export")
    ):
        raise HTTPException(
            status_code=403, detail="Export is available on Growth and Pro plans"
        )
    deps._bind_tenant_db_context(tenant)  # contextvar from the dep doesn't reach this sync handler
    days = _call_log_days(tenant)
    log = _load_call_log(days=days)

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(
        [
            "call_sid",
            "from_number",
            "to_number",
            "start_iso",
            "end_iso",
            "outcome",
            "duration_sec",
            "category",
            "created_at",
            "recording_sid",
            "recording_duration_sec",
            "recording_status",
            "call_summary",
        ]
    )
    for e in log:
        writer.writerow(
            [
                e.get("call_sid", ""),
                e.get("from_number", ""),
                e.get("to_number", ""),
                e.get("start_iso", ""),
                e.get("end_iso", ""),
                e.get("outcome", ""),
                e.get("duration_sec", ""),
                e.get("category", ""),
                e.get("created_at", ""),
                e.get("recording_sid", ""),
                e.get("recording_duration_sec", ""),
                e.get("recording_status", ""),
                e.get("call_summary", ""),
            ]
        )
    return Response(
        content=buf.getvalue(),
        media_type="text/csv",
        headers={"Content-Disposition": "attachment; filename=call_log.csv"},
    )


# ===== call-recording proxy (moved from main) =====

@router.get("/api/analytics/calls/{call_sid}/recording")
async def get_call_recording_audio(
    call_sid: str,
    tenant: Optional[dict] = Depends(deps.require_tenant),
    _: None = Depends(deps.require_active_subscription),
):
    """Stream call recording (MP3) from Twilio using server-side credentials; tenant must own the call."""
    if not tenant or not runtime.USE_DB:
        raise HTTPException(status_code=404, detail="Recording not available")
    if not voice_service._call_recording_enabled_for_tenant(tenant):
        raise HTTPException(status_code=404, detail="Recording not available")
    if not TWILIO_ACCOUNT_SID or not TWILIO_AUTH_TOKEN:
        raise HTTPException(
            status_code=503, detail="Recording playback is not configured"
        )
    row = database.db_call_log_get_by_call_sid(tenant["client_id"], call_sid)
    if not row or not row.get("recording_url"):
        raise HTTPException(status_code=404, detail="Recording not available")
    code, data = await asyncio.to_thread(
        voice_service._fetch_twilio_recording_bytes, row["recording_url"]
    )
    if code != 200:
        raise HTTPException(status_code=502, detail="Could not fetch recording")
    return Response(
        content=data,
        media_type="audio/mpeg",
        headers={
            "Content-Disposition": f'inline; filename="{call_sid}.mp3"',
            "Cache-Control": "private, max-age=300",
        },
    )
