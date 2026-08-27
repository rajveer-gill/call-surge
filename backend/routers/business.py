"""Business config API — settings (business-info), greeting preview, setup status, onboarding.

A clean relocation: all the voice/recording/greeting helpers now live in services. Cross-module
helpers are module-qualified (config_service / voice_service / deps / database); staff/transfer
sanitization is imported from staff_transfers within the routes.
"""

from __future__ import annotations

import json
import re
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, List, Literal, Optional
from uuid import uuid4

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from pydantic import BaseModel, EmailStr, Field, TypeAdapter, ValidationError, field_validator

import config_service
import database
import deps
import runtime
import voice_service
from observability import _stable_sha256, system_info, voice_info
from staff_transfers import TransferTarget

try:
    from plans import get_plan_limits
except ImportError:  # pragma: no cover
    get_plan_limits = None  # type: ignore

import logging as _logging
logger = _logging.getLogger("nuvatra")
import os as _os
TWILIO_ACCOUNT_SID = _os.getenv("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = _os.getenv("TWILIO_AUTH_TOKEN")

router = APIRouter()

SETUP_REQUIRED_FIELDS = [
    ("name", "Business name"),
    ("hours", "Hours of operation"),
    ("forwarding_phone", "Store phone (real person)"),
    ("address", "Address"),
]


class StaffMember(BaseModel):
    id: Optional[str] = Field(default=None, max_length=36)
    name: str = Field(default="", max_length=120)
    phone: str = Field(default="", max_length=32)
    email: str = Field(default="", max_length=254)
    notes: str = Field(default="", max_length=4000)
    service_ids: List[str] = Field(default_factory=list)
    # Days of week this person works (lowercase 3-letter: mon..sun). Empty = no
    # constraint (works whenever the shop is open) — backward compatible.
    working_days: List[str] = Field(default_factory=list)
    # Optional per-day hour windows {day: {"start": "HH:MM", "end": "HH:MM"}}.
    # A working day with no entry = full shop hours that day.
    working_hours: dict = Field(default_factory=dict)
    # Specific dates (YYYY-MM-DD) this person is off (sick / vacation). Validated + capped.
    time_off: List[str] = Field(default_factory=list)

    @field_validator("working_days", mode="before")
    @classmethod
    def _normalize_working_days(cls, v):
        import staff_schedule

        return staff_schedule.normalize_working_days(v)

    @field_validator("working_hours", mode="before")
    @classmethod
    def _normalize_working_hours(cls, v):
        import staff_schedule

        return staff_schedule.normalize_working_hours(v)

    @field_validator("time_off", mode="before")
    @classmethod
    def _normalize_time_off(cls, v):
        import staff_schedule

        return staff_schedule.normalize_date_list(v)

    @field_validator("id", mode="before")
    @classmethod
    def strip_id_optional(cls, v):
        if v is None:
            return None
        vv = str(v).strip()
        return vv if vv else None

    @field_validator("id")
    @classmethod
    def id_must_be_uuid(cls, v: Optional[str]) -> Optional[str]:
        if v is None:
            return None
        try:
            return str(uuid.UUID(v))
        except ValueError as e:
            raise ValueError("Staff id must be a valid UUID when provided.") from e

    @field_validator("name", mode="before")
    @classmethod
    def sanitize_name(cls, v):
        return _staff_sanitize_single_line(v if v is not None else "")[:120]

    @field_validator("phone", mode="before")
    @classmethod
    def sanitize_phone(cls, v):
        return _staff_sanitize_single_line(v if v is not None else "")[:32]

    @field_validator("phone")
    @classmethod
    def validate_phone_optional(cls, v: str) -> str:
        s = (v or "").strip()
        if not s:
            return ""
        digits = "".join(c for c in s if c.isdigit())
        if len(digits) < 10:
            raise ValueError("Phone must be at least 10 digits when provided.")
        return s

    @field_validator("notes", mode="before")
    @classmethod
    def sanitize_notes_field(cls, v):
        return _staff_sanitize_notes(v if v is not None else "")

    @field_validator("email", mode="before")
    @classmethod
    def sanitize_email_raw(cls, v):
        if v is None:
            return ""
        s = "".join(c for c in str(v).strip() if ord(c) >= 32)
        return s[:254]

    @field_validator("email")
    @classmethod
    def validate_email_optional(cls, v: str) -> str:
        if not v:
            return ""
        try:
            return str(TypeAdapter(EmailStr).validate_python(v))
        except ValidationError as e:
            raise ValueError("Invalid email address.") from e

    @field_validator("service_ids", mode="before")
    @classmethod
    def normalize_service_ids(cls, v):
        if not v:
            return []
        if not isinstance(v, list):
            return []
        out: List[str] = []
        for item in v:
            raw = str(item).strip()
            if not raw:
                continue
            try:
                out.append(str(uuid.UUID(raw)))
            except ValueError:
                continue
        return out[:50]


class BusinessInfoUpdate(BaseModel):
    name: Optional[str] = None
    hours: Optional[str] = None
    phone: Optional[str] = None
    forwarding_phone: Optional[str] = None
    email: Optional[str] = None
    address: Optional[str] = None
    departments: Optional[List[str]] = None
    services: Optional[List[Any]] = None
    specials: Optional[List[Any]] = None
    reservation_rules: Optional[List[Any]] = None
    menu_link: Optional[str] = None
    greeting: Optional[str] = None
    voice: Optional[str] = None
    speed: Optional[float] = None
    receptionist_name: Optional[str] = None
    business_type: Optional[str] = None
    staff: Optional[List[StaffMember]] = None
    transfer_targets: Optional[List[TransferTarget]] = None
    # Dates (YYYY-MM-DD) the whole shop is closed (holidays, etc.). Validated + capped.
    closures: Optional[List[str]] = None
    # When True, the AI takes a message instead of transferring "talk to a real person"
    # calls (for businesses whose only number forwards to the AI line). Replaces the need
    # for a separate forwarding_phone to satisfy call-readiness.
    transfer_takes_message: Optional[bool] = None
    quote_prices: Optional[bool] = None
    # Who owns the calendar: "internal" (we do — the default for everyone) or
    # "external" (it lives in Zenoti/Mindbody/etc and we can't write to it, so the AI
    # takes requests staff approve). See config_service.BOOKING_MODES.
    booking_mode: Optional[Literal["internal", "external"]] = None
    booking_provider_name: Optional[str] = Field(default=None, max_length=80)
    # Services this business will not book over the phone (e.g. corrective color).
    consult_only_services: Optional[List[str]] = None
    # Free-text policies injected into this store's receptionist prompt.
    booking_rules: Optional[List[str]] = None


def _settings_load_debug_log_business_info(tenant: Optional[dict], out: dict) -> None:
    if not deps._settings_load_debug_enabled():
        return
    cid = (tenant or {}).get("client_id") if tenant else None
    prefix = (str(cid)[:10] + "…") if cid else "none"

    def _tn(key: str) -> str:
        v = out.get(key)
        return type(v).__name__ if v is not None else "none"

    logger.info(
        "settings_load_debug GET /api/business-info client_id_prefix=%s response_keys=%s "
        "services_ty=%s specials_ty=%s reservation_rules_ty=%s staff_ty=%s "
        "config_source=%s greeting_len=%s voice=%s receptionist_set=%s",
        prefix,
        sorted(out.keys()),
        _tn("services"),
        _tn("specials"),
        _tn("reservation_rules"),
        _tn("staff"),
        config_service.client_config_source(str(cid)) if cid else "none",
        len((out.get("greeting") or "")),
        out.get("voice"),
        bool((out.get("receptionist_name") or "").strip()),
    )


def get_setup_status(
    info_override: Optional[dict] = None,
    *,
    twilio_phone: Optional[str] = None,
    is_demo: bool = False,
) -> dict:
    """Return setup completeness. Uses info_override if provided (e.g. with tenant phone merged), else config_service.get_business_info().

    is_demo suppresses the "your number is on its way" messaging — a demo account
    deliberately never gets a phone line, so promising one is a lie.
    """
    info = info_override if info_override is not None else config_service.get_business_info()
    missing: List[str] = []
    warnings: List[str] = []
    take_message = config_service.transfer_takes_message(info)
    for key, label in SETUP_REQUIRED_FIELDS:
        val = info.get(key)
        if not (val and str(val).strip()):
            # The store/transfer phone is optional when "take a message instead" is on.
            if key == "forwarding_phone" and take_message:
                continue
            missing.append(label)
    services_ready = config_service.services_configured(info)
    roster_ready = config_service.staff_roster_ready_for_booking(info)
    store_phone_ready = config_service.forwarding_phone_ready(info)
    handoff_ready = store_phone_ready or take_message
    voice_ready = roster_ready and handoff_ready and services_ready
    roster_only_gap = voice_service.setup_transfers_to_store_after_message(info)
    if not services_ready:
        warnings.append(
            "Add at least one service in Settings so the AI knows what you offer. Your AI receptionist will not "
            "take calls until a service menu is set—without one it can't tell callers what you provide."
        )
    if not roster_ready:
        if roster_only_gap:
            warnings.append(
                "Add at least one team member with a name on the Team roster so your AI receptionist can take calls. "
                "Until then, callers hear a message and are transferred to your store phone."
            )
        else:
            warnings.append(
                "Add at least one team member with a name on the Team roster so callers can book appointments."
            )
    if not handoff_ready:
        warnings.append(
            "Add a transfer number so callers can reach a real person, "
            "or turn on \"take a message instead\" and the AI will capture messages for callback."
        )
    if not voice_ready and not roster_only_gap:
        warnings.append(
            "Your AI receptionist cannot take calls until setup is complete in Settings "
            "(team roster, a service menu, and a way to reach a real person)."
        )
    staff_count = len(
        [s for s in (info.get("staff") or []) if (s.get("name") or "").strip()]
    )
    service_count = len(config_service._normalize_service_entries(info.get("services") or []))
    if roster_ready and staff_count >= 2 and service_count == 0:
        warnings.append(
            "Add services in Settings so callers can choose a service type during booking. "
            "Without a service menu, the AI will not ask which service they want."
        )
    twilio_number_set = bool((twilio_phone or "").strip())
    webhooks_configured = False
    if twilio_number_set:
        from twilio_provision import verify_webhooks_match_cached

        base = deps._public_base_url()
        if base and TWILIO_ACCOUNT_SID and TWILIO_AUTH_TOKEN:
            verify = verify_webhooks_match_cached(
                account_sid=TWILIO_ACCOUNT_SID,
                auth_token=TWILIO_AUTH_TOKEN,
                phone=twilio_phone or "",
                base_url=base,
            )
            webhooks_configured = bool(verify.get("webhooks_configured"))
            if not webhooks_configured:
                warnings.append(
                    "Twilio webhooks for your AI phone number are missing or misconfigured. "
                    "Ask your admin to save the number again in Admin or set Voice + Messaging URLs in Twilio Console."
                )
        else:
            warnings.append(
                "AI phone number is set but webhook verification is unavailable (PUBLIC_BASE_URL or Twilio credentials missing on server)."
            )
    elif voice_ready:
        warnings.append(
            "No AI phone number is linked to this account yet. Your admin must assign a Twilio number before callers can reach the AI."
        )
    return {
        "complete": len(missing) == 0,
        "missing": missing,
        "warnings": warnings,
        "roster_ready": roster_ready,
        "services_ready": services_ready,
        "forwarding_phone_ready": store_phone_ready,
        "transfer_takes_message": take_message,
        "quote_prices": config_service.quotes_prices(info),
        "voice_ready": voice_ready,
        "roster_only_gap": roster_only_gap,
        "twilio_number_set": twilio_number_set,
        "webhooks_configured": webhooks_configured,
        "number_mode": (info.get("number_mode") or "new"),
        "existing_business_number": (info.get("existing_business_number") or "").strip()
        or None,
        "forwarding_verified_at": (info.get("forwarding_verified_at") or "").strip() or None,
        # The provisioned Twilio line — surfaced so the onboarding wizard can show the
        # "forward <existing> → <ai line>" instruction once the number is assigned.
        "ai_phone_number": (twilio_phone or "").strip() or None,
        # A demo account never gets a phone line, so the wizard must not claim one is
        # on its way — see SetupWizard's go-live step.
        "demo_mode": bool(is_demo),
        "onboarding_completed_at": (info.get("onboarding_completed_at") or "").strip()
        or None,
    }


def _staff_sanitize_single_line(raw: Optional[str]) -> str:
    """Strip whitespace; disallow control chars and newlines (name, phone paths)."""
    if raw is None:
        return ""
    s = str(raw)
    s = "".join(c for c in s if ord(c) >= 32)
    return s.strip()


def _staff_sanitize_notes(raw: Optional[str]) -> str:
    """Notes: allow TAB/LF/CR; strip NUL and other C0 controls."""
    if raw is None:
        return ""
    s = "".join(c for c in str(raw) if ord(c) >= 32 or c in "\t\n\r")
    return s.strip()


def _valid_service_id_set(services_raw: Any) -> set[str]:
    return {
        s["id"] for s in config_service._normalize_service_entries(services_raw or []) if s.get("id")
    }


def finalize_staff_records_for_storage(
    members: List[StaffMember],
    *,
    valid_service_ids: Optional[set[str]] = None,
) -> List[dict]:
    """Serialize staff for config.json; assign UUID when id omitted (backward compatible rows)."""
    out: List[dict] = []
    for m in members:
        sid = (m.id or "").strip() or str(uuid.uuid4())
        svc_ids: List[str] = []
        for raw_id in m.service_ids or []:
            rid = str(raw_id).strip()
            if not rid:
                continue
            if valid_service_ids is not None and rid not in valid_service_ids:
                continue
            svc_ids.append(rid)
        row: dict = {
            "id": sid,
            "name": m.name,
            "phone": m.phone,
            "email": m.email,
            "notes": m.notes,
        }
        if svc_ids:
            row["service_ids"] = svc_ids
        if m.working_days:
            row["working_days"] = list(m.working_days)
        if m.working_hours:
            row["working_hours"] = dict(m.working_hours)
        if m.time_off:
            row["time_off"] = list(m.time_off)
        out.append(row)
    return out


@router.get("/api/business-info")
def api_get_business_info(
    tenant: Optional[dict] = Depends(deps.require_active_subscription),
):
    out = config_service.business_info_for_dashboard(tenant)
    if tenant:
        out["client_id"] = (tenant.get("client_id") or "").strip()
    _settings_load_debug_log_business_info(tenant, out)
    return out


class UpdateNumberModeRequest(BaseModel):
    number_mode: Literal["new", "existing"]
    existing_number: Optional[str] = None


@router.post("/api/business/number-mode")
def api_update_number_mode(
    req: UpdateNumberModeRequest,
    request: Request,
    tenant: Optional[dict] = Depends(deps.require_active_subscription),
):
    """Self-serve: switch between a dedicated AI number ('new') and forwarding the
    business's own number to the AI line ('existing').

    The provisioned Twilio line stays put in both modes — this only changes how the
    business routes calls to it. Changing the number (or the mode) resets forwarding
    verification so it re-confirms against the new setup.
    """
    if not tenant:
        raise HTTPException(status_code=403, detail="Tenant required")
    cid = (tenant.get("client_id") or "").strip()
    if not cid:
        raise HTTPException(status_code=400, detail="No client context")
    data = config_service._read_raw_client_config(cid)
    if data is None:
        data = config_service._default_client_config_data(cid, tenant.get("plan") or "free")
    if req.number_mode == "existing":
        existing = _normalize_us_phone_display(req.existing_number)
        if len(re.sub(r"\D", "", existing)) < 10:
            raise HTTPException(
                status_code=400,
                detail="Enter the existing business number you want to forward calls from.",
            )
        if (data.get("number_mode") != "existing") or ((data.get("existing_business_number") or "") != existing):
            data["forwarding_verified_at"] = ""  # re-verify on any change
        data["number_mode"] = "existing"
        data["existing_business_number"] = existing
    else:
        data["number_mode"] = "new"
        data["forwarding_verified_at"] = ""  # N/A when publishing the AI line directly
    config_service.save_raw_client_config(cid, data)
    deps.audit_log(
        "user",
        "number_mode_updated",
        resource_type="tenant",
        resource_id=tenant.get("id"),
        client_id=cid,
        details={"number_mode": data["number_mode"]},
        request=request,
    )
    return {
        "ok": True,
        "number_mode": data["number_mode"],
        "existing_business_number": data.get("existing_business_number") or None,
        "forwarding_verified_at": data.get("forwarding_verified_at") or None,
    }


@router.get("/api/greeting-preview")
def api_greeting_preview(
    tenant: Optional[dict] = Depends(deps.require_active_subscription),
):
    """
    Return the exact phone greeting text (placeholders resolved, recording line last).
    Use in Settings to verify what callers will hear before placing a test call.
    """
    tid = tenant or {}
    cid = (tid.get("client_id") or "").strip()
    if cid:
        database.set_request_client_id(cid)
    info = config_service.business_info_for_dashboard(tid) if tid else config_service.get_business_info()
    payload = voice_service.build_phone_greeting_payload(info, tid or voice_service._tenant_for_call_recording())
    return payload


@router.get("/api/setup-status")
def api_setup_status(
    tenant: Optional[dict] = Depends(deps.require_active_subscription),
):
    """Return which required/recommended business info fields are missing. Used for setup checklist."""
    info = config_service.business_info_for_dashboard(tenant)
    twilio_phone = (tenant or {}).get("twilio_phone_number") if tenant else None
    body = get_setup_status(
        info_override=info,
        twilio_phone=twilio_phone,
        is_demo=bool((tenant or {}).get("demo_mode")),
    )
    if deps._settings_load_debug_enabled():
        cid = (tenant or {}).get("client_id") if tenant else None
        prefix = (str(cid)[:10] + "…") if cid else "none"
        logger.info(
            "settings_load_debug GET /api/setup-status client_id_prefix=%s complete=%s missing_n=%s",
            prefix,
            body.get("complete"),
            len(body.get("missing") or []),
        )
    return body


@router.post("/api/business-info/services/import")
async def api_import_services(
    file: UploadFile = File(...),
    sheet: Optional[str] = Form(None),
    tenant: Optional[dict] = Depends(deps.require_active_subscription),
):
    """Parse an uploaded service list. Writes NOTHING.

    Returns the rows for the caller to review and edit; saving goes through the
    normal business-info PATCH, so there's one write path for services rather than a
    second one that could drift from it.
    """
    import service_import

    deps._bind_tenant_db_context(tenant)
    raw = await file.read()
    result = service_import.parse_service_spreadsheet(
        raw, filename=file.filename or "", sheet=sheet
    )
    system_info(
        "service_import_preview",
        client_id=(tenant or {}).get("client_id"),
        filename=(file.filename or "")[:120],
        found=len(result.get("services") or []),
        sheet=result.get("sheet"),
    )
    return result


class CreateBusinessRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=120)
    plan: Literal["starter", "growth", "pro"] = "starter"
    business_vertical: str = "salon_chair"
    # "new" = give them a fresh Twilio number to publish; "existing" = they keep their
    # current number and forward calls to the (hidden) provisioned Twilio line.
    number_mode: Literal["new", "existing"] = "new"
    # The number customers currently dial — required (display/forwarding only) when
    # number_mode == "existing". A Twilio line is still provisioned in both modes.
    existing_number: Optional[str] = None


