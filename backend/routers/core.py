"""Core misc routes — AI conversation (text channel), messages, and email-notify helper.

Thin transport over the extracted services (conversation_service / booking_service /
config_service / deps / database / runtime).
"""

from __future__ import annotations

from datetime import datetime
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

import booking_service
import config_service
import conversation_service
import database
import deps
import llm_provider
import runtime
import sms_service
from observability import email_hint_for_log, system_info
from booking_fields import booking_context_from_business, is_valid_booking_date, looks_like_booking_time

try:
    from plans import get_plan_limits
except ImportError:  # pragma: no cover - plans module always present in practice
    get_plan_limits = None  # type: ignore

router = APIRouter()


def _tenant_has_messages(tenant: Optional[dict]) -> bool:
    """SMS conversations inbox is a Growth+ perk (trial = pro-level access)."""
    if not get_plan_limits:
        return True
    return bool(get_plan_limits(tenant).get("has_messages"))


class ConversationRequest(BaseModel):
    message: str
    session_id: str
    conversation_history: Optional[List[dict]] = []


class ConversationResponse(BaseModel):
    response: str
    action: Optional[str] = None
    data: Optional[dict] = None


class MessageRequest(BaseModel):
    caller_name: str
    caller_phone: str
    message: str
    urgency: str = "normal"


class MessageReplyBody(BaseModel):
    text: str = Field(..., min_length=1, max_length=1000)


def _send_appointment_email_notification(apt: dict, *, kind: str) -> bool:
    """Send submitted/confirmed email when enabled and provider is configured."""
    if not booking_service._appointment_email_enabled():
        return False
    from email_notify import format_appointment_email, send_appointment_email

    email = (apt.get("email") or "").strip()
    if not email:
        return False
    business_name = (config_service.get_business_info().get("name") or "us").strip()
    subject, html, text = format_appointment_email(
        kind=kind,
        business_name=business_name,
        customer_name=(apt.get("name") or "").strip(),
        date=apt.get("date") or "",
        time_ampm=booking_service._hhmm_to_ampm(apt.get("time") or ""),
        service=(apt.get("reason") or "").strip(),
    )
    ok = send_appointment_email(
        to=email, subject=subject, html_body=html, text_body=text
    )
    from observability import email_hint_for_log

    system_info(
        "appointment_email_notification",
        apt_id=apt.get("id"),
        kind=kind,
        sent=ok,
        email_hint=email_hint_for_log(email),
    )
    return ok


@router.post("/api/conversation", response_model=ConversationResponse)
def handle_conversation(
    request: ConversationRequest, _: None = Depends(deps.require_active_subscription)
):
    try:
        # Always include booked slots so the AI knows which times are taken and avoids double-booking
        system_content = conversation_service.get_system_prompt(include_booked_slots=True)
        messages = [{"role": "system", "content": system_content}]
        if request.conversation_history:
            runtime.messages.extend(request.conversation_history)
        runtime.messages.append({"role": "user", "content": request.message})

        ai_response = llm_provider.chat(
            model=conversation_service.VOICE_LLM_MODEL,
            messages=runtime.messages,
            temperature=0.7,
            max_tokens=200,
        )
        action = None
        data = None

        # BOOKING: create appointment from AI output if present
        booking = conversation_service.parse_booking(ai_response)
        if booking:
            booking, repairs, reject = conversation_service._prepare_parsed_booking(booking)
            if reject:
                system_info(
                    "chat_booking_line_rejected",
                    reason=reject,
                    repairs=repairs or None,
                )
                booking = None
            elif repairs:
                system_info("chat_booking_line_repaired", repairs=repairs)
        if booking:
            ok_booking, fail_msg, _, canonical_service = conversation_service._validate_booking_requirements(
                booking
            )
            if not ok_booking:
                apt = None
            else:
                if canonical_service:
                    booking["reason"] = canonical_service
                apt = conversation_service._create_appointment_from_booking(booking)
            if apt:
                ai_response = f"You're all set! We have you down for {apt['date']} at {booking_service._hhmm_to_ampm(apt.get('time', '') or '')}. The store will confirm shortly."
                action = "schedule_appointment"
                data = {"appointment_id": apt["id"]}
            else:
                ctx = booking_context_from_business(config_service.get_business_info())
                name_ok = bool((booking.get("name") or "").strip())
                date_ok = is_valid_booking_date(booking.get("date"))
                time_ok = looks_like_booking_time(booking.get("time"), ctx)
                if not ok_booking:
                    ai_response = (
                        fail_msg
                        or "Before I can book this, please choose a stylist and service."
                    )
                elif not name_ok:
                    ai_response = "I'd love to book that for you—what's your name?"
                elif not date_ok or not time_ok:
                    ai_response = "I need the date and time again to confirm—which day and time would you like?"
                else:
                    ai_response = "That time slot just got booked. Would you like to try another time or another day?"

        ai_response = conversation_service._strip_booking_directive_for_voice(ai_response or "")
        if not ai_response:
            # The reply was nothing but the machine directive; say something human
            # rather than reading the marker back.
            ai_response = "Thanks—we've noted that. Let us know if you need anything else."
        if (
            "schedule" in request.message.lower()
            or "appointment" in request.message.lower()
        ):
            action = action or "schedule_appointment"
        elif (
            "message" in request.message.lower()
            or "leave a message" in request.message.lower()
        ):
            action = "take_message"
        elif (
            "transfer" in request.message.lower()
            or "department" in request.message.lower()
        ):
            action = "route_call"

        return ConversationResponse(response=ai_response, action=action, data=data)

    except Exception as e:
        raise deps._server_error("conversation endpoint failed", e)


