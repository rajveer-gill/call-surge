"""Business-config loading and normalization (shared service).

Resolves a tenant's business configuration from PostgreSQL (production) or the
on-disk clients/<id>/config.json (local dev), normalizes it to the shape the
voice/SMS code expects, and exposes get_business_info()/get_tts_* helpers.

Pulled out of main.py so routers can use it without importing main. DB calls are
module-qualified (database.X) so they stay patchable in tests.
"""

from __future__ import annotations

import json
import logging
import os
import re
import uuid
from pathlib import Path
from typing import List, Optional

from fastapi import HTTPException

import database
import runtime

logger = logging.getLogger("nuvatra")

# Repo root (parent of backend/) for on-disk client configs — same value main computes.
PROJECT_ROOT = Path(__file__).resolve().parent.parent


ALLOWED_BUSINESS_VERTICALS = frozenset({"salon_chair"})
BUSINESS_VERTICAL_LABELS = {
    "salon_chair": "Salon, barbershop, nails & similar (chair services)",
}


def _normalize_service_entries(raw) -> List[dict]:
    """Migrate legacy string lists to structured service rows."""
    if not raw:
        return []
    out: List[dict] = []
    if isinstance(raw, list) and raw and isinstance(raw[0], dict):
        for s in raw:
            sid = (s.get("id") or "").strip() or str(uuid.uuid4())
            try:
                price = float(s.get("price", 0))
            except (TypeError, ValueError):
                price = 0.0
            try:
                dm = int(s.get("duration_minutes", 30))
            except (TypeError, ValueError):
                dm = 30
            # An add-on rides along with a real service and can never be the whole
            # appointment (conditioners, hot tools, length/master-stylist charges).
            # Defaults False, so every existing service is unchanged.
            is_addon = bool(s.get("is_addon", False))
            out.append(
                {
                    "id": sid,
                    "name": str(s.get("name") or "")[:200],
                    "price": max(0.0, min(price, 999999.0)),
                    # A bookable service needs real time on the calendar, so 5 is the
                    # floor. An add-on legitimately has none of its own — Master Stylist
                    # 5 is a $5 charge, not five minutes — and flooring it there
                    # overwrote the imported 0 and would pad any appointment it joins.
                    "duration_minutes": max(0 if is_addon else 5, min(dm, 480)),
                    "is_addon": is_addon,
                    # The menu section this came from ("Color Services"). Carried through
                    # from the import so staff can be assigned a whole category at once
                    # instead of ticking services one at a time. Free text — every
                    # booking system names these differently.
                    "category": str(s.get("category") or "").strip()[:120],
                    # Which services this add-on may attach to. EMPTY means "any",
                    # which is the sane default — a business that hasn't thought about
                    # it shouldn't have its add-ons silently become unofferable.
                    # e.g. Master Stylist 5 applies to cuts and styling, Master Stylist
                    # 10 to chemical services.
                    "applies_to_service_ids": _normalize_str_list(
                        s.get("applies_to_service_ids"), 100
                    ),
                }
            )
        # Drop references to services that no longer exist, so a deleted service can't
        # leave an add-on pointing at nothing.
        valid_ids = {e["id"] for e in out}
        for e in out:
            e["applies_to_service_ids"] = [
                x for x in e["applies_to_service_ids"] if x in valid_ids and x != e["id"]
            ]
        return out[:100]
    for line in raw if isinstance(raw, list) else []:
        t = str(line).strip()
        if t:
            out.append(
                {
                    "id": str(uuid.uuid4()),
                    "name": t[:200],
                    "price": 0.0,
                    "duration_minutes": 30,
                    "is_addon": False,  # legacy string services are always bookable
                    "category": "",
                    "applies_to_service_ids": [],
                }
            )
    return out[:100]


