"""Scheduled-job endpoints (X-Cron-Secret auth). Idempotent where possible."""

from __future__ import annotations

import asyncio
import hmac
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import List

from fastapi import APIRouter, HTTPException, Request

import config_service
import database
import runtime
import sms_service

# Bounded concurrency for fan-out SMS sends so reminder runs stay well under the
# cron HTTP timeout as the tenant count grows.
_REMINDER_SEND_CONCURRENCY = 8


def _send_reminder_with_ctx(cid: str, phone: str, body: str, from_num: str) -> bool:
    """Run one reminder send in a worker thread: bind tenant context for usage
    metering, then release the pooled DB connection (no request middleware here)."""
    database.set_request_client_id(cid)
    try:
        return bool(sms_service.send_sms(phone, body, from_override=from_num))
    except Exception:
        return False
    finally:
        database.db_release_thread_connection()

try:
    from plans import get_plan_limits
except ImportError:  # pragma: no cover
    get_plan_limits = None  # type: ignore

try:
    import stripe

    STRIPE_AVAILABLE = True
except ImportError:  # pragma: no cover
    stripe = None
    STRIPE_AVAILABLE = False

logger = logging.getLogger("nuvatra")
router = APIRouter()


def _verify_cron_secret(request: Request) -> bool:
    """Constant-time comparison of X-Cron-Secret. Returns True if valid."""
    expected = (os.getenv("CRON_SECRET") or "").strip()
    if not expected:
        logger.warning("CRON_SECRET not set; cron auth disabled")
        return False
    received = request.headers.get("X-Cron-Secret", "")
    return (
        hmac.compare_digest(expected.encode(), received.encode()) if received else False
    )


@router.post("/api/cron/appointment-reminders")
async def cron_appointment_reminders(request: Request):
    """Day-before SMS reminders for accepted appointments. Requires X-Cron-Secret. Idempotent."""
    if not _verify_cron_secret(request):
        raise HTTPException(status_code=401, detail="Unauthorized")
    run_id = database.db_cron_run_start("appointment-reminders") if runtime.USE_DB else None
    if not runtime.USE_DB:
        result = {
            "ok": True,
            "reminders_sent": 0,
            "errors": 0,
            "skipped": 0,
            "tenants_processed": 0,
        }
        return result
    tz_name = (os.getenv("REMINDER_TIMEZONE") or "UTC").strip()
    try:
        from zoneinfo import ZoneInfo

        tz = ZoneInfo(tz_name)
    except Exception:
        tz = timezone.utc
    tomorrow_local = (datetime.now(tz) + timedelta(days=1)).strftime("%Y-%m-%d")
    skipped = 0
    tenants_processed = 0
    tenants = database.db_tenant_list_all()
    # Phase 1: walk tenants/appointments, mark reminders (idempotency guard), and
    # collect the sends. Config is loaded once per tenant, not per appointment.
    sends: List[tuple] = []
    for t in tenants:
        limits = get_plan_limits(t) if get_plan_limits else {}
        if not limits.get("has_reminders"):
            continue
        tenants_processed += 1
        cid = t.get("client_id")
        twilio_num = t.get("twilio_phone_number")
        if not cid or not twilio_num:
            continue
        appointments = database.db_appointments_get_accepted_for_date(cid, tomorrow_local)
        if not appointments:
            continue
        cfg = config_service.load_client_config(cid)
        business_name = (config_service.customer_facing_name(cfg) or "us") if cfg else "us"
        for apt in appointments:
            phone = apt.get("phone")
            if not phone:
                skipped += 1
                continue
            if not database.db_appointments_mark_reminder_sent(apt.get("id"), cid):
                skipped += 1
                continue
            time_str = apt.get("time", "")
            body = f"Reminder: You have an appointment tomorrow at {time_str} at {business_name}. Reply YES to confirm or if you need to reschedule."
            sends.append((cid, phone, body, twilio_num))

    # Phase 2: dispatch sends with bounded concurrency (threads; send_sms retries
    # internally). Keeps wall-clock ~= (sends / concurrency) × latency at any tenant count.
    sem = asyncio.Semaphore(_REMINDER_SEND_CONCURRENCY)

    async def _dispatch(args) -> bool:
        async with sem:
            return await asyncio.to_thread(_send_reminder_with_ctx, *args)

    outcomes = await asyncio.gather(*[_dispatch(s) for s in sends], return_exceptions=True)
    reminders_sent = sum(1 for r in outcomes if r is True)
    errors = len(sends) - reminders_sent
    result = {
        "ok": True,
        "reminders_sent": reminders_sent,
        "errors": errors,
        "skipped": skipped,
        "tenants_processed": tenants_processed,
    }
    database.db_cron_run_finish(run_id, "success", result)
    return result


