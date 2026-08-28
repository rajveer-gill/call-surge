"""Conversation service — the AI voice-receptionist booking logic.

The voice/SMS conversation brain lifted out of main: booking-intent detection, the
GPT booking-line extraction, BOOKING: parsing/validation, appointment creation from a
parsed booking, and system-prompt composition. The /api/conversation route stays in
main and calls these via re-export. Cross-module helpers are module-qualified
(config_service / booking_service / database / sms_service / runtime); pure leaf logic
(booking_fields / business_hours / prompts.receptionist / observability) is imported by name.
"""

from __future__ import annotations

import asyncio
import logging
import os
import re
from datetime import date, datetime, timedelta, timezone
from urllib.parse import quote
from typing import List, Optional

import booking_service
import caller_memory
import config_service
import database
import llm_provider
import runtime
import sms_service
import voice_service
from observability import (
    name_initial_for_log,
    sms_info,
    system_info,
    voice_call_phase,
    voice_debug,
    voice_forward,
    voice_info,
    voice_transcript,
    voice_warning,
)
from booking_fields import (
    assistant_asked_service_recently,
    booking_context_from_business,
    is_valid_booking_date,
    looks_like_booking_time,
    normalize_and_validate_booking,
    normalize_booking_time,
    service_choice_resolved,
    service_prompt_message,
)
from business_hours import (
    after_hours_prompt_block,
    business_local_now,
    is_past_closing_for_date,
    same_day_after_hours_message,
)
from prompts.receptionist import (
    build_system_prompt,
    caller_message_suggests_pricing,
    latest_user_message,
)

logger = logging.getLogger("nuvatra")

# Voice reasoning model. gpt-4o-mini is faster and cheaper than gpt-3.5-turbo and far more
# reliable at per-stylist scheduling (gpt-3.5 would misapply one stylist's working days to
# another). Override via VOICE_LLM_MODEL to roll back or A/B a different model without a deploy.
VOICE_LLM_MODEL = (os.getenv("VOICE_LLM_MODEL") or "gpt-4o-mini").strip()

_STYLIST_NO_PREF_PHRASES = (
    "anyone",
    "any stylist",
    "any one",
    "no preference",
    "no pref",
    "don't care",
    "doesn't matter",
    "whoever",
    "first available",
    "any available",
    "no particular",
    "you choose",
    "surprise me",
)


def _phones_match_for_booking(a: str, b: str) -> bool:
    da = sms_service.normalize_phone(a or "")
    db = sms_service.normalize_phone(b or "")
    if not da or not db:
        return not da and not db
    return da == db or da.endswith(db[-10:]) or db.endswith(da[-10:])


def _supersede_pending_customer_drafts_for_slot(
    date: str,
    time: str,
    staff_id: Optional[str],
    *,
    client_id: Optional[str] = None,
    phone: Optional[str] = None,
) -> int:
    """
    Cancel stale voice bookings so the same caller can rebook / modify without leaving duplicates.
    - pending_customer: unconfirmed draft (slot not held until SMS YES). When the caller's phone is
      known, ALL of their same-date drafts are superseded — so a mid-call change to the time,
      service, or stylist replaces the draft instead of leaving a stale one. Without a phone, falls
      back to exact-slot matching.
    - pending_review: same caller + receptionist source at the exact slot — frees a held slot when
      they call again.
    """
    if not runtime.USE_DB:
        return 0
    cid = (client_id or "").strip() or database._client_id()
    if not cid:
        return 0
    want_staff = booking_service._staff_slot_key(staff_id)
    norm_time = booking_service._normalize_time_to_hhmm(time) or time
    cancelled = 0
    for apt in booking_service._appointment_rows_for_calendar_merge():
        st = apt.get("status") or ""
        if st not in ("pending_customer", "pending_review"):
            continue
        if (apt.get("date") or "") != date:
            continue
        if phone and st == "pending_review" and (apt.get("source") or "").strip() == "receptionist":
            # A REQUEST holds nothing — the salon has not confirmed it, and the caller
            # is told so. Superseding it at the exact slot only meant that moving the
            # time left the old request standing: a caller who said "actually, make it
            # 4pm" produced requests at BOTH 3pm and 4pm, and the salon had to guess.
            #
            # So the same rule as an unconfirmed draft: any of this caller's same-date
            # requests taken by the receptionist are replaced, whatever the slot.
            # Scoped to their phone and to receptionist-sourced rows, so a request
            # someone else made, or one taken another way, is never touched.
            if not _phones_match_for_booking(phone, apt.get("phone") or ""):
                continue
        elif st == "pending_customer" and phone:
            # An unconfirmed draft holds no slot, so a caller should have at most one per day.
            # Match any of THIS caller's same-date drafts so a mid-call change (time, service, OR
            # stylist) REPLACES the draft instead of leaving a stale duplicate on the dashboard.
            if not _phones_match_for_booking(phone, apt.get("phone") or ""):
                continue
        else:
            # pending_review (a held slot) or an anonymous draft with no caller phone: only
            # supersede the exact same slot, and for pending_review require the same caller +
            # receptionist source so we never free an unrelated held slot.
            if st == "pending_review":
                if (apt.get("source") or "").strip() != "receptionist":
                    continue
                if not phone or not _phones_match_for_booking(
                    phone, apt.get("phone") or ""
                ):
                    continue
            apt_time = booking_service._normalize_time_to_hhmm(apt.get("time") or "") or (
                apt.get("time") or ""
            )
            if apt_time != norm_time:
                continue
            if booking_service._staff_slot_key(apt.get("staff_id")) != want_staff:
                continue
            if phone and not _phones_match_for_booking(phone, apt.get("phone") or ""):
                continue
        aid = apt.get("id")
        if not aid:
            continue
        try:
            database.db_appointments_update(int(aid), status="cancelled", client_id=cid)
            booking_service.release_slot(int(aid))
            cancelled += 1
        except Exception as e:
            logger.warning("supersede_voice_booking_draft failed apt_id=%s: %s", aid, e)
    if cancelled:
        booking_service._invalidate_booked_slots_cache()
        system_info(
            "voice_booking_draft_superseded",
            count=cancelled,
            date=date,
            time=norm_time,
            client_id=cid,
        )
    return cancelled


def _suggests_booking(text: str) -> bool:
    """True if the message suggests the caller wants to book/appointment/reservation."""
    if not text or len(text.strip()) < 2:
        return False
    t = text.lower()
    return any(
        k in t
        for k in (
            "book",
            "appointment",
            "reservation",
            "reserve",
            "schedule",
            "available",
            "slot",
            "time for",
        )
    )


def _conversation_user_text(conversation_history: Optional[list]) -> str:
    if not conversation_history:
        return ""
    parts = [
        (m.get("content") or "").strip()
        for m in conversation_history
        if (m.get("role") or "").strip() == "user"
    ]
    return " ".join(p for p in parts if p)


def _caller_indicated_stylist_choice(
    user_text: str, info: Optional[dict] = None
) -> bool:
    t = (user_text or "").lower()
    if not t.strip():
        return False
    if any(p in t for p in _STYLIST_NO_PREF_PHRASES):
        return True
    for s in (info or config_service.get_business_info()).get("staff") or []:
        name = (s.get("name") or "").strip()
        if not name:
            continue
        nl = name.lower()
        if len(name) == 1 and nl == "a":
            # Avoid "book a haircut" — only stylist-context uses of the name A.
            if re.search(
                r"\b(with|stylist|see|prefer|choose)\s+a\b|\ba\s+(please|for|at)\b", t
            ):
                return True
            continue
        if re.search(rf"\b{re.escape(nl)}\b", t):
            return True
        if _name_said_aloud(nl, t):
            return True
    return False


def _staff_id_from_spoken_text(user_text: str, info: Optional[dict] = None) -> Optional[str]:
    """The roster id of a stylist the caller named out loud, if any.

    The staff field of a BOOKING line is filled in by the model, and it does not always
    fill it in — or it writes the name the way the caller said it ("Terence") rather
    than the way the roster spells it ("Terrance"), which the exact-match resolver
    rejects. Either way the booking arrives with no stylist and the caller is asked
    which stylist they want, for the second and third time. The name is right there in
    what they said; read it from there.

    Latest mention wins: a caller who changes their mind names the new stylist last.
    """
    t = (user_text or "").lower()
    if not t.strip():
        return None
    best: Optional[tuple[int, str]] = None
    for s in (info or config_service.get_business_info()).get("staff") or []:
        name = (s.get("name") or "").strip()
        sid = (s.get("id") or "").strip()
        if not name or not sid:
            continue
        nl = name.lower()
        pos = -1
        for m in re.finditer(rf"\b{re.escape(nl)}\b", t):
            pos = m.start()
        if pos < 0 and _name_said_aloud(nl, t):
            pos = 0
        if pos < 0:
            continue
        if best is None or pos >= best[0]:
            best = (pos, sid)
    return best[1] if best else None


_STYLIST_ASK_RE = re.compile(r"which stylist|stylist would you|prefer.{0,20}stylist", re.I)


def _stylist_asked_too_many_times(
    conversation_history: Optional[list], *, limit: int = 2
) -> bool:
    """Have we already asked this caller which stylist they want, twice?

    Lana's second test call: she asked for a haircut and an all-over color with
    Terrance and "got stuck in a loop of the AI asking what stylist I wanted when I
    had already said." Whatever makes the answer unreadable — a mis-heard name, a
    stylist who isn't on the roster, a model that leaves the field blank — asking a
    third time will not fix it, and the caller has no way to escape it.

    So the third time, we stop asking and take the request with no stylist on it. A
    request the salon has to assign is a small piece of work for them; a caller who
    gives up is a lost customer.
    """
    asked = sum(
        1
        for m in (conversation_history or [])
        if (m.get("role") or "").strip() == "assistant"
        and _STYLIST_ASK_RE.search(m.get("content") or "")
    )
    return asked >= limit


def _name_said_aloud(name_lower: str, text_lower: str) -> bool:
    """Did the caller say this name, allowing for how speech comes back?

    The roster spelled "Terrance"; the caller said "Terrence" and the transcript came
    back "Terence". Exact matching decided no stylist had been named and asked again
    — and again, because the caller kept saying the same word. One vowel, and the
    call could not complete.

    Guarded by a shared three-letter prefix rather than distance alone. Distance <= 2
    on its own also matches Tyler against Taylor, which are different people; the
    prefix separates a mis-heard spelling from a genuinely different name, because
    speech errors land in the middle and end of a name far more than the start.
    """
    if len(name_lower) < 4:
        return False
    head = name_lower[:3]
    for token in re.findall(r"[a-z']+", text_lower):
        if len(token) < 4 or abs(len(token) - len(name_lower)) > 1:
            continue
        if token[:3] != head:
            continue
        if _edit_distance_at_most(token, name_lower, 2):
            return True
    return False


def _edit_distance_at_most(a: str, b: str, limit: int) -> bool:
    """Levenshtein distance <= limit. Small strings, so clarity over cleverness."""
    if abs(len(a) - len(b)) > limit:
        return False
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        if min(cur) > limit:
            return False
        prev = cur
    return prev[-1] <= limit


def _caller_indicated_service_choice(
    user_text: str, info: Optional[dict] = None
) -> bool:
    biz = info or config_service.get_business_info()
    services = config_service._normalize_service_entries(biz.get("services") or [])
    if not services:
        return True
    t = (user_text or "").lower()
    if not t.strip():
        return False
    for s in services:
        nm = (s.get("name") or "").strip()
        if not nm:
            continue
        nml = nm.lower()
        if nml in t or re.search(rf"\b{re.escape(nml)}\b", t):
            return True
    return False


def _staff_choice_required(info: Optional[dict] = None) -> bool:
    biz = info or config_service.get_business_info()
    names = [
        (s.get("name") or "").strip()
        for s in (biz.get("staff") or [])
        if (s.get("name") or "").strip()
    ]
    return len(names) >= 2


# Phrases that mean the ASSISTANT was taking booking details. Deliberately about
# collecting specifics ("what time", "which day", "your name for") rather than the
# generic "would you like to book?" offer, which it makes on almost every call.
_ASSISTANT_TAKING_DETAILS = (
    "what time",
    "which day",
    "what day",
    "day and time",
    "specify a time",
    "your name",
    "which stylist",
    "prefer for your",
    "request for",
)


def _conversation_suggests_booking(conversation_history: Optional[list]) -> bool:
    """Could this call contain a booking worth recovering at the end?

    Gates the end-of-call reconciler, so a false negative silently loses a request
    that the caller was told had been sent.

    Keyword-matching the CALLER alone was not enough. A real call: "I wanna dye my
    hair bright blue" / "Yeah" / "Thursday afternoon" / "That's fine" / "Anyone's
    fine" / "Raj" — a complete booking in which the caller never once says book,
    appointment or schedule, because the AI supplies all of that vocabulary and they
    just answer it. The gate said no intent and the request was dropped.

    So an assistant that was actively collecting booking details counts too. That
    costs an extra extraction call on some conversations that turn out not to be
    bookings; losing a real one is far worse.
    """
    history = conversation_history or []
    for m in history:
        if (m.get("role") or "").strip() == "user" and _suggests_booking(
            m.get("content") or ""
        ):
            return True
    # Assistant-led: only once the caller has actually engaged, so a single question
    # answered by an offer to book doesn't drag every call into the extractor.
    if _count_booking_user_turns(history) < 2:
        return False
    for m in history:
        if (m.get("role") or "").strip() != "assistant":
            continue
        t = (m.get("content") or "").lower()
        if any(p in t for p in _ASSISTANT_TAKING_DETAILS):
            return True
    return False