def _normalize_special_entries(raw) -> List[dict]:
    if not raw:
        return []
    out: List[dict] = []
    if isinstance(raw, list) and raw and isinstance(raw[0], dict):
        for s in raw:
            sid = (s.get("id") or "").strip() or str(uuid.uuid4())
            out.append(
                {
                    "id": sid,
                    "title": str(s.get("title") or "")[:200],
                    "description": str(s.get("description") or "")[:2000],
                    "valid_until": str(s.get("valid_until") or "")[:32],
                }
            )
        return out[:80]
    for line in raw if isinstance(raw, list) else []:
        t = str(line).strip()
        if t:
            out.append(
                {
                    "id": str(uuid.uuid4()),
                    "title": t[:200],
                    "description": "",
                    "valid_until": "",
                }
            )
    return out[:80]


def _normalize_rule_entries(raw) -> List[dict]:
    if not raw:
        return []
    out: List[dict] = []
    if isinstance(raw, list) and raw and isinstance(raw[0], dict):
        for s in raw:
            sid = (s.get("id") or "").strip() or str(uuid.uuid4())
            out.append({"id": sid, "rule_text": str(s.get("rule_text") or "")[:2000]})
        return out[:100]
    for line in raw if isinstance(raw, list) else []:
        t = str(line).strip()
        if t:
            out.append({"id": str(uuid.uuid4()), "rule_text": t[:2000]})
    return out[:100]


# Appointment-calendar ownership. "internal" is the default and the historical
# behavior; a value we don't recognize floors to internal so a typo can never flip a
# store into request-mode and silently stop it booking.
BOOKING_MODES = ("internal", "external")


def _normalize_booking_mode(raw) -> str:
    m = (str(raw or "").strip().lower())
    return m if m in BOOKING_MODES else "internal"


def _normalize_str_list(raw, cap: int) -> List[str]:
    """Trimmed, de-duplicated, capped list of non-empty strings."""
    if not isinstance(raw, list):
        return []
    out: List[str] = []
    seen = set()
    for item in raw:
        s = str(item or "").strip()[:300]
        key = s.lower()
        if s and key not in seen:
            seen.add(key)
            out.append(s)
    return out[:cap]


def _service_key(name: str) -> str:
    """Loose comparison key: lowercase, alphanumerics only. Lets 'Corrective Color
    (charged by the hour)*' match a caller saying 'corrective color'."""
    return re.sub(r"[^a-z0-9]", "", str(name or "").lower())


def service_requires_consult(service_name: str, info: Optional[dict] = None) -> bool:
    """True when this service must not be booked over the phone and needs a human.

    Substring match in both directions so the configured entry ("Corrective Color")
    catches the fuller catalog name and a caller's shorter phrasing alike. Always
    False when the store configured nothing, which is every store by default.
    """
    key = _service_key(service_name)
    if not key:
        return False
    data = info if info is not None else get_business_info()
    for entry in data.get("consult_only_services") or []:
        ek = _service_key(entry)
        if ek and (ek in key or key in ek):
            return True
    return False


def is_addon_service(service_name: str, info: Optional[dict] = None) -> bool:
    """True when the named service is flagged as an add-on (can't stand alone)."""
    key = _service_key(service_name)
    if not key:
        return False
    data = info if info is not None else get_business_info()
    for s in _normalize_service_entries(data.get("services") or []):
        if s.get("is_addon") and _service_key(s.get("name")) == key:
            return True
    return False


def is_external_booking(info: Optional[dict] = None) -> bool:
    """True when this store's real calendar lives in another system (e.g. Zenoti), so
    the AI produces requests instead of booking. False (internal) for everyone by
    default — this is the gate that keeps request-mode off for every other customer."""
    data = info if info is not None else get_business_info()
    return _normalize_booking_mode(data.get("booking_mode")) == "external"


