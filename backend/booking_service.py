"""Booking service: stateless appointment primitives.

Duration/time-formatting/service-canonicalization/staff-name/validation helpers
lifted out of main.py (strangler-fig refactor). Pure functions — no runtime or DB
state; depend only on config_service + stdlib. The stateful slot-storage/calendar
engine (load/save/cache/reserve/release/merge/reconcile) is a separate follow-up.
"""

from __future__ import annotations

import os
import re
from typing import List, Optional

from fastapi import HTTPException

import logging

import config_service
import database
import runtime
import json
from datetime import date as _date, datetime, timezone
from observability import system_debug, system_info

logger = logging.getLogger("nuvatra")

DEFAULT_SLOT_DURATION_MINUTES = 30

def _service_duration_minutes_for_reason(
    reason: Optional[str], info: Optional[dict] = None
) -> Optional[int]:
    """Total configured minutes for everything named in reason; None when nothing matches.

    A reason can name more than one service ("Haircut + All Over Color"), so the block
    on the calendar is the SUM of what was booked. Reserving 30 minutes for two hours
    of work is how a stylist ends up double-booked on their own day.
    """
    raw = (reason or "").strip()
    if not raw or raw == "—":
        return None
    biz = info or config_service.get_business_info()
    chosen, _ = normalize_service_choices_for_booking(raw, biz)
    if not chosen:
        return None
    by_name = {
        (s.get("name") or "").strip().lower(): s
        for s in config_service._normalize_service_entries(biz.get("services") or [])
        if (s.get("name") or "").strip()
    }
    total = 0
    matched = False
    for name in chosen:
        svc = by_name.get(name.strip().lower())
        if svc is None:
            continue
        matched = True
        raw_minutes = svc.get("duration_minutes")
        # `or DEFAULT` would turn a deliberate 0 (an add-on that takes no time of its
        # own) into half an hour of padding on every appointment it joins.
        if raw_minutes in (None, ""):
            raw_minutes = DEFAULT_SLOT_DURATION_MINUTES
        try:
            minutes = int(raw_minutes)
        except (TypeError, ValueError):
            minutes = DEFAULT_SLOT_DURATION_MINUTES
        # An add-on legitimately carries 0 minutes of its own (a $5 master-stylist
        # charge is not five minutes), so only the total gets a floor.
        total += max(0, min(minutes, 480))
    if not matched:
        return None
    return max(5, min(total, 480))

def _booking_duration_minutes(
    booking: dict, info: Optional[dict] = None
) -> int:
    dm = _service_duration_minutes_for_reason(booking.get("reason"), info)
    return dm if dm is not None else DEFAULT_SLOT_DURATION_MINUTES

def _appointment_duration_minutes(
    apt: dict, info: Optional[dict] = None
) -> int:
    dm = _service_duration_minutes_for_reason(apt.get("reason"), info)
    return dm if dm is not None else DEFAULT_SLOT_DURATION_MINUTES

def _duration_minutes_for_appointment(
    apt: dict,
    slots_by_appointment_id: dict[int, int],
    services: Optional[List[dict]] = None,
) -> int:
    """Resolve block length for calendar display (service menu, then booked_slots, else default)."""
    info = {"services": services} if services is not None else None
    svc_dm = _service_duration_minutes_for_reason(apt.get("reason"), info)
    if svc_dm is not None:
        return svc_dm
    try:
        aid = int(apt.get("id") or 0)
    except (TypeError, ValueError):
        aid = 0
    if aid and aid in slots_by_appointment_id:
        return max(5, min(int(slots_by_appointment_id[aid]), 480))
    return DEFAULT_SLOT_DURATION_MINUTES

def _time_to_minutes(t: str) -> int:
    """Parse time string (e.g. '10', '10:00', '2:00 PM') to minutes since midnight."""
    if not t:
        return 0
    raw = (t or "").strip()
    upper = raw.upper()
    meridian: Optional[str] = None
    if re.search(r"\bP\.?\s*M\.?\b", upper) or re.search(r"\bPM\b", upper):
        meridian = "pm"
    elif re.search(r"\bA\.?\s*M\.?\b", upper) or re.search(r"\bAM\b", upper):
        meridian = "am"
    cleaned = re.sub(r"(?i)\s*(a\.?\s*m\.?|p\.?\s*m\.?)\s*$", "", raw).strip()
    cleaned = re.sub(r"(?i)\s*(am|pm)\s*$", "", cleaned).strip()
    parts = cleaned.split(":")
    h = 0
    m = 0
    try:
        if parts:
            h = int("".join(c for c in parts[0] if c.isdigit()) or "0")
        if len(parts) > 1:
            m = int("".join(c for c in parts[1] if c.isdigit()) or "0")
    except (ValueError, TypeError):
        pass
    if meridian == "pm":
        if h != 12:
            h += 12
    elif meridian == "am":
        if h == 12:
            h = 0
    elif meridian is None and cleaned:
        # Salon-style times without AM/PM: 9–11 → AM, 1–8 → PM, 12 → noon
        if h == 12:
            pass
        elif 1 <= h <= 8:
            h += 12
    return h * 60 + m