def _count_booking_user_turns(conversation_history: Optional[list]) -> int:
    return sum(
        1
        for m in (conversation_history or [])
        if (m.get("role") or "").strip() == "user" and (m.get("content") or "").strip()
    )


def _assistant_awaiting_message_content(conversation_history: Optional[list]) -> bool:
    """True when the assistant's most recent turn asked the caller for the CONTENT of a message to
    leave (take-a-message mode). Narrow to content-request phrasing — not the generic "I can take a
    message" offer or the disambiguation question, which would over-trigger / loop."""
    for m in reversed(conversation_history or []):
        if (m.get("role") or "").strip() != "assistant":
            continue
        t = (m.get("content") or "").lower()
        return any(
            p in t
            for p in (
                "what message",
                "message would you like",
                "like me to leave",
                "leave for the team",
                "message to say",
                "the message to be",
            )
        )
    return False


_RELAY_MARKERS = (
    "tell them",
    "tell her",
    "tell him",
    "let them know",
    "let her know",
    "let him know",
    "have them know",
    "leave a message",
    "leave them a message",
    "message that",
    "message saying",
    "pass along",
    "pass it along",
)
_BOOKING_WORDS = ("appointment", "book", "schedul", "reschedul", "cancel")
_CHOSE_MESSAGE_MARKERS = (
    "leave it as a message",
    "just a message",
    "just leave a message",
    "as a message",
    "leave the message",
    "just the message",
)


def _text_mentions_booking(text: str) -> bool:
    return any(w in (text or "").lower() for w in _BOOKING_WORDS)


def _text_has_relay_marker(text: str) -> bool:
    return any(w in (text or "").lower() for w in _RELAY_MARKERS)


def _caller_chose_to_leave_message(text: str) -> bool:
    return any(w in (text or "").lower() for w in _CHOSE_MESSAGE_MARKERS)


def _voice_booking_nudge_message(
    conversation_history: list,
    info: Optional[dict] = None,
    *,
    appointment_created: bool = False,
) -> Optional[str]:
    """Inject during booking if GPT has not emitted BOOKING: yet.

    Never nudges once an appointment exists for this call: the BOOKING: directive is stripped
    from the spoken reply before it reaches conversation_history, so the nudge can't tell from
    the transcript alone that a booking already landed. Without this guard a caller who keeps
    talking after booking ("Okay, thanks!") gets nudged into a SECOND BOOKING: line — creating
    a duplicate appointment AND a duplicate confirmation SMS.
    """
    if appointment_created:
        return None
    biz = info or config_service.get_business_info()
    if not _conversation_suggests_booking(conversation_history):
        return None
    last_user = latest_user_message(conversation_history)
    awaiting_msg = _assistant_awaiting_message_content(conversation_history)
    # Caller committed to just leaving a message (and it isn't itself a booking request) — let the
    # AI capture it; don't push booking.
    if last_user and _caller_chose_to_leave_message(last_user) and not _text_mentions_booking(last_user):
        return None
    # Caller voiced a booking/reschedule/cancel AS a message ("tell them I want an appointment"), or
    # said something booking-ish while we're capturing a message. Ask which they meant rather than
    # assuming — don't start booking and don't silently record it.
    if last_user and _text_mentions_booking(last_user) and (
        awaiting_msg or _text_has_relay_marker(last_user)
    ):
        return (
            "DISAMBIGUATION REMINDER: The caller mentioned booking, rescheduling, or canceling "
            "while leaving (or being offered) a message. Do NOT start booking and do NOT silently "
            "record it. Ask ONE short question: whether they'd like you to take care of it right "
            "now, or just leave it as a message for the team. Then act on their answer."
        )
    # Otherwise, still in message mode: don't nudge toward booking.
    if awaiting_msg:
        return None
    turns = _count_booking_user_turns(conversation_history)
    user_text = _conversation_user_text(conversation_history)

    last_user = latest_user_message(conversation_history)
    if last_user and caller_message_suggests_pricing(last_user):
        return (
            "BOOKING REMINDER: Caller asked about price or cost. This is a normal business question—not off-topic. "
            "Answer briefly using the dollar amounts in the Services menu in your system prompt; "
            "speak naturally (e.g. a long cut runs around fifty dollars). "
            "Do NOT say you are not sure or deflect to booking without giving the price when it is listed. "
            "After answering, invite them to continue scheduling if they were booking."
        )

    services = config_service._normalize_service_entries(biz.get("services") or [])
    ctx = booking_context_from_business(biz)

    # Service-first: when a service menu exists, get the service before the stylist.
    if services and not service_choice_resolved(conversation_history, ctx):
        if turns >= 2 and not assistant_asked_service_recently(conversation_history):
            return (
                f"BOOKING REMINDER: This caller wants an appointment ({turns} user turns). "
                "Ask ONE short question: which service from the menu they'd like. Do NOT ask which "
                "stylist yet—after they choose a service, suggest only the stylists who provide it."
            )
        return None

    # Service chosen (or no menu) → now resolve the stylist if required.
    if _staff_choice_required(biz) and not _caller_indicated_stylist_choice(user_text, biz):
        if turns >= 2:
            # Inject the EXACT eligible stylists for the chosen service (computed
            # deterministically) so the model reads the list instead of reasoning about who
            # qualifies — gpt-4o/Haiku both over-listed stylists (e.g. offering a stylist who
            # doesn't do the service) when left to infer it.
            chosen_service, _svc_req = booking_service._normalize_service_choice_for_booking(
                user_text, biz
            )
            eligible = _stylists_offering_service(biz, chosen_service) if chosen_service else []
            if chosen_service and eligible:
                who = ", ".join(eligible[:6])
                return (
                    f"BOOKING REMINDER: Caller picked {chosen_service} ({turns} turns) but no "
                    "stylist yet. Ask ONE short question: which stylist they'd prefer, or if anyone "
                    f"is fine. ONLY these stylists provide {chosen_service}: {who}. Name ONLY these "
                    "stylists — never mention any other stylist for this service."
                )
            return (
                f"BOOKING REMINDER: Caller picked a service ({turns} turns) but no stylist yet. "
                "Ask ONE short question: which stylist they prefer (or anyone is fine), suggesting "
                "only those who provide the chosen service."
            )
        return None

    if turns < 3:
        return None
    return (
        f"BOOKING REMINDER: After {turns} turns you have enough details. "
        "Output BOOKING: name|phone|email|date|time|reason|staff on this turn. "
        "Never say the appointment is confirmed until BOOKING is output."
    )


def _services_from_recent_user_turns(
    conversation_history: Optional[list], biz: dict
) -> List[str]:
    """The services named in the caller's most recent turn that named any.

    Read newest-first rather than over the whole transcript: a caller who asks what a
    haircut costs and then books a colour has said both words on the call, and rolling
    them together would book them for a service they never asked for. What they last
    asked for is what they want — and it takes every service in that one turn, so "a
    haircut and an all over color" stays two services.
    """
    for m in reversed(conversation_history or []):
        if (m.get("role") or "").strip() != "user":
            continue
        text = (m.get("content") or "").strip()
        if not text:
            continue
        names, _ = booking_service.normalize_service_choices_for_booking(text, biz)
        if names:
            return names
    return []


def booking_details_recap_note(
    conversation_history: Optional[list], info: Optional[dict] = None
) -> Optional[str]:
    """What the caller has already told us, handed back to the model each turn.

    Lana's first test call: she asked for a haircut on a day that was not available,
    and when she offered a different day the AI asked what service she wanted — again.
    Nothing was lost from the transcript; the model simply treated the new day as the
    start of a new booking. Her question afterwards was the right one: "Can the AI
    remember the service if you need to book a different day, or will customers have
    to repeat the service each time?"

    Rather than hope a longer instruction fixes it, the service and stylist are pulled
    out of what the caller actually said — deterministically, the same matchers the
    booking validator uses — and put in front of the model as facts it already has.
    """
    biz = info or config_service.get_business_info()
    if not _conversation_suggests_booking(conversation_history):
        return None
    user_text = _conversation_user_text(conversation_history)
    if not user_text.strip():
        return None
    services = _services_from_recent_user_turns(conversation_history, biz)
    stylist = ""
    sid = _staff_id_from_spoken_text(user_text, biz)
    if sid:
        stylist = next(
            (
                (s.get("name") or "").strip()
                for s in (biz.get("staff") or [])
                if (s.get("id") or "").strip() == sid
            ),
            "",
        )
    elif any(p in user_text.lower() for p in _STYLIST_NO_PREF_PHRASES):
        stylist = "no preference — any available stylist is fine"
    known: List[str] = []
    if services:
        known.append("Service(s) they asked for: " + ", ".join(services))
    if stylist:
        known.append(f"Stylist: {stylist}")
    if not known:
        return None
    return (
        "DETAILS THE CALLER HAS ALREADY GIVEN YOU ON THIS CALL:\n"
        + "\n".join(f"- {k}" for k in known)
        + "\nDo NOT ask for any of these again — they have already answered. If the day "
        "or time they asked for does not work, KEEP the service and stylist above and ask "
        "only for a different day or time; never restart the booking or re-ask the service. "
        "If they name more than one service, the request covers ALL of them together — "
        "carry every one into the BOOKING line, joined with ' + '."
    )


# Structural patterns for "the model is claiming a booking exists". Regex (not literal
# substrings) so paraphrases and tense changes can't slip through — a literal blocklist is
# exactly how "Perfect, I've got everything I need." reached a live customer demo while
# "you're all set" was caught. These only ever run when NO BOOKING: line was emitted, so any
# match is a false promise by definition; the literal list below is kept as belt-and-braces.
#
# Deliberately NOT matched: "…will text you to confirm" on its own. The prompt explicitly
# tells the model to say that WHILE still gathering details, so blocking it would break the
# normal flow. Only a claim that the appointment itself exists/is complete counts.
_COMMITTED_BOOKING_RE = re.compile(
    "|".join(
        (
            # Completeness claims: "I've got everything I need", "that's all we need".
            r"\b(?:everything|all)\s+(?:i|we)\s+need\b",
            # Asserting the appointment exists as a thing to confirm//that is set.
            r"\bconfirm\s+your\s+(?:appointment|booking|visit|spot)\b",
            r"\byour\s+(?:appointment|booking)\s+(?:is|has\s+been)\s+(?:set|booked|scheduled|confirmed)\b",
            # Completion: you're booked/scheduled/set/confirmed/all set.
            r"\b(?:you'?re|you\s+are|your\s+all)\s+(?:all\s+set|booked|scheduled|confirmed)\b",
            r"\b(?:you'?re|you\s+are)\s+set\s+for\b",
            r"\ball\s+set\s+for\b",
            # I/we booked|scheduled|got|put you ...
            r"\b(?:i|we)\s*(?:'ve|'ll|\s+have|\s+will)?\s*(?:booked|scheduled)\s+you\b",
            r"\b(?:i|we)\s*(?:'ve|\s+have)?\s*got\s+you\s+(?:down|in|scheduled|booked)\b",
            r"\b(?:i|we)\s*(?:'ve|'ll|\s+have|\s+will)?\s*put\s+you\s+(?:down|in)\b",
            r"\bconsider\s+it\s+(?:booked|scheduled|done)\b",
        )
    ),
    re.IGNORECASE,
)


def _ai_implies_committed_booking(ai_text: str) -> bool:
    t = (ai_text or "").lower()
    if not t:
        return False
    if _COMMITTED_BOOKING_RE.search(t):
        return True
    return any(
        p in t
        for p in (
            "you're all set",
            "you are all set",
            "all set for",
            "you're booked",
            "you are booked",
            "i've booked",
            "i have booked",
            "have you scheduled",
            "you're scheduled",
            "you are scheduled",
            "i have you scheduled",
            "we have you scheduled",
            "got you scheduled",
            "got you down",
            "appointment is confirmed",
            "you're confirmed",
            "you are confirmed",
            "booking is confirmed",
            # Claims of completeness / a confirmation text on the way. This check only runs when
            # NO BOOKING: line was emitted, so any of these is a false promise by definition —
            # the caller hangs up believing they're booked and nothing exists. These exact
            # phrasings burned a live demo: "Perfect, I've got everything I need. We'll send a
            # text to confirm your appointment for a long cut with Andrew on Tuesday..."
            "i've got everything i need",
            "i have everything i need",
            "got everything i need",
            "confirm your appointment for",
            "confirm your appointment with",
            "consider it booked",
            "i've put you down",
            "i have put you down",
            "put you down for",
            "you're set for",
            "you are set for",
            "i've got you in",
            "i have got you in",
            "see you then",
            "see you tomorrow",
            "see you at",
            "we'll see you",
            "we will see you",
            # Mid-call CHANGE acknowledgments — the model narrates an update without re-emitting
            # BOOKING, so the change is lost unless the extraction net fires on these too.
            "i've updated",
            "i have updated",
            "updated your request",
            "updated your appointment",
            "i've changed",
            "i have changed",
            "changed your appointment",
            "changed it to",
            "i've switched",
            "i have switched",
            "switched your",
        )
    )