@router.post("/api/cron/process-overage")
def cron_process_overage(request: Request):
    """Monthly overage billing. Compute overage for previous month and create Stripe invoice items. Requires X-Cron-Secret."""
    if not _verify_cron_secret(request):
        raise HTTPException(status_code=401, detail="Unauthorized")
    run_id = database.db_cron_run_start("process-overage") if runtime.USE_DB else None
    if not runtime.USE_DB or not STRIPE_AVAILABLE or not stripe:
        result = {
            "ok": True,
            "tenants_processed": 0,
            "invoices_created": 0,
            "errors": 0,
        }
        if run_id:
            database.db_cron_run_finish(run_id, "success", result)
        return result
    from billing_config import get_overage_price_per_minute, get_overage_price_per_sms

    price_per_min = get_overage_price_per_minute()
    price_per_sms = get_overage_price_per_sms()
    # Previous calendar month: first-of-this-month minus one day always lands in it,
    # regardless of month length (avoids the "-28 days" bug late in long months).
    now = datetime.now(timezone.utc)
    first_of_this_month = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    prev_month = (first_of_this_month - timedelta(days=1)).strftime("%Y-%m")
    tenants_processed = 0
    invoices_created = 0
    errors = 0
    secret = (os.getenv("STRIPE_SECRET_KEY") or "").strip()
    if not secret:
        result = {
            "ok": True,
            "tenants_processed": 0,
            "invoices_created": 0,
            "errors": 1,
        }
        if run_id:
            database.db_cron_run_finish(run_id, "success", result)
        return result
    stripe.api_key = secret
    tenants = database.db_tenant_list_all()
    for t in tenants:
        if t.get("subscription_status") != "active" or not t.get("stripe_customer_id"):
            continue
        cid = t.get("client_id")
        if not cid:
            continue
        if database.db_overage_processed_exists(cid, prev_month):
            continue
        limits = get_plan_limits(t) if get_plan_limits else {}
        voice_cap = limits.get("minutes_cap", 999999)
        sms_cap = limits.get("sms_cap", 999999)
        usage = database.db_usage_get(cid, prev_month)
        voice_minutes = usage.get("voice_minutes") or 0
        sms_count = usage.get("sms_count") or 0
        voice_over = max(0, voice_minutes - voice_cap)
        sms_over = max(0, sms_count - sms_cap)
        # round(), not int(): float truncation (e.g. 0.029*100) would silently undercharge a cent.
        voice_cents = round(voice_over * price_per_min * 100)
        sms_cents = round(sms_over * price_per_sms * 100)
        if voice_cents <= 0 and sms_cents <= 0:
            database.db_overage_processed_insert(cid, prev_month)
            tenants_processed += 1
            continue
        try:
            # Bill voice and SMS overage as separate line items. Both are created before
            # the processed marker so a re-run cannot double-bill either channel.
            if voice_cents > 0:
                stripe.InvoiceItem.create(
                    customer=t["stripe_customer_id"],
                    amount=voice_cents,
                    currency="usd",
                    description=f"Extra minutes ({prev_month})",
                )
                invoices_created += 1
            if sms_cents > 0:
                stripe.InvoiceItem.create(
                    customer=t["stripe_customer_id"],
                    amount=sms_cents,
                    currency="usd",
                    description=f"Extra texts ({prev_month})",
                )
                invoices_created += 1
            database.db_overage_processed_insert(cid, prev_month)
        except Exception as e:
            logger.error(
                "overage_invoice_failed",
                extra={"client_id": cid, "month": prev_month, "error": str(e)},
            )
            errors += 1
        tenants_processed += 1
    result = {
        "ok": True,
        "tenants_processed": tenants_processed,
        "invoices_created": invoices_created,
        "errors": errors,
    }
    if run_id:
        database.db_cron_run_finish(run_id, "success", result)
    return result


@router.post("/api/cron/retention-purge")
def cron_retention_purge(request: Request):
    """Purge expired rows (default 3 years) while honoring active legal holds."""
    if not _verify_cron_secret(request):
        raise HTTPException(status_code=401, detail="Unauthorized")
    run_id = database.db_cron_run_start("retention-purge") if runtime.USE_DB else None
    if not runtime.USE_DB:
        result = {
            "ok": True,
            "deleted": {"audit_events": 0, "call_log": 0, "sms_sessions": 0},
            "days": 0,
        }
        return result
    days = max(1, int((os.getenv("RETENTION_DAYS") or str(365 * 3)).strip()))
    deleted = database.db_retention_purge(days=days)
    result = {"ok": True, "deleted": deleted, "days": days}
    database.db_cron_run_finish(run_id, "success", result)
    return result


