"""Parse business hours text and detect after-hours same-day booking."""

from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from typing import List, Optional
from zoneinfo import ZoneInfo

DEFAULT_OPEN = "09:00"
DEFAULT_CLOSE = "17:00"

_DAY_MAP = {
    "monday": 0,
    "mon": 0,
    "tuesday": 1,
    "tue": 1,
    "tues": 1,
    "wednesday": 2,
    "wed": 2,
    "thursday": 3,
    "thu": 3,
    "thur": 3,
    "friday": 4,
    "fri": 4,
    "saturday": 5,
    "sat": 5,
    "sunday": 6,
    "sun": 6,
}


@dataclass(frozen=True)
class DaySlot:
    closed: bool
    open: str
    close: str


def default_weekly_schedule() -> List[DaySlot]:
    return [
        DaySlot(closed=i >= 5, open=DEFAULT_OPEN, close=DEFAULT_CLOSE) for i in range(7)
    ]


def normalize_time_24(raw: str) -> Optional[str]:
    s = (raw or "").strip().lower().replace(".", "")
    ampm = re.search(r"\s*(a|p)\.?m\.?$", s)
    base = re.sub(r"\s*(a|p)\.?m\.?$", "", s, flags=re.I).strip()
    parts = base.split(":")
    try:
        h = int(re.sub(r"\D", "", parts[0]) or "0")
        m = int(re.sub(r"\D", "", parts[1]) if len(parts) > 1 else "0")
    except (TypeError, ValueError):
        return None
    if ampm:
        mer = ampm.group(1).lower()
        if mer == "p" and h < 12:
            h += 12
        if mer == "a" and h == 12:
            h = 0
    if h < 0 or h > 23 or m < 0 or m > 59:
        return None
    return f"{h:02d}:{m:02d}"


def time_to_minutes(hhmm: str) -> int:
    n = normalize_time_24(hhmm)
    if not n:
        return -1
    h, m = n.split(":")
    return int(h) * 60 + int(m)


def _expand_day_range_label(left: str) -> Optional[List[int]]:
    a = left.strip().lower()
    range_parts = re.split(r"\s*[–-]\s*", a)
    if len(range_parts) == 2:
        s, e = _DAY_MAP.get(range_parts[0]), _DAY_MAP.get(range_parts[1])
        if s is not None and e is not None:
            out: List[int] = []
            i = s
            while True:
                out.append(i)
                if i == e:
                    break
                i = 0 if i == 6 else i + 1
                if len(out) > 8:
                    break
            if out and out[0] == s and out[-1] == e:
                return out
    single = _DAY_MAP.get(a.rstrip("."))
    return [single] if single is not None else None


def _extract_two_times(fragment: str) -> Optional[tuple[str, str]]:
    f = fragment.replace("\u2013", "-").replace("–", "-").strip()
    pieces = re.split(r"\s*-\s*", f)
    if len(pieces) >= 2:
        o, c = normalize_time_24(pieces[0]), normalize_time_24(pieces[-1])
        if o and c:
            return o, c
    alt = re.findall(r"(\d{1,2}(?::\d{2})?\s*[ap]\.?m\.?|\d{1,2}:\d{2})", f, re.I)
    if len(alt) >= 2:
        o, c = normalize_time_24(alt[0]), normalize_time_24(alt[1])
        if o and c:
            return o, c
    return None


def parse_hours_to_weekly(text: str) -> List[DaySlot]:
    raw = (text or "").strip()
    if not raw:
        return default_weekly_schedule()
    lower = raw.lower()
    if re.search(r"\b24\s*/\s*7\b", lower) or re.search(r"\b24-7\b", lower):
        return [DaySlot(False, "00:00", "23:59") for _ in range(7)]

    sched = [DaySlot(True, DEFAULT_OPEN, DEFAULT_CLOSE) for _ in range(7)]
    matched = False
    for line in raw.splitlines():
        for piece in line.split(";"):
            piece = piece.strip()
            if not piece:
                continue
            colon = piece.find(":")
            if colon < 0:
                continue
            left, right = piece[:colon].strip(), piece[colon + 1 :].strip()
            days = _expand_day_range_label(left)
            if not days:
                continue
            if re.match(r"^closed\b", right, re.I):
                for d in days:
                    sched[d] = DaySlot(True, DEFAULT_OPEN, DEFAULT_CLOSE)
                matched = True
                continue
            times = _extract_two_times(right)
            if not times:
                continue
            for d in days:
                sched[d] = DaySlot(False, times[0], times[1])
            matched = True

    if not matched:
        loose = _extract_two_times(raw)
        if loose:
            for d in range(5):
                sched[d] = DaySlot(False, loose[0], loose[1])
            return sched
        return default_weekly_schedule()
    return sched