def _normalize_time_to_hhmm(t: str) -> str:
    """Normalize time to HH:MM (e.g. '10' -> '10:00', '10:00 AM' -> '10:00')."""
    if not t or not (t or "").strip():
        return ""
    mins = _time_to_minutes(t)
    h, m = divmod(mins, 60)
    return f"{h:02d}:{m:02d}"

def _format_appointment_details_confirmation_sms(apt: dict) -> str:
    """Full appointment summary for SMS — used after voice booking and when customer updates details."""
    phone_display = (apt.get("phone") or "").strip() or "Not provided"
    time_display = _hhmm_to_ampm(apt.get("time") or "") or (apt.get("time") or "")
    service = (apt.get("reason") or "").strip() or "—"
    status = (apt.get("status") or "").strip()
    customer = (apt.get("name") or "").strip()
    stylist = _staff_display_name_for_appointment(apt)
    if customer:
        customer_line = f"Customer: {customer}"
    else:
        customer_line = (
            "Customer: Not on file yet - reply with your name if we should update it."
        )
    stylist_line = f"Stylist: {stylist}\n" if stylist else ""
    if status == "pending_customer":
        intro = "Here's what we have for you. The time is NOT locked in until you text back YES or CONFIRM:"
        footer = (
            "Reply YES or CONFIRM only when this looks exactly right - that reserves the time and sends this to the store. "
            "You can also reply with changes.\n\n"
        )
    elif status == "pending_review":
        # Request mode: the real calendar is elsewhere, so nothing is booked yet and the
        # customer must not be told otherwise. The old wording fell through to "your
        # updated appointment info on file", which reads like a confirmation for someone
        # whose request hasn't even been looked at.
        intro = (
            "APPOINTMENT REQUEST - nothing is booked yet.\n"
            "This is a request we've passed to the salon - no time is held for you and "
            "they'll confirm it with you. Here's what we sent them:"
        )
        footer = "Reply if you'd like to change anything before they confirm.\n\n"
    else:
        intro = "Here's your updated appointment info on file:"
        footer = "Reply if anything still needs to change.\n\n"
    return (
        f"Hey! {intro}\n"
        f"{customer_line}\n"
        f"{stylist_line}"
        f"Phone: {phone_display}\n"
        f"Date: {apt.get('date', '')}\n"
        f"Time: {time_display}\n"
        f"Service: {service}\n\n"
        f"{footer}"
        f"Msg & data rates may apply. Reply STOP to opt out."
    )

def _hhmm_to_ampm(hhmm: str) -> str:
    """Format HH:MM as 12-hour AM/PM (e.g. '13:00' -> '1:00 PM', '09:00' -> '9:00 AM')."""
    if not hhmm or not (hhmm or "").strip():
        return hhmm or ""
    normalized = _normalize_time_to_hhmm(hhmm.strip())
    if not normalized:
        return hhmm
    parts = normalized.split(":")
    try:
        h, m = int(parts[0]), int(parts[1]) if len(parts) > 1 else 0
    except (ValueError, IndexError):
        return hhmm
    if h == 0:
        return f"12:{m:02d} AM"
    if h < 12:
        return f"{h}:{m:02d} AM"
    if h == 12:
        return f"12:{m:02d} PM"
    return f"{h - 12}:{m:02d} PM"


def _hourly_slots_for_date(info: dict, day) -> List[str]:
    """Hourly HH:MM open-time suggestions within the business's configured hours for
    `day`. Empty when the shop is closed that day. Falls back to 9 AM–5 PM when hours
    are unparseable or the shop is open 24/7 (so we never suggest 3 AM)."""
    fallback = [f"{h:02d}:00" for h in range(9, 17)]
    try:
        import business_hours

        slot = business_hours.day_slot_for_date(info, day)
        if slot.closed:
            return []
        if business_hours.is_open_247(slot):
            return fallback
        open_min = business_hours.time_to_minutes(slot.open)
        close_min = business_hours.time_to_minutes(slot.close)
        if open_min < 0 or close_min < 0 or close_min <= open_min:
            return fallback
        start_h = open_min // 60
        last_h = (close_min - 1) // 60  # last hour you can start before closing
        return [f"{h:02d}:00" for h in range(start_h, last_h + 1)]
    except Exception:
        return fallback