def _config_data_to_business_info(data: dict) -> dict:
    """Normalize raw config.json / DB business_config dict to get_business_info() shape."""
    forwarding = data.get("forwarding_phone") or ""
    if not forwarding and data.get("locations"):
        forwarding = data["locations"][0].get("forwarding_phone", "")
    _departments = data.get("departments")
    if not isinstance(_departments, list):
        _departments = []
    return {
        "name": data.get("business_name") or data.get("name") or "",
        "hours": data.get("hours", ""),
        "phone": data.get("phone", ""),
        "forwarding_phone": forwarding,
        "email": data.get("email", ""),
        "address": data.get("address", ""),
        "departments": _departments,
        "menu_link": data.get("menu_link", ""),
        "services": _normalize_service_entries(data.get("services", [])),
        "specials": _normalize_special_entries(data.get("specials", [])),
        "reservation_rules": _normalize_rule_entries(data.get("reservation_rules", [])),
        "staff": data.get("staff", []),
        "transfer_targets": data.get("transfer_targets", []),
        # Absent means "quote prices", which is how every tenant behaved before this
        # existed. Carried explicitly because this mapping is key-by-key: a setting
        # the PATCH writes but this does not read is stored and never applied, which
        # is exactly what happened the first time.
        "quote_prices": data.get("quote_prices", True),
        "locations": data.get("locations", []),
        "greeting": data.get("greeting", ""),
        "plan": data.get("plan", "starter"),
        "voice": data.get("voice", "fable"),
        "speed": (
            float(data.get("speed", 1.0)) if data.get("speed") is not None else 1.0
        ),
        "receptionist_name": data.get("receptionist_name", ""),
        "business_type": data.get("business_type", ""),
        # How the tenant's AI line is reached: "new" = the provisioned Twilio number IS
        # their published number; "existing" = they keep their own number and forward
        # calls to the (hidden) Twilio line. existing_business_number is the number
        # customers actually dial in "existing" mode — display-only, used for forwarding
        # instructions in onboarding/Settings.
        "number_mode": data.get("number_mode") or "new",
        "existing_business_number": data.get("existing_business_number", ""),
        # Dates (YYYY-MM-DD) the whole shop is closed (holidays, etc.).
        "closures": data.get("closures") or [],
        # When True, the business has no separate transfer line (e.g. their published
        # number IS the AI line in "existing" mode). Instead of dialing a real person,
        # the AI takes a message so the team can call back. This is one of two ways to
        # satisfy human_handoff_configured (the other is a real forwarding_phone).
        "transfer_takes_message": bool(data.get("transfer_takes_message", False)),
        # Who owns the appointment calendar.
        #   "internal" (default) — WE are the calendar; the AI books directly into it.
        #     Every existing customer is this, because it's the default.
        #   "external" — the real calendar lives in another system (e.g. Zenoti) that we
        #     can't write to. The AI takes a *request* the staff approve and enter on
        #     their side; it never claims or reserves a slot here. See
        #     _create_appointment_from_booking and the receptionist prompt.
        "booking_mode": _normalize_booking_mode(data.get("booking_mode")),
        # Display name of that external system ("Zenoti"), for prompt/UI copy only.
        "booking_provider_name": (data.get("booking_provider_name") or "").strip(),
        # Services this business will not let anyone book over the phone — they need a
        # human consult first (e.g. corrective color, where nobody knows the scope or
        # who's qualified until a stylist talks to the guest). Matched loosely by name.
        # EMPTY by default: no store gets this behavior unless it's configured.
        "consult_only_services": _normalize_str_list(data.get("consult_only_services"), 60),
        # Free-text booking policies injected into the receptionist prompt, e.g.
        # "Fashion colors (blue, pink, purple) must be booked as Vivid color."
        # EMPTY by default.
        "booking_rules": _normalize_str_list(data.get("booking_rules"), 40),
    }


def client_config_source(cid: str) -> str:
    """Where business config was loaded from: database, file, or none."""
    c = (cid or "").strip()
    if not c:
        return "none"
    if runtime.USE_DB:
        try:
            if database.db_tenant_get_business_config(c):
                return "database"
        except Exception:
            pass
    config_path = PROJECT_ROOT / "clients" / c / "config.json"
    if config_path.exists():
        return "file"
    return "none"