def business_timezone(info: Optional[dict] = None) -> ZoneInfo:
    tz_name = ""
    if info:
        tz_name = (info.get("timezone") or "").strip()
    if not tz_name:
        tz_name = (os.getenv("BUSINESS_TIMEZONE") or "America/Los_Angeles").strip()
    try:
        return ZoneInfo(tz_name)
    except Exception as e:
        # Almost always a missing IANA tz database (slim container without `tzdata`). Falling
        # back to UTC silently corrupts "today"/"tomorrow" date math, so make it loud.
        logging.getLogger("nuvatra").warning(
            "business_timezone: could not load %r (%s); falling back to UTC. "
            "Ensure the `tzdata` package is installed.",
            tz_name,
            e,
        )
        try:
            return ZoneInfo("UTC")
        except Exception:
            return timezone.utc


def business_local_now(
    info: Optional[dict] = None, now: Optional[datetime] = None
) -> datetime:
    dt = now or datetime.now(timezone.utc)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(business_timezone(info))


def day_slot_for_date(info: dict, target_date: date) -> DaySlot:
    schedule = parse_hours_to_weekly(info.get("hours") or "")
    return schedule[target_date.weekday()]


def is_open_247(slot: DaySlot) -> bool:
    return not slot.closed and slot.open == "00:00" and slot.close == "23:59"


def is_past_closing_for_date(
    info: dict, target_date: date, now: Optional[datetime] = None
) -> bool:
    """True when target_date is today (business local) and the shop is closed for the rest of the day."""
    local_now = business_local_now(info, now)
    if target_date != local_now.date():
        return False
    slot = day_slot_for_date(info, target_date)
    if slot.closed:
        return True
    if is_open_247(slot):
        return False
    now_mins = local_now.hour * 60 + local_now.minute
    close_mins = time_to_minutes(slot.close)
    if close_mins < 0:
        return False
    return now_mins >= close_mins


def same_day_after_hours_message(info: Optional[dict] = None) -> str:
    data = info or {}
    biz = (
        (data.get("public_name") or "").strip()
        or (data.get("name") or "").strip()
        or "the shop"
    )
    return (
        f"We're already closed for today at {biz}. "
        "Would another day work for your appointment?"
    )


def after_hours_prompt_block(
    info: dict, now: Optional[datetime] = None
) -> Optional[str]:
    local_now = business_local_now(info, now)
    if not is_past_closing_for_date(info, local_now.date(), now):
        return None
    biz = (
        (info.get("public_name") or "").strip()
        or (info.get("name") or "").strip()
        or "the business"
    )
    hours = (info.get("hours") or "").strip()
    hours_line = f" Store hours: {hours}" if hours else ""
    # Spell out tomorrow's status explicitly. Without it the model has inverted things after hours —
    # telling the caller an OPEN tomorrow was closed while offering the already-past today.
    tomorrow = local_now.date() + timedelta(days=1)
    tmr_slot = day_slot_for_date(info, tomorrow)
    if tmr_slot.closed:
        tomorrow_line = (
            f" Tomorrow ({tomorrow.strftime('%A')} {tomorrow.isoformat()}) the shop is closed."
        )
    else:
        tomorrow_line = (
            f" Tomorrow ({tomorrow.strftime('%A')} {tomorrow.isoformat()}) the shop is OPEN "
            "as normal — you MAY book then."
        )
    return (
        f"AFTER HOURS: It is currently after closing on {local_now.strftime('%A')} "
        f"({local_now.date().isoformat()}) at {biz}.{hours_line} "
        "ONLY today is already over — every FUTURE day still follows the normal hours above."
        f"{tomorrow_line} "
        "If the caller tries to book for TODAY, tell them the shop is already closed for today and "
        "offer another day; do NOT output BOOKING for today. NEVER tell the caller a future day "
        "(including tomorrow) is closed unless the hours list that weekday as closed."
    )