def _should_attempt_voice_booking_extraction(
    conversation_history: Optional[list], ai_text: str
) -> bool:
    """Retry BOOKING: extraction when the model spoke like it booked but omitted the marker."""
    if not _conversation_suggests_booking(conversation_history):
        return False
    if not config_service.staff_roster_ready_for_booking(config_service.get_business_info()):
        return False
    turns = _count_booking_user_turns(conversation_history)
    if turns < 3:
        return False
    if _ai_implies_committed_booking(ai_text or ""):
        return True
    t = (ai_text or "").lower()
    if any(
        p in t
        for p in (
            "scheduled",
            "see you",
            "tomorrow at",
            "today at",
            " at 3",
            " at 2",
            " at 1",
            " at 4",
            " at 5",
        )
    ):
        return True
    return turns >= 4


def _extract_booking_line_from_conversation(
    conversation_history: list,
    *,
    caller_memory: Optional[dict] = None,
) -> Optional[dict]:
    """Second GPT pass: emit BOOKING: line only from agreed transcript details."""
    biz = config_service.get_business_info()
    # Use business-local "today" so date math matches the caller's day, not UTC's
    # (which is already tomorrow on the US west coast after ~5pm).
    today = business_local_now(biz).date()
    today_str = today.isoformat()
    tomorrow_str = (today + timedelta(days=1)).isoformat()
    staff_names = [
        (s.get("name") or "").strip()
        for s in (biz.get("staff") or [])
        if (s.get("name") or "").strip()
    ]
    service_names = [
        (s.get("name") or "").strip()
        for s in config_service._normalize_service_entries(biz.get("services") or [])
        if (s.get("name") or "").strip()
    ]
    mem_name = ((caller_memory or {}).get("name") or "").strip()
    transcript = "\n".join(
        f"{(m.get('role') or '').strip().upper()}: {(m.get('content') or '').strip()}"
        for m in (conversation_history or [])[-14:]
        if (m.get("content") or "").strip()
    )
    if not transcript.strip():
        return None
    sys = (
        "Extract appointment details from this phone transcript. "
        f"Today is {today_str}, tomorrow is {tomorrow_str}. "
        "If caller name, date, and time are all clearly agreed, reply with EXACTLY one line:\n"
        "BOOKING: name|phone|email|date|time|reason|staff\n"
        "Field order is FIXED: (1) caller name, (2) phone, (3) email, (4) date YYYY-MM-DD, "
        "(5) time — copy the agreed clock time WITH its am/pm period exactly as spoken, "
        "e.g. '3 PM', '9:30 AM', '12 PM' for noon; do NOT convert to 24-hour yourself. "
        "NEVER put a stylist name in the time field, "
        "(6) service/reason from menu, (7) stylist name.\n"
        "Leave phone and email empty. reason=exact service from menu if known. "
        "If the caller asked for MORE THAN ONE service in this visit (\"a haircut and an "
        "all over color\", \"add a highlight\"), put EVERY one of them in the reason field "
        "joined with ' + ' — e.g. 'Haircut + All Over Color'. Dropping one loses a service "
        "the caller asked for. "
        "staff=stylist name if chosen.\n"
        "If the caller CHANGED a detail during the call (e.g. asked for a different time, day, "
        "service, or stylist), use the LATEST value they agreed to — not the earlier one.\n"
        f"Staff: {', '.join(staff_names) or 'none'}. "
        f"Services: {', '.join(service_names) or 'any'}.\n"
        f"Caller name on file: {mem_name or 'unknown'}.\n"
        # A returning caller often never says their name, because the AI never asks —
        # it already greeted them by it. Refusing to use the name we hold means the
        # whole request is dropped at the end of a call where the day, time and service
        # were all agreed. The name is from this phone number's own history, not a
        # guess.
        + (
            f"The caller did not have to state their name: if the transcript does not "
            f"contain one, use the name on file ({mem_name}) in field 1.\n"
            if mem_name
            else ""
        )
        + "If date or time is missing or ambiguous, reply with exactly: NONE. "
        "Reply NONE for the name only when there is no name in the transcript AND none "
        "on file."
    )
    try:
        raw = (
            llm_provider.chat(
                model=VOICE_LLM_MODEL,
                messages=[
                    {"role": "system", "content": sys},
                    {"role": "user", "content": transcript},
                ],
                temperature=0,
                max_tokens=120,
            )
            or ""
        ).strip()
    except Exception as e:
        logger.warning("voice_booking_extraction_failed: %s", e)
        return None
    if not raw or raw.upper().startswith("NONE"):
        return None
    parsed = parse_booking(raw)
    if not parsed:
        return None
    biz = config_service.get_business_info()
    ctx = booking_context_from_business(biz)
    prepared, repairs, reject = normalize_and_validate_booking(parsed, ctx)
    if reject:
        system_info(
            "voice_booking_extraction_rejected",
            reason=reject,
            repairs=repairs or None,
        )
        return None
    if repairs:
        system_info("voice_booking_extraction_repaired", repairs=repairs)
    return prepared


def _prepare_parsed_booking(
    booking: dict,
    *,
    info: Optional[dict] = None,
    caller_memory: Optional[dict] = None,
) -> tuple[Optional[dict], list[str], Optional[str]]:
    """Sanitize and validate date/time on a parsed BOOKING payload."""
    _apply_booking_customer_name(booking, caller_memory=caller_memory, info=info)
    ctx = booking_context_from_business(info or config_service.get_business_info())
    return normalize_and_validate_booking(booking, ctx)


def booking_reject_recovery_text(
    reject: Optional[str],
    *,
    raw_time: str = "",
    raw_date: str = "",
) -> Optional[str]:
    """What to say instead when a BOOKING line was thrown out.

    A rejected line is not always a problem: the model emits partial markers while it is
    still collecting details, and its own question should go through untouched. It IS a
    problem when the model filled the field in and we could not use it — the line is
    dropped, nothing is recorded, and the reply typically tells the caller their request
    is in. Those cases get a specific re-ask; everything else returns None (say nothing).
    """
    time_filled = bool((raw_time or "").strip())
    date_filled = bool((raw_date or "").strip())

    if reject == "invalid_time" and time_filled:
        return (
            "Sorry — I didn't catch that as a specific time. "
            "What time would you like? Something like 2 PM."
        )
    if reject == "past_time" and time_filled:
        return (
            "That time has already passed today. "
            "What time would you like instead?"
        )
    if reject == "past_date" and date_filled:
        return "That date has already passed. What day would you like instead?"
    if reject == "invalid_date" and date_filled:
        return "Sorry — I didn't catch the day. What day would you like to come in?"
    return None


def parse_booking(ai_text: str) -> Optional[dict]:
    """If AI responded with BOOKING: name|phone|email|date|time|reason|staff_optional, return dict; else None.

    The marker may appear after prose on the same line or after newlines — not only at line start.
    Empty fields are allowed (e.g. name|||date|time|reason with ||| for missing phone/email).
    """
    if not ai_text or "BOOKING:" not in ai_text.upper():
        return None
    m = re.search(r"(?is)BOOKING:\s*([^\n]+)", ai_text)
    if not m:
        return None
    rest = (m.group(1) or "").strip()
    vals = [v.strip() for v in rest.split("|")]
    # The model sometimes drops one of the always-empty phone/email fields (e.g.
    # "Raj||2026-07-06|2:00 PM|..."), which shifts the date into an earlier slot and gets the
    # whole booking thrown out as invalid_date. Realign by the ISO date: pad empty fields before
    # it so it lands in the date position (index 3).
    _date_idx = next(
        (i for i, v in enumerate(vals) if re.match(r"^\d{4}-\d{2}-\d{2}$", v)), None
    )
    if _date_idx is not None and _date_idx < 3:
        vals = vals[:_date_idx] + [""] * (3 - _date_idx) + vals[_date_idx:]
    if len(vals) < 5:
        return None
    return {
        "name": vals[0] if len(vals) > 0 else "",
        "phone": vals[1] if len(vals) > 1 else "",
        "email": vals[2] if len(vals) > 2 else "",
        "date": vals[3] if len(vals) > 3 else "",
        "time": vals[4] if len(vals) > 4 else "",
        "reason": vals[5] if len(vals) > 5 else "",
        "staff": vals[6] if len(vals) > 6 else "",
    }


def _caller_phone_for_booking(booking_phone: Optional[str], from_num: str) -> str:
    """Caller ID is authoritative for voice bookings. Use the phone the model emitted only if it
    is a real number (>=7 digits); otherwise fall back to the caller's Twilio number. Guards
    against the model copying the literal 'phone' placeholder from the BOOKING template into the
    field (which then showed up as 'Phone: phone' in the confirmation text)."""
    ai = (booking_phone or "").strip()
    digits = sum(c.isdigit() for c in ai)
    return ai if digits >= 7 else (from_num or "")


# What the caller HEARS once the appointment row exists. The model's own reply is
# discarded at this point, so this text is the last thing they're told — and it was
# hardcoded to the internal flow ("reply YES or CONFIRM, that locks the time"). In
# request mode there is no slot to lock and nothing for the caller to confirm; the
# salon confirms to them. It also has to agree with the SMS, which asks for no reply.
_SPOKEN_AFTER_BOOKING = {
    ("request", "texted"): (
        "I've sent your request to the salon and texted you the details. This is a "
        "request, not a confirmed appointment — they'll confirm your time with you "
        "shortly. Just reply to that text if anything needs changing."
    ),
    ("request", "sms_failed"): (
        "Your request is saved — it's a request, not a confirmed appointment, and the "
        "salon will confirm your time with you. We couldn't send you a text from this "
        "line right now."
    ),
    ("request", "no_phone"): (
        "We've saved your request. It's a request rather than a confirmed appointment, "
        "and the salon will confirm your time with you."
    ),
    ("internal", "texted"): (
        "I've texted you the details. Please check your phone and reply YES or CONFIRM "
        "when everything looks right—that locks the time and sends your request to "
        "the shop. The time is not finalized until you confirm by text."
    ),
    ("internal", "sms_failed"): (
        "Your visit request is saved. We could not send the confirmation text from this "
        "line right now—please text YES to this business number from your mobile when "
        "you're ready to confirm, or call us back."
    ),
    ("internal", "no_phone"): (
        "We've saved your booking request. We don't have a mobile number on this call to "
        "text you—please call back or text us from your phone with YES to confirm."
    ),
    # The caller is on a landline. Twilio told us the number cannot receive SMS at all,
    # so promising a text would be a promise we already know we cannot keep.
    ("request", "not_textable"): (
        "This number can't receive texts, so I can't send you the details — but your "
        "request is with the salon and they'll call you on this number to confirm the time."
    ),
    ("internal", "not_textable"): (
        "This number can't receive texts, so I can't send you the details — but I've saved "
        "your request and the shop will call you back on this number to confirm."
    ),
    # A change late in the same call. They already have a text; another one seconds later
    # says nothing new, so the final details go out once, when the call ends.
    ("request", "deferred"): (
        "I've updated your request with the salon. I'll send you one text with the final "
        "details when we hang up, and they'll confirm your time with you."
    ),
    ("internal", "deferred"): (
        "I've updated your request. I'll send you one text with the final details when we "
        "hang up."
    ),
}


_SENT_CONFIRMATIONS: dict = {}
_SENT_CONFIRMATIONS_MAX = 500

# How many confirmation texts one call may ever produce, in total.
#
# Lana's first test call ended with five text messages. Every amendment supersedes the
# draft and writes a new appointment row, and each one texted the caller again — from
# the caller's side, five texts about one haircut with nothing to say which is current.
#
# One text when the request is taken, and (only if something changed after that) one
# more when the call ends carrying the final details. Never a stream of them mid-call.
DEFAULT_BOOKING_TEXTS_PER_CALL = 2

_CALL_TEXT_COUNTS: dict = {}
_CALL_TEXT_COUNTS_MAX = 500


def _booking_texts_per_call_limit() -> int:
    raw = (os.getenv("MAX_BOOKING_TEXTS_PER_CALL") or "").strip()
    try:
        return max(1, int(raw)) if raw else DEFAULT_BOOKING_TEXTS_PER_CALL
    except ValueError:
        return DEFAULT_BOOKING_TEXTS_PER_CALL


def _in_call_booking_text_budget() -> int:
    """Texts allowed while the caller is still on the line — one fewer than the total,
    so there is always one left for the final details at the end of the call."""
    return max(1, _booking_texts_per_call_limit() - 1)


def booking_texts_sent_on_call(call_sid: str) -> int:
    return int(_CALL_TEXT_COUNTS.get((call_sid or "").strip(), 0))


def _note_booking_text_sent(call_sid: str) -> None:
    sid = (call_sid or "").strip()
    if not sid:
        return
    if len(_CALL_TEXT_COUNTS) >= _CALL_TEXT_COUNTS_MAX:
        _CALL_TEXT_COUNTS.clear()
    _CALL_TEXT_COUNTS[sid] = _CALL_TEXT_COUNTS.get(sid, 0) + 1