def _hours_phrase_for_date(info: dict, day) -> str:
    """Human phrase for a day's open window, e.g. '9 AM–5 PM'. Empty when closed/unknown."""
    try:
        import business_hours

        slot = business_hours.day_slot_for_date(info, day)
        if slot.closed or business_hours.is_open_247(slot):
            return ""
        return f"{_hhmm_to_ampm(slot.open)}–{_hhmm_to_ampm(slot.close)}"
    except Exception:
        return ""

def _staff_display_name_for_appointment(apt: dict, info: Optional[dict] = None) -> str:
    sid = (apt.get("staff_id") or "").strip()
    if not sid:
        return ""
    for s in (info or config_service.get_business_info()).get("staff") or []:
        if (s.get("id") or "").strip() == sid:
            return (s.get("name") or "").strip()
    return ""

SERVICE_JOINER = " + "


def normalize_service_choices_for_booking(
    reason_raw: Optional[str], info: Optional[dict] = None
) -> tuple[List[str], bool]:
    """Every menu service named in the reason field, in the order the caller said them.

    Returns (canonical_names, service_required).

    One appointment is regularly more than one service — "a haircut and an all-over
    color", "a cut, and can you add a highlight". The single-service version of this
    matched the first menu entry it found and returned it alone, so the second service
    was dropped on the floor: the AI said it would add the highlight, the request that
    reached the salon said Haircut, and nobody knew until the guest was in the chair.

    Longest names are matched first so "Color" inside "All Over Color" cannot claim
    the text that belongs to the longer entry.
    """
    biz = info or config_service.get_business_info()
    services = config_service._normalize_service_entries((biz or {}).get("services") or [])
    reason = (reason_raw or "").strip()
    if not services:
        return ([reason] if reason and reason != "—" else []), False
    if not reason or reason == "—":
        return [], True
    reason_l = reason.lower()
    names = sorted(
        ((s.get("name") or "").strip() for s in services if (s.get("name") or "").strip()),
        key=len,
        reverse=True,
    )
    # Claim each stretch of the reason text once, so an entry that overlaps a longer
    # one already matched cannot match the same characters again.
    claimed = [False] * len(reason_l)
    found: list[tuple[int, str]] = []
    for nm in names:
        nml = nm.lower()
        start = reason_l.find(nml)
        while start != -1:
            if not any(claimed[start : start + len(nml)]):
                for i in range(start, start + len(nml)):
                    claimed[i] = True
                found.append((start, nm))
                break
            start = reason_l.find(nml, start + 1)
    if found:
        return [nm for _, nm in sorted(found)], True
    # The reason is a fragment of a menu name ("cut" for "Long Cut") rather than the
    # other way round. Only when exactly ONE menu entry contains it: a word covering
    # several ("highlight" where the menu lists mini, full, partial, cap) has to be
    # asked about, not guessed at. It used to take the longest match, so a caller who
    # said "a highlight" was booked for a partial highlight they never chose.
    partial = [nm for nm in names if reason_l in nm.lower()]
    if len(partial) == 1:
        return [partial[0]], True
    return [], True


# Words too generic to pin a service on their own.
_SERVICE_WORD_STOPWORDS = frozenset({"and", "the", "with", "full", "mini", "cap", "partial"})


def service_candidates_needing_clarification(
    reason_raw: Optional[str], info: Optional[dict] = None
) -> List[str]:
    """Menu services a caller half-named and has to choose between.

    Gill Salons' own booking policy says it: "A highlight could be any of the following
    services Mini highlight, full highlight, partial highlight, cap highlight or
    balayage, ask for clarification." The matcher could not do that — "a haircut and a
    highlight" matched Haircut exactly, left "highlight" matching nothing on its own,
    and filed a haircut with the highlight silently gone.

    So whatever the exact matcher did NOT claim is checked for words that belong to
    menu entries it did not choose. Two or more, and the caller is asked which.
    """
    biz = info or config_service.get_business_info()
    services = config_service._normalize_service_entries((biz or {}).get("services") or [])
    reason = (reason_raw or "").strip()
    if not services or not reason or reason == "—":
        return []
    chosen, _ = normalize_service_choices_for_booking(reason, biz)
    chosen_l = {c.lower() for c in chosen}
    leftover = reason.lower()
    for nm in chosen:
        leftover = leftover.replace(nm.lower(), " ")
    leftover_words = {w for w in re.findall(r"[a-z]+", leftover) if len(w) >= 4}
    leftover_words -= _SERVICE_WORD_STOPWORDS
    if not leftover_words:
        return []
    candidates: List[str] = []
    for s in services:
        nm = (s.get("name") or "").strip()
        if not nm or nm.lower() in chosen_l:
            continue
        words = {w for w in re.findall(r"[a-z]+", nm.lower()) if len(w) >= 4}
        words -= _SERVICE_WORD_STOPWORDS
        if words & leftover_words:
            candidates.append(nm)
    return candidates if len(candidates) >= 2 else []