def _normalize_us_phone_display(raw: Optional[str]) -> str:
    """Best-effort E.164 for a US number entered in onboarding; falls back to the
    trimmed input so we never reject a valid foreign/edge number outright."""
    digits = re.sub(r"\D", "", raw or "")
    if len(digits) == 10:
        return f"+1{digits}"
    if len(digits) == 11 and digits.startswith("1"):
        return f"+{digits}"
    return (raw or "").strip()


def _create_account_org(user_id: str, name: str, tenant: dict, plan: str) -> Optional[str]:
    """Wrap a brand-new store in an org and make the signer-up its manager.

    Every account is an org, whether it holds one store or thirty-four. That means
    there's a single shape in the system — billing and identity live on the org, a
    store is just a phone line and a calendar under it — so opening a second location
    later is "add a store", not a migration onto a different kind of account.

    Returns the org id, or None if the org couldn't be created (the store is still
    usable on its own, so this never blocks signup).
    """
    org = database.db_org_create(name)
    if not org:
        logger.error("signup_org_create_failed user=%s name=%r", user_id, name)
        return None
    database.db_org_update_subscription(org["id"], plan=plan)
    database.db_org_attach_tenant(tenant["id"], org["id"])
    # "manager" (not viewer): it's their own account — they provision and edit it.
    database.db_org_member_add(user_id, org["id"], "manager")
    return org["id"]