def _confirmation_already_sent(call_sid: str, to_number: str, body: str) -> bool:
    """Has this exact confirmation already gone to this number on this call?

    In-process and bounded rather than a database table: it only has to survive the
    seconds between two bookings in one live call, and a duplicate text after a
    restart is a far smaller problem than a table to maintain. Without a call_sid we
    cannot tell repeats apart, so nothing is suppressed.
    """
    sid = (call_sid or "").strip()
    if not sid:
        return False
    key = (sid, (to_number or "").strip(), (body or "").strip())
    if key in _SENT_CONFIRMATIONS:
        return True
    if len(_SENT_CONFIRMATIONS) >= _SENT_CONFIRMATIONS_MAX:
        _SENT_CONFIRMATIONS.clear()
    _SENT_CONFIRMATIONS[key] = True
    return False


def post_booking_spoken_confirmation(status: str, outcome: str) -> str:
    """Spoken confirmation after a booking lands.

    status is the appointment's own status — pending_review means request mode, which
    is set only by the external-booking branch. outcome is one of "texted",
    "sms_failed", "no_phone".
    """
    mode = "request" if (status or "").strip() == "pending_review" else "internal"
    return _SPOKEN_AFTER_BOOKING[(mode, outcome)]


def _end_call_after_booking_enabled() -> bool:
    """Hang up once the caller has been told their details are on the way.

    Lana's first test call: "After it tells me that I will get a text with the details,
    it does not disconnect the call. It continues to repeat itself." There is nothing
    left to say at that point — the receptionist has taken the request and told them
    what happens next — but the pipeline always set up another listen, so the AI kept
    talking, kept re-emitting BOOKING lines, and kept texting.

    Set VOICE_END_CALL_AFTER_BOOKING=0 to keep the line open instead.
    """
    raw = (os.getenv("VOICE_END_CALL_AFTER_BOOKING") or "").strip().lower()
    return raw not in ("0", "false", "no", "off")


def _goodbye_line(info: Optional[dict] = None) -> str:
    biz = info if info is not None else config_service.get_business_info()
    name = config_service.customer_facing_name(biz) if biz else ""
    return (
        f"Thanks for calling {name}. Have a great day. Goodbye."
        if name
        else "Thanks for calling. Have a great day. Goodbye."
    )


ANYTHING_ELSE_LINE = "Is there anything else I can help you with before I let you go?"


def _close_call_after_booking(call_data: dict, ai_text: str) -> str:
    """Wind the call up once the request is taken — over two turns, not one.

    Hanging up the instant the confirmation is spoken also takes away the moment a
    caller says "oh — can you add a highlight to that", which is the other half of
    Lana's feedback. So the confirmation ends by offering one last thing, and whatever
    they say next is answered and then the call ends. One extra turn, and the call
    still cannot run on: the goodbye after that turn is unconditional.
    """
    if not _end_call_after_booking_enabled():
        return ai_text
    text = (ai_text or "").strip()
    if call_data.get("post_booking_grace_offered"):
        call_data.pop("post_booking_grace_offered", None)
        call_data["end_call_after_reply"] = True
        goodbye = _goodbye_line()
        return f"{text} {goodbye}".strip() if text else goodbye
    call_data["post_booking_grace_offered"] = True
    return f"{text} {ANYTHING_ELSE_LINE}".strip() if text else ANYTHING_ELSE_LINE


def _booking_identity(booking: dict) -> tuple:
    """What makes two BOOKING lines the same request rather than a change of mind.

    Canonical, not literal. The identity is stored from a booking dict the validator and
    the create path have already rewritten — reason to the menu spelling, staff to the
    roster spelling — while the next turn's line arrives raw. Comparing one against the
    other, "Terence"/"Haircut and all over color" never matched the "Terrance"/"Haircut +
    All Over Color" it had just been turned into, so an identical re-emit read as an
    amendment and superseded the request it was a copy of.
    """
    def norm(v):
        return re.sub(r"\s+", " ", str(v or "").strip()).lower()

    reason_raw = (booking.get("reason") or "").strip()
    try:
        services, _ = booking_service.normalize_service_choices_for_booking(reason_raw)
        reason_key = norm(booking_service.format_service_choices(services)) or norm(reason_raw)
    except Exception:
        reason_key = norm(reason_raw)
    staff_raw = (booking.get("staff") or "").strip()
    try:
        staff_key = resolve_staff_id_from_booking_fragment(staff_raw) or norm(staff_raw)
    except Exception:
        staff_key = norm(staff_raw)
    return (
        norm(booking.get("date")),
        booking_service._normalize_time_to_hhmm(booking.get("time") or "")
        or norm(booking.get("time")),
        reason_key,
        norm(booking.get("name")),
        staff_key,
    )


def _strip_booking_directive_for_voice(ai_text: str) -> str:
    """Remove BOOKING:... from model output so it is never read aloud by TTS.

    Returns "" when the directive WAS the whole reply — every caller substitutes its own
    wording for an empty result. Falling back to the raw text here, which is what it used
    to do, meant a reply consisting only of the marker was read down the phone: "BOOKING
    colon Lana pipe pipe pipe two thousand twenty six dash…". It stayed hidden while the
    booking path always replaced the reply with a confirmation, and surfaces the moment a
    line is parsed and then dropped — a repeat, or a change the caller never asked for.
    """
    if not ai_text or "BOOKING:" not in ai_text.upper():
        return (ai_text or "").strip()
    return re.sub(r"(?is)\s*BOOKING:\s*[^\n]+", "", ai_text).strip()


def _strip_message_directive_for_voice(ai_text: str) -> str:
    """Remove a MESSAGE:... directive line so it is never read aloud by TTS.

    Empty when the directive was the whole reply — see the BOOKING version above.
    """
    if not ai_text or "MESSAGE:" not in ai_text.upper():
        return (ai_text or "").strip()
    return re.sub(r"(?is)\s*MESSAGE:\s*[^\n]+", "", ai_text).strip()


def _store_caller_message(call_data: dict, body: str) -> bool:
    """Persist a caller's message (from a MESSAGE: directive) so it appears in the
    dashboard. Caller name comes from caller-memory, phone from the live call. Graceful:
    never raises into the voice turn."""
    body = (body or "").strip()
    if not body:
        return False
    client_id = str(call_data.get("client_id") or "").strip() or None
    caller_mem = call_data.get("caller_memory") or {}
    name = (caller_mem.get("name") or "").strip()
    phone = (call_data.get("from_number") or "").strip()
    low = body.lower()
    urgency = (
        "high"
        if any(w in low for w in ("urgent", "emergency", "asap", "right away"))
        else "normal"
    )
    data = {
        "caller_name": name,
        "caller_phone": phone,
        "message": body[:2000],
        "urgency": urgency,
        "status": "unread",
    }
    try:
        if runtime.USE_DB:
            database.db_messages_insert(data, client_id=client_id)
        else:
            data["id"] = len(runtime.messages) + 1
            data["created_at"] = datetime.now().isoformat()
            runtime.messages.append(data)
        return True
    except Exception as e:
        logger.warning("store_caller_message failed: %s", e, exc_info=True)
        return False


def resolve_staff_id_from_booking_fragment(fragment: Optional[str]) -> Optional[str]:
    frag = (fragment or "").strip()
    if not frag:
        return None
    biz = config_service.get_business_info()
    staff = biz.get("staff") or []
    for s in staff:
        sid = (s.get("id") or "").strip()
        if sid and frag == sid:
            return sid
        name = (s.get("name") or "").strip()
        if name and frag.lower() == name.lower():
            return sid if sid else None
    # The model writes the name as the caller said it — "Terence" for a roster that
    # spells it "Terrance" — and an exact match drops the stylist on the floor, which
    # sends the caller back round the "which stylist would you like?" loop.
    return _staff_id_from_spoken_text(frag, biz)


def _staff_name_set(info: Optional[dict] = None) -> set[str]:
    biz = info or config_service.get_business_info()
    return {
        (s.get("name") or "").strip().lower()
        for s in (biz.get("staff") or [])
        if (s.get("name") or "").strip()
    }


def _caller_memory_name_usable(mem_name: str, staff_names: set[str]) -> bool:
    n = (mem_name or "").strip()
    if len(n) < 2:
        return False
    low = n.lower()
    if low in staff_names or low in ("there", "caller", "customer", "guest"):
        return False
    return True


def _apply_booking_customer_name(
    booking: dict,
    *,
    caller_memory: Optional[dict] = None,
    info: Optional[dict] = None,
) -> None:
    """Ensure BOOKING field 1 is the caller's name, not a stylist from the roster."""
    biz = info or config_service.get_business_info()
    staff_names = _staff_name_set(biz)
    name = (booking.get("name") or "").strip()
    staff_frag = (booking.get("staff") or "").strip()
    mem_name = ((caller_memory or {}).get("name") or "").strip()
    mem_ok = _caller_memory_name_usable(mem_name, staff_names)

    if name and staff_names and name.lower() in staff_names:
        booking["name"] = mem_name if mem_ok else ""
        return

    if (
        name
        and staff_frag
        and name.lower() == staff_frag.lower()
        and staff_frag.lower() in staff_names
    ):
        booking["name"] = mem_name if mem_ok else ""
        return

    if not name and mem_ok:
        booking["name"] = mem_name


def _spoken_list(items: list, conjunction: str = "or") -> str:
    """"a, b or c" — read aloud, so no serial comma and no bullet points."""
    vals = [str(i).strip() for i in items if str(i or "").strip()]
    if not vals:
        return ""
    if len(vals) == 1:
        return vals[0]
    return f"{', '.join(vals[:-1])} {conjunction} {vals[-1]}"


def _stylists_offering_service(biz: dict, service_name: Optional[str]) -> list:
    """Names of stylists who provide the given service (their service_ids include it, or an
    empty service_ids means they do everything). Falls back to all stylists when the service
    is unknown or none match."""
    staff_rows = [s for s in (biz.get("staff") or []) if (s.get("name") or "").strip()]
    all_names = [(s.get("name") or "").strip() for s in staff_rows if (s.get("name") or "").strip()]
    if not service_name:
        return all_names
    svc_id = None
    for s in config_service._normalize_service_entries(biz.get("services") or []):
        if (s.get("name") or "").strip().lower() == service_name.strip().lower():
            svc_id = (s.get("id") or "").strip()
            break
    matched = []
    for st in staff_rows:
        nm = (st.get("name") or "").strip()
        if not nm:
            continue
        ids = st.get("service_ids") or []
        if not ids or (svc_id and svc_id in ids):
            matched.append(nm)
    return matched or all_names


def _staff_offers_service(biz: dict, staff_row: dict, service_name: Optional[str]) -> bool:
    """True if this stylist provides the service. Empty service_ids = does everything. An
    unknown/unmatched service is NOT blocked (we can't prove it isn't offered)."""
    if not service_name:
        return True
    ids = staff_row.get("service_ids") or []
    if not ids:
        return True
    svc_id = None
    for s in config_service._normalize_service_entries(biz.get("services") or []):
        if (s.get("name") or "").strip().lower() == service_name.strip().lower():
            svc_id = (s.get("id") or "").strip()
            break
    if not svc_id:
        return True
    return svc_id in ids