def primary_service_name(names: List[str], info: Optional[dict] = None) -> Optional[str]:
    """The service the appointment is really for — the first that is not an add-on.

    "Highlight and a haircut" is a haircut appointment with a highlight on it, whichever
    order the caller happened to say them in.
    """
    biz = info or config_service.get_business_info()
    for nm in names:
        if nm and not config_service.is_addon_service(nm, biz):
            return nm
    return names[0] if names else None


def _normalize_service_choice_for_booking(
    reason_raw: Optional[str], info: Optional[dict] = None
) -> tuple[Optional[str], bool]:
    """Return (canonical_service_name_or_none, service_required).

    The PRIMARY service only — callers that care about every service the guest asked
    for use normalize_service_choices_for_booking.
    """
    chosen, required = normalize_service_choices_for_booking(reason_raw, info)
    return primary_service_name(chosen, info), required


def format_service_choices(names: List[str]) -> str:
    """The reason field as it is stored and shown: 'Haircut + All Over Color'."""
    return SERVICE_JOINER.join(n.strip() for n in names if (n or "").strip())

def _optional_staff_id_validated(raw: Optional[str]) -> Optional[str]:
    """If staff_id is set, ensure it matches a row in this tenant's staff list."""
    sid = (raw or "").strip()
    if not sid:
        return None
    for s in config_service.get_business_info().get("staff") or []:
        if (s.get("id") or "").strip() == sid:
            return sid
    raise HTTPException(status_code=400, detail="Invalid staff_id for this business.")

def _appointment_email_enabled() -> bool:
    """Off by default; set APPOINTMENT_EMAIL_ENABLED=1 to send Resend/SMTP confirmations."""
    return (os.getenv("APPOINTMENT_EMAIL_ENABLED") or "").strip().lower() in (
        "1",
        "true",
        "yes",
        "on",
    )


# ===== stateful slot/calendar engine (cut 2) =====

_CALENDAR_HOLDING_STATUSES = frozenset(
    {"accepted", "confirmed", "completed", "pending", "pending_review"}
)

_booked_slots_cache: dict = {}

_BOOKED_SLOTS_CACHE_TTL_SEC = (
    10  # Short TTL so "available" and actual check stay in sync
)


def _tenant_sms_from_number() -> Optional[str]:
    """Outbound SMS From: tenant's Twilio number in DB, else business config phone (non-DB). None → send_sms uses TWILIO_SMS_FROM."""
    if runtime.USE_DB:
        cid = database._client_id()
        if cid and cid != "default":
            tenant = database.db_tenant_get_by_client_id(cid)
            if tenant:
                n = (tenant.get("twilio_phone_number") or "").strip()
                if n:
                    return n
    phone = (config_service.get_business_info().get("phone") or "").strip()
    return phone or None

def _booked_slot_duration_by_appointment_id() -> dict[int, int]:
    out: dict[int, int] = {}
    for s in _load_booked_slots():
        try:
            aid = int(s.get("appointment_id") or 0)
        except (TypeError, ValueError):
            continue
        if not aid:
            continue
        try:
            dm = int(s.get("duration_minutes") or DEFAULT_SLOT_DURATION_MINUTES)
        except (TypeError, ValueError):
            dm = DEFAULT_SLOT_DURATION_MINUTES
        out[aid] = max(5, min(dm, 480))
    return out

def _load_booked_slots() -> List[dict]:
    """Load booked slots from client data dir. Each entry: {date, time, appointment_id, duration_minutes?}."""
    if runtime.USE_DB:
        return database.db_booked_slots_load()
    data_dir = config_service.get_client_data_dir()
    if not data_dir:
        return []
    path = data_dir / "booked_slots.json"
    if not path.exists():
        return []
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []

def _save_booked_slots(slots: List[dict]) -> None:
    if runtime.USE_DB:
        database.db_booked_slots_save(slots)
        return
    data_dir = config_service.get_client_data_dir()
    if not data_dir:
        return
    path = data_dir / "booked_slots.json"
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(slots, f, indent=2)
    except Exception as e:
        print(f"Failed to save booked_slots: {e}")

