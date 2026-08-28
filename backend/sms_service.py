"""Outbound SMS via Twilio, plus E.164 normalization.

send_sms is shared across many domains (appointments, cron, SMS/phone webhooks),
so it lives here rather than in main.py. It reads the Twilio client singleton as
runtime.twilio_client and records usage / audit through database and deps.
"""

from __future__ import annotations

import hashlib
import logging
import os
import time
from datetime import datetime, timezone
from typing import Optional

import database
import deps
import runtime
from observability import sms_debug, sms_info, sms_trace
from security.redaction import mask_phone_e164

logger = logging.getLogger("nuvatra")

# From number for SMS — env-derived and immutable after process start.
TWILIO_SMS_FROM = os.getenv("TWILIO_SMS_FROM") or os.getenv("TWILIO_PHONE_NUMBER") or ""


def _default_messaging_service_sid() -> str:
    """A2P-registered Messaging Service SID (read at call time so env is picked up).

    Same env as twilio_provision.a2p_messaging_service_sid — numbers enrolled in this
    service inherit the approved US 10DLC campaign. When set, send_sms routes through it
    so messages are A2P-registered; raw long codes outside it get carrier error 30034
    ("unregistered number") and are silently dropped. Empty → fall back to a From number.
    """
    return (os.getenv("TWILIO_A2P_MESSAGING_SERVICE_SID") or "").strip()


def normalize_phone(phone: str) -> str:
    """Normalize to a digits-only key (no +). Used for caller-memory/booking phone matching."""
    return "".join(c for c in phone if c.isdigit())


def _phone_to_e164(phone: str) -> Optional[str]:
    """Convert to E.164 for Twilio SMS (e.g. +15551234567). Returns None if too short."""
    digits = "".join(c for c in phone if c.isdigit())
    if len(digits) == 10:
        return f"+1{digits}"
    if len(digits) == 11 and digits.startswith("1"):
        return f"+{digits}"
    if len(digits) >= 10:
        return f"+{digits}"
    return None


# Twilio errors that mean this number will never accept a text, however many times we
# try: a landline or VoIP line with no SMS (21614), a number that isn't reachable as a
# mobile (21612), and an unroutable/invalid recipient (21211, 21408). Retrying these
# three times costs seconds of dead air on a live call and cannot change the answer —
# and the caller needs to be told something true instead of "I've texted you".
NON_TEXTABLE_ERROR_CODES = frozenset({21211, 21408, 21612, 21614})


def _twilio_error_code(err: Exception) -> Optional[int]:
    code = getattr(err, "code", None)
    try:
        return int(code) if code is not None else None
    except (TypeError, ValueError):
        return None