def _validate_booking_requirements(
    booking: dict,
    info: Optional[dict] = None,
    *,
    conversation_history: Optional[list] = None,
) -> tuple[bool, Optional[str], Optional[str], Optional[str]]:
    """
    Validate required stylist/service when configured.
    Returns: (ok, fail_message, staff_id, canonical_service_name)
    """
    biz = info or config_service.get_business_info()
    user_text = _conversation_user_text(conversation_history)
    staff_rows = [s for s in (biz.get("staff") or []) if (s.get("name") or "").strip()]
    staff_id = resolve_staff_id_from_booking_fragment(booking.get("staff"))

    service_names, service_required = booking_service.normalize_service_choices_for_booking(
        booking.get("reason"), biz
    )
    service_name = booking_service.primary_service_name(service_names, biz)
    # DIAGNOSTIC: pinpoint why the service is (re)asked — what was captured vs the menu,
    # and whether the conversation already indicates a service the matcher is missing.
    try:
        import booking_fields as _bf

        _ctx_dbg = booking_context_from_business(biz)
        system_info(
            "booking_service_check",
            service_required=service_required,
            reason_raw=(booking.get("reason") or "")[:40],
            normalized_service=service_name or "",
            service_resolved_in_convo=service_choice_resolved(conversation_history, _ctx_dbg),
            last_user_named_service=_bf.user_indicated_service_name(user_text, _ctx_dbg.service_names),
            menu=", ".join(sorted(_ctx_dbg.service_names))[:120],
            user_text_tail=(user_text or "")[-60:],
        )
    except Exception:
        pass

    booking_date = (booking.get("date") or "").strip()
    if booking_date:
        try:
            from business_hours import is_past_closing_for_date, same_day_after_hours_message

            target = date.fromisoformat(booking_date)
            if is_past_closing_for_date(biz, target):
                return False, same_day_after_hours_message(biz), staff_id, None
        except ValueError:
            pass
        # Shop-wide closure: never book on a closed date, regardless of stylist.
        import staff_schedule

        closed_msg = staff_schedule.shop_closure_message(biz.get("closures"), booking_date)
        if closed_msg:
            return False, closed_msg, staff_id, None

    ctx = booking_context_from_business(biz)
    service_choices = ", ".join(
        (s.get("name") or "").strip()
        for s in config_service._normalize_service_entries(biz.get("services") or [])[:5]
        if (s.get("name") or "").strip()
    )
    # SERVICE FIRST — get the service before the stylist. Trust a service that normalized to a
    # real menu item (the extraction only sets it when the caller named one); no redundant
    # re-confirm (which previously caused loops when STT phrasing differed, e.g. "shortcut").
    if service_required and not service_name:
        # The model left the reason field empty — most often after the caller changed the
        # day, which it treats as the start of a new booking. The caller already told us
        # what they want; re-asking is what Lana hit ("When I changed days, it had to ask
        # what service I wanted again"). Take it from what they said.
        recovered = _services_from_recent_user_turns(conversation_history, biz)
        if recovered:
            service_names = recovered
            service_name = booking_service.primary_service_name(recovered, biz)
            booking["reason"] = booking_service.format_service_choices(recovered)
            system_info(
                "booking_service_recovered_from_transcript",
                service=booking["reason"][:60],
            )
    if service_required and not service_name:
        msg = service_prompt_message(
            staff_name="",  # don't tie to a stylist yet — the stylist comes after the service
            service_choices=service_choices,
            already_asked=assistant_asked_service_recently(conversation_history),
        )
        return False, msg, staff_id, None

    # Half-named service ("a highlight" where the menu lists four of them, or one asked
    # for alongside a service that DID match). Ask which — never file the part we
    # understood and drop the rest, which is how a highlight goes missing.
    unclear = booking_service.service_candidates_needing_clarification(
        booking.get("reason"), biz
    )
    if unclear:
        system_info("booking_service_ambiguous", options=", ".join(unclear)[:120])
        return (
            False,
            f"Just to make sure I put the right thing down — did you want {_spoken_list(unclear)}?",
            staff_id,
            None,
        )

    # Everything the caller asked for, kept together: this is what goes in the reason
    # field, so a second service can't be quietly dropped between here and the salon.
    canonical_reason = booking_service.format_service_choices(service_names) or None

    # THEN STYLIST — suggest only the stylists who provide the chosen service.
    if staff_rows and not staff_id:
        # The caller named someone the model failed to put in the staff field (or spelled
        # differently to the roster). Resolve it from what they actually said rather than
        # asking again — asking again is how a caller who has already answered ends up
        # answering the same question until they hang up.
        staff_id = _staff_id_from_spoken_text(user_text, biz)
        if staff_id:
            # Write it back: the appointment is created from the booking dict, and it
            # re-resolves the stylist from this field. Leaving it empty would pass the
            # check here and still file the request with no stylist on it.
            booking["staff"] = next(
                (
                    (s.get("name") or "").strip()
                    for s in staff_rows
                    if (s.get("id") or "").strip() == staff_id
                ),
                booking.get("staff") or "",
            )
    if staff_rows and not staff_id:
        no_pref = any(p in user_text.lower() for p in _STYLIST_NO_PREF_PHRASES)
        if not no_pref and not _stylist_asked_too_many_times(conversation_history):
            choices = ", ".join(_stylists_offering_service(biz, service_name)[:5])
            msg = (
                "Great — which stylist would you like to see?"
                + (f" For that, we have {choices}." if choices else "")
                + " You can also say anyone if you have no preference."
            )
            return False, msg, None, None
    if (
        staff_id
        and _staff_choice_required(biz)
        and not _caller_indicated_stylist_choice(user_text, biz)
        and not _stylist_asked_too_many_times(conversation_history)
    ):
        choices = ", ".join(_stylists_offering_service(biz, service_name)[:5])
        msg = (
            "Before I lock this in, which stylist would you like?"
            + (f" For that service we have {choices}." if choices else "")
            + " Or say anyone if you have no preference."
        )
        return False, msg, None, None

    # Hard check: the chosen stylist must actually offer every service booked. Applies to
    # changes too (e.g. caller keeps the stylist but adds a service that stylist doesn't do).
    if staff_id and service_names:
        srow = next((s for s in staff_rows if str(s.get("id")) == str(staff_id)), None)
        if srow is not None:
            not_offered = [
                nm for nm in service_names if not _staff_offers_service(biz, srow, nm)
            ]
            if not_offered:
                name = (srow.get("name") or "").strip() or "That stylist"
                missing = " or ".join(not_offered)
                alt = ", ".join(_stylists_offering_service(biz, not_offered[0])[:5])
                msg = (
                    f"{name} doesn't do {missing}. "
                    + (f"For {missing} you can book {alt}. " if alt else "")
                    + "Would you like one of them, or a different service?"
                )
                return False, msg, staff_id, canonical_reason

    # Backstop: never book a stylist on a day/time they don't work, even if the AI tried to.
    if staff_id and booking_date:
        srow = next((s for s in staff_rows if str(s.get("id")) == str(staff_id)), None)
        if srow:
            import staff_schedule

            unavailable = staff_schedule.staff_unavailable_message(
                srow, booking_date, (booking.get("time") or "").strip()
            )
            if unavailable:
                return False, unavailable, staff_id, canonical_reason

    return True, None, staff_id, canonical_reason


def _create_appointment_from_booking(
    booking: dict,
    client_id_override: Optional[str] = None,
    reserve_slot_immediately: bool = True,
    caller_memory: Optional[dict] = None,
) -> Optional[dict]:
    """Create appointment from parsed BOOKING; check slot; return appointment_data or None (slot taken).
    Pass client_id_override from voice flow so appointment is stored under correct tenant (async task may not have context).
    When reserve_slot_immediately is False (voice), the row is created as pending_customer but the calendar slot
    is only reserved after the customer SMS-confirms (see handle_incoming_sms)."""
    date = (booking.get("date") or "").strip()
    time_raw = (booking.get("time") or "").strip()
    _business_info = config_service.get_business_info()
    ctx = booking_context_from_business(_business_info)
    # External-booking stores (e.g. Zenoti) keep their real calendar elsewhere, so we
    # never check or reserve a slot here — we just capture the request for staff to
    # enter on their side. Internal stores (the default, every other customer) keep
    # the full slot-checking + reserve flow below unchanged.
    external = config_service.is_external_booking(_business_info)
    time = normalize_booking_time(time_raw) or ""
    if not is_valid_booking_date(date) or not looks_like_booking_time(time, ctx):
        return None
    _apply_booking_customer_name(booking, caller_memory=caller_memory)
    name = (booking.get("name") or "").strip()
    if not name or not date or not time:
        return None
    cid_for_slot = (client_id_override or "").strip() or database._client_id()
    if cid_for_slot:
        database.set_request_client_id(cid_for_slot)
    staff_key = resolve_staff_id_from_booking_fragment(booking.get("staff"))
    canonical_services, _ = booking_service.normalize_service_choices_for_booking(
        booking.get("reason"), _business_info
    )
    if canonical_services:
        booking["reason"] = booking_service.format_service_choices(canonical_services)

    # --- Structural booking guards -------------------------------------------
    # These are enforced in code, not just asked for in the prompt, because a model
    # having an off day must not be able to book them anyway (same reasoning as the
    # false-booking-confirm guard). Both are no-ops unless the store configured them,
    # so every other customer is unaffected.
    requested_service = (booking.get("reason") or "").strip()
    # Checked per service, not on the joined text: "Haircut + Corrective Color" must
    # still be stopped, and it matches neither list as one string.
    checked_services = canonical_services or ([requested_service] if requested_service else [])
    consult_hit = next(
        (s for s in checked_services if config_service.service_requires_consult(s, _business_info)),
        None,
    )
    if consult_hit:
        # e.g. corrective color: nobody knows the scope, price, or who's qualified
        # until a stylist speaks to the guest.
        system_info(
            "booking_blocked_consult_required",
            service=consult_hit,
            client_id=cid_for_slot,
            name=name,
        )
        return None
    if checked_services and all(
        config_service.is_addon_service(s, _business_info) for s in checked_services
    ):
        # An add-on (conditioner, hot tools, length/master-stylist charge) rides along
        # with a real service — it can never be the whole appointment. Alongside one,
        # it is exactly what the caller asked for and goes through.
        system_info(
            "booking_blocked_addon_only",
            service=requested_service,
            client_id=cid_for_slot,
            name=name,
        )
        return None
    duration_min = booking_service._booking_duration_minutes(booking)
    # Superseding the caller's own earlier draft and checking slot availability are two
    # different jobs that used to be gated together. Only the availability check belongs
    # to internal mode — we can't consult a calendar we don't own. Clearing the caller's
    # stale row applies either way, and this function already knows how to retire a
    # pending_review one; leaving it gated meant a second BOOKING line in a request-mode
    # call left two identical requests for the salon to sort out.
    _supersede_pending_customer_drafts_for_slot(
        date,
        time,
        staff_key,
        client_id=cid_for_slot,
        phone=(booking.get("phone") or "").strip(),
    )
    if not external:
        if not booking_service.is_slot_available(date, time, duration_min, staff_key):
            booking_service._invalidate_booked_slots_cache()  # Next prompt build will see slot as taken
            blockers = booking_service._slot_blocking_details(
                date, time, duration_min, staff_key
            )
            system_info(
                "booking_create_failed_slot_taken",
                name=name,
                date=date,
                time=time,
                client_id=cid_for_slot,
                blocking=blockers,
            )
            return None
    appointment_data = {
        "name": name,
        "email": (booking.get("email") or "").strip(),
        "phone": (booking.get("phone") or "").strip(),
        "date": date,
        "time": time,
        "reason": (booking.get("reason") or "").strip() or "—",
        "source": "receptionist",
        # External: land straight in the store's approval queue (pending_review) — the
        # request goes to staff, not through an SMS-confirm gate the (older) caller may
        # never complete. Internal: unchanged (pending_customer -> SMS confirm).
        "status": "pending_review" if external else "pending_customer",
        "staff_id": staff_key,
    }
    if client_id_override:
        appointment_data["client_id"] = client_id_override
    if runtime.USE_DB:
        row = database.db_appointments_insert(appointment_data)
        apt_id = row["id"]
    else:
        apt_id = len(runtime.appointments) + 1
        appointment_data["id"] = apt_id
        appointment_data["created_at"] = datetime.now().isoformat()
        runtime.appointments.append(appointment_data)
    if reserve_slot_immediately and not external:
        if not booking_service.reserve_slot(date, time, apt_id, duration_min, staff_key):
            # Concurrent booking won the slot between our availability check and reserve.
            if runtime.USE_DB:
                try:
                    database.db_appointments_update(apt_id, status="cancelled", client_id=cid_for_slot)
                except Exception:
                    pass
            booking_service._invalidate_booked_slots_cache()
            system_info(
                "booking_create_failed_slot_taken_race",
                apt_id=apt_id, date=date, time=time, client_id=cid_for_slot,
            )
            return None
    appointment_data["id"] = apt_id
    appointment_data.setdefault("created_at", datetime.now().isoformat())
    system_info(
        "booking_created_request" if external else "booking_created_pending_customer",
        apt_id=apt_id,
        client_id=appointment_data.get("client_id") or "(request_context)",
        name=name,
        date=date,
        time=time,
        # DIAGNOSTIC: the exact time string the model emitted, before normalization.
        # If a caller asks for "2 PM" but this shows time_raw="12:00", the model
        # mis-converted to 24h; if time_raw="2 PM" and time="14:00", normalization is fine.
        # This is the line to grep (event=booking_created_pending_customer) if a stored
        # time is ever wrong. Logs are ephemeral on the host, so check within retention.
        time_raw=time_raw or None,
        time_changed_by_normalize=(time_raw or "").strip() != time,
        staff_id=staff_key,
        slot_reserved_immediately=reserve_slot_immediately,
    )
    return appointment_data