def _staff_slot_key(sid: Optional[str]) -> str:
    s = (sid or "").strip()
    return s if s else "__unassigned__"

def _staff_label_for_slot_key(staff_key: str, id_to_name: dict[str, str]) -> str:
    if staff_key == "__unassigned__":
        return "Unassigned"
    return id_to_name.get(staff_key, staff_key)

def _appointment_rows_for_calendar_merge() -> List[dict]:
    if runtime.USE_DB:
        return database.db_appointments_get_all()
    return list(runtime.appointments)

def _appointment_by_id_map(rows: List[dict]) -> dict[int, dict]:
    m: dict[int, dict] = {}
    for a in rows:
        aid = a.get("id")
        if aid is None:
            continue
        try:
            m[int(aid)] = a
        except (TypeError, ValueError):
            continue
    return m

def _booked_slot_rows_that_hold_calendar(
    raw_slots: List[dict], apt_by_id: dict[int, dict]
) -> List[dict]:
    """Keep persisted booked_slots entries only when the linked appointment still holds the slot."""
    kept: List[dict] = []
    for s in raw_slots:
        aid = s.get("appointment_id")
        if aid is None:
            continue
        try:
            aid_int = int(aid)
        except (TypeError, ValueError):
            continue
        apt = apt_by_id.get(aid_int)
        if not apt:
            continue
        st = (apt.get("status") or "").strip()
        if st not in _CALENDAR_HOLDING_STATUSES:
            continue
        kept.append(s)
    return kept

def _get_all_booked_slots_merged() -> List[dict]:
    """Merge booked_slots table with appointments (accepted/pending) so AI sees all taken times."""
    apts = _appointment_rows_for_calendar_merge()
    apt_by_id = _appointment_by_id_map(apts)
    slots = _booked_slot_rows_that_hold_calendar(_load_booked_slots(), apt_by_id)
    if runtime.USE_DB:
        seen = {
            (s.get("date"), s.get("time"), _staff_slot_key(s.get("staff_id")))
            for s in slots
        }
        for a in apts:
            if not a.get("date") or not a.get("time"):
                continue
            # pending_customer: details texted to caller; slot is not held until they SMS-confirm (see handle_incoming_sms).
            if a.get("status") in (
                "accepted",
                "confirmed",
                "completed",
                "pending",
                "pending_review",
            ):
                sk = _staff_slot_key(a.get("staff_id"))
                k = (a["date"], a["time"], sk)
                if k not in seen:
                    slots.append(
                        {
                            "date": a["date"],
                            "time": a["time"],
                            "appointment_id": a.get("id", 0),
                            "duration_minutes": _appointment_duration_minutes(a),
                            "staff_id": a.get("staff_id"),
                        }
                    )
                    seen.add(k)
    return slots

def get_booked_slots(date: str) -> List[dict]:
    """Return slots already booked for the given date (YYYY-MM-DD)."""
    slots = _get_all_booked_slots_merged()
    return [s for s in slots if s.get("date") == date]

def _slot_overlaps(
    start_a: str, duration_a: int, start_b: str, duration_b: int
) -> bool:
    """True if two time windows overlap. start_* is HH:MM or flexible (10, 10:00, etc.)."""
    a_start = _time_to_minutes(start_a)
    a_end = a_start + duration_a
    b_start = _time_to_minutes(start_b)
    b_end = b_start + duration_b
    return a_start < b_end and b_start < a_end

def _weekday_label_for_date(date_str: str) -> str:
    """"Thursday " for an ISO date, or "" when it will not parse.

    A bare 2026-09-03 makes the model work out the weekday itself, and it gets that wrong:
    a 2 PM hold on Thursday was read back to a caller as Friday being unavailable at 2 PM.
    Naming the day removes the inference, the same reason the booking rules ship a
    DATE REFERENCE list rather than trusting the model to map dates to weekdays.
    """
    try:
        return _date.fromisoformat((date_str or "").strip()).strftime("%A") + " "
    except (ValueError, TypeError):
        return ""