def _unique_client_id(name: str) -> str:
    """Slugify the business name into a stable, unique client_id (lowercase a-z0-9-)."""
    base = re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-")[:40] or "business"
    candidate, n = base, 2
    while database.db_tenant_get_by_client_id(candidate) is not None:
        candidate = f"{base}-{n}"
        n += 1
        if n > 200:
            candidate = f"{base}-{uuid4().hex[:6]}"
            break
    return candidate


@router.post("/api/onboarding/create-business")
def api_create_business(
    req: CreateBusinessRequest,
    request: Request,
    user_id: str = Depends(deps.require_user),
):
    """Self-serve signup: create a *pending* tenant (no number yet) owned by the
    signed-in user. The number is provisioned when Stripe checkout completes. One
    tenant per user — returns the existing one if they already have one."""
    if not runtime.USE_DB:
        raise HTTPException(status_code=503, detail="Database required")
    bv = (req.business_vertical or "salon_chair").strip()
    if bv not in config_service.ALLOWED_BUSINESS_VERTICALS:
        raise HTTPException(status_code=400, detail="Invalid business type")
    # "Use my existing number" must come with the number to forward (display only).
    existing_number = ""
    if req.number_mode == "existing":
        existing_number = _normalize_us_phone_display(req.existing_number)
        if len(re.sub(r"\D", "", existing_number)) < 10:
            raise HTTPException(
                status_code=400,
                detail="Enter your existing business phone number to forward calls from.",
            )
    # One tenant per user — don't create a duplicate.
    existing_ids = database.db_tenant_membership_tenant_ids(user_id)
    if existing_ids:
        existing = database.db_tenant_get_by_id(existing_ids[0])
        if existing:
            # A demo tenant coming back through the form to activate. Record the
            # number choice on the tenant they already have, so checkout provisions
            # the right thing — this is the one answer that survives the sample-data
            # purge (see billing._deactivate_demo_if_needed).
            if existing.get("demo_mode"):
                cid = (existing.get("client_id") or "").strip()
                database.set_request_client_id(cid)
                # They can correct the business name on the way out of the demo;
                # ignoring it here would silently drop what they typed.
                new_name = req.name.strip()
                if new_name and new_name != (existing.get("name") or "").strip():
                    if database.db_tenant_set_name(existing["id"], new_name):
                        existing["name"] = new_name
                raw = config_service._read_raw_client_config(cid) or (
                    config_service._default_client_config_data(cid, req.plan)
                )
                raw["number_mode"] = req.number_mode
                raw["existing_business_number"] = (
                    existing_number if req.number_mode == "existing" else ""
                )
                raw["business_name"] = existing.get("name") or raw.get("business_name") or ""
                raw["name"] = raw["business_name"]
                config_service.save_raw_client_config(cid, raw)
            return {"tenant": existing, "already_existed": True}
    name = req.name.strip()
    client_id = _unique_client_id(name)
    tenant = database.db_tenant_create_pending(client_id, name, req.plan, bv)
    if not tenant:
        raise HTTPException(status_code=409, detail="Could not create business; please try again")
    database.set_request_client_id(client_id)
    cfg = config_service._default_client_config_data(client_id, tenant.get("plan") or req.plan)
    cfg["number_mode"] = req.number_mode
    if req.number_mode == "existing":
        cfg["existing_business_number"] = existing_number
    config_service.save_raw_client_config(client_id, cfg)
    org_id = _create_account_org(user_id, name, tenant, req.plan)
    # Still a member of their first store, so logging in lands them straight in it
    # rather than on a one-row store list.
    database.db_tenant_member_set_single(user_id, tenant["id"])
    deps._clerk_patch_user_tenant_metadata(user_id, tenant["id"])
    deps.audit_log(
        "user",
        "self_serve_business_created",
        actor_id=user_id,
        resource_type="tenant",
        resource_id=tenant["id"],
        client_id=client_id,
        details={"name": name, "plan": req.plan, "vertical": bv, "org_id": org_id},
        request=request,
    )
    return {"tenant": tenant, "org_id": org_id, "already_existed": False}


class StartDemoRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=120)
    business_vertical: str = "salon_chair"


# How long a prospect can explore the seeded dashboard before the demo lapses.
DEMO_WINDOW_DAYS = 14


@router.post("/api/onboarding/start-demo")
def api_start_demo(
    req: StartDemoRequest,
    request: Request,
    user_id: str = Depends(deps.require_user),
):
    """Card-free demo: create a tenant seeded with sample data so a prospect can see
    the product working before paying.

    The tenant deliberately gets NO phone number. That's what makes this safe — the
    voice/SMS webhooks resolve a tenant by the dialed Twilio number, so a tenant
    without one is unreachable and can't burn call minutes. It also means the normal
    7-day trial still applies on activation, since create-checkout-session decides
    `needs_trial` by the absence of a number.

    Access comes from a short billing exemption rather than a fake 'trialing' status,
    so the trial clock only ever starts when a real card does. The exemption also
    grants pro-tier features (see plans.get_plan_limits), which is what we want: the
    demo should show everything.
    """
    if not runtime.USE_DB:
        raise HTTPException(status_code=503, detail="Database required")
    bv = (req.business_vertical or "salon_chair").strip()
    if bv not in config_service.ALLOWED_BUSINESS_VERTICALS:
        raise HTTPException(status_code=400, detail="Invalid business type")
    # One tenant per user — a returning demo user lands back on their own demo.
    existing_ids = database.db_tenant_membership_tenant_ids(user_id)
    if existing_ids:
        existing = database.db_tenant_get_by_id(existing_ids[0])
        if existing:
            return {"tenant": existing, "already_existed": True}
    name = req.name.strip()
    client_id = _unique_client_id(name)
    tenant = database.db_tenant_create_pending(client_id, name, "pro", bv)
    if not tenant:
        raise HTTPException(status_code=409, detail="Could not start demo; please try again")
    database.set_request_client_id(client_id)
    # Flag first, then grant access: if seeding dies halfway, the tenant is still
    # marked demo_mode and its partial data is still purged on activation.
    database.db_tenant_set_demo_mode(tenant["id"], True)
    database.db_tenant_set_billing_exempt(
        tenant["id"], datetime.now(timezone.utc) + timedelta(days=DEMO_WINDOW_DAYS)
    )
    try:
        import demo_seed

        counts = demo_seed.seed_demo_tenant(client_id, name, plan="pro")
    except Exception as e:
        logger.exception("demo_seed_failed client_id=%s", client_id)
        raise deps._server_error("Could not build the demo dashboard", e)
    # A demo is an account too, so it has the same shape as a paid one — nothing has
    # to be restructured when they activate.
    org_id = _create_account_org(user_id, name, tenant, "pro")
    database.db_tenant_member_set_single(user_id, tenant["id"])
    deps._clerk_patch_user_tenant_metadata(user_id, tenant["id"])
    deps.audit_log(
        "user",
        "demo_started",
        actor_id=user_id,
        resource_type="tenant",
        resource_id=tenant["id"],
        client_id=client_id,
        details={"name": name, "vertical": bv, "seeded": counts, "org_id": org_id},
        request=request,
    )
    # Re-read so the response carries demo_mode / billing_exempt_until / org_id.
    fresh = database.db_tenant_get_by_id(tenant["id"]) or tenant
    return {"tenant": fresh, "org_id": org_id, "already_existed": False, "seeded": counts}