@router.post("/api/messages")
def create_message(
    message: MessageRequest, _: None = Depends(deps.require_active_subscription)
):
    try:
        data = {
            "caller_name": message.caller_name,
            "caller_phone": message.caller_phone,
            "message": message.message,
            "urgency": message.urgency,
            "status": "unread",
        }
        if runtime.USE_DB:
            message_data = database.db_messages_insert(data)
        else:
            message_data = {
                "id": len(runtime.messages) + 1,
                **data,
                "created_at": datetime.now().isoformat(),
            }
            runtime.messages.append(message_data)
        return {"success": True, "message": message_data}
    except Exception as e:
        raise deps._server_error("create message failed", e)


@router.get("/api/messages")
def get_messages(tenant: Optional[dict] = Depends(deps.require_active_subscription)):
    deps._bind_tenant_db_context(tenant)  # contextvar from the dep doesn't reach this sync handler
    lst = database.db_messages_get_all() if runtime.USE_DB else runtime.messages
    return {"messages": lst}


@router.get("/api/sms/threads")
def get_sms_threads(
    search: Optional[str] = None,
    tenant: Optional[dict] = Depends(deps.require_active_subscription),
):
    """List SMS conversation threads (one per phone number) for the tenant, newest first.
    The inbox is a Growth+ perk; lower tiers get an empty list flagged `locked`."""
    cid = deps._bind_tenant_db_context(tenant)
    if not _tenant_has_messages(tenant):
        return {"threads": [], "locked": True}
    threads = database.db_sms_threads_list(cid, search=search) if runtime.USE_DB else []
    return {"threads": threads, "locked": False}


@router.get("/api/sms/thread")
def get_sms_thread(
    phone: str,
    tenant: Optional[dict] = Depends(deps.require_active_subscription),
):
    """Full message history for a single SMS thread (caller <-> AI receptionist)."""
    cid = deps._bind_tenant_db_context(tenant)
    if not _tenant_has_messages(tenant):
        raise HTTPException(status_code=403, detail="The Messages inbox is available on Growth and Pro plans")
    if not runtime.USE_DB:
        return {"phone": phone, "messages": [], "appointment_id": None, "updated_at": ""}
    sess = database.db_sms_session_get(phone, cid)
    if not sess:
        raise HTTPException(status_code=404, detail="Conversation not found")
    msgs = [
        {"role": m.get("role") or "", "content": m.get("content") or ""}
        for m in (sess.get("messages") or [])
        if isinstance(m, dict)
    ]
    updated = sess.get("updated_at")
    return {
        "phone": database._normalize_phone(phone),
        "messages": msgs,
        "appointment_id": sess.get("appointment_id"),
        "updated_at": updated.isoformat() if hasattr(updated, "isoformat") else (updated or ""),
    }


def _find_inmem_message(message_id: int) -> Optional[dict]:
    return next((m for m in runtime.messages if m.get("id") == message_id), None)


@router.post("/api/messages/{message_id}/read")
def mark_message_read(
    message_id: int,
    request: Request,
    read: bool = True,
    tenant: Optional[dict] = Depends(deps.require_active_subscription),
):
    """Mark a caller message read (or unread with ?read=false)."""
    cid = deps._bind_tenant_db_context(tenant)
    status = "read" if read else "unread"
    if runtime.USE_DB:
        updated = database.db_messages_set_status(message_id, status, client_id=cid)
    else:
        updated = _find_inmem_message(message_id)
        if updated:
            updated["status"] = status
    if not updated:
        raise HTTPException(status_code=404, detail="Message not found")
    deps.audit_log(
        "user",
        "message_marked_read" if read else "message_marked_unread",
        resource_type="message",
        resource_id=str(message_id),
        request=request,
    )
    return {"success": True, "message": updated}


@router.post("/api/messages/{message_id}/reply")
def reply_to_message(
    message_id: int,
    body: MessageReplyBody,
    request: Request,
    tenant: Optional[dict] = Depends(deps.require_active_subscription),
):
    """Text the caller back from the business number, and mark the message read."""
    cid = deps._bind_tenant_db_context(tenant)
    msg = (
        database.db_messages_get_by_id(message_id, client_id=cid)
        if runtime.USE_DB
        else _find_inmem_message(message_id)
    )
    if not msg:
        raise HTTPException(status_code=404, detail="Message not found")
    to_phone = (msg.get("caller_phone") or "").strip()
    if not to_phone:
        raise HTTPException(status_code=400, detail="This message has no caller phone to reply to")
    sent = sms_service.send_sms(
        to_phone, body.text.strip(), from_override=booking_service._tenant_sms_from_number()
    )
    # Replying resolves the message regardless of delivery; the caller is contacted.
    if runtime.USE_DB:
        updated = database.db_messages_set_status(message_id, "read", client_id=cid) or msg
    else:
        msg["status"] = "read"
        updated = msg
    deps.audit_log(
        "user",
        "message_replied",
        resource_type="message",
        resource_id=str(message_id),
        details={"reply_sms_sent": bool(sent)},
        request=request,
    )
    return {"success": True, "message": updated, "reply_sms_sent": sent}