def _slot_blocking_details(
    date: str,
    time: str,
    duration_minutes: int = DEFAULT_SLOT_DURATION_MINUTES,
    staff_id: Optional[str] = None,
) -> List[dict]:
    """Return merged slot rows (with appointment status) that block this window."""
    want = _staff_slot_key(staff_id)
    norm_time = _normalize_time_to_hhmm(time) or time
    apt_by_id = _appointment_by_id_map(_appointment_rows_for_calendar_merge())
    out: List[dict] = []
    for s in _get_all_booked_slots_merged():
        if s.get("date") != date or _staff_slot_key(s.get("staff_id")) != want:
            continue
        slot_time = s.get("time") or ""
        d = s.get("duration_minutes") or DEFAULT_SLOT_DURATION_MINUTES
        if not _slot_overlaps(norm_time, duration_minutes, slot_time, d):
            continue
        aid = s.get("appointment_id")
        apt_status = ""
        if aid is not None:
            apt = apt_by_id.get(int(aid))
            if apt:
                apt_status = (apt.get("status") or "").strip()
        out.append(
            {
                "appointment_id": aid,
                "time": slot_time,
                "status": apt_status,
            }
        )
    return out

def is_slot_available(
    date: str,
    time: str,
    duration_minutes: int = DEFAULT_SLOT_DURATION_MINUTES,
    staff_id: Optional[str] = None,
) -> bool:
    """True if no overlapping booking for this date+time and staff column."""
    blockers = _slot_blocking_details(date, time, duration_minutes, staff_id)
    if blockers:
        system_debug(
            "slot_unavailable",
            date=date,
            time=time,
            staff_key=_staff_slot_key(staff_id),
            blockers=blockers,
        )
        return False
    system_debug(
        "slot_available", date=date, time=time, staff_key=_staff_slot_key(staff_id)
    )
    return True

def reserve_slot(
    date: str,
    time: str,
    appointment_id: int,
    duration_minutes: int = DEFAULT_SLOT_DURATION_MINUTES,
    staff_id: Optional[str] = None,
) -> bool:
    """Record a slot as booked when creating an appointment. Returns True if reserved,
    False if the slot was already taken by a concurrent booking (DB enforces a unique
    hold per date/time/staff, so two simultaneous calls can't double-book)."""
    if runtime.USE_DB:
        reserved = database.db_booked_slot_reserve(
            date, time, appointment_id, duration_minutes, staff_id
        )
    else:
        # File-backed single-tenant dev path: no concurrent writers.
        slots = _load_booked_slots()
        slots.append(
            {
                "date": date,
                "time": time,
                "appointment_id": appointment_id,
                "duration_minutes": duration_minutes,
                "staff_id": staff_id,
            }
        )
        _save_booked_slots(slots)
        reserved = True
    _invalidate_booked_slots_cache()
    system_debug(
        "slot_reserved",
        date=date,
        time=time,
        appointment_id=appointment_id,
        staff_id=staff_id,
        reserved=reserved,
    )
    return reserved

def release_slot(appointment_id: int) -> None:
    """Remove slot when appointment is rejected or cancelled."""
    if runtime.USE_DB:
        database.db_booked_slot_release(appointment_id)
    else:
        slots = _load_booked_slots()
        slots = [s for s in slots if s.get("appointment_id") != appointment_id]
        _save_booked_slots(slots)
    _invalidate_booked_slots_cache()
    system_debug("slot_released", appointment_id=appointment_id)

def _reconcile_sms_appointment_slot_after_detail_change(apt: dict) -> None:
    """After SMS time/date change, move calendar hold when the appointment already reserves a slot."""
    aid = apt.get("id")
    if not aid:
        return
    st = (apt.get("status") or "").strip()
    if st not in ("pending_review", "accepted", "confirmed", "completed"):
        return
    release_slot(int(aid))
    date_str = (apt.get("date") or "").strip()
    time_hhmm = _normalize_time_to_hhmm(apt.get("time") or "") or (apt.get("time") or "").strip()
    staff_for = (apt.get("staff_id") or "").strip() or None
    duration = _appointment_duration_minutes(apt)
    if date_str and time_hhmm and is_slot_available(date_str, time_hhmm, duration, staff_for):
        reserve_slot(date_str, time_hhmm, int(aid), duration, staff_for)

def _reconcile_booked_slots_orphans() -> int:
    """Drop booked_slots rows whose appointment no longer holds the calendar (fixes AI 'taken' with empty UI)."""
    if not runtime.USE_DB:
        return 0
    apts = _appointment_rows_for_calendar_merge()
    apt_by_id = _appointment_by_id_map(apts)
    raw = _load_booked_slots()
    kept = _booked_slot_rows_that_hold_calendar(raw, apt_by_id)
    removed = len(raw) - len(kept)
    if removed > 0:
        _save_booked_slots(kept)
        _invalidate_booked_slots_cache()
        system_info(
            "booked_slots_orphans_removed",
            removed=removed,
            client_id=database._client_id(),
        )
    return removed