def send_sms(
    to_phone: str,
    body: str,
    from_override: Optional[str] = None,
    *,
    messaging_service_sid: Optional[str] = None,
    force: bool = False,
    detail_out: Optional[dict] = None,
) -> bool:
    """Send SMS via Twilio.

    Routing: PREFER the tenant's own From number (from_override, else TWILIO_SMS_FROM) so
    two-way replies come back to that number and resolve to the tenant (db_tenant_get_by_phone)
    — required for SMS appointment confirmation. That number must be enrolled in the A2P
    Messaging Service (see twilio_provision.enroll_in_messaging_service) so it inherits the
    registered 10DLC campaign; a raw long code outside the campaign is dropped with carrier
    error 30034. Only when there is NO From number do we fall back to sending via the
    Messaging Service SID (param, else TWILIO_A2P_MESSAGING_SERVICE_SID env) — note that path
    sends from a pooled number, so replies can't be mapped back to a tenant.

    Records usage via db_usage_increment_sms when client_id is set.
    If force=True, skip per-tenant opt-out check (STOP/START/HELP confirmations only).

    detail_out, when given, is filled in with why a send failed: error_code, and
    not_textable=True when the recipient cannot receive SMS at all (a landline).
    """
    if not runtime.twilio_client:
        sms_info("outbound_skipped", reason="twilio_not_configured")
        return False
    msid = (messaging_service_sid or _default_messaging_service_sid() or "").strip()
    from_num = (from_override or TWILIO_SMS_FROM or "").strip()
    if not msid and not from_num:
        sms_info(
            "outbound_skipped",
            reason="from_number_missing",
            from_override_set=bool(from_override),
            twilio_sms_from_set=bool(TWILIO_SMS_FROM),
            messaging_service_set=bool(msid),
        )
        return False
    e164 = _phone_to_e164(to_phone or "")
    if not e164:
        sms_info("outbound_skipped", reason="invalid_recipient_phone")
        return False
    if runtime.USE_DB and not force:
        cid = database._client_id()
        if cid and cid != "default":
            if database.db_sms_opt_out_is_blocked(e164, cid):
                to_masked = mask_phone_e164(e164)
                sms_info(
                    "outbound_skipped",
                    reason="recipient_opted_out",
                    client_id_prefix=cid[:12],
                    to_masked=to_masked,
                )
                return False
    to_masked = mask_phone_e164(e164)
    # For logs/audit: never leak the raw service SID; show the From or a service marker.
    sender_label = mask_phone_e164(from_num) if from_num else (f"msgsvc:…{msid[-4:]}" if msid else "")
    via = "from_number" if from_num else ("messaging_service" if msid else "none")
    sms_debug(
        "outbound_attempt",
        via=via,
        sender=sender_label,
        to_masked=to_masked,
        body_len=len(body or ""),
        force=force,
    )
    sms_trace(
        "outbound_attempt",
        via=via,
        sender=sender_label,
        to_masked=to_masked,
        body_len=len(body or ""),
        force=force,
    )
    last_err = None
    for attempt in range(3):
        try:
            create_kwargs = {"to": e164, "body": body}
            # Prefer the tenant's own number so replies route back to it and resolve the
            # tenant; fall back to the Messaging Service only when no From is available.
            if from_num:
                create_kwargs["from_"] = from_num
            else:
                create_kwargs["messaging_service_sid"] = msid
            msg = runtime.twilio_client.messages.create(**create_kwargs)
            sid = getattr(msg, "sid", None) or getattr(msg, "id", None)
            sms_info(
                "outbound_twilio_ok",
                message_sid=sid,
                to_masked=to_masked,
                body_len=len(body or ""),
            )
            # Record SMS usage for billing (graceful degradation)
            if runtime.USE_DB:
                cid = database._client_id()
                if cid and cid != "default":
                    try:
                        month = datetime.now(timezone.utc).strftime("%Y-%m")
                        database.db_usage_increment_sms(cid, month)
                    except Exception as e:
                        logger.error("SMS usage increment failed: %s", e)
            deps.audit_log(
                "sms",
                "outbound_sent",
                resource_type="message",
                resource_id=str(sid) if sid else None,
                client_id=database._client_id() if runtime.USE_DB else None,
                details={
                    "to_masked": to_masked,
                    "from_masked": sender_label,
                    "body_len": len(body or ""),
                    "body_sha256": hashlib.sha256((body or "").encode("utf-8")).hexdigest(),
                    "force": bool(force),
                },
            )
            return True
        except Exception as e:
            last_err = e
            code = _twilio_error_code(e)
            if code is not None and detail_out is not None:
                detail_out["error_code"] = code
            if code in NON_TEXTABLE_ERROR_CODES:
                if detail_out is not None:
                    detail_out["not_textable"] = True
                sms_info(
                    "outbound_not_textable",
                    error_code=code,
                    to_masked=to_masked,
                )
                return False
            logger.warning(
                "[SMS] outbound_twilio_retry attempt=%s error=%s to_masked=%s",
                attempt + 1,
                e,
                to_masked,
            )
            if attempt < 2:
                time.sleep(2**attempt)
    sms_info("outbound_failed_after_retries", error=str(last_err), to_masked=to_masked)
    deps.audit_log(
        "sms",
        "outbound_failed",
        resource_type="message",
        client_id=database._client_id() if runtime.USE_DB else None,
        details={
            "to_masked": to_masked,
            "from_masked": sender_label,
            "body_len": len(body or ""),
            "body_sha256": hashlib.sha256((body or "").encode("utf-8")).hexdigest(),
            "error": str(last_err)[:240] if last_err else None,
        },
    )
    return False