@router.post("/api/onboarding/complete")
def api_onboarding_complete(
    tenant: Optional[dict] = Depends(deps.require_active_subscription),
):
    """Mark guided onboarding as completed for this tenant."""
    if not tenant:
        raise HTTPException(status_code=403, detail="Tenant required")
    cid = (tenant.get("client_id") or "").strip()
    if not cid:
        raise HTTPException(status_code=400, detail="client_id missing")
    database.set_request_client_id(cid)
    raw = config_service._read_raw_client_config(cid) or config_service._default_client_config_data(
        cid, tenant.get("plan") or "free"
    )
    raw["onboarding_completed_at"] = datetime.now(timezone.utc).isoformat()
    if runtime.USE_DB:
        if not database.db_tenant_set_business_config(cid, raw):
            raise HTTPException(
                status_code=500, detail="Failed to save onboarding state"
            )
    config_service.save_raw_client_config(cid, raw)
    info = config_service.business_info_for_dashboard(tenant)
    twilio_phone = tenant.get("twilio_phone_number")
    return get_setup_status(
        info_override=info,
        twilio_phone=twilio_phone,
        is_demo=bool(tenant.get("demo_mode")),
    )


@router.patch("/api/business-info")
async def api_update_business_info(
    update: BusinessInfoUpdate,
    request: Request,
    tenant: Optional[dict] = Depends(deps.require_active_subscription),
):
    """Update business config (store info, voice, etc.). Writes to clients/<client_id>/config.json."""
    tid = tenant or {}
    cid = ((tid.get("client_id") or "").strip() or database._client_id()).strip()
    if not cid or cid == "default":
        raise HTTPException(status_code=400, detail="No client context")
    data = config_service._read_raw_client_config(cid)
    if data is None:
        plan = tid.get("plan") or "free"
        if runtime.USE_DB:
            trow = database.db_tenant_get_by_client_id(cid)
            if trow and trow.get("plan"):
                plan = trow.get("plan") or plan
        data = config_service._default_client_config_data(cid, plan)
    before_data = json.loads(json.dumps(data))
    voice_affecting = False
    if update.name is not None:
        data["business_name"] = update.name
        voice_affecting = True
    if update.hours is not None:
        data["hours"] = update.hours
    if update.phone is not None:
        data["phone"] = update.phone
    if update.forwarding_phone is not None:
        data["forwarding_phone"] = update.forwarding_phone
    if update.transfer_takes_message is not None:
        data["transfer_takes_message"] = bool(update.transfer_takes_message)
    if update.quote_prices is not None:
        data["quote_prices"] = bool(update.quote_prices)
    if update.closures is not None:
        import staff_schedule

        data["closures"] = staff_schedule.normalize_date_list(update.closures)
    if update.email is not None:
        data["email"] = update.email
    if update.address is not None:
        data["address"] = update.address
    if update.departments is not None:
        data["departments"] = update.departments
    if update.services is not None:
        data["services"] = config_service._normalize_service_entries(update.services)
        valid_svc = _valid_service_id_set(data["services"])
        if data.get("staff"):
            data["staff"] = [
                {
                    **s,
                    "service_ids": [
                        x for x in (s.get("service_ids") or []) if x in valid_svc
                    ],
                }
                for s in data["staff"]
            ]
    if update.booking_mode is not None:
        # Normalized (unknown -> internal) so a bad value can never silently stop a
        # store booking. Changing this changes what the AI promises callers, so it's
        # voice-affecting: the prompt has to be rebuilt.
        data["booking_mode"] = config_service._normalize_booking_mode(update.booking_mode)
        voice_affecting = True
    if update.booking_provider_name is not None:
        data["booking_provider_name"] = update.booking_provider_name.strip()[:80]
    if update.consult_only_services is not None:
        data["consult_only_services"] = config_service._normalize_str_list(
            update.consult_only_services, 60
        )
        voice_affecting = True
    if update.booking_rules is not None:
        data["booking_rules"] = config_service._normalize_str_list(update.booking_rules, 40)
        voice_affecting = True
    if update.specials is not None:
        data["specials"] = config_service._normalize_special_entries(update.specials)
    if update.reservation_rules is not None:
        data["reservation_rules"] = config_service._normalize_rule_entries(update.reservation_rules)
    if update.menu_link is not None:
        data["menu_link"] = update.menu_link
    if update.greeting is not None:
        data["greeting"] = update.greeting
        voice_affecting = True
    if update.voice is not None:
        data["voice"] = update.voice
        voice_affecting = True
    if update.speed is not None:
        data["speed"] = update.speed
        voice_affecting = True
    if update.receptionist_name is not None:
        data["receptionist_name"] = update.receptionist_name
        voice_affecting = True
    if update.business_type is not None:
        if not (runtime.USE_DB and tid and tid.get("business_vertical")):
            data["business_type"] = update.business_type
    if update.staff is not None:
        from staff_transfers import (
            STAFF_ROSTER_MAX,
            prune_transfer_targets_for_removed_staff,
        )

        new_staff = finalize_staff_records_for_storage(
            update.staff,
            valid_service_ids=_valid_service_id_set(data.get("services")),
        )
        if len(new_staff) > STAFF_ROSTER_MAX:
            raise HTTPException(
                status_code=400,
                detail=f"Staff roster cannot exceed {STAFF_ROSTER_MAX} members. Contact support if you need more.",
            )
        old_ids = {str(s.get("id")) for s in (data.get("staff") or []) if s.get("id")}
        new_ids = {s["id"] for s in new_staff}
        removed_ids = old_ids - new_ids
        data["staff"] = new_staff
        if removed_ids and data.get("transfer_targets"):
            data["transfer_targets"] = prune_transfer_targets_for_removed_staff(
                list(data["transfer_targets"]), removed_ids
            )
    if update.transfer_targets is not None:
        from staff_transfers import (
            TransferTarget,
            finalize_transfer_targets_for_storage,
        )

        tenant_limits = database.db_tenant_get_by_client_id(cid) or tid
        transfer_max = 1
        if tenant_limits and get_plan_limits:
            transfer_max = int(get_plan_limits(tenant_limits).get("transfer_max") or 1)
        try:
            data["transfer_targets"] = finalize_transfer_targets_for_storage(
                update.transfer_targets,
                data.get("staff") or [],
                transfer_max=transfer_max,
            )
        except ValueError as e:
            msg = str(e)
            if msg.startswith("Plan allows"):
                raise HTTPException(status_code=403, detail=msg) from e
            raise HTTPException(status_code=400, detail=msg) from e
    config_service.save_raw_client_config(cid, data)
    if voice_affecting:
        voice_service.invalidate_voice_cache(cid)
        deps.create_tracked_task(
            voice_service._warm_client_voice_cache_async(cid), name=f"warm_voice_cache:{cid}"
        )
    if voice_service._greeting_debug_enabled():
        voice_info(
            "greeting_settings_saved",
            client_id_prefix=cid[:12],
            config_source="database" if runtime.USE_DB else "file",
            fields=[k for k in update.model_dump(exclude_none=True)],
            greeting_len=len(data.get("greeting") or ""),
            voice=data.get("voice"),
            receptionist_set=bool((data.get("receptionist_name") or "").strip()),
            business_name_len=len(data.get("business_name") or data.get("name") or ""),
        )
    changed_fields = [k for k in update.model_dump(exclude_none=True)]
    before_subset = {k: before_data.get(k) for k in changed_fields}
    after_subset = {k: data.get(k) for k in changed_fields}
    deps.audit_log(
        "user",
        "business_info_updated",
        resource_type="config",
        client_id=cid,
        details={
            "fields": changed_fields,
            "before_sha256": _stable_sha256(
                json.dumps(before_subset, sort_keys=True, default=str)
            ),
            "after_sha256": _stable_sha256(
                json.dumps(after_subset, sort_keys=True, default=str)
            ),
        },
        request=request,
    )
    resp_tenant: dict = {**tid, "client_id": cid}
    if "plan" not in resp_tenant or not resp_tenant.get("plan"):
        resp_tenant["plan"] = data.get("plan") or "free"
    resp_tenant.setdefault("twilio_phone_number", tid.get("twilio_phone_number") or "")
    return config_service.business_info_for_dashboard(resp_tenant)