def _voice_calendar_holds() -> List[dict]:
    """Slots the AI receptionist treats as unavailable, with linked appointment when one exists."""
    apts = _appointment_rows_for_calendar_merge()
    apt_by_id = _appointment_by_id_map(apts)
    holds: List[dict] = []
    for s in _get_all_booked_slots_merged():
        aid = s.get("appointment_id")
        apt = None
        if aid is not None:
            try:
                apt = apt_by_id.get(int(aid))
            except (TypeError, ValueError):
                apt = None
        holds.append(
            {
                "date": s.get("date"),
                "time": _normalize_time_to_hhmm(s.get("time") or "")
                or (s.get("time") or ""),
                "appointment_id": aid,
                "status": (apt.get("status") if apt else None) or "unknown",
                "name": (apt.get("name") if apt else None) or "",
                "phone": (apt.get("phone") if apt else None) or "",
                "source": (apt.get("source") if apt else None) or "",
            }
        )
    return holds

def _invalidate_booked_slots_cache() -> None:
    """Clear booked slots cache so next prompt build sees current availability (e.g. after reserve/release)."""
    _booked_slots_cache.clear()

def get_booked_slots_prompt_text(days_ahead: int = 90, skip_cache: bool = False) -> str:
    """Build booked-slot lines for the system prompt (per-stylist when multi-staff)."""
    from datetime import timedelta

    now = datetime.now(timezone.utc)
    client_key = database._client_id() or "default"
    cache_key = f"{client_key}:{days_ahead}"
    if not skip_cache and cache_key in _booked_slots_cache:
        text, expires = _booked_slots_cache[cache_key]
        if expires > now:
            system_debug(
                "booked_slots_prompt_cache_hit",
                client_key=client_key,
                slots_text_len=len(text),
            )
            return text
        del _booked_slots_cache[cache_key]
    all_slots = _get_all_booked_slots_merged()
    system_debug(
        "booked_slots_prompt_built",
        client_key=client_key,
        skip_cache=skip_cache,
        total_slots=len(all_slots),
    )
    info = config_service.get_business_info()
    roster = [
        ((s.get("id") or "").strip(), (s.get("name") or "").strip())
        for s in (info.get("staff") or [])
        if (s.get("name") or "").strip()
    ]
    multi_staff = len(roster) >= 2
    id_to_name = {sid: name for sid, name in roster if sid}
    today = now.date()
    parts: List[str] = []
    suggest_parts: List[str] = []

    dates_with_bookings: set[str] = set()
    for s in all_slots:
        dt = (s.get("date") or "").strip()
        if dt:
            dates_with_bookings.add(dt)

    if multi_staff:
        by_stylist_booked: dict[str, List[str]] = {}
        by_date_staff: dict[tuple[str, str], List[str]] = {}
        for s in all_slots:
            dt = (s.get("date") or "").strip()
            t = (s.get("time") or "").strip()
            if not dt or not t:
                continue
            sk = _staff_slot_key(s.get("staff_id"))
            by_date_staff.setdefault((dt, sk), []).append(t)
        for (dt, sk), times in sorted(by_date_staff.items()):
            label = _staff_label_for_slot_key(sk, id_to_name)
            times_display = [_hhmm_to_ampm(x) for x in sorted(set(times))]
            by_stylist_booked.setdefault(label, []).append(
                f"{_weekday_label_for_date(dt)}{dt} at {', '.join(times_display)}"
            )
        if by_stylist_booked:
            booked_lines = [
                f"{label}: {'; '.join(lines)}"
                for label, lines in sorted(by_stylist_booked.items())
            ]
            parts.append(
                "Booked slots by stylist. Each calendar is separate—do not merge across "
                "people, and do not carry a booked time from one DATE to another: a slot "
                "listed here blocks ONLY the exact weekday and date it is listed under: "
                + " | ".join(booked_lines)
            )
        roster_with_ids = [(sid, name) for sid, name in roster if sid]
        for d in range(days_ahead):
            day = today + timedelta(days=d)
            date_str = day.isoformat()
            if date_str not in dates_with_bookings:
                continue
            day_times = _hourly_slots_for_date(info, day)
            if not day_times:
                continue  # shop closed that day — don't suggest any times
            hours_phrase = _hours_phrase_for_date(info, day)
            for sid, name in roster_with_ids:
                times = by_date_staff.get((date_str, sid), [])
                taken_set = {
                    t
                    for t in (
                        _normalize_time_to_hhmm(x.strip()) for x in times if x
                    )
                    if t
                }
                if not taken_set:
                    hrs = f" ({hours_phrase})" if hours_phrase else ""
                    suggest_parts.append(
                        f"For {name} on {date_str} no times are booked for {name}—"
                        f"all open hours{hrs} are available with {name}."
                    )
                    continue
                safe = [t for t in day_times if t not in taken_set]
                taken_display = [_hhmm_to_ampm(t) for t in sorted(taken_set)]
                if safe:
                    safe_display = [_hhmm_to_ampm(t) for t in safe]
                    suggest_parts.append(
                        f"For {name} on {date_str} ONLY suggest these times (free for {name}): "
                        f"{', '.join(safe_display)}. Never suggest {', '.join(taken_display)} for {name}—"
                        f"already taken for {name}."
                    )
                else:
                    suggest_parts.append(
                        f"For {name} on {date_str} standard hours appear fully booked for {name} "
                        f"({', '.join(taken_display)}). Offer another day or another stylist—not "
                        f"that the whole salon is closed."
                    )
    else:
        by_date: dict[str, List[str]] = {}
        for s in all_slots:
            dt = (s.get("date") or "").strip()
            if not dt:
                continue
            t = (s.get("time") or "").strip()
            if t:
                by_date.setdefault(dt, []).append(t)
        for d in range(days_ahead):
            day = today + timedelta(days=d)
            date_str = day.isoformat()
            times = by_date.get(date_str) or []
            if times:
                times_display = [_hhmm_to_ampm(t) for t in sorted(times)]
                parts.append(f"{date_str} at {', '.join(times_display)}")
                taken_set = {
                    t
                    for t in (
                        _normalize_time_to_hhmm(x.strip()) for x in times if x
                    )
                    if t
                }
                day_times = _hourly_slots_for_date(info, day)
                safe = [t for t in day_times if t not in taken_set]
                if safe:
                    safe_display = [_hhmm_to_ampm(t) for t in safe]
                    taken_display = [_hhmm_to_ampm(t) for t in sorted(taken_set)]
                    suggest_parts.append(
                        f"For {date_str} ONLY suggest these times (they are free): "
                        f"{', '.join(safe_display)}. Never suggest {', '.join(taken_display)}—already taken."
                    )

    if parts:
        if multi_staff:
            text = " ".join(parts) + ". "
        else:
            text = "Booked slots (do not double-book): " + "; ".join(parts) + ". "
    else:
        text = ""
    if suggest_parts:
        text += " " + " ".join(suggest_parts)
    expires_at = now + timedelta(seconds=_BOOKED_SLOTS_CACHE_TTL_SEC)
    _booked_slots_cache[cache_key] = (text, expires_at)
    return text


