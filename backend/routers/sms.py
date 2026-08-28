"""Inbound SMS webhook — the AI mobile-receptionist handler (relocated verbatim).

The handle_incoming_sms body and its SMS-only helpers, moved from main with zero logic
change. Cross-module helpers are resolved by module (booking_service/deps/database/
sms_service/config_service/caller_memory/conversational_sms/webhook_responses) so
monkeypatches target the owning module; logging/format/hash utils are imported by name.
"""

from __future__ import annotations

import json
import logging
import os
import re

import openai
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, HTTPException, Request, Response

import booking_service
import caller_memory
import config_service
import database
import deps
import runtime
import sms_service
from observability import (
    _stable_sha256,
    auth_warning,
    email_hint_for_log,
    name_initial_for_log,
    sms_debug,
    system_info,
    sms_info,
    sms_trace,
)
from security.redaction import mask_phone_e164
from prompts.receptionist import appointment_focus_guidance

try:
    from plans import get_plan_limits
except ImportError:  # pragma: no cover
    get_plan_limits = None  # type: ignore

try:
    from twilio.request_validator import RequestValidator as _RequestValidator  # noqa: F401
    TWILIO_AVAILABLE = True
except ImportError:  # pragma: no cover
    TWILIO_AVAILABLE = False

logger = logging.getLogger("nuvatra")

router = APIRouter()


def _is_sms_confirmation(body: str) -> bool:
    """True if the message looks like the customer confirming their appointment (yes, looks good, etc.)."""
    if not body or len(body) > 80:
        return False
    b = body.lower().strip()
    # Whole-message exact matches
    exact = (
        "yes",
        "yep",
        "yeah",
        "confirm",
        "confirmed",
        "correct",
        "perfect",
        "great",
        "ok",
        "okay",
        "approved",
    )
    if b in exact:
        return True
    # Multi-word phrases (substring OK; still length-capped above)
    phrases = (
        "looks good",
        "look good",
        "that's right",
        "thats right",
        "all good",
        "sounds good",
        "sounds great",
        "that works for me",
        "that works",
    )
    for p in phrases:
        if p in b:
            return True
    # Single-word confirms: whole tokens only (avoids "yes" in "yesterday", "ok" in "token", "good" in "goods")
    tokens = set(re.findall(r"[a-z0-9']+", b))
    word_ok = {
        "yes",
        "yep",
        "yeah",
        "ok",
        "confirm",
        "confirmed",
        "correct",
        "perfect",
        "great",
        "approved",
        "okay",
    }
    return bool(tokens & word_ok)


def _sms_compliance_keyword(body: str) -> Optional[str]:
    """Parse CTIA-style keywords from inbound SMS body. Returns 'stop' | 'start' | 'help' or None."""
    words = (body or "").strip().upper().split()
    if not words:
        return None
    first = words[0].rstrip(".!")
    if first in ("STOP", "END", "CANCEL", "UNSUBSCRIBE", "QUIT", "STOPALL"):
        return "stop"
    if first in ("START", "UNSTOP"):
        return "start"
    if first in ("HELP", "INFO"):
        return "help"
    return None