def _send_booking_confirmation_sms(
    apt: dict,
    call_data: dict,
    cid: Optional[str],
    call_sid: Optional[str],
    *,
    final_summary: bool = False,
) -> str:
    """Send the post-booking confirmation SMS for a freshly-created appointment and update
    caller memory. Returns the caller-facing AI text describing what happened. Shared by the
    live voice booking path and the end-of-call reconciliation backstop.

    final_summary marks the one text sent after the call ends, carrying the details as
    they finally stood; it spends the last of the per-call budget rather than deferring."""
    thanks_msg = booking_service._format_appointment_details_confirmation_sms(apt)
    to_number_sms = (
        (call_data.get("from_number") or "").strip()
        or (apt.get("phone") or "").strip()
        or ""
    )
    from_number_sms = (call_data.get("to_number") or "").strip() or None
    if not from_number_sms and cid and runtime.USE_DB:
        tenant_row = database.db_tenant_get_by_client_id(cid)
        if tenant_row:
            from_number_sms = (tenant_row.get("twilio_phone_number") or "").strip()
            sms_info("confirmation_sms_from_tenant_lookup", client_id=cid)
        else:
            sms_info("confirmation_sms_tenant_missing_for_from_override", client_id=cid)
    if not from_number_sms:
        from_number_sms = booking_service._tenant_sms_from_number()
    sms_info(
        "post_booking_confirmation_dispatch",
        client_id=cid,
        to_set=bool(to_number_sms),
        from_set=bool(from_number_sms),
    )
    # Request mode: the calendar is in another system, so there is no slot for the
    # caller to lock and nothing for them to confirm — the salon confirms to them.
    # Read off the row we just wrote; pending_review is set only by the external branch.
    is_request = (apt.get("status") or "").strip() == "pending_review"
    # One call, one confirmation. A caller who amends anything mid-call — the time,
    # the stylist, the service — supersedes the earlier draft and creates a new
    # appointment, and this used to text on every creation. A 140-second call
    # produced appointments 193, 194 and 195 and three IDENTICAL texts, 336 bytes
    # each, seconds apart. Nothing new was being said; the caller was told the same
    # thing three times and had no way to know it was one booking.
    #
    # Keyed on the exact body, so a genuine correction — a different time, a
    # different stylist — still goes out. Only the repeat is dropped.
    # The parameter first, then the dict. This read only call_data["call_sid"], which
    # is empty on this path, so the key was always blank and the suppression below
    # never once ran — it shipped inert. Same resolution order the reconcile path
    # already uses a few hundred lines down.
    call_sid_for_dedupe = (call_sid or call_data.get("call_sid") or "").strip()
    # Budget before dedupe: the dedupe cache records a body the first time it is asked
    # about, so checking it for a text we are about to defer would burn the entry and
    # then swallow the real send at the end of the call.
    #
    # One text while they are on the line. A later amendment is spoken now and texted
    # once at the end of the call, so the caller ends up with the final details and not
    # a thread of near-identical messages.
    if (
        to_number_sms
        and not final_summary
        and call_sid_for_dedupe
        and booking_texts_sent_on_call(call_sid_for_dedupe) >= _in_call_booking_text_budget()
    ):
        call_data["confirmation_text_deferred_apt_id"] = apt.get("id")
        sms_info(
            "post_booking_confirmation_deferred_to_call_end",
            client_id=cid,
            call_sid=call_sid_for_dedupe,
            apt_id=apt.get("id"),
            already_sent=booking_texts_sent_on_call(call_sid_for_dedupe),
        )
        return post_booking_spoken_confirmation(apt.get("status") or "", "deferred")
    if (
        to_number_sms
        and call_sid_for_dedupe
        and booking_texts_sent_on_call(call_sid_for_dedupe) >= _booking_texts_per_call_limit()
    ):
        sms_info(
            "post_booking_confirmation_over_call_limit",
            client_id=cid,
            call_sid=call_sid_for_dedupe,
            apt_id=apt.get("id"),
        )
        to_number_sms = None
    if to_number_sms and _confirmation_already_sent(call_sid_for_dedupe, to_number_sms, thanks_msg):
        sms_info(
            "post_booking_confirmation_suppressed_duplicate",
            client_id=cid,
            to_number=to_number_sms,
            call_sid=call_sid_for_dedupe,
        )
        to_number_sms = None
    if to_number_sms:
        if runtime.USE_DB and cid and cid != "default":
            database.db_sms_consent_record(
                to_number_sms,
                cid,
                "voice_booking",
                detail={"appointment_id": apt.get("id")},
            )
        send_detail: dict = {}
        ok = sms_service.send_sms(
            to_number_sms,
            thanks_msg,
            from_override=from_number_sms or None,
            detail_out=send_detail,
        )
        sms_info(
            "post_booking_confirmation_sms",
            client_id=cid,
            to_number=to_number_sms,
            from_number=from_number_sms,
            success=ok,
            not_textable=bool(send_detail.get("not_textable")),
        )
        if ok:
            _note_booking_text_sent(call_sid_for_dedupe)
            call_data.pop("confirmation_text_deferred_apt_id", None)
            if runtime.USE_DB and cid and apt.get("id"):
                try:
                    database.db_sms_session_upsert(
                        to_number_sms,
                        cid,
                        [
                            {
                                "role": "assistant",
                                "content": (
                                    "Request details sent by text. The salon will "
                                    "confirm the time."
                                    if is_request
                                    else "Appointment details sent by text. "
                                    "Reply YES or CONFIRM when everything looks right."
                                ),
                            }
                        ],
                        int(apt["id"]),
                    )
                    sms_info(
                        "post_booking_sms_session_linked",
                        client_id=cid,
                        apt_id=apt.get("id"),
                    )
                except Exception as sess_err:
                    logger.warning(
                        "post_booking_sms_session_link_failed apt_id=%s: %s",
                        apt.get("id"),
                        sess_err,
                        exc_info=True,
                    )
            ai_text = post_booking_spoken_confirmation(apt.get("status") or "", "texted")
        elif send_detail.get("not_textable"):
            # A landline (or any line the carrier won't deliver SMS to). Don't retry, don't
            # promise a text, and leave the shop something to act on — the caller has no
            # other way to hear back.
            call_data.pop("confirmation_text_deferred_apt_id", None)
            _store_caller_message(
                call_data,
                "Called from a number that cannot receive texts (landline). "
                f"Appointment request for {apt.get('date') or '?'} at "
                f"{booking_service._hhmm_to_ampm(apt.get('time') or '') or '?'} — "
                "please call them back to confirm.",
            )
            system_info(
                "booking_caller_not_textable",
                client_id=cid,
                apt_id=apt.get("id"),
                call_sid=call_sid_for_dedupe or "",
            )
            ai_text = post_booking_spoken_confirmation(apt.get("status") or "", "not_textable")
        else:
            ai_text = post_booking_spoken_confirmation(apt.get("status") or "", "sms_failed")
    else:
        sms_info(
            "post_booking_confirmation_skipped",
            reason="no_caller_phone",
            client_id=cid,
        )
        ai_text = post_booking_spoken_confirmation(apt.get("status") or "", "no_phone")
    fn_mem = (call_data.get("from_number") or "").strip()
    if fn_mem:
        dp = {
            "last_voice_booking_date": apt.get("date"),
            "last_voice_booking_time": apt.get("time"),
            "last_service": ((apt.get("reason") or "").strip()[:120] or None),
        }
        em_patch = (apt.get("email") or "").strip()
        if em_patch:
            dp["email_on_file"] = em_patch
        dp = {k: v for k, v in dp.items() if v}
        try:
            caller_memory.update_caller_memory(
                fn_mem,
                name=(apt.get("name") or "").strip() or None,
                last_reason="appointment details texted (pending SMS confirmation)",
                increment_count=False,
                data_patch=dp if dp else None,
            )
            if call_sid:
                voice_service._merge_call_session(
                    call_sid,
                    {"caller_memory": caller_memory.get_caller_memory(fn_mem)},
                )
        except Exception:
            pass
    return ai_text


def flush_deferred_confirmation_sms(
    call_data: dict, call_sid: Optional[str] = None
) -> bool:
    """Send the one text held back during the call, carrying the final details.

    When a caller amends their request after the first confirmation went out, the change
    is spoken and the text is deferred — otherwise every amendment is another message on
    their phone. The call is over now, so what the row says is final: send it once.

    Returns True when a text was dispatched.
    """
    apt_id = call_data.get("confirmation_text_deferred_apt_id")
    if not apt_id:
        return False
    call_data.pop("confirmation_text_deferred_apt_id", None)
    cid = (call_data.get("client_id") or "").strip() or None
    apt: Optional[dict] = None
    try:
        if runtime.USE_DB:
            if cid:
                database.set_request_client_id(cid)
            apt = database.db_appointments_get_by_id(int(apt_id), client_id=cid)
        else:
            apt = next(
                (a for a in runtime.appointments if str(a.get("id")) == str(apt_id)), None
            )
    except Exception as e:
        logger.warning("flush_deferred_confirmation_lookup_failed: %s", e, exc_info=True)
        return False
    if not apt:
        return False
    # Cancelled between the amendment and the hangup, or already confirmed by another
    # path — either way this text would be telling the caller something untrue.
    if (apt.get("status") or "").strip() not in ("pending_customer", "pending_review"):
        return False
    _send_booking_confirmation_sms(apt, call_data, cid, call_sid, final_summary=True)
    sms_info(
        "post_booking_confirmation_final_summary_sent",
        client_id=cid,
        apt_id=apt.get("id"),
        call_sid=(call_sid or call_data.get("call_sid") or ""),
    )
    return True


def reconcile_booking_at_call_end(
    call_data: dict, call_sid: Optional[str] = None
) -> bool:
    """End-of-call safety net: if the transcript shows the caller agreed to a booking but no
    appointment was created during the call (e.g. the model never emitted the BOOKING: marker,
    or the caller hung up mid-turn), try once to extract + validate + create it here.

    Returns True only when an appointment is actually created (and the confirmation SMS sent).
    Returns False when there is nothing to book, the details are incomplete, or the schedule
    backstop rejects it (e.g. a stylist on a day they don't work) — in which case the call
    correctly falls through to lead capture. Never books past the stylist/shop schedule."""
    history = call_data.get("conversation_history")
    # DIAGNOSTIC: this backstop used to return False silently at every gate, so a call that
    # ended with the AI verbally confirming ("I've got you down…") but no appointment gave no
    # clue why the rescue declined. Log the reason at each exit.
    _sid = call_sid or call_data.get("call_sid") or ""
    if not _conversation_suggests_booking(history):
        system_info(
            "reconcile_booking_skipped",
            reason="no_booking_intent",
            call_sid=_sid,
            history_len=len(history or []),
        )
        return False
    if not config_service.staff_roster_ready_for_booking(config_service.get_business_info()):
        system_info("reconcile_booking_skipped", reason="roster_not_ready", call_sid=_sid)
        return False
    cid = (call_data.get("client_id") or "").strip() or None
    call_sid = call_sid or call_data.get("call_sid")
    if cid:
        database.set_request_client_id(cid)
    # A booking already exists this call. We do NOT resurrect a mid-call change from the transcript
    # after the fact — a change only finalizes when the caller states it and the receptionist
    # confirms it live (real-time handler: spoken + texted + dashboard together). If they hung up
    # before that, the original booking stands untouched.
    if call_data.get("appointment_created"):
        system_info("reconcile_booking_skipped", reason="already_created", call_sid=_sid)
        return False
    try:
        booking = _extract_booking_line_from_conversation(
            history or [], caller_memory=call_data.get("caller_memory")
        )
    except Exception as e:
        logger.warning("reconcile_extract_failed: %s", e, exc_info=True)
        system_info(
            "reconcile_booking_skipped",
            reason="extract_raised",
            call_sid=_sid,
            error_type=type(e).__name__,
        )
        return False
    if not booking:
        # The end-of-call extractor found no complete booking in the transcript. This is the
        # gate that silently swallowed a call where the AI had verbally confirmed the booking.
        system_info(
            "reconcile_booking_skipped",
            reason="extract_returned_nothing",
            call_sid=_sid,
            client_id=cid or "",
            history_len=len(history or []),
        )
        return False
    from_num = (call_data.get("from_number") or "").strip()
    if from_num:
        booking["phone"] = _caller_phone_for_booking(booking.get("phone"), from_num)
    ok_booking, fail_msg, _, canonical_service = _validate_booking_requirements(
        booking, conversation_history=history
    )
    if not ok_booking:
        # The schedule backstop or a missing-required-field check rejected it. Do NOT book;
        # log so the shop can see a caller tried an unavailable slot (e.g. stylist off that day).
        system_info(
            "reconcile_booking_rejected",
            call_sid=call_sid or "",
            client_id=cid or "",
            reason=(fail_msg or "requirements_not_met")[:120],
        )
        return False
    if canonical_service:
        booking["reason"] = canonical_service
    apt = _create_appointment_from_booking(
        booking,
        client_id_override=cid,
        reserve_slot_immediately=False,
        caller_memory=call_data.get("caller_memory"),
    )
    if not apt:
        system_info(
            "reconcile_booking_not_created",
            call_sid=call_sid or "",
            client_id=cid or "",
        )
        return False
    call_data["appointment_created"] = True
    if not (apt.get("phone") or "").strip() and from_num:
        apt["phone"] = from_num
        if runtime.USE_DB and apt.get("id"):
            try:
                database.db_appointments_update(apt["id"], phone=apt["phone"])
            except Exception:
                pass
    _send_booking_confirmation_sms(apt, call_data, cid, call_sid)
    system_info(
        "reconcile_booking_created",
        call_sid=call_sid or "",
        client_id=cid or "",
        apt_id=apt.get("id"),
        date=apt.get("date"),
        time=apt.get("time"),
    )
    return True


# Pivot words that mark a caller ASKING to change an existing booking, vs merely mentioning a
# stylist/time in a question ("does Andrew work Tuesdays?"). The real-time change handler fires
# only when one of these is present, so a question can never silently rewrite the appointment.
_CHANGE_INTENT_CUES = (
    "actually",
    "instead",
    "change",
    "switch",
    "rather",
    "make it",
    "let's do",
    "lets do",
    "let us do",
    "can we do",
    "could we do",
    "can we make",
    "reschedule",
    "move it",
    "move that",
    "move my",
    "push it",
    "bump it",
    "different ",
    "how about",
    "what about",
)


def _utterance_requests_change(text: str) -> bool:
    t = (text or "").lower()
    return any(cue in t for cue in _CHANGE_INTENT_CUES)


_DAY_WORD_RE = re.compile(
    r"\b(mon|tues|wednes|thurs|fri|satur|sun)day\b|\btomorrow\b|\btoday\b|\bnext week\b", re.I
)


