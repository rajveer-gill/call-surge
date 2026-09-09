"""Strict parsing, sanitization, and validation for voice BOOKING: field payloads."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from typing import Any, Optional

_DATE_ISO_RE = re.compile(r"^20\d{2}-\d{2}-\d{2}$")
_SERVICE_ASK_RE = re.compile(
    r"which service|what service|service would you|from the menu|"
    r"short cut|long cut|offer(?:s|ing)?\s+(?:short|long)",
    re.I,
)
_AFFIRM_RE = re.compile(
    r"^(?:yes|yeah|yep|yup|correct|right|that(?:'s| is) (?:it|right|correct)|"
    r"sounds good|perfect|sure|ok(?:ay)?|please|that one)\.?$",
    re.I,
)


@dataclass(frozen=True)
class BookingFieldContext:
    staff_names: frozenset[str]
    service_names: frozenset[str]
    # Business-local "now" at booking time, used to reject past dates/times. Both are
    # hashable (so the frozen dataclass stays hashable) and optional so existing callers
    # and tests that don't pass them keep their prior behavior (no past-date check).
    today: Optional[date] = None
    now_minutes: Optional[int] = None


def booking_context_from_business(biz: dict[str, Any]) -> BookingFieldContext:
    staff = {
        (s.get("name") or "").strip().lower()
        for s in (biz.get("staff") or [])
        if (s.get("name") or "").strip()
    }
    services = {
        (s.get("name") or "").strip().lower()
        for s in (biz.get("services") or [])
        if (s.get("name") or "").strip()
    }
    today: Optional[date] = None
    now_minutes: Optional[int] = None
    try:
        import business_hours

        local_now = business_hours.business_local_now(biz)
        today = local_now.date()
        now_minutes = local_now.hour * 60 + local_now.minute
    except Exception:
        pass
    return BookingFieldContext(
        staff_names=frozenset(staff),
        service_names=frozenset(services),
        today=today,
        now_minutes=now_minutes,
    )


def is_valid_booking_date(raw: object) -> bool:
    s = (raw or "").strip() if isinstance(raw, str) else ""
    if not _DATE_ISO_RE.match(s):
        return False
    try:
        date.fromisoformat(s)
        return True
    except ValueError:
        return False


# Spoken time forms. Callers say "two in the afternoon" or "half past three" at least as
# often as "2 PM", and the model passes the phrase straight through into the time field.
# Before this, anything without a digit was rejected outright and the request was dropped.
_WORD_HOURS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
    "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12,
}
_WORD_MINUTES = {
    "oclock": 0, "o'clock": 0, "clock": 0,
    "five": 5, "ten": 10, "fifteen": 15, "quarter": 15, "twenty": 20,
    "twentyfive": 25, "thirty": 30, "half": 30, "fortyfive": 45, "forty-five": 45,
    "fifty": 50,
}
# Phrases that pin the half of the day. "at night" and "tonight" map to PM.
_MERIDIAN_PHRASES = (
    (re.compile(r"\b(?:in the )?morning\b|\bam\b", re.I), "am"),
    (re.compile(r"\b(?:in the )?(?:afternoon|evening)\b|\b(?:at )?night\b|\btonight\b|\bpm\b", re.I), "pm"),
)


def _normalize_word_time_text(text: str) -> str:
    s = text.strip().lower()
    s = re.sub(r"^(?:at|around|about)\s+", "", s)
    s = re.sub(r"[.,]", "", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _word_time_to_minutes(raw: str) -> Optional[int]:
    """Parse a spoken time with no digits. Returns None for genuinely vague input.

    Deliberately strict: the whole string must be a time. "afternoon" on its own stays
    unparseable — it names a half of the day, not a time, and guessing one would put a
    made-up slot in front of the salon.
    """
    s = _normalize_word_time_text(raw)
    if not s:
        return None

    meridian: Optional[str] = None
    for pattern, which in _MERIDIAN_PHRASES:
        if pattern.search(s):
            meridian = which
            break
    # Strip the meridian phrase; what remains must be the hour/minute part.
    core = re.sub(
        r"\b(?:in the )?(?:morning|afternoon|evening)\b|\b(?:at )?night\b|\btonight\b|\b(?:am|pm)\b",
        "",
        s,
        flags=re.I,
    )
    core = re.sub(r"\s+", " ", core).strip()

    if core in ("noon", "midday"):
        return 12 * 60
    if core == "midnight":
        return 0

    # "half past three", "quarter past three", "quarter to four"
    m = re.fullmatch(r"(half|quarter|twenty|ten|five|fifteen)\s+(past|after|to|till|before)\s+(\w+)", core)
    if m:
        offset_word, direction, hour_word = m.group(1), m.group(2), m.group(3)
        offset = _WORD_MINUTES.get(offset_word)
        hour = _WORD_HOURS.get(hour_word)
        if hour is None and hour_word in ("noon", "midday"):
            hour = 12
        if offset is None or hour is None:
            return None
        base = hour * 60
        mins = base + offset if direction in ("past", "after") else base - offset
        mins %= 24 * 60
        hour_out = mins // 60
        if meridian == "pm" and hour_out < 12:
            mins += 12 * 60
        elif meridian == "am" and hour_out == 12:
            mins -= 12 * 60
        elif meridian is None and 1 <= hour_out <= 8:
            mins += 12 * 60
        return mins % (24 * 60)

    # "two", "two thirty", "two o'clock", "ten fifteen"
    parts = core.split(" ")
    if not parts or parts[0] not in _WORD_HOURS:
        return None
    h = _WORD_HOURS[parts[0]]
    m_out = 0
    if len(parts) == 2:
        key = parts[1].replace(" ", "")
        if key not in _WORD_MINUTES:
            return None
        m_out = _WORD_MINUTES[key]
    elif len(parts) == 3:
        # "twenty five" / "forty five" as two tokens
        key = (parts[1] + parts[2]).replace("-", "")
        if key not in _WORD_MINUTES:
            return None
        m_out = _WORD_MINUTES[key]
    elif len(parts) > 3:
        return None

    if meridian == "pm":
        if h != 12:
            h += 12
    elif meridian == "am":
        if h == 12:
            h = 0
    elif meridian is None:
        # Same salon-hours convention the numeric path uses: a bare 1-8 means afternoon.
        if 1 <= h <= 8:
            h += 12
    if not (0 <= h <= 23 and 0 <= m_out <= 59):
        return None
    return h * 60 + m_out


def _time_to_minutes(raw: str) -> Optional[int]:
    text = (raw or "").strip()
    if not text:
        return None
    if not re.search(r"\d", text):
        return _word_time_to_minutes(text)
    upper = text.upper()
    meridian: Optional[str] = None
    if re.search(r"\bP\.?\s*M\.?\b", upper) or re.search(r"\bPM\b", upper):
        meridian = "pm"
    elif re.search(r"\bA\.?\s*M\.?\b", upper) or re.search(r"\bAM\b", upper):
        meridian = "am"
    cleaned = re.sub(r"(?i)\s*(a\.?\s*m\.?|p\.?\s*m\.?)\s*$", "", text).strip()
    cleaned = re.sub(r"(?i)\s*(am|pm)\s*$", "", cleaned).strip()
    parts = cleaned.split(":")
    try:
        h = int("".join(c for c in parts[0] if c.isdigit()) or "0") if parts else 0
        m = int("".join(c for c in parts[1] if c.isdigit()) or "0") if len(parts) > 1 else 0
    except (ValueError, TypeError):
        return None
    if meridian == "pm":
        if h != 12:
            h += 12
    elif meridian == "am":
        if h == 12:
            h = 0
    elif meridian is None and cleaned:
        if h == 12:
            pass
        elif 1 <= h <= 8:
            h += 12
    if not (0 <= h <= 23 and 0 <= m <= 59):
        return None
    return h * 60 + m


def normalize_booking_time(raw: object) -> Optional[str]:
    s = (raw or "").strip() if isinstance(raw, str) else ""
    if not s:
        return None
    mins = _time_to_minutes(s)
    if mins is None:
        return None
    h, m = divmod(mins, 60)
    return f"{h:02d}:{m:02d}"


def looks_like_booking_time(
    raw: object,
    ctx: BookingFieldContext,
) -> bool:
    s = (raw or "").strip() if isinstance(raw, str) else ""
    if not s:
        return False
    low = s.lower()
    if low in ctx.staff_names or low in ctx.service_names:
        return False
    # Digit-free strings used to be rejected here outright. They now go to the word-time
    # parser, which only matches a whole string that IS a time — a service or stylist name
    # ("Long Cut", "Jake") still fails, on top of the exact-name guard above.
    return normalize_booking_time(s) is not None


def sanitize_parsed_booking(
    booking: dict[str, Any],
    ctx: BookingFieldContext,
) -> tuple[dict[str, Any], list[str]]:
    """
    Repair common GPT field misalignment (e.g. stylist name in time slot).
    Returns a new dict and a list of repair action labels (for logs).
    """
    out = dict(booking)
    repairs: list[str] = []

    time_raw = (out.get("time") or "").strip()
    staff_raw = (out.get("staff") or "").strip()
    reason_raw = (out.get("reason") or "").strip()

    if time_raw and time_raw.lower() in ctx.staff_names:
        if not staff_raw:
            out["staff"] = time_raw
            repairs.append("staff_from_time")
        out["time"] = ""
        time_raw = ""

    if time_raw and time_raw.lower() in ctx.service_names:
        if not reason_raw:
            out["reason"] = time_raw
            repairs.append("service_from_time")
        out["time"] = ""
        time_raw = ""

    if reason_raw and looks_like_booking_time(reason_raw, ctx):
        if not looks_like_booking_time(time_raw, ctx):
            normalized = normalize_booking_time(reason_raw)
            if normalized:
                out["time"] = normalized
                out["reason"] = ""
                repairs.append("time_from_reason")
                time_raw = normalized

    if (
        reason_raw
        and reason_raw.lower() in ctx.staff_names
        and not staff_raw
        and reason_raw.lower() not in ctx.service_names
    ):
        out["staff"] = reason_raw
        if not looks_like_booking_time(time_raw, ctx):
            out["reason"] = ""
            repairs.append("staff_from_reason")

    if looks_like_booking_time(out.get("time"), ctx):
        normalized = normalize_booking_time(out.get("time"))
        if normalized:
            out["time"] = normalized

    return out, repairs


def validate_booking_datetime_fields(
    booking: dict[str, Any],
    ctx: BookingFieldContext,
) -> tuple[bool, Optional[str]]:
    if not is_valid_booking_date(booking.get("date")):
        return False, "invalid_date"
    if not looks_like_booking_time(booking.get("time"), ctx):
        return False, "invalid_time"
    # Reject bookings in the past (business-local). Only enforced when the context
    # carries a reference "now" — legacy callers without it keep prior behavior.
    if ctx.today is not None:
        try:
            booking_date = date.fromisoformat((booking.get("date") or "").strip())
        except ValueError:
            return False, "invalid_date"
        if booking_date < ctx.today:
            return False, "past_date"
        if booking_date == ctx.today and ctx.now_minutes is not None:
            mins = _time_to_minutes(booking.get("time") or "")
            if mins is not None and mins < ctx.now_minutes:
                return False, "past_time"
    return True, None


def validate_booking_schedule_fields(
    booking: dict[str, Any],
    ctx: BookingFieldContext,
) -> tuple[bool, Optional[str]]:
    if not (booking.get("name") or "").strip():
        return False, "missing_name"
    ok, reason = validate_booking_datetime_fields(booking, ctx)
    return ok, reason


def normalize_and_validate_booking(
    booking: dict[str, Any],
    ctx: BookingFieldContext,
    *,
    require_name: bool = False,
) -> tuple[Optional[dict[str, Any]], list[str], Optional[str]]:
    """
    Sanitize then validate schedule fields.
    Returns (booking_or_none, repairs, reject_reason).
    """
    sanitized, repairs = sanitize_parsed_booking(booking, ctx)
    if require_name:
        ok, reason = validate_booking_schedule_fields(sanitized, ctx)
    else:
        ok, reason = validate_booking_datetime_fields(sanitized, ctx)
    if not ok:
        return None, repairs, reason
    return sanitized, repairs, None


def _user_text_from_history(conversation_history: Optional[list]) -> str:
    if not conversation_history:
        return ""
    parts = [
        (m.get("content") or "").strip()
        for m in conversation_history
        if (m.get("role") or "").strip() == "user"
    ]
    return " ".join(p for p in parts if p)


def user_indicated_service_name(user_text: str, service_names: frozenset[str]) -> bool:
    t = (user_text or "").lower()
    if not t.strip():
        return False
    # Whitespace-insensitive comparison too: speech-to-text often runs multi-word service
    # names together ("short cut" -> "shortcut"), which shouldn't fail the match.
    t_nospace = re.sub(r"\s+", "", t)
    for nm in service_names:
        nm = (nm or "").strip()
        if not nm:
            continue
        if re.search(rf"\b{re.escape(nm)}\b", t) or nm in t:
            return True
        nm_nospace = re.sub(r"\s+", "", nm)
        if nm_nospace and nm_nospace in t_nospace:
            return True
    return False


def assistant_asked_service_recently(
    conversation_history: Optional[list],
    *,
    assistant_window: int = 4,
) -> bool:
    if not conversation_history:
        return False
    assistant_msgs = [
        (m.get("content") or "")
        for m in conversation_history
        if (m.get("role") or "").strip() == "assistant"
    ][-assistant_window:]
    return any(_SERVICE_ASK_RE.search(c) for c in assistant_msgs)


_NAME_ASK_RE = re.compile(
    r"your name|name for the|have your name|who(?:'s| is) (?:this|the appointment)|"
    r"who am i (?:speaking|booking)|whose name",
    re.I,
)


def assistant_asked_name_recently(
    conversation_history: Optional[list],
    *,
    assistant_window: int = 4,
) -> bool:
    """Have we asked this caller their name in the last few turns?

    A booking cannot be filed without a name, and on 2026-09-09 a caller spent four
    minutes giving a service, a stylist and a time, was told three times the request was
    noted, and left with nothing — because nobody ever asked who they were.

    The nudge that uses this must not fire when we have already asked and are waiting for
    the answer, or a caller gets asked their name twice running, which is the other thing
    the customer complained about the same morning.
    """
    if not conversation_history:
        return False
    assistant_msgs = [
        (m.get("content") or "")
        for m in conversation_history
        if (m.get("role") or "").strip() == "assistant"
    ][-assistant_window:]
    return any(_NAME_ASK_RE.search(c) for c in assistant_msgs)


def user_affirmed_after_service_prompt(
    conversation_history: Optional[list],
    ctx: BookingFieldContext,
) -> bool:
    """True when the caller answered a service question with a service name or clear yes."""
    if not conversation_history:
        return False
    for i in range(len(conversation_history) - 1, -1, -1):
        msg = conversation_history[i]
        if (msg.get("role") or "").strip() != "assistant":
            continue
        assistant_text = (msg.get("content") or "")
        if not _SERVICE_ASK_RE.search(assistant_text):
            continue
        for follow in conversation_history[i + 1 :]:
            if (follow.get("role") or "").strip() != "user":
                continue
            user_text = (follow.get("content") or "").strip()
            if not user_text:
                continue
            if user_indicated_service_name(user_text, ctx.service_names):
                return True
            if _AFFIRM_RE.match(user_text.lower()):
                if user_indicated_service_name(assistant_text, ctx.service_names):
                    return True
            return False
    return False


def service_choice_resolved(
    conversation_history: Optional[list],
    ctx: BookingFieldContext,
    *,
    canonical_service: Optional[str] = None,
) -> bool:
    user_text = _user_text_from_history(conversation_history)
    if user_indicated_service_name(user_text, ctx.service_names):
        return True
    if canonical_service and user_affirmed_after_service_prompt(conversation_history, ctx):
        return True
    return False


def service_prompt_message(
    *,
    staff_name: str,
    service_choices: str,
    already_asked: bool,
) -> str:
    if already_asked:
        base = (
            f"I still need the service for {staff_name} — "
            if staff_name
            else "I still need the service — "
        )
    else:
        base = (
            f"Great, and which service would you like with {staff_name}? "
            if staff_name
            else "Great, which service would you like? "
        )
    return base + (f"We currently offer {service_choices}." if service_choices else "")