def _read_raw_client_config(cid: str) -> Optional[dict]:
    """Load raw config from PostgreSQL (production) then clients/<cid>/config.json (local dev)."""
    raw = None
    if runtime.USE_DB:
        try:
            raw = database.db_tenant_get_business_config(cid)
        except Exception as e:
            logger.warning("business_config db read failed client_id=%s: %s", cid, e)
    if raw is not None:
        return raw
    config_path = PROJECT_ROOT / "clients" / cid / "config.json"
    if not config_path.exists():
        logger.debug("client_config_missing path=%s client_id=%s", config_path, cid)
        return None
    try:
        with open(config_path, "r", encoding="utf-8") as f:
            raw = json.load(f)
        if runtime.USE_DB and raw:
            try:
                database.db_tenant_set_business_config(cid, raw)
            except Exception as e:
                logger.warning(
                    "business_config file->db migrate failed client_id=%s: %s", cid, e
                )
        return raw
    except Exception as e:
        logger.warning("Failed to read client config file client_id=%s: %s", cid, e)
        return None


def save_raw_client_config(cid: str, data: dict) -> None:
    """Persist business config to DB (required on Render) and optionally to clients/<cid>/config.json."""
    db_ok = True
    if runtime.USE_DB:
        db_ok = bool(database.db_tenant_set_business_config(cid, data))
        if not db_ok:
            raise HTTPException(
                status_code=500, detail="Failed to save settings to database"
            )
    config_path = PROJECT_ROOT / "clients" / cid / "config.json"
    try:
        config_path.parent.mkdir(parents=True, exist_ok=True)
        with open(config_path, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
    except Exception as e:
        if not runtime.USE_DB:
            raise HTTPException(
                status_code=500, detail=f"Failed to write config: {e}"
            ) from e
        logger.warning(
            "config file write failed client_id=%s (saved to db): %s", cid, e
        )


def load_client_config(client_id: Optional[str] = None):
    """Load business config for client_id (DB first, then on-disk file)."""
    cid = (client_id or database._client_id()).strip()
    if not cid:
        return None
    raw = _read_raw_client_config(cid)
    if not raw:
        return None
    try:
        info = _config_data_to_business_info(raw)
        print(f"Loaded client config: {cid} ({info['name']})")
        return info
    except Exception as e:
        print(f"WARNING: Failed to load client config: {e}")
        return None


# Business configuration: loaded per-request (multi-tenant) or at startup (single-tenant).
# Single-tenant / no-DB fallback only — do not put global env (e.g. BUSINESS_FORWARDING_PHONE) here
# or it will appear as every tenant’s “forwarding” in the UI when config is missing.
_DEMO_BUSINESS_INFO = {
    "name": "",
    "hours": "",
    "phone": "",
    "forwarding_phone": "",
    "email": "",
    "address": "",
    "departments": [],
    "menu_link": "",
    "services": [],
    "specials": [],
    "reservation_rules": [],
    "staff": [],
    "transfer_targets": [],
    "locations": [],
    "greeting": "",
    "plan": "starter",
    "voice": "fable",
    "speed": 1.0,
    "receptionist_name": "",
    "business_type": "",
}


def _minimal_business_info_from_tenant_dict(tenant: dict) -> dict:
    """Empty user-edited fields; Twilio line from tenant when no on-disk config (e.g. Render has no clients/)."""
    plan = tenant.get("plan") or "starter"
    bv = (tenant.get("business_vertical") or "salon_chair").strip()
    return {
        "name": (tenant.get("name") or "").strip(),
        "hours": "",
        "phone": (tenant.get("twilio_phone_number") or "").strip(),
        "forwarding_phone": "",
        "email": "",
        "address": "",
        "departments": [],
        "menu_link": "",
        "services": [],
        "specials": [],
        "reservation_rules": [],
        "staff": [],
        "transfer_targets": [],
        "locations": [],
        "greeting": "",
        "plan": plan,
        "voice": "fable",
        "speed": 1.0,
        "receptionist_name": "",
        "business_type": "",
        "business_vertical": bv,
        "business_vertical_label": BUSINESS_VERTICAL_LABELS.get(bv, bv),
    }


def _default_business_info_for_tenant() -> Optional[dict]:
    """Build minimal business info from the tenant DB record when no config file exists."""
    if not runtime.USE_DB:
        return None
    cid = database._client_id()
    if not cid or cid == "default":
        return None
    try:
        from database import _get_conn

        conn = _get_conn()
        if not conn:
            return None
        cur = conn.cursor()
        cur.execute(
            "SELECT name, twilio_phone_number, plan, business_vertical FROM tenants WHERE client_id = %s",
            (cid,),
        )
        row = cur.fetchone()
        cur.close()
        if not row:
            return None
        return _minimal_business_info_from_tenant_dict(
            {
                "twilio_phone_number": row[1] or "",
                "plan": row[2] or "starter",
                "business_vertical": row[3] if len(row) > 3 else "salon_chair",
            }
        )
    except Exception:
        return None


def business_info_for_dashboard(tenant: Optional[dict]) -> dict:
    """Settings / business-info API: never use _DEMO when a real tenant is authenticated."""
    if not tenant:
        tenant = {}
    cid = (tenant.get("client_id") or "").strip()
    if cid:
        cfg = load_client_config(cid)
        if cfg:
            out = dict(cfg)
            if not (out.get("phone") or "").strip():
                out["phone"] = (tenant.get("twilio_phone_number") or "").strip()
            if not (out.get("name") or "").strip():
                out["name"] = (tenant.get("name") or "").strip()
            bv = (tenant.get("business_vertical") or "salon_chair").strip()
            out["business_vertical"] = bv
            out["business_vertical_label"] = BUSINESS_VERTICAL_LABELS.get(bv, bv)
            out["business_type_admin_locked"] = True
            return out
    out = _minimal_business_info_from_tenant_dict(tenant)
    bv = (tenant.get("business_vertical") or "salon_chair").strip()
    out["business_vertical"] = bv
    out["business_vertical_label"] = BUSINESS_VERTICAL_LABELS.get(bv, bv)
    out["business_type_admin_locked"] = bool(cid)
    return out


def _default_client_config_data(client_id: str, plan: str = "free") -> dict:
    """Seed clients/<client_id>/config.json from template when present."""
    template_path = PROJECT_ROOT / "clients" / "template" / "config.json"
    if template_path.is_file():
        try:
            base = json.loads(template_path.read_text(encoding="utf-8"))
            if isinstance(base, dict):
                out = dict(base)
                out["client_id"] = client_id
                out["plan"] = plan
                # The template ships placeholder example staff (Manager/Reception) for
                # human reference; a freshly-created tenant must start with an empty
                # roster so the owner adds their own team, not inherit the placeholders.
                out["staff"] = []
                return out
        except Exception as e:
            logger.warning("template_config_load_failed err=%s", e)
    return {
        "client_id": client_id,
        "business_name": "",
        "phone": "",
        "plan": plan,
        "hours": "",
        "forwarding_phone": "",
        "email": "",
        "address": "",
        "departments": [],
        "services": [],
        "specials": [],
        "reservation_rules": [],
        "menu_link": "",
        "greeting": "",
        "staff": [],
        "transfer_targets": [],
        "locations": [],
        "voice": "fable",
        "speed": 1.0,
        "receptionist_name": "",
        "business_type": "",
    }


def get_business_info() -> dict:
    """Get business config for current request (multi-tenant) or env CLIENT_ID (single-tenant)."""
    cfg = load_client_config()
    if cfg:
        out = dict(cfg)
        if not out.get("phone") and runtime.USE_DB:
            cid = database._client_id()
            if cid:
                tenant = database.db_tenant_get_by_client_id(cid)
                if tenant:
                    out["phone"] = tenant.get("twilio_phone_number") or ""
    else:
        tenant_info = _default_business_info_for_tenant()
        if tenant_info:
            out = dict(tenant_info)
        else:
            out = dict(_DEMO_BUSINESS_INFO)
    if runtime.USE_DB:
        cid = database._client_id()
        if cid:
            t = database.db_tenant_get_by_client_id(cid)
            if t:
                bv = (t.get("business_vertical") or "salon_chair").strip()
                out["business_vertical"] = bv
                out["business_vertical_label"] = BUSINESS_VERTICAL_LABELS.get(bv, bv)
                if not (out.get("name") or "").strip():
                    out["name"] = (t.get("name") or "").strip()
    return out


def get_tts_voice() -> str:
    """Voice for TTS (phone/SMS). From business config or default fable."""
    return get_business_info().get("voice", "fable") or "fable"


def get_tts_speed() -> float:
    """Speaking speed for TTS (OpenAI allows 0.25–4.0). From business config or default 1.0."""
    try:
        s = float(get_business_info().get("speed", 1.0))
        return max(0.25, min(4.0, s))
    except (TypeError, ValueError):
        return 1.0


# Default TTS model. tts-1-hd is stable and polished — on real calls it sounded more
# natural, positive, and peppy than the steerable gpt-4o-mini-tts for short receptionist
# lines (chosen by ear, not theory). Opt into the steerable model per-env with
# VOICE_TTS_MODEL=gpt-4o-mini-tts (its `instructions` steering only applies to gpt-* models).
_DEFAULT_TTS_MODEL = "tts-1-hd"


def get_tts_model() -> str:
    """TTS model for phone/preview synthesis. Env-overridable for rollout/rollback."""
    return (os.getenv("VOICE_TTS_MODEL") or _DEFAULT_TTS_MODEL).strip() or _DEFAULT_TTS_MODEL


def voice_streaming_enabled() -> bool:
    """Option C: bidirectional media streaming (persistent stream, outbound audio over the
    websocket, barge-in) instead of the batch <Play> path. Env-gated + default OFF so the
    working <Play> path is untouched and rollback is instant (unset the env)."""
    return (os.getenv("VOICE_STREAMING_TTS") or "").strip().lower() in ("1", "true", "yes", "on")


# Delivery style passed to steerable TTS (gpt-4o-mini-tts). Keyed by business vertical so
# each vertical can sound right; only salon_chair is live today (see ALLOWED_BUSINESS_VERTICALS
# above), so this is one good default plus a hook for future verticals. Ignored by tts-1/hd.
_TTS_INSTRUCTIONS_BY_VERTICAL = {
    "salon_chair": (
        "Voice: a friendly, upbeat salon receptionist — natural and personable, not "
        "exaggerated. Pace: a normal, brisk conversational speed; do NOT slow down, "
        "draw out words, or over-enunciate. Tone: pleasant and helpful with even, "
        "consistent energy and a natural pitch. Sound like a real receptionist answering "
        "the phone — not a narrator or voice actor."
    ),
}
_DEFAULT_TTS_INSTRUCTIONS = _TTS_INSTRUCTIONS_BY_VERTICAL["salon_chair"]


def get_tts_instructions() -> str:
    """Steering instructions for the current tenant's vertical (empty to skip steering)."""
    vertical = (get_business_info().get("business_vertical") or "").strip()
    return _TTS_INSTRUCTIONS_BY_VERTICAL.get(vertical, _DEFAULT_TTS_INSTRUCTIONS)


def get_client_data_dir() -> Optional[Path]:
    """Path to the client data directory (call_log, caller_memory on local/file mode).
    None when there's no real client_id."""
    cid = database._client_id()
    if not cid or cid == "default":
        return None
    d = PROJECT_ROOT / "clients" / cid
    d.mkdir(parents=True, exist_ok=True)
    return d


# ===== staff-roster / voice-readiness checks (business-config derived) =====


def get_staff_phone_by_name(name: str) -> Optional[str]:
    """Return E.164 for a plan-authorized transfer destination by name (not the full staff roster)."""
    from staff_transfers import get_transfer_phone_by_name

    return get_transfer_phone_by_name(name, get_business_info())


def staff_on_roster(info: Optional[dict] = None) -> List[dict]:
    """Staff rows with a display name (required for calendar booking / AI roster)."""
    data = info if info is not None else get_business_info()
    out: List[dict] = []
    for s in data.get("staff") or []:
        if not isinstance(s, dict):
            continue
        name = (s.get("name") or "").strip()
        if name:
            out.append(s)
    return out


def staff_roster_ready_for_booking(info: Optional[dict] = None) -> bool:
    """True when at least one named team member is on the roster."""
    return len(staff_on_roster(info)) >= 1


def forwarding_phone_ready(info: Optional[dict] = None) -> bool:
    """True when store forwarding phone is configured for live handoffs."""
    data = info if info is not None else get_business_info()
    return bool((data.get("forwarding_phone") or "").strip())


def transfer_takes_message(info: Optional[dict] = None) -> bool:
    """True when the business opted to take a message instead of transferring to a real person.

    Used when the business has only one number (their published line forwards to the AI),
    so there is no separate human to dial—the AI captures a message for callback instead.
    """
    data = info if info is not None else get_business_info()
    return bool(data.get("transfer_takes_message", False))


def quotes_prices(info: Optional[dict] = None) -> bool:
    """True when the AI may say what a service costs.

    Some businesses do not quote over the phone at all — the price depends on the
    stylist, hair length, or what they find in the chair, and a number said out loud
    becomes a promise the shop has to honour. Defaults to True so nothing changes for
    anyone who has not thought about it.
    """
    data = info if info is not None else get_business_info()
    v = data.get("quote_prices")
    return True if v is None else bool(v)


def human_handoff_configured(info: Optional[dict] = None) -> bool:
    """True when the AI has a way to hand a caller off to a human.

    Satisfied either by a real store/forwarding phone to dial, OR by the
    "take a message instead" toggle (capture a message for callback).
    """
    data = info if info is not None else get_business_info()
    return forwarding_phone_ready(data) or transfer_takes_message(data)


def services_configured(info: Optional[dict] = None) -> bool:
    """True when at least one service is configured in the menu.

    Required for calls: with no services the AI has nothing real to offer and tends to
    invent them, so it should not take calls until the owner adds a service menu.
    """
    data = info if info is not None else get_business_info()
    return len(_normalize_service_entries(data.get("services") or [])) >= 1


def voice_receptionist_ready(info: Optional[dict] = None) -> bool:
    """True when the team roster, a human-handoff path, and a service menu are configured for full AI receptionist calls."""
    return (
        staff_roster_ready_for_booking(info)
        and human_handoff_configured(info)
        and services_configured(info)
    )


def mark_forwarding_verified_if_match(client_id: str, forwarded_from: str) -> bool:
    """Confirm a 'bring your own number' tenant is forwarding correctly.

    On a forwarded inbound call Twilio sends ``ForwardedFrom`` (the number that
    forwarded the call). When the tenant is in 'existing' mode and that matches the
    configured business number, stamp ``forwarding_verified_at``. Cheap + idempotent:
    only loads/writes config when a ForwardedFrom is present (forwarded calls only)
    and only the first time. Returns True if it newly verified.
    """
    cid = (client_id or "").strip()
    ff_digits = "".join(c for c in (forwarded_from or "") if c.isdigit())
    if not cid or len(ff_digits) < 10:
        return False
    cfg = load_client_config(cid)
    if not cfg or (cfg.get("number_mode") or "new") != "existing":
        return False
    if (cfg.get("forwarding_verified_at") or "").strip():
        return False  # already verified
    existing_digits = "".join(c for c in (cfg.get("existing_business_number") or "") if c.isdigit())
    # Match on the last 10 digits so a leading country code never blocks it.
    if not existing_digits or existing_digits[-10:] != ff_digits[-10:]:
        return False
    from datetime import datetime, timezone

    cfg["forwarding_verified_at"] = datetime.now(timezone.utc).isoformat()
    try:
        save_raw_client_config(cid, cfg)
    except Exception:
        return False
    return True