def _caller_asked_for_a_change(text: str, info: Optional[dict] = None) -> bool:
    """Did the caller actually ask to change something, or is the model repeating itself?

    Once a request exists, the model re-emits BOOKING lines freely — on Lana's call it
    did so on the goodbye turn, against "Okay, thanks!", sometimes with a time it had
    invented. An unchanged repeat is caught by the identity check; a CHANGED one used to
    go straight through and rewrite the request the caller had just been read back.

    So a change is only taken from a turn where the caller said something that could be
    one: a pivot word, an added service, a number, a day, a stylist, or a service name.
    Deliberately generous — a real correction should never be dropped — but "Okay,
    thanks!" contains none of them.
    """
    t = (text or "").strip()
    if not t:
        return False
    if _utterance_requests_change(t):
        return True
    from sms_appointment_updates import text_requests_additional_service

    if text_requests_additional_service(t):
        return True
    if re.search(r"\d", t) or _DAY_WORD_RE.search(t):
        return True
    biz = info or config_service.get_business_info()
    if _staff_id_from_spoken_text(t, biz):
        return True
    names, _ = booking_service.normalize_service_choices_for_booking(t, biz)
    return bool(names)


def _apply_voice_detail_change_if_pending(
    call_data: dict, call_sid: Optional[str], outcome_out: Optional[dict] = None
) -> Optional[str]:
    """Real-time mid-call change. The caller altered a detail (time/date/service/stylist) on the
    booking they already made this call, but the model narrated the change without re-emitting a
    marker. Apply it deterministically from what the caller JUST said — no reliance on the model's
    wording — and re-send the confirmation. Returns the spoken confirmation (or a truthful
    rejection when the change isn't allowed), or None if nothing changed. Only touches a still-
    unconfirmed draft; never rewrites a booking the customer already YES'd or the salon accepted.

    It looked up the draft with db_appointments_get_pending_by_phone, which returns only
    pending_review rows, and then required the row to be pending_customer — a pair of
    conditions nothing can satisfy. Every mid-call change fell straight through: the caller
    said "actually, make it Thursday", the model agreed, and the request that reached the
    salon still said Wednesday.

    Which statuses are still open depends on the store. Internally, pending_review means
    the customer already texted YES and the store is holding the slot — untouchable here.
    In request mode it is where every request lands the moment it is taken, so it is the
    only status a mid-call change can ever apply to."""
    from_num = (call_data.get("from_number") or "").strip()
    if not (runtime.USE_DB and from_num):
        return None
    existing = database.db_appointments_get_by_phone_for_sms(
        from_num, client_id=(call_data.get("client_id") or "").strip() or None
    )
    if not existing:
        return None
    open_statuses = (
        ("pending_customer", "pending_review")
        if config_service.is_external_booking()
        else ("pending_customer",)
    )
    if (existing.get("status") or "") not in open_statuses:
        return None
    history = call_data.get("conversation_history") or []
    latest_user = ""
    for m in reversed(history):
        if (m.get("role") or "").strip() == "user" and (m.get("content") or "").strip():
            latest_user = (m.get("content") or "").strip()
            break
    if not latest_user:
        return None
    # Only act on an explicit change request — never on a question that happens to name a stylist
    # or time (e.g. "does Andrew work Tuesdays?"), which must not silently rewrite the booking. A
    # change is applied only here, live in-call; if the caller hangs up before stating it clearly,
    # the original booking stands (there is no after-the-fact reconciler).
    from sms_appointment_updates import (
        apply_sms_appointment_detail_updates_from_bodies,
        text_requests_additional_service,
    )

    # "Can you add a highlight to that" is a change to the request too, and it carries
    # none of the pivot words above — the caller isn't replacing anything, they're
    # adding to it.
    if not _utterance_requests_change(latest_user) and not text_requests_additional_service(
        latest_user
    ):
        return None

    biz = config_service.get_business_info() or {}
    svc_entries = config_service._normalize_service_entries(biz.get("services") or [])
    known_services = [
        (s.get("name") or "").strip() for s in svc_entries if (s.get("name") or "").strip()
    ]
    service_id_by_name = {
        (s.get("name") or "").strip().lower(): (s.get("id") or "").strip()
        for s in svc_entries
        if (s.get("name") or "").strip()
    }
    known_staff = [s for s in (biz.get("staff") or []) if (s.get("name") or "").strip()]
    cid = (call_data.get("client_id") or "").strip() or None
    rejection: dict = {}
    try:
        apt, changed = apply_sms_appointment_detail_updates_from_bodies(
            [latest_user],
            existing,
            client_id=cid or "",
            from_number=from_num,
            db_appointments_update=database.db_appointments_update,
            db_appointments_get_by_id=database.db_appointments_get_by_id,
            update_caller_memory=caller_memory.update_caller_memory,
            db_appointments_update_active_name_by_phone=(
                database.db_appointments_update_active_name_by_phone
                if runtime.USE_DB
                else None
            ),
            system_info=system_info,
            logger=logger,
            known_services=known_services,
            known_staff=known_staff,
            service_id_by_name=service_id_by_name,
            business_info=biz,
            rejection_out=rejection,
        )
    except Exception as e:
        logger.exception("voice_detail_change_failed call_sid=%s: %s", call_sid, e)
        return None
    if rejection.get("message"):
        system_info(
            "voice_change_rejected",
            call_sid=call_sid or "",
            client_id=cid or "",
            reason=(rejection.get("reason") or "")[:60],
        )
        if outcome_out is not None:
            outcome_out["rejected"] = True
        return rejection["message"]
    if not changed:
        return None
    if any(f in changed for f in ("time", "date")):
        try:
            booking_service._reconcile_sms_appointment_slot_after_detail_change(apt)
        except Exception:
            pass
    system_info(
        "voice_change_applied",
        call_sid=call_sid or "",
        client_id=cid or "",
        apt_id=apt.get("id"),
        fields=",".join(changed),
        date=apt.get("date"),
        time=apt.get("time"),
    )
    return _send_booking_confirmation_sms(apt, call_data, cid, call_sid)


def get_system_prompt(
    detected_language: str = "English",
    caller_memory: Optional[dict] = None,
    include_booked_slots: bool = False,
    skip_slots_cache: bool = False,
):
    """Compose GPT system prompt for voice; slot lines come from live booking state."""
    info = config_service.get_business_info()
    booked_text = None
    if include_booked_slots:
        booked_text = booking_service.get_booked_slots_prompt_text(skip_cache=skip_slots_cache)
    prompt = build_system_prompt(
        business_info=info,
        detected_language=detected_language,
        caller_memory=caller_memory,
        include_booked_slots=include_booked_slots,
        booked_slots_prompt_text=booked_text,
    )
    from business_hours import after_hours_prompt_block

    after_hours = after_hours_prompt_block(info)
    if after_hours:
        prompt = f"{prompt}\n\n{after_hours}"
    return prompt


# ===== AI conversation turn (the voice/SMS response generator) =====

# Honest reply when a caller asks for a human but no transfer number is configured.
# Never let the AI claim to be a person — offer a callback/message instead.
# We already have the caller's number from caller ID, so don't ask for it here.
_NO_TRANSFER_FALLBACK_TEXT = (
    "I'm the AI receptionist, so I can't put a person on the line right now—but I can take "
    "a message and have the team call you back. What's it regarding?"
)