def _staff_pending_review_sms_enabled() -> bool:
    return (os.getenv("STAFF_PENDING_REVIEW_SMS") or "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


def _notify_staff_pending_review(
    apt: dict, tenant: dict, twilio_from_number: str
) -> None:
    """Optional cost-controlled SMS to each staff phone when a customer submits the booking for shop approval."""
    if not _staff_pending_review_sms_enabled():
        return
    apt_id = apt.get("id")
    if not apt_id:
        return
    cfg = config_service.load_client_config(tenant["client_id"]) or {}
    staff_list = cfg.get("staff") or []
    from staff_transfers import staff_members_for_pending_review_sms

    targets = staff_members_for_pending_review_sms(staff_list, apt)
    sms_info(
        "staff_pending_review_notify_start",
        apt_id=apt_id,
        client_id=tenant["client_id"],
        staff_sms_targets=len(targets),
        staff_id=(apt.get("staff_id") or "") or None,
    )
    nm = (apt.get("name") or "").strip() or "Customer"
    ds = (apt.get("date") or "").strip()
    tm = booking_service._hhmm_to_ampm((apt.get("time") or "").strip())
    msg = (
        f"New booking request #{apt_id}: {nm}, {ds} at {tm}. "
        f"Reply YES {apt_id} to approve or NO {apt_id} plus a short reason to decline."
    )
    for s in targets:
        phone = (s.get("phone") or "").strip()
        if not phone:
            continue
        try:
            sms_service.send_sms(phone, msg[:1580], from_override=twilio_from_number)
        except Exception as e:
            logger.warning(
                "[SMS] staff_pending_review_notify_failed apt_id=%s err=%s", apt_id, e
            )


def _maybe_handle_staff_sms_approval(
    from_number: str, body: str, tenant: dict, to_number: str
) -> bool:
    """
    If From matches a staff member's phone, parse APPROVE/YES or DECLINE/NO <apt_id> [reason].
    Returns True if this webhook turn was consumed as a staff command.
    """
    norm_from = sms_service._phone_to_e164(from_number)
    if not norm_from:
        return False
    cfg = config_service.load_client_config(tenant["client_id"]) or {}
    staff_list = cfg.get("staff") or []
    is_staff = False
    for s in staff_list:
        sp = sms_service._phone_to_e164(s.get("phone") or "")
        if sp and sp == norm_from:
            is_staff = True
            break
    if not is_staff:
        return False
    raw = (body or "").strip()
    tokens = raw.split()
    sms_trace(
        "inbound_staff_phone_matched",
        client_id=tenant["client_id"],
        body_len=len(raw),
        token_count=len(tokens),
    )
    if len(tokens) < 2:
        sms_debug(
            "staff_command_incomplete", from_number=from_number, body_len=len(raw)
        )
        sms_trace(
            "inbound_staff_command_incomplete",
            client_id=tenant["client_id"],
            token_count=len(tokens),
        )
        return False
    verb = tokens[0].upper()
    try:
        apt_id = int(tokens[1])
    except ValueError:
        sms_info(
            "staff_command_invalid_id_token",
            from_number=from_number,
            token=str(tokens[1])[:20],
        )
        return False
    apt = database.db_appointments_get_by_id(apt_id) if runtime.USE_DB else None
    if not apt:
        sms_info(
            "staff_command_unknown_appointment",
            apt_id=apt_id,
            client_id=tenant["client_id"],
            from_number=from_number,
        )
        sms_service.send_sms(
            from_number,
            "We could not find that booking reference.",
            from_override=to_number,
            force=True,
        )
        return True
    if str(apt.get("status") or "") != "pending_review":
        sms_info(
            "staff_command_wrong_status",
            apt_id=apt_id,
            status=apt.get("status"),
            client_id=tenant["client_id"],
            from_number=from_number,
        )
        sms_service.send_sms(
            from_number,
            "That booking is not awaiting approval.",
            from_override=to_number,
            force=True,
        )
        return True
    business_name = config_service.get_business_info().get("name", "your shop")
    if verb in ("YES", "APPROVE", "OK", "ACCEPT"):
        if runtime.USE_DB:
            database.db_appointments_update(apt_id, status="accepted")
        deps.audit_log(
            "staff_sms",
            "appointment_accepted",
            resource_type="appointment",
            resource_id=str(apt_id),
            client_id=tenant["client_id"],
            details={"via": "sms"},
        )
        msg = (
            f"Your appointment at {business_name} is confirmed for {apt.get('date')} at "
            f"{booking_service._hhmm_to_ampm(apt.get('time') or '')}. Reply if you need to change."
        )
        sms_service.send_sms(apt.get("phone") or "", msg, from_override=to_number)
        sms_service.send_sms(
            from_number,
            f"Booking {apt_id} approved. Customer notified.",
            from_override=to_number,
            force=True,
        )
        sms_info(
            "staff_sms_approved",
            apt_id=apt_id,
            client_id=tenant["client_id"],
            from_number=from_number,
        )
        return True
    if verb in ("NO", "DECLINE", "REJECT"):
        reason = " ".join(tokens[2:]).strip() or "We could not accommodate that time."
        if runtime.USE_DB:
            database.db_appointments_update(
                apt_id, status="rejected", owner_decline_reason=reason[:2000]
            )
        booking_service.release_slot(apt_id)
        deps.audit_log(
            "staff_sms",
            "appointment_rejected",
            resource_type="appointment",
            resource_id=str(apt_id),
            client_id=tenant["client_id"],
            details={"via": "sms"},
        )
        polished = booking_service.polish_owner_decline_sms(reason, business_name, apt)
        sms_service.send_sms(apt.get("phone") or "", polished, from_override=to_number)
        sms_service.send_sms(
            from_number,
            "Decline sent to the customer.",
            from_override=to_number,
            force=True,
        )
        sms_info(
            "staff_sms_declined",
            apt_id=apt_id,
            client_id=tenant["client_id"],
            from_number=from_number,
        )
        return True
    return False


@router.post("/api/sms/incoming")
async def handle_incoming_sms(request: Request):
    """Twilio webhook for incoming SMS. AI-powered mobile receptionist replies like a real person."""
    rid = getattr(request.state, "request_id", None)
    if not TWILIO_AVAILABLE:
        sms_debug("inbound_skipped", reason="twilio_not_available")
        sms_trace("inbound_early_exit", reason="twilio_not_available", request_id=rid)
        return Response(
            content='<?xml version="1.0" encoding="UTF-8"?><Response></Response>',
            media_type="application/xml",
        )
    if not runtime.USE_DB:
        sms_debug("inbound_skipped", reason="database_not_enabled")
        sms_trace("inbound_early_exit", reason="database_not_enabled", request_id=rid)
        return Response(
            content='<?xml version="1.0" encoding="UTF-8"?><Response></Response>',
            media_type="application/xml",
        )
    try:
        form_data = await request.form()
        form_dict = dict(form_data)
        sig_mode = "skipped"
        if (os.getenv("TWILIO_AUTH_TOKEN") or "").strip():
            sig_mode = "enforced"
        sms_trace(
            "inbound_form_parsed",
            request_id=rid,
            signature_mode=sig_mode,
            from_number=str(form_dict.get("From") or ""),
            to_number=str(form_dict.get("To") or ""),
            body_len=len(str(form_dict.get("Body") or "")),
            message_sid=str(
                form_dict.get("MessageSid") or form_dict.get("SmsMessageSid") or ""
            ),
            num_media=str(form_dict.get("NumMedia") or ""),
        )
        if not deps._validate_twilio_webhook(request, form_dict):
            auth_warning(
                "sms_webhook_invalid_signature",
                path=request.url.path,
                request_id=rid,
            )
            sms_trace(
                "inbound_signature_invalid", request_id=rid, signature_mode=sig_mode
            )
            return Response(content="", status_code=403, media_type="application/xml")
        sms_trace("inbound_signature_ok", request_id=rid, signature_mode=sig_mode)
        from_number = form_data.get("From", "").strip()
        to_number = form_data.get("To", "").strip()
        body = (form_data.get("Body", "") or "").strip()
        msg_sid = (
            form_data.get("MessageSid") or form_data.get("SmsMessageSid") or ""
        ).strip()
        deps.audit_log(
            "sms",
            "inbound_received",
            resource_type="message",
            resource_id=msg_sid or None,
            details={
                "from_masked": mask_phone_e164(from_number),
                "to_masked": mask_phone_e164(to_number),
                "body_len": len(body),
                "body_sha256": _stable_sha256(body),
            },
            request=request,
        )
        if not from_number or not to_number or not body:
            sms_info(
                "inbound_skipped", reason="missing_fields", message_sid=msg_sid or None
            )
            sms_trace(
                "inbound_early_exit",
                reason="missing_fields",
                request_id=rid,
                has_from=bool(from_number),
                has_to=bool(to_number),
                has_body=bool(body),
                message_sid=msg_sid or None,
            )
            return Response(
                content='<?xml version="1.0" encoding="UTF-8"?><Response></Response>',
                media_type="application/xml",
            )
        tenant = database.db_tenant_get_by_phone(to_number)
        if not tenant:
            sms_info(
                "inbound_skipped",
                reason="unknown_to_number",
                to_number=to_number,
                message_sid=msg_sid or None,
            )
            sms_trace(
                "inbound_tenant_not_found",
                request_id=rid,
                to_number=to_number,
                message_sid=msg_sid or None,
                hint="ensure_twilio_to_matches_tenant_twilio_phone_number",
            )
            return Response(
                content='<?xml version="1.0" encoding="UTF-8"?><Response></Response>',
                media_type="application/xml",
            )
        database.set_request_client_id(tenant["client_id"])
        sms_info(
            "inbound_received",
            client_id=tenant["client_id"],
            from_number=from_number,
            to_number=to_number,
            body_len=len(body),
            message_sid=msg_sid or None,
            request_id=rid,
        )
        sms_trace(
            "inbound_tenant_resolved",
            request_id=rid,
            client_id=tenant["client_id"],
            tenant_name=(tenant.get("name") or "")[:80],
            message_sid=msg_sid or None,
        )
        kw = _sms_compliance_keyword(body)
        if kw:
            sms_trace(
                "inbound_compliance_keyword",
                request_id=rid,
                keyword=kw,
                client_id=tenant["client_id"],
                message_sid=msg_sid or None,
            )
            cid = tenant["client_id"]
            if kw == "stop":
                database.db_sms_opt_out_set(from_number, cid)
                sms_service.send_sms(
                    from_number,
                    "You've opted out and won't get more texts from this number. Reply START to get messages again. Msg and data rates may apply.",
                    from_override=to_number,
                    force=True,
                )
            elif kw == "start":
                database.db_sms_opt_out_clear(from_number, cid)
                database.db_sms_consent_record(
                    from_number,
                    cid,
                    "sms_start",
                    detail={"message_sid": msg_sid or None},
                )
                sms_service.send_sms(
                    from_number,
                    "You're subscribed again to texts from this number. Msg and data rates may apply. Reply STOP to opt out.",
                    from_override=to_number,
                    force=True,
                )
            elif kw == "help":
                sms_service.send_sms(
                    from_number,
                    "Call Surge: texts for appointments and replies from this business. Msg and data rates may apply. Reply STOP to opt out. Help: info@nuvatrahq.com",
                    from_override=to_number,
                    force=True,
                )
            sms_trace(
                "inbound_compliance_handled", request_id=rid, keyword=kw, client_id=cid
            )
            return Response(
                content='<?xml version="1.0" encoding="UTF-8"?><Response></Response>',
                media_type="application/xml",
            )
        if runtime.USE_DB and database.db_sms_opt_out_is_blocked(from_number, tenant["client_id"]):
            sms_info(
                "inbound_blocked_opt_out",
                client_id=tenant["client_id"],
                from_number=from_number,
            )
            sms_trace(
                "inbound_early_exit",
                reason="recipient_opted_out",
                request_id=rid,
                client_id=tenant["client_id"],
            )
            return Response(
                content='<?xml version="1.0" encoding="UTF-8"?><Response></Response>',
                media_type="application/xml",
            )
        if runtime.USE_DB and from_number and tenant.get("client_id"):
            database.db_sms_consent_record(
                from_number,
                tenant["client_id"],
                "inbound_sms",
                detail={"message_sid": msg_sid or None},
            )
        staff_handled = _maybe_handle_staff_sms_approval(
            from_number, body, tenant, to_number
        )
        if staff_handled:
            sms_trace(
                "inbound_staff_command_handled",
                request_id=rid,
                client_id=tenant["client_id"],
            )
            return Response(
                content='<?xml version="1.0" encoding="UTF-8"?><Response></Response>',
                media_type="application/xml",
            )
        from webhook_responses import (
            SMS_SUBSCRIPTION_LAPSED_MESSAGE,
            check_webhook_tenant_access,
        )

        if not check_webhook_tenant_access(tenant, channel="sms", request_id=rid):
            sms_service.send_sms(
                from_number,
                SMS_SUBSCRIPTION_LAPSED_MESSAGE,
                from_override=to_number,
                force=True,
            )
            return Response(
                content='<?xml version="1.0" encoding="UTF-8"?><Response></Response>',
                media_type="application/xml",
            )
        # Pre-SMS usage check: alert-only, never cut off. SMS is metered independently
        # of voice minutes (its own plan cap); overage is billed monthly.
        if get_plan_limits:
            limits = get_plan_limits(tenant)
            month = datetime.now(timezone.utc).strftime("%Y-%m")
            usage = database.db_usage_get(tenant["client_id"], month)
            voice_minutes = usage.get("voice_minutes") or 0
            sms_count = usage.get("sms_count") or 0
            sms_cap = limits.get("sms_cap", 999999)
            sms_trace(
                "inbound_usage_snapshot",
                request_id=rid,
                client_id=tenant["client_id"],
                month=month,
                voice_minutes=voice_minutes,
                sms_count=sms_count,
                sms_cap=sms_cap,
                at_or_over_sms_cap=sms_count >= sms_cap,
            )
            if sms_count >= sms_cap:
                deps.audit_log(
                    "usage",
                    "overage_exceeded",
                    client_id=tenant["client_id"],
                    details={"month": month, "channel": "sms", "sms_count": sms_count, "cap": sms_cap},
                    request=request,
                )
                deps.maybe_alert_usage_cap(
                    client_id=tenant["client_id"],
                    month=month,
                    channel="sms",
                    voice_minutes=voice_minutes,
                    voice_cap=limits.get("minutes_cap", 999999),
                    sms_count=sms_count,
                    sms_cap=sms_cap,
                    request=request,
                )
        apt = None
        resolve_via = "none"
        if runtime.USE_DB:
            apt, resolve_via = database.db_appointments_resolve_for_sms(
                from_number, tenant["client_id"]
            )
        sms_info(
            "inbound_appointment_resolve",
            client_id=tenant["client_id"],
            resolve_via=resolve_via,
            apt_id=apt.get("id") if apt else None,
            apt_status=(apt.get("status") or "") if apt else None,
            body_len=len(body),
        )
        if apt:
            sms_debug(
                "inbound_context",
                apt_id=apt.get("id"),
                apt_status=apt.get("status"),
                body_len=len(body),
                from_number=from_number,
            )
            sms_trace(
                "inbound_appointment_context",
                request_id=rid,
                apt_id=apt.get("id"),
                apt_status=apt.get("status"),
                body_len=len(body),
            )
        else:
            sms_info(
                "inbound_no_pending_appointment",
                client_id=tenant["client_id"],
                body_len=len(body),
                looks_like_confirm=_is_sms_confirmation(body),
            )
            sms_debug(
                "inbound_no_pending_appointment",
                body_len=len(body),
                from_number=from_number,
            )
            sms_trace(
                "inbound_no_appointment_for_number", request_id=rid, body_len=len(body)
            )
        session = (
            database.db_sms_session_get(from_number, tenant["client_id"]) if runtime.USE_DB else None
        )
        messages = (session["messages"] if session else []) if session else []
        prior_turns = len(messages)
        # Persist name/email from this text and recent inbound SMS (e.g. "my name is Raj" then "Yes")
        if (
            apt
            and apt.get("status") in ("pending_customer", "pending_review", "accepted")
            and runtime.USE_DB
            and apt.get("id")
        ):
            from sms_appointment_updates import (
                apply_sms_appointment_detail_updates_from_bodies,
            )

            prior_user_bodies = [
                (m.get("content") or "")
                for m in messages
                if (m.get("role") or "").strip() == "user"
            ][-8:]
            # The shop's real service menu, so "make it a long cut" matches without the caller
            # having to say the rigid phrase "change service to ...". Use get_business_info (the
            # NORMALIZED staff + services) so the service ids in the menu line up with the ids in
            # each stylist's service_ids — the raw config didn't align, which let an SMS switch a
            # stylist onto a service they don't offer.
            _svc_cfg = config_service.get_business_info() or {}
            _svc_entries = config_service._normalize_service_entries(_svc_cfg.get("services") or [])
            known_services = [
                (s.get("name") or "").strip()
                for s in _svc_entries
                if (s.get("name") or "").strip()
            ]
            service_id_by_name = {
                (s.get("name") or "").strip().lower(): (s.get("id") or "").strip()
                for s in _svc_entries
                if (s.get("name") or "").strip()
            }
            # Staff roster (with service_ids / working days) so an SMS "switch me to Andrew"
            # resolves to a stylist and is validated against the service + that day.
            known_staff = [s for s in (_svc_cfg.get("staff") or []) if (s.get("name") or "").strip()]
            sms_trace(
                "sms_detail_updates_session_context",
                request_id=rid,
                apt_id=apt.get("id"),
                prior_user_turns=len(prior_user_bodies),
                current_body_len=len(body or ""),
            )
            stylist_rejection: dict = {}
            apt, detail_fields_updated = (
                apply_sms_appointment_detail_updates_from_bodies(
                    prior_user_bodies + [body],
                    apt,
                    client_id=tenant["client_id"],
                    from_number=from_number,
                    db_appointments_update=database.db_appointments_update,
                    db_appointments_get_by_id=database.db_appointments_get_by_id,
                    update_caller_memory=caller_memory.update_caller_memory,
                    db_appointments_update_active_name_by_phone=(
                        database.db_appointments_update_active_name_by_phone if runtime.USE_DB else None
                    ),
                    system_info=system_info,
                    logger=logger,
                    known_services=known_services,
                    known_staff=known_staff,
                    service_id_by_name=service_id_by_name,
                    business_info=_svc_cfg,
                    rejection_out=stylist_rejection,
                )
            )
            if detail_fields_updated and apt and any(
                f in detail_fields_updated for f in ("time", "date")
            ):
                booking_service._reconcile_sms_appointment_slot_after_detail_change(apt)
            # A requested change was refused (stylist can't do the service / is off that day, or
            # the new date is a closed day). Tell the customer the truth directly instead of
            # letting the AI confirm a change that didn't happen. Mirrors the applied-change
            # summary short-circuit below.
            if stylist_rejection.get("message") and not _is_sms_confirmation(body):
                reject_msg = stylist_rejection["message"]
                send_ok = sms_service.send_sms(from_number, reject_msg, from_override=to_number)
                sms_info(
                    "sms_change_rejected_reply",
                    request_id=rid,
                    apt_id=apt.get("id") if apt else None,
                    client_id=tenant["client_id"],
                    reason=stylist_rejection.get("reason"),
                    send_sms_ok=send_ok,
                )
                messages.append({"role": "user", "content": body})
                messages.append({"role": "assistant", "content": reject_msg})
                try:
                    database.db_sms_session_upsert(
                        from_number, tenant["client_id"], messages, apt.get("id") if apt else None
                    )
                except Exception:
                    pass
                return Response(
                    content='<?xml version="1.0" encoding="UTF-8"?><Response></Response>',
                    media_type="application/xml",
                )
        else:
            detail_fields_updated = []
        messages.append({"role": "user", "content": body})
        sms_trace(
            "inbound_session_loaded",
            request_id=rid,
            prior_turns=prior_turns,
            session_existed=session is not None,
        )
        # After detail changes, text full summary so customer can verify before YES/CONFIRM
        if (
            apt
            and detail_fields_updated
            and not _is_sms_confirmation(body)
            and (apt.get("status") or "")
            in ("pending_customer", "pending_review", "accepted")
        ):
            summary_sms = booking_service._format_appointment_details_confirmation_sms(apt)
            send_ok = sms_service.send_sms(from_number, summary_sms, from_override=to_number)
            sms_info(
                "sms_detail_summary_sent",
                request_id=rid,
                apt_id=apt.get("id"),
                client_id=tenant["client_id"],
                fields=detail_fields_updated,
                send_sms_ok=send_ok,
            )
            messages.append({"role": "assistant", "content": summary_sms})
            try:
                database.db_sms_session_upsert(
                    from_number, tenant["client_id"], messages, apt["id"]
                )
            except Exception as upsert_err:
                sms_info(
                    "inbound_session_persist_failed",
                    request_id=rid,
                    client_id=tenant["client_id"],
                    error_type=type(upsert_err).__name__,
                    phase="detail_summary_reply",
                )
                logger.warning(
                    "database.db_sms_session_upsert failed (detail summary): %s",
                    upsert_err,
                    exc_info=True,
                )
            return Response(
                content='<?xml version="1.0" encoding="UTF-8"?><Response></Response>',
                media_type="application/xml",
            )
        # If they have an appointment awaiting their confirmation (pending_customer) and they reply yes/looks good, promote to pending_review so store can Accept/Decline
        if (
            apt
            and apt.get("status") == "pending_customer"
            and _is_sms_confirmation(body)
        ):
            sms_trace(
                "inbound_customer_confirm_branch",
                request_id=rid,
                apt_id=apt.get("id"),
                client_id=tenant["client_id"],
            )
            apt_after = apt
            if runtime.USE_DB and apt.get("id"):
                aid = int(apt["id"])
                apt_full = database.db_appointments_get_by_id(aid) or apt
                date = (apt_full.get("date") or "").strip()
                time_raw = (apt_full.get("time") or "").strip()
                time_hhmm = booking_service._normalize_time_to_hhmm(time_raw) or time_raw

                sms_info(
                    "sms_customer_confirm_snapshot",
                    request_id=rid,
                    apt_id=aid,
                    client_id=tenant["client_id"],
                    name_initial=name_initial_for_log(apt_full.get("name")),
                    email_hint=email_hint_for_log(apt_full.get("email")),
                    date=date,
                    time_raw=time_raw,
                    time_normalized=time_hhmm,
                    time_was_normalized=bool(
                        time_raw and time_hhmm and time_raw != time_hhmm
                    ),
                )
                staff_for = (apt_full.get("staff_id") or "").strip() or None
                confirm_duration = booking_service._appointment_duration_minutes(apt_full)
                # Atomic claim: is_slot_available catches duration overlaps; reserve_slot
                # then claims the exact slot and returns False if a concurrent booking won
                # the race between the check and the claim. Short-circuit so a lost race
                # routes to the same "just taken" reply (and never leaves an orphan hold).
                if not booking_service.is_slot_available(
                    date, time_hhmm, confirm_duration, staff_for
                ) or not booking_service.reserve_slot(
                    date, time_hhmm, aid, confirm_duration, staff_for
                ):
                    sorry = (
                        "Sorry — that time was just taken and we can't hold it anymore. "
                        "Text us another time that works or call the shop. Msg & data rates may apply. Reply STOP to opt out."
                    )
                    send_ok = sms_service.send_sms(from_number, sorry, from_override=to_number)
                    sms_trace(
                        "inbound_customer_confirm_slot_unavailable",
                        request_id=rid,
                        apt_id=aid,
                        send_sms_ok=send_ok,
                    )
                    messages.append({"role": "assistant", "content": sorry})
                    try:
                        database.db_sms_session_upsert(
                            from_number, tenant["client_id"], messages, apt["id"]
                        )
                    except Exception as upsert_err:
                        sms_info(
                            "inbound_session_persist_failed",
                            request_id=rid,
                            client_id=tenant["client_id"],
                            error_type=type(upsert_err).__name__,
                            phase="pending_customer_confirm_slot_taken",
                        )
                        logger.warning(
                            "database.db_sms_session_upsert failed (slot taken path): %s",
                            upsert_err,
                            exc_info=True,
                        )
                    return Response(
                        content='<?xml version="1.0" encoding="UTF-8"?><Response></Response>',
                        media_type="application/xml",
                    )
                # Slot already claimed atomically in the guard above.
                database.db_appointments_update(
                    aid, status="pending_review", client_id=tenant["client_id"]
                )
                apt_after = (
                    database.db_appointments_get_by_id(aid, client_id=tenant["client_id"])
                    or apt_full
                )
            try:
                em_conf = (apt_after.get("email") or "").strip()
                mem_patch: dict = {"last_pending_review_apt_id": apt.get("id")}
                if em_conf:
                    mem_patch["email_on_file"] = em_conf
                caller_memory.update_caller_memory(
                    from_number,
                    name=(apt_after.get("name") or "").strip() or None,
                    last_reason="details confirmed; awaiting store approval",
                    increment_count=False,
                    data_patch=mem_patch,
                )
            except Exception:
                pass
            _notify_staff_pending_review(apt_after, tenant, to_number)

            sms_info(
                "customer_confirmed_pending_to_review",
                apt_id=apt_after.get("id"),
                client_id=tenant["client_id"],
                from_number=from_number,
                name_initial=name_initial_for_log(apt_after.get("name")),
                email_hint=email_hint_for_log(apt_after.get("email")),
                time_normalized=booking_service._normalize_time_to_hhmm(apt_after.get("time") or "")
                or (apt_after.get("time") or ""),
                date=apt_after.get("date") or "",
            )
            reply = (
                "Thanks! We've sent this to the store. We'll text you when they confirm. "
                "Msg & data rates may apply. Reply STOP to opt out."
            )
            send_ok = sms_service.send_sms(from_number, reply, from_override=to_number)
            sms_trace(
                "inbound_customer_confirm_reply_sent",
                request_id=rid,
                send_sms_ok=send_ok,
                reply_len=len(reply),
            )
            messages.append({"role": "assistant", "content": reply})
            try:
                database.db_sms_session_upsert(
                    from_number, tenant["client_id"], messages, apt["id"]
                )
            except Exception as upsert_err:
                sms_info(
                    "inbound_session_persist_failed",
                    request_id=rid,
                    client_id=tenant["client_id"],
                    error_type=type(upsert_err).__name__,
                    phase="pending_customer_confirm",
                )
                logger.warning(
                    "database.db_sms_session_upsert failed (pending_customer path): %s",
                    upsert_err,
                    exc_info=True,
                )
            return Response(
                content='<?xml version="1.0" encoding="UTF-8"?><Response></Response>',
                media_type="application/xml",
            )
        from conversational_sms import (
            conversational_sms_cap_fallback_body,
            reserve_conversational_sms_session,
        )

        conv_reserve = reserve_conversational_sms_session(tenant, from_number)
        if not conv_reserve.allowed:
            fallback_body = conversational_sms_cap_fallback_body(tenant)
            sms_service.send_sms(from_number, fallback_body, from_override=to_number)
            sms_trace(
                "inbound_conversational_session_cap",
                request_id=rid,
                client_id=tenant["client_id"],
                session_cap=conv_reserve.session_cap,
                session_count=conv_reserve.session_count,
                billing_period_key=conv_reserve.billing_period_key,
            )
            return Response(
                content='<?xml version="1.0" encoding="UTF-8"?><Response></Response>',
                media_type="application/xml",
            )
        sms_context_apts: list[dict] = []
        if runtime.USE_DB:
            try:
                sms_context_apts = database.db_appointments_get_active_for_sms_context(
                    from_number, client_id=tenant["client_id"], limit=5
                )
            except Exception as context_err:
                logger.warning(
                    "database.db_appointments_get_active_for_sms_context failed: %s",
                    context_err,
                    exc_info=True,
                )
        apt_info = ""
        if sms_context_apts:
            lines = []
            for row in sms_context_apts[:5]:
                lines.append(
                    f"- {row.get('date','')} at {booking_service._hhmm_to_ampm(row.get('time','') or '')} "
                    f"(status: {row.get('status','')}), service: {row.get('reason','')}, "
                    f"name on file: {row.get('name','')}"
                )
            apt_info = (
                f"The customer has {len(sms_context_apts)} active appointment(s) in the system:\n"
                + "\n".join(lines)
            )
        elif apt:
            stylist = booking_service._staff_display_name_for_appointment(apt)
            stylist_txt = f", stylist: {stylist}" if stylist else ""
            apt_info = (
                f"The customer has one active appointment: {apt.get('date','')} at "
                f"{booking_service._hhmm_to_ampm(apt.get('time','') or '')}, status {apt.get('status','')}, "
                f"service: {apt.get('reason','')}, customer name on file: {apt.get('name','')}"
                f"{stylist_txt}."
            )
        else:
            apt_info = "The customer has no active appointments in the system."
        pending_customer_note = ""
        if apt and apt.get("status") == "pending_customer":
            pending_customer_note = (
                "\nThey are refining DETAILS before the booking goes to the shop for approval. "
                "Echo date, time, name, and service back clearly when they change something. "
                "Never change the appointment time unless they explicitly ask—use the time in the system prompt above. "
                "Do not say the shop already confirmed it—only that you will pass it along once they finalize. "
                "Ask them to reply YES or CONFIRM only when everything looks exactly right; that submits the request "
                "to the business for approval (you cannot approve it yourself)."
            )
        business_name = config_service.get_business_info().get("name", "us")
        history_str = "\n".join(
            [f"{m['role']}: {m['content']}" for m in messages[-10:]]
        )
        booking_focus = appointment_focus_guidance(
            business_name, include_booked_slots=True, channel="sms"
        )
        sys_prompt = f"""You're the friendly text receptionist for {business_name}. Keep replies short (1-3 sentences), casual, like texting a friend.

{booking_focus}

{apt_info}{pending_customer_note}

They just texted: "{body}"

Previous conversation:
{history_str}

Respond naturally. If they confirm it's correct, say we'll text when the business confirms. If they want changes (date, time, name, etc.), acknowledge and say we'll update it—don't make up new details. Be warm and helpful."""

        openai_configured = bool((os.getenv("OPENAI_API_KEY") or "").strip())
        sms_trace(
            "inbound_ai_prepare",
            request_id=rid,
            client_id=tenant["client_id"],
            model="gpt-4o-mini",
            openai_key_configured=openai_configured,
            history_turns=len(messages),
            apt_id=apt.get("id") if apt else None,
            apt_status=(apt.get("status") if apt else None) or "",
            pending_customer_flow=bool(pending_customer_note),
            sys_prompt_len=len(sys_prompt),
            user_body_len=len(body),
        )
        reply = ""
        if not openai_configured:
            sms_info(
                "inbound_ai_skipped_no_openai_key",
                request_id=rid,
                client_id=tenant["client_id"],
            )
        else:
            client = openai.OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
            try:
                resp = client.chat.completions.create(
                    model="gpt-4o-mini",
                    messages=[
                        {"role": "system", "content": sys_prompt},
                        {"role": "user", "content": body},
                    ],
                    temperature=0.8,
                    max_tokens=150,
                )
                reply = (resp.choices[0].message.content or "").strip()
                finish_reason = getattr(resp.choices[0], "finish_reason", None)
                sms_trace(
                    "inbound_ai_complete",
                    request_id=rid,
                    reply_len=len(reply),
                    finish_reason=finish_reason or "",
                    empty_reply=not bool(reply),
                )
            except Exception as ai_err:
                sms_info(
                    "inbound_ai_openai_failed",
                    request_id=rid,
                    client_id=tenant["client_id"],
                    error_type=type(ai_err).__name__,
                    error=str(ai_err)[:400],
                )
                logger.warning(
                    "SMS OpenAI completion failed: %s", ai_err, exc_info=True
                )
                reply = ""
        if not reply:
            sms_info(
                "inbound_ai_empty_reply",
                request_id=rid,
                client_id=tenant["client_id"],
                openai_configured=openai_configured,
            )
            if apt and str(apt.get("status") or "") == "pending_customer":
                reply = (
                    "Thanks — we got that. Reply YES when everything looks right and we'll send it to the shop. "
                    "Msg & data rates may apply. Reply STOP to opt out."
                )
            else:
                reply = (
                    "Thanks — we got your message and will follow up shortly. "
                    "Msg & data rates may apply. Reply STOP to opt out."
                )
            sms_trace(
                "inbound_ai_fallback_reply_used",
                request_id=rid,
                pending_customer=bool(
                    apt and str(apt.get("status") or "") == "pending_customer"
                ),
            )
        send_ok = False
        if reply:
            send_ok = bool(sms_service.send_sms(from_number, reply, from_override=to_number))
            sms_trace(
                "inbound_ai_reply_send_result",
                request_id=rid,
                send_sms_ok=send_ok,
                reply_len=len(reply),
            )
        messages.append({"role": "assistant", "content": reply})
        try:
            database.db_sms_session_upsert(
                from_number, tenant["client_id"], messages, apt["id"] if apt else None
            )
            sms_trace(
                "inbound_session_persist_ok",
                request_id=rid,
                messages_stored=len(messages),
                appointment_id_attached=apt.get("id") if apt else None,
            )
        except Exception as upsert_err:
            sms_info(
                "inbound_session_persist_failed",
                request_id=rid,
                client_id=tenant["client_id"],
                error_type=type(upsert_err).__name__,
                phase="ai_reply_path",
            )
            logger.warning(
                "database.db_sms_session_upsert failed (AI path): %s", upsert_err, exc_info=True
            )
        # Lead capture: when no pending appointment and plan allows, treat as inquiry
        if (
            not apt
            and get_plan_limits
            and get_plan_limits(tenant).get("has_lead_capture")
        ):
            body_lower = (body or "").lower().strip()
            if len(body_lower) > 5 and body_lower not in (
                "yes",
                "no",
                "ok",
                "nope",
                "sure",
                "thanks",
            ):
                lead_inserted = False
                try:
                    database.db_leads_insert(
                        tenant["client_id"],
                        None,
                        from_number,
                        body[:500] if body else "inquiry",
                        "sms",
                    )
                    lead_inserted = True
                except Exception as lead_err:
                    sms_info(
                        "inbound_lead_insert_failed",
                        request_id=rid,
                        client_id=tenant["client_id"],
                        error_type=type(lead_err).__name__,
                    )
                    logger.warning(
                        "database.db_leads_insert SMS failed: %s", lead_err, exc_info=True
                    )
                sms_trace(
                    "inbound_lead_capture",
                    request_id=rid,
                    lead_inserted=lead_inserted,
                    body_qualifies=True,
                )
                # SMS automation: after_inquiry - send template to customer
                if runtime.USE_DB:
                    automations = database.db_sms_automations_get_by_trigger(
                        tenant["client_id"], "after_inquiry"
                    )
                    sms_trace(
                        "inbound_after_inquiry_automations",
                        request_id=rid,
                        automation_count=len(automations),
                    )
                    for auto in automations:
                        template = (auto.get("template") or "").strip()
                        if not template:
                            sms_trace(
                                "inbound_automation_skipped",
                                request_id=rid,
                                automation_id=str(auto.get("id") or ""),
                                reason="empty_template",
                            )
                            continue
                        cfg = config_service.load_client_config(tenant["client_id"])
                        business_name = (
                            (config_service.customer_facing_name(cfg) or "us") if cfg else "us"
                        )
                        msg = template.replace(
                            "{business_name}", business_name
                        ).replace("{name}", business_name)
                        try:
                            database.set_request_client_id(tenant["client_id"])
                            sms_service.send_sms(from_number, msg[:1600], from_override=to_number)
                            sms_trace(
                                "inbound_automation_sent",
                                request_id=rid,
                                automation_id=str(auto.get("id") or ""),
                                template_len=len(msg),
                            )
                        except Exception as auto_err:
                            sms_info(
                                "inbound_automation_send_failed",
                                request_id=rid,
                                automation_id=str(auto.get("id") or ""),
                                error_type=type(auto_err).__name__,
                            )
                            logger.warning(
                                "after_inquiry automation send failed: %s",
                                auto_err,
                                exc_info=True,
                            )
        sms_trace(
            "inbound_pipeline_done", request_id=rid, client_id=tenant["client_id"]
        )
        return Response(
            content='<?xml version="1.0" encoding="UTF-8"?><Response></Response>',
            media_type="application/xml",
        )
    except Exception as e:
        sms_info(
            "inbound_webhook_unhandled_exception",
            error_type=type(e).__name__,
            error=str(e)[:400],
            request_id=rid,
        )
        logger.exception("SMS webhook error: %s", e)
        try:
            import alerts

            alerts.notify_failure("twilio_sms", "inbound_unhandled", rid, str(e), sms=False)
        except Exception:
            pass
        return Response(
            content='<?xml version="1.0" encoding="UTF-8"?><Response></Response>',
            media_type="application/xml",
        )