# ===== appointment decline/cancel SMS polish (uses runtime.client) =====


def polish_owner_customer_sms(
    raw_reason: str,
    business_name: str,
    apt: dict,
    *,
    event: str = "decline",
) -> str:
    """Rewrite owner note into a warm customer SMS (decline pending request or cancel accepted booking)."""
    text = (raw_reason or "").strip()
    if not text:
        text = (
            "We need to cancel your appointment."
            if event == "cancel"
            else "We could not accommodate that time."
        )
    date = apt.get("date") or ""
    time_ampm = _hhmm_to_ampm(apt.get("time") or "") or (apt.get("time") or "")
    if event == "cancel":
        system = (
            "You write brief SMS messages for a salon, barbershop, or nail studio. "
            "The business is CANCELLING an already confirmed appointment. "
            "Rewrite the owner's note into ONE warm, natural cancellation message. "
            "State clearly that the appointment is cancelled. Max 480 characters. "
            "Do not invent policies. Invite them to rebook if appropriate."
        )
        user = (
            f"Business name: {business_name}\n"
            f"Confirmed appointment: {date} at {time_ampm}\n"
            f"Owner note: {text[:1800]}"
        )
    else:
        system = (
            "You write brief SMS messages for a salon, barbershop, or nail studio. "
            "Rewrite the owner's decline reason into ONE warm, natural message. "
            "Max 480 characters. Do not invent discounts, guarantees, or policies. "
            "If appropriate, invite alternative dates/times. Match the tone of the owner's note."
        )
        user = (
            f"Business name: {business_name}\n"
            f"Appointment requested: {date} at {time_ampm}\n"
            f"Owner note: {text[:1800]}"
        )
    try:
        runtime._ensure_openai_client()
        r = runtime.client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
            max_tokens=220,
            temperature=0.45,
        )
        out = (r.choices[0].message.content or "").strip()
        return out[:1580] if out else text[:1580]
    except Exception as e:
        logger.warning("polish_owner_customer_sms_openai_failed event=%s: %s", event, e)
        return text[:1580]


def polish_owner_decline_sms(raw_reason: str, business_name: str, apt: dict) -> str:
    return polish_owner_customer_sms(raw_reason, business_name, apt, event="decline")