@router.post("/api/cron/export-snapshot")
def cron_export_snapshot(request: Request):
    """Daily tenant-scoped JSON snapshot export with SHA256 manifest."""
    if not _verify_cron_secret(request):
        raise HTTPException(status_code=401, detail="Unauthorized")
    run_id = database.db_cron_run_start("export-snapshot") if runtime.USE_DB else None
    if not runtime.USE_DB:
        return {"ok": True, "exported": False}
    export_root = (
        os.getenv("OFFSITE_EXPORT_DIR") or str(config_service.PROJECT_ROOT / "exports")
    ).strip()
    include_audit = (
        os.getenv("EXPORT_INCLUDE_AUDIT_EVENTS") or "1"
    ).strip().lower() in ("1", "true", "yes")
    result = database.db_export_tenant_snapshot(export_root, include_audit=include_audit)
    if not result:
        out = {"ok": False, "exported": False}
        database.db_cron_run_finish(run_id, "error", out)
        return out
    out = {"ok": True, "exported": True, **result}
    database.db_cron_run_finish(run_id, "success", out)
    return out


def _stale_daily_crons() -> List[str]:
    """Daily cron jobs with no successful run in the last 36 hours."""
    last_success = database.db_cron_runs_last_success()
    cutoff = datetime.now(timezone.utc) - timedelta(hours=36)
    stale: List[str] = []
    for job in database.DAILY_CRON_JOBS:
        ts = last_success.get(job)
        if not ts:
            stale.append(job)
            continue
        try:
            finished = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            if finished.tzinfo is None:
                finished = finished.replace(tzinfo=timezone.utc)
            if finished < cutoff:
                stale.append(job)
        except Exception:
            stale.append(job)
    return stale


@router.post("/api/cron/health-digest")
def cron_health_digest(request: Request):
    """Daily heartbeat: email the operator a status digest every morning, and escalate by
    SMS if anything is actually wrong (stale crons, unresolved incidents, DB down)."""
    if not _verify_cron_secret(request):
        raise HTTPException(status_code=401, detail="Unauthorized")
    run_id = database.db_cron_run_start("health-digest") if runtime.USE_DB else None
    if not runtime.USE_DB:
        return {"ok": True, "sent": False}
    try:
        db_ok = database.db_ping()
        stale = _stale_daily_crons() if db_ok else []
        failed_open = database.db_failed_events_unresolved_count() if db_ok else 0
        tenants = database.db_tenant_list_all() if db_ok else []
        past_due = [t for t in tenants if (t.get("subscription_status") == "past_due")]
        paused = [t for t in tenants if t.get("account_paused")]
        unpaid = database.db_referral_commissions_list_all(include_paid=False) if db_ok else []
        unpaid_total = sum(c["amount_cents"] for c in unpaid)

        problems = []
        if not db_ok:
            problems.append("Database is unreachable")
        if stale:
            problems.append(f"Stale cron jobs: {', '.join(stale)}")
        if failed_open:
            problems.append(f"{failed_open} unresolved incident(s) in the failed-events log")

        lines = [
            f"Database: {'OK' if db_ok else 'UNREACHABLE'}",
            f"Cron jobs: {'all healthy' if not stale else 'STALE: ' + ', '.join(stale)}",
            f"Unresolved incidents: {failed_open}",
            f"Active tenants: {len(tenants)}",
            f"Past-due tenants: {len(past_due)}",
            f"Paused tenants: {len(paused)}",
            f"Unpaid referral payouts: {len(unpaid)} (${unpaid_total / 100:.2f})",
        ]
        body = "\n".join(lines)
        healthy = not problems
        subject = "Daily health digest — all systems healthy" if healthy else f"Daily health digest — {len(problems)} issue(s)"

        # Always email the heartbeat; escalate by SMS only when something is wrong.
        try:
            import email_notify

            html = f"<p><strong>{subject}</strong></p><pre>{body}</pre>"
            if problems:
                html += "<p><strong>Needs attention:</strong></p><ul>" + "".join(f"<li>{p}</li>" for p in problems) + "</ul>"
            email_notify.send_operator_alert(f"[Call Surge] {subject}", html, body + ("\n\nNeeds attention:\n- " + "\n- ".join(problems) if problems else ""))
        except Exception as e:
            logger.warning("health_digest_email_failed: %s", e)
        if problems:
            try:
                import alerts

                alerts.report_critical(
                    "health_digest_problems",
                    f"{len(problems)} system issue(s) need attention",
                    "; ".join(problems),
                )
            except Exception:
                pass

        out = {"ok": True, "sent": True, "healthy": healthy, "problems": problems}
        database.db_cron_run_finish(run_id, "success", out)
        return out
    except Exception as e:
        logger.exception("health_digest_failed: %s", e)
        out = {"ok": False, "error": str(e)[:200]}
        database.db_cron_run_finish(run_id, "error", out)
        return out