async def generate_response_async(
    call_sid: str, call_data: dict, detected_lang: str, base_url: str
):
    """
    Background task to generate GPT response and TTS audio.
    Updates runtime.call_store.response_status when ready.
    """
    try:
        # Keep tenant context so SMS and DB use correct client_id (async runs outside request)
        database.set_request_client_id(call_data.get("client_id") or database._client_id())
        fn_refresh = (call_data.get("from_number") or "").strip()
        if fn_refresh:
            call_data["caller_memory"] = caller_memory.refresh_caller_memory_for_prompt(
                fn_refresh, call_data.get("client_id")
            )
        voice_info(
            "generate_response_start",
            call_sid=call_sid,
            from_number=call_data.get("from_number") or None,
            client_id=call_data.get("client_id") or None,
        )
        # Read before anything on this turn can set it: it means the PREVIOUS reply
        # closed the booking and asked if there was anything else, so this turn is the
        # last one. See the close at the bottom of this function.
        grace_offered_before_turn = bool(call_data.get("post_booking_grace_offered"))
        # Diagnostic (only emitted when OBS_TRACE_TRANSCRIPT=1): the exact date + per-stylist
        # schedule the AI is reasoning over, so a wrong "tomorrow" or a misattributed stylist
        # schedule is visible in the logs instead of inferred.
        try:
            import staff_schedule as _ss

            _biz = config_service.get_business_info()
            _tz = business_local_now(_biz)
            _roster = "; ".join(
                f"{(s.get('name') or '?').strip()}="
                + (",".join(_ss.normalize_working_days(s.get("working_days"))) or "any")
                for s in (_biz.get("staff") or [])
                if (s.get("name") or "").strip()
            )
            voice_transcript(
                "booking_debug_context",
                call_sid=call_sid,
                text=(
                    f"model={VOICE_LLM_MODEL} tz={getattr(_tz.tzinfo, 'key', _tz.tzinfo)} "
                    f"today={_tz.strftime('%A')} {_tz.date()} "
                    f"tomorrow={(_tz + timedelta(days=1)).strftime('%A')} {(_tz + timedelta(days=1)).date()} "
                    f"hours=[{(_biz.get('hours') or '')[:60]}] "
                    f"closures={(_biz.get('closures') or [])[:15]} | roster: {_roster}"
                ),
            )
        except Exception:
            pass

        # Always include booked slots (skip cache so prompt and is_slot_available see same data—avoids "available" then "booked")
        messages = [
            {
                "role": "system",
                "content": get_system_prompt(
                    detected_lang,
                    call_data.get("caller_memory"),
                    include_booked_slots=True,
                    skip_slots_cache=True,
                ),
            }
        ]
        # Cap history sent to GPT to the recent tail — long calls would otherwise grow
        # the prompt (and token cost) unbounded turn over turn. The system prompt above
        # carries the durable context (business info, booked slots, caller memory).
        messages.extend(call_data["conversation_history"][-16:])
        recap = booking_details_recap_note(call_data["conversation_history"])
        if recap:
            messages.append({"role": "system", "content": recap})
        nudge = _voice_booking_nudge_message(
            call_data["conversation_history"],
            appointment_created=bool(call_data.get("appointment_created")),
        )
        if nudge:
            messages.append({"role": "system", "content": nudge})
            voice_info(
                "voice_booking_nudge_injected",
                call_sid=call_sid,
                client_id=str(call_data.get("client_id") or ""),
                user_turns=_count_booking_user_turns(call_data["conversation_history"]),
            )

        # Run on a worker thread: the OpenAI SDK call is blocking, and this
        # coroutine runs as a tracked task on the event loop. Calling it inline
        # would stall every concurrent call's loop work for the request's
        # duration. (The booking-extraction call below is threaded for the same
        # reason.) A hung request is bounded by the client timeout in runtime.py.
        ai_text = await asyncio.to_thread(
            llm_provider.chat,
            model=VOICE_LLM_MODEL,
            messages=messages,
            temperature=0.8,
            max_tokens=200,
        )
        voice_debug("gpt_reply", call_sid=call_sid, reply_preview=(ai_text or "")[:80])
        # Full AI reply (incl. any BOOKING marker) when OBS_TRACE_TRANSCRIPT=1 — pairs with the
        # caller_said lines so the whole conversation is reconstructable from the logs.
        voice_transcript("ai_said", call_sid=call_sid, text=ai_text or "")
        _model_reply_raw = ai_text or ""
        booking = parse_booking(ai_text)
        if booking:
            _raw_booking_time = (booking.get("time") or "").strip()
            _raw_booking_date = (booking.get("date") or "").strip()
            booking, repairs, reject = _prepare_parsed_booking(
                booking,
                caller_memory=call_data.get("caller_memory"),
            )
            if reject:
                system_info(
                    "voice_booking_line_rejected",
                    call_sid=call_sid,
                    reason=reject,
                    repairs=repairs or None,
                )
                # The line is gone. If the model had actually filled the field in, the
                # request just died here while the reply goes on to say it was taken —
                # a live call lost a request this way. Re-ask instead of letting that
                # stand, and log loudly enough to find it without asking the caller.
                _recovery = booking_reject_recovery_text(
                    reject,
                    raw_time=_raw_booking_time,
                    raw_date=_raw_booking_date,
                )
                if _recovery:
                    voice_warning(
                        "voice_booking_dropped_unusable_field",
                        call_sid=call_sid,
                        client_id=str(call_data.get("client_id") or ""),
                        reason=reject,
                        raw_time=_raw_booking_time[:40] or None,
                        raw_date=_raw_booking_date[:40] or None,
                    )
                    ai_text = _recovery
                booking = None
            elif repairs:
                system_info(
                    "voice_booking_line_repaired",
                    call_sid=call_sid,
                    repairs=repairs,
                )
        if not booking and _should_attempt_voice_booking_extraction(
            call_data.get("conversation_history"), ai_text or ""
        ):
            extracted = await asyncio.to_thread(
                _extract_booking_line_from_conversation,
                call_data.get("conversation_history") or [],
                caller_memory=call_data.get("caller_memory"),
            )
            if extracted:
                booking = extracted
                voice_info(
                    "voice_booking_extracted_retry",
                    call_sid=call_sid,
                    client_id=str(call_data.get("client_id") or ""),
                )
        # Real-time mid-call change: the caller altered a detail on the booking they already made
        # this call and the model narrated it without re-emitting a marker (its wording is
        # unreliable, so phrase-matching can't catch it). Apply the change deterministically from
        # what the caller just said and re-send the confirmation — before falling through.
        if not booking and call_data.get("appointment_created"):
            _change_outcome: dict = {}
            _change_text = _apply_voice_detail_change_if_pending(
                call_data, call_sid, _change_outcome
            )
            _change_rejected = bool(_change_outcome.get("rejected"))
            if _change_text:
                # Speak the fresh confirmation/rejection and fall through the normal pipeline
                # (history append + session persist). The booking blocks below are skipped since
                # `booking` is None and the appointment already exists.
                ai_text = _change_text
                # A rejection ("Terrance isn't available that day") is the start of a
                # conversation, not the end of one — only a confirmed change closes the call.
                if not _change_rejected:
                    ai_text = _close_call_after_booking(call_data, ai_text)
        # The model re-emits BOOKING freely — on a live call it produced the same line
        # when asking for a name and again on the goodbye turn, creating two identical
        # requests and texting the caller twice. An unchanged repeat is not news: drop
        # it and let the model's own words through. A CHANGED one still falls through
        # and supersedes, because that is a real amendment.
        if booking and _booking_identity(booking) == call_data.get("last_booking_identity"):
            system_info(
                "voice_booking_repeat_ignored",
                call_sid=call_sid,
                client_id=str(call_data.get("client_id") or ""),
            )
            booking = None
        elif (
            booking
            and call_data.get("appointment_created")
            and not _caller_asked_for_a_change(
                latest_user_message(call_data.get("conversation_history"))
            )
        ):
            # A DIFFERENT booking line on a turn where the caller asked for nothing. That
            # is the model reworking a request that is already with the salon, not the
            # caller changing their mind — and it would supersede the request they were
            # just read back.
            system_info(
                "voice_booking_unrequested_change_ignored",
                call_sid=call_sid,
                client_id=str(call_data.get("client_id") or ""),
            )
            booking = None

        # BOOKING: create appointment from AI output if present; replace response with confirmation or slot-taken message
        if booking:
            fail_msg = None
            if not config_service.staff_roster_ready_for_booking():
                ai_text = (
                    "I'm not able to book appointments until the business adds team members to their roster online. "
                    "Let me connect you with the store."
                )
            else:
                try:
                    from_num = call_data.get("from_number") or ""
                    to_num = call_data.get("to_number") or ""
                    cid_raw = call_data.get("client_id") or ""
                    from observability import name_initial_for_log

                    system_info(
                        "voice_booking_line_parsed",
                        name_initial=name_initial_for_log(booking.get("name")),
                        date=booking.get("date"),
                        time=booking.get("time"),
                        # DIAGNOSTIC: what service/stylist did the extraction capture?
                        service_captured=(booking.get("reason") or "")[:40],
                        stylist_captured=(booking.get("staff") or "")[:40],
                        from_number=from_num or None,
                        to_number=to_num or None,
                        client_id=cid_raw or None,
                    )
                    # Use caller's phone from Twilio when available (don't require asking)
                    if from_num:
                        booking["phone"] = _caller_phone_for_booking(
                            booking.get("phone"), from_num
                        )
                    cid = (call_data.get("client_id") or "").strip() or None
                    ok_booking, fail_msg, _, canonical_service = (
                        _validate_booking_requirements(
                            booking,
                            conversation_history=call_data.get("conversation_history"),
                        )
                    )
                    if not ok_booking:
                        ai_text = (
                            fail_msg
                            or "I need your stylist and service before I can book that."
                        )
                        apt = None
                    else:
                        if canonical_service:
                            booking["reason"] = canonical_service
                        apt = _create_appointment_from_booking(
                            booking,
                            client_id_override=cid,
                            reserve_slot_immediately=False,
                            caller_memory=call_data.get("caller_memory"),
                        )
                    if apt:
                        call_data["appointment_created"] = True
                        call_data["last_booking_identity"] = _booking_identity(booking)
                        if not (apt.get("phone") or "").strip() and call_data.get(
                            "from_number"
                        ):
                            apt["phone"] = call_data["from_number"]
                            if runtime.USE_DB and apt.get("id"):
                                try:
                                    database.db_appointments_update(
                                        apt["id"], phone=apt["phone"]
                                    )
                                except Exception:
                                    pass
                        ai_text = _send_booking_confirmation_sms(
                            apt, call_data, cid, call_sid
                        )
                        ai_text = _close_call_after_booking(call_data, ai_text)
                    else:
                        ctx = booking_context_from_business(config_service.get_business_info())
                        name_ok = bool((booking.get("name") or "").strip())
                        date_ok = is_valid_booking_date(booking.get("date"))
                        time_ok = looks_like_booking_time(booking.get("time"), ctx)
                        if fail_msg:
                            reason = "missing_required_booking_fields"
                        else:
                            reason = (
                                "slot_taken"
                                if (name_ok and date_ok and time_ok)
                                else ("no_name" if not name_ok else "no_date_time")
                            )
                        system_info(
                            "voice_booking_not_created",
                            reason=reason,
                            name_ok=name_ok,
                            date_ok=date_ok,
                            time_ok=time_ok,
                        )
                        if fail_msg:
                            ai_text = fail_msg
                        elif not name_ok:
                            ai_text = "I'd love to book that for you—what's your name?"
                        elif not date_ok or not time_ok:
                            ai_text = "I need the date and time again to confirm—which day and time would you like?"
                        else:
                            ai_text = "That time slot just got booked. Would you like to try another time or another day?"
                except Exception as e:
                    logger.exception(
                        "voice_booking_or_sms_failed call_sid=%s: %s", call_sid, e
                    )
                    ai_text = "We've got your request. If you don't get a confirmation text in a moment, please call back—we'll have your details."
        elif _conversation_suggests_booking(call_data.get("conversation_history")):
            user_turns = _count_booking_user_turns(
                call_data.get("conversation_history")
            )
            if user_turns >= 2:
                system_info(
                    "voice_booking_intent_no_marker",
                    call_sid=call_sid,
                    client_id=str(call_data.get("client_id") or ""),
                    user_turns=user_turns,
                    reply_len=len(ai_text or ""),
                )
            call_data["booking_intent"] = True

        if (
            not booking
            and not call_data.get("appointment_created")
            and _ai_implies_committed_booking(ai_text or "")
        ):
            system_info(
                "voice_booking_false_verbal_confirm",
                call_sid=call_sid,
                client_id=str(call_data.get("client_id") or ""),
            )
            ai_text = (
                "I haven't locked anything in just yet—I want to make sure I've got it right. "
                "Can you confirm the service, day, and time you'd like? Then I'll text you to confirm."
            )

        # Never send BOOKING: machine line to TTS or conversation history
        ai_text = _strip_booking_directive_for_voice(ai_text or "")
        if not ai_text:
            ai_text = "Thanks—we've noted that. Let us know if you need anything else."

        # Caller wants to leave a message — capture it, then strip the directive from speech.
        message_body = voice_service.parse_message_directive(ai_text)
        if message_body:
            stored = _store_caller_message(call_data, message_body)
            ai_text = _strip_message_directive_for_voice(ai_text)
            system_info(
                "voice_message_captured",
                call_sid=call_sid,
                client_id=str(call_data.get("client_id") or ""),
                stored=stored,
                msg_len=len(message_body),
            )
            if not ai_text:
                ai_text = "Got it—I've passed your message along to the team. Anything else I can help with?"

        # Honest fallback: the caller asked for a human earlier (flagged in the utterance
        # path) but no transfer number is configured — replace the reply so the AI never
        # pretends to be a person; offer a callback/message instead.
        if call_data.pop("forward_unavailable", False):
            if not (config_service.get_business_info().get("forwarding_phone") or "").strip():
                ai_text = _NO_TRANSFER_FALLBACK_TEXT

        # The last turn: the previous reply took the request and asked whether there was
        # anything else. Whatever that was, it has now been answered — say goodbye and end
        # the call rather than looping back into another listen. Skipped when the caller
        # has just been offered a callback, which is a question they still have to answer.
        if (
            grace_offered_before_turn
            and not call_data.get("end_call_after_reply")
            and ai_text != _NO_TRANSFER_FALLBACK_TEXT
        ):
            call_data.pop("post_booking_grace_offered", None)
            if _end_call_after_booking_enabled():
                call_data["end_call_after_reply"] = True
                ai_text = f"{(ai_text or '').strip()} {_goodbye_line()}".strip()
                voice_info("voice_call_closing_after_booking", call_sid=call_sid)

        if (ai_text or "") != _model_reply_raw:
            voice_transcript(
                "ai_spoken",
                call_sid=call_sid,
                text=ai_text or "",
                substituted=True,
            )

        # Add AI response to conversation
        ai_message = {"role": "assistant", "content": ai_text}
        call_data["conversation_history"].append(ai_message)
        # Merge into the latest session under the per-call lock — a full overwrite here
        # would clobber a caller turn that arrived while we were generating (the AI would
        # then re-ask for info already given), which surfaces under concurrent-call load.
        await voice_service.persist_generated_session_locked(call_sid, call_data)

        # Pro: Staff transfer - AI may respond with TRANSFER_TO: Name
        transfer_name = voice_service.parse_transfer_to(ai_text)
        if transfer_name:
            staff_phone = config_service.get_staff_phone_by_name(transfer_name)
            if staff_phone:
                voice_forward(
                    "staff_transfer_by_name",
                    call_sid=call_sid,
                    client_id=str(call_data.get("client_id") or ""),
                    forward_kind="staff_named",
                    staff_name=transfer_name,
                )
                call_data["outcome"] = "forwarded"
                voice_service.call_log_set_outcome(call_sid, "forwarded")
                runtime.call_store.response_status[call_sid] = {
                    "status": "forward",
                    "audio_url": None,
                    "ai_text": ai_text,
                    "forwarding_phone": staff_phone,
                }
                return
            voice_warning(
                "staff_transfer_name_not_found",
                call_sid=call_sid,
                client_id_prefix=str(call_data.get("client_id") or "")[:12],
                staff_name=transfer_name[:80],
            )

        # Check if user wants to talk to a real person - forward if needed
        if voice_service.should_forward_to_human(
            "",
            ai_text,
            call_sid=call_sid,
            client_id=str(call_data.get("client_id") or ""),
        ):
            forwarding_phone = config_service.get_business_info().get("forwarding_phone")
            if forwarding_phone:
                voice_forward(
                    "ai_transfer_intent_in_reply",
                    call_sid=call_sid,
                    client_id=str(call_data.get("client_id") or ""),
                    forward_kind="fallback",
                    has_fallback_configured=True,
                )
                call_data["outcome"] = "forwarded"
                voice_service.call_log_set_outcome(call_sid, "forwarded")
                runtime.call_store.response_status[call_sid] = {
                    "status": "forward",
                    "audio_url": None,
                    "ai_text": ai_text,
                    "forwarding_phone": forwarding_phone,
                }
                return
            # AI reply implied a transfer but there's no number — speak the honest line.
            ai_text = _NO_TRANSFER_FALLBACK_TEXT

        # Generate TTS audio URL
        ai_text_encoded = quote(ai_text)
        tts_audio_url = f"{base_url}/api/phone/tts-audio?text={ai_text_encoded}&voice={config_service.get_tts_voice()}"

        # Mark as ready. end_call tells the TwiML/stream layer to hang up once this
        # reply has played instead of listening for another turn.
        runtime.call_store.response_status[call_sid] = {
            "status": "ready",
            "audio_url": tts_audio_url,
            "ai_text": ai_text,
            "end_call": bool(call_data.get("end_call_after_reply")),
        }
        voice_call_phase(
            "gpt_response_ready",
            call_sid=call_sid,
            client_id=str(call_data.get("client_id") or ""),
            reply_len=len(ai_text or ""),
        )

    except Exception as e:
        voice_warning(
            "gpt_response_failed",
            call_sid=call_sid,
            client_id_prefix=str(call_data.get("client_id") or "")[:12],
            error_type=type(e).__name__,
        )
        logger.exception("generate_response_async failed call_sid=%s", call_sid)
        # Graceful fallback: play fallback message so caller does not get dead air
        fallback_encoded = quote(voice_service.TTS_FALLBACK_TEXT)
        fallback_tts_url = f"{base_url}/api/phone/tts-audio?text={fallback_encoded}&voice={config_service.get_tts_voice()}"
        runtime.call_store.response_status[call_sid] = {
            "status": "ready",
            "audio_url": fallback_tts_url,
            "ai_text": voice_service.TTS_FALLBACK_TEXT,
            "error": type(e).__name__,
        }
        voice_info(
            "gpt_response_fallback_tts",
            call_sid=call_sid,
            client_id_prefix=str(call_data.get("client_id") or "")[:12],
        )
    finally:
        await voice_service.persist_generated_session_locked(call_sid, call_data)
