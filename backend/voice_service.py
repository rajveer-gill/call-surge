"""Voice service — phone/voice domain logic lifted out of main.py.

Cut 1: recording-gating (plan + env) and phone-greeting payload composition. These are
shared by the phone routes (still in main), the business-info / greeting-preview routes,
and the call-recording proxy, so they're re-exported from main. Helpers are module-
qualified (database / config_service / deps / plans) per the strangler-fig discipline.
"""

from __future__ import annotations

import asyncio
import hashlib
import io
import json
import logging
import os
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any, List, Optional
from urllib.parse import quote, urlparse

from fastapi import Request

import config_service
import database
import runtime
import deps
from observability import mask_phone, voice_forward, voice_info, voice_trace, voice_warning
from security.redaction import mask_phone_e164
from voice_preview import add_sentence_pauses
from voice.call_session_store import MemoryCallSessionStore, UtteranceLockError

try:
    from plans import get_plan_limits
except ImportError:  # pragma: no cover
    get_plan_limits = None  # type: ignore

try:
    from twilio.request_validator import RequestValidator as _RequestValidator  # noqa: F401
    from twilio.twiml.voice_response import VoiceResponse
    TWILIO_AVAILABLE = True
except ImportError:  # pragma: no cover
    VoiceResponse = None  # type: ignore
    TWILIO_AVAILABLE = False

logger = logging.getLogger("nuvatra")

# Self-computed (same value as main/config_service) so voice_service has no main dependency.
PROJECT_ROOT = Path(__file__).resolve().parent.parent
CLIENT_ID = os.getenv("CLIENT_ID", "").strip()
TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN")

RECORDING_DISCLOSURE_TEXT = "This call may be recorded for quality and training."

DEFAULT_GREETING_TEMPLATE = (
    "Thank you for calling {business_name}. How can I help you today?"
)

GOT_IT_PHRASE = "Got it, one moment."
ONE_MOMENT_PHRASE = "One moment."

# Progressive fillers played while the AI is still composing a reply. The caller
# has just heard "Got it, one moment.", so the first wait poll stays silent and we
# never lead with "One moment." again; on longer waits we alternate these so it
# never sounds like a broken record. Served cached via /api/phone/filler-audio.
PENDING_FILLER_PHRASES = ["Almost there.", "One moment.", "Just a moment longer."]

# Fallback when OpenAI/TTS fails - play this so caller does not get dead air.
TTS_FALLBACK_TEXT = (
    "We're experiencing a brief technical issue. Please try again in a moment."
)


def cleanup_call_runtime_state(call_sid: str) -> None:
    """Clear per-call runtime state deterministically."""
    if not call_sid:
        return
    runtime.call_store.cleanup_call(call_sid)


def _greeting_debug_enabled() -> bool:
    """GREETING_DEBUG=1 or SETTINGS_LOAD_DEBUG=1 — logs greeting resolution on calls and Settings saves."""
    return deps._settings_load_debug_enabled() or os.getenv(
        "GREETING_DEBUG", ""
    ).strip().lower() in (
        "1",
        "true",
        "yes",
    )


def _call_recording_env_enabled() -> bool:
    return os.getenv("CALL_RECORDING_ENABLED", "").strip().lower() in (
        "1",
        "true",
        "yes",
    )


def _tenant_for_call_recording(tenant: Optional[dict] = None) -> Optional[dict]:
    """Resolve tenant dict for plan-gated recording (explicit tenant or current client)."""
    if tenant:
        return tenant
    if not runtime.USE_DB:
        return None
    cid = database._client_id()
    if cid and cid != "default":
        return database.db_tenant_get_by_client_id(cid)
    return None


def _call_recording_enabled_for_tenant(tenant: Optional[dict] = None) -> bool:
    """Env flag AND a plan with has_call_recording (Growth+; trial gets effective Pro limits)."""
    if not _call_recording_env_enabled():
        return False
    t = _tenant_for_call_recording(tenant)
    if not t or not get_plan_limits:
        return False
    return bool(get_plan_limits(t).get("has_call_recording"))


def _call_recording_enabled() -> bool:
    """Backward-compatible alias when tenant context is resolved from request client."""
    return _call_recording_enabled_for_tenant(None)


def _call_summary_enabled_for_tenant(tenant: Optional[dict] = None) -> bool:
    raw = os.getenv("CALL_SUMMARY_ENABLED")
    if raw is None or not str(raw).strip():
        return _call_recording_enabled_for_tenant(tenant)
    if not str(raw).strip().lower() in ("1", "true", "yes"):
        return False
    return _call_recording_enabled_for_tenant(tenant)


def _format_greeting_template(raw: str, info: dict) -> str:
    """Substitute {business_name} and {receptionist_name} in custom greeting text."""
    business_name = (info.get("name") or "us").strip() or "us"
    receptionist_name = (info.get("receptionist_name") or "").strip()
    subs = {"business_name": business_name, "receptionist_name": receptionist_name}
    try:
        return raw.format(**subs)
    except KeyError:
        out = raw
        for key, val in subs.items():
            out = out.replace("{" + key + "}", val)
        return out


def _resolve_greeting_business_name(info: dict, tenant: Optional[dict] = None) -> str:
    """Business name for {business_name} — config first, then tenant row from admin.

    public_name wins: it exists precisely because the account name is not the name
    to answer the phone with.
    """
    name = (info.get("public_name") or "").strip()
    if name:
        return name
    name = (info.get("name") or "").strip()
    if name:
        return name
    if tenant:
        name = (tenant.get("name") or "").strip()
        if name:
            return name
    cid = database._client_id()
    if runtime.USE_DB and cid:
        t = database.db_tenant_get_by_client_id(cid)
        if t:
            name = (t.get("name") or "").strip()
            if name:
                return name
    return "us"


def _split_trailing_question(text: str) -> tuple[str, str]:
    """Split a greeting into (everything before the closing question, the question).

    Greetings almost always end by handing the call over — "How can I help you today?" —
    and that is the cue callers answer. Anything spoken after it gets talked over, so the
    disclosure has to go in front of it rather than behind.

    Returns ("", text) when there is no trailing question to move ahead of, or when the
    greeting is nothing but that question: with no lead-in, putting the disclosure first
    would have the call open on legal boilerplate before the salon's own name.
    """
    stripped = text.rstrip()
    if not stripped.endswith("?"):
        return "", text
    # The question is the last sentence; find where the one before it ended.
    boundary = max(stripped.rfind(c, 0, len(stripped) - 1) for c in ".!?")
    if boundary < 0:
        return "", text
    lead = stripped[: boundary + 1].strip()
    question = stripped[boundary + 1 :].strip()
    if not lead or not question:
        return "", text
    return lead, question


def build_phone_greeting_payload(info: dict, tenant: Optional[dict] = None) -> dict:
    """
    Build phone greeting text: main message, then the recording disclosure, then the
    greeting's closing question last so the caller answers into an open line.
    Returns a debug-friendly dict (used by get_greeting_text and GET /api/greeting-preview).
    """
    cid = (database._client_id() or (tenant or {}).get("client_id") or "").strip()
    raw_saved = (info.get("greeting") or "").strip()
    used_default_template = not bool(raw_saved)
    raw_template = raw_saved if raw_saved else DEFAULT_GREETING_TEMPLATE

    business_name = _resolve_greeting_business_name(info, tenant)
    receptionist_name = (info.get("receptionist_name") or "").strip()
    fmt_info = {**info, "name": business_name, "receptionist_name": receptionist_name}
    main_greeting = _format_greeting_template(raw_template, fmt_info).strip()

    prepended_receptionist = False
    if receptionist_name and receptionist_name.lower() not in main_greeting.lower():
        main_greeting = f"Hi, I'm {receptionist_name}. {main_greeting}"
        prepended_receptionist = True

    tenant_rec = tenant if tenant is not None else _tenant_for_call_recording()
    recording_enabled = _call_recording_enabled_for_tenant(tenant_rec)
    recording_disclosure = RECORDING_DISCLOSURE_TEXT if recording_enabled else ""
    if recording_disclosure:
        lead, question = _split_trailing_question(main_greeting)
        spoken_text = (
            f"{lead} {recording_disclosure} {question}".strip()
            if lead
            else f"{main_greeting} {recording_disclosure}".strip()
        )
    else:
        spoken_text = main_greeting

    warnings: List[str] = []
    if "{receptionist_name}" in raw_template and not receptionist_name:
        warnings.append(
            "Greeting uses {receptionist_name} but AI receptionist name is empty in Settings."
        )
    if (
        "{business_name}" in raw_template
        and business_name == "us"
        and not (info.get("name") or "").strip()
    ):
        warnings.append(
            "Greeting uses {business_name} but business name is empty in Settings (using fallback 'us')."
        )

    return {
        "spoken_text": spoken_text,
        "main_greeting": main_greeting,
        "recording_disclosure": recording_disclosure or None,
        "used_default_template": used_default_template,
        "raw_greeting_saved": raw_saved,
        "prepended_receptionist": prepended_receptionist,
        "placeholders": {
            "business_name": business_name,
            "receptionist_name": receptionist_name,
        },
        "recording_enabled": recording_enabled,
        "config_source": config_service.client_config_source(cid) if cid else "none",
        "client_id": cid,
        "voice": (info.get("voice") or "fable") or "fable",
        "warnings": warnings,
    }


def _log_greeting_debug(
    event: str, payload: dict, *, call_sid: str = "", cache_hit: Optional[bool] = None
) -> None:
    """Structured greeting logs (Render: GREETING_DEBUG=1 or OBS_TRACE_VOICE=1)."""
    cid = (payload.get("client_id") or "")[:12]
    fields = {
        "client_id_prefix": cid or "(none)",
        "config_source": payload.get("config_source"),
        "used_default_template": payload.get("used_default_template"),
        "recording_enabled": payload.get("recording_enabled"),
        "prepended_receptionist": payload.get("prepended_receptionist"),
        "raw_greeting_len": len(payload.get("raw_greeting_saved") or ""),
        "spoken_len": len(payload.get("spoken_text") or ""),
        "business_name": (payload.get("placeholders") or {}).get("business_name"),
        "receptionist_name": (payload.get("placeholders") or {}).get(
            "receptionist_name"
        ),
        "voice": payload.get("voice"),
    }
    if call_sid:
        fields["call_sid"] = call_sid
    if cache_hit is not None:
        fields["cache_hit"] = cache_hit
    if payload.get("warnings"):
        fields["warnings"] = "; ".join(payload["warnings"])
    # Spoken text is not secret — needed to verify placeholders on production calls.
    spoken = (payload.get("spoken_text") or "")[:500]
    fields["spoken_preview"] = spoken
    voice_trace(event, **fields)
    if _greeting_debug_enabled():
        voice_info(event, **fields)


# ===== TTS synthesis + audio cache (cut 2) =====


def _got_it_cache_key(client_id: str) -> tuple:
    info = config_service.get_business_info()
    try:
        speed_key = round(float(info.get("speed", 1.0)), 2)
    except (TypeError, ValueError):
        speed_key = 1.0
    return (
        client_id,
        GOT_IT_PHRASE,
        (config_service.get_tts_voice() or "fable").strip(),
        speed_key,
    ) + _tts_variant_suffix()


def _one_moment_cache_key(client_id: str) -> tuple:
    info = config_service.get_business_info()
    try:
        speed_key = round(float(info.get("speed", 1.0)), 2)
    except (TypeError, ValueError):
        speed_key = 1.0
    return (
        client_id,
        ONE_MOMENT_PHRASE,
        (config_service.get_tts_voice() or "fable").strip(),
        speed_key,
    ) + _tts_variant_suffix()


def _filler_cache_key(client_id: str, phrase: str) -> tuple:
    info = config_service.get_business_info()
    try:
        speed_key = round(float(info.get("speed", 1.0)), 2)
    except (TypeError, ValueError):
        speed_key = 1.0
    return (
        client_id,
        phrase,
        (config_service.get_tts_voice() or "fable").strip(),
        speed_key,
    ) + _tts_variant_suffix()


def pending_filler_for_poll(poll_count: int):
    """Pick the wait-loop filler for a given poll index. Returns (index, phrase),
    or None for the first poll (stay silent — the caller just heard
    'Got it, one moment.'). Alternates phrases so long waits don't loop one line."""
    if poll_count <= 0 or not PENDING_FILLER_PHRASES:
        return None
    idx = (poll_count - 1) % len(PENDING_FILLER_PHRASES)
    return idx, PENDING_FILLER_PHRASES[idx]


def _synthesize_tts_clip(
    text: str,
    *,
    voice: str,
    speed: float,
    model: Optional[str] = None,
    instructions: Optional[str] = None,
) -> bytes:
    """Synthesize an mp3 clip. Defaults to the configured TTS model; `instructions` steers
    delivery on gpt-4o TTS models and is omitted for tts-1/tts-1-hd (which reject it)."""
    runtime._ensure_openai_client()
    model = (model or config_service.get_tts_model()).strip()
    apply_instructions = bool(instructions) and model.startswith("gpt-")
    kwargs: dict[str, Any] = dict(
        model=model,
        voice=voice,
        input=add_sentence_pauses(text),
        speed=max(0.25, min(4.0, float(speed))),
    )
    if apply_instructions:
        kwargs["instructions"] = instructions
    _synth_start = time.perf_counter()
    try:
        resp = runtime.client.audio.speech.create(**kwargs)
    except TypeError as e:
        # Deployed openai SDK predates the `instructions` kwarg (needs >= 1.68.0). Degrade
        # gracefully: drop steering and retry so the call never hard-fails or pages ops.
        if apply_instructions and "instructions" in str(e):
            voice_warning("tts_instructions_unsupported", model=model, error=str(e)[:120])
            kwargs.pop("instructions", None)
            apply_instructions = False
            resp = runtime.client.audio.speech.create(**kwargs)
        else:
            raise
    data = resp.content
    # DEBUG: proves which TTS model + steering actually ran on this synth (Tier 1 verification).
    voice_info(
        "tts_synth",
        model=model,
        instructions_applied=apply_instructions,
        instr_len=(len(instructions) if apply_instructions else 0),
        voice=voice,
        speed=round(float(speed), 2),
        input_len=len(text or ""),
        gen_ms=int((time.perf_counter() - _synth_start) * 1000),
        bytes=len(data or b""),
    )
    return data


def _tts_variant_suffix() -> tuple:
    """Model + instructions fingerprint appended to clip cache keys so switching the TTS
    model or steering style bypasses stale clips on disk instead of serving them."""
    model = config_service.get_tts_model()
    instr = config_service.get_tts_instructions() if model.startswith("gpt-") else ""
    instr_fp = hashlib.sha256(instr.encode("utf-8")).hexdigest()[:12] if instr else ""
    return (model, instr_fp)


def _ensure_greeting_audio_cached(client_id: str) -> bool:
    """Ensure greeting clip exists in cache; synthesize on miss. Returns True when cached."""
    from voice.tts_cache import get_cached, put_cached

    cid = (client_id or "").strip()
    if not cid or cid == "default":
        return False
    database.set_request_client_id(cid)
    greeting_key = _greeting_audio_cache_key(cid)
    if get_cached(PROJECT_ROOT, "greeting", greeting_key):
        return True
    info = config_service.get_business_info()
    tenant = _tenant_for_call_recording()
    payload = build_phone_greeting_payload(info, tenant)
    voice = (payload.get("voice") or config_service.get_tts_voice() or "fable").strip()
    speed = config_service.get_tts_speed()
    data = _synthesize_tts_clip(
        payload["spoken_text"],
        voice=voice,
        speed=speed,
        instructions=config_service.get_tts_instructions(),
    )
    put_cached(PROJECT_ROOT, "greeting", greeting_key, data)
    voice_info(
        "greeting_audio_prewarmed",
        client_id_prefix=cid[:12],
        voice=voice,
        bytes=len(data),
    )
    return True


def _warm_auxiliary_voice_cache(client_id: str) -> None:
    """Pre-generate got-it and one-moment clips (non-blocking for call answer)."""
    from voice.tts_cache import get_cached, put_cached

    cid = (client_id or "").strip()
    if not cid or cid == "default":
        return
    database.set_request_client_id(cid)
    got_it_key = _got_it_cache_key(cid)
    if not get_cached(PROJECT_ROOT, "got_it", got_it_key):
        voice = got_it_key[2]
        speed = got_it_key[3]
        data = _synthesize_tts_clip(
            GOT_IT_PHRASE,
            voice=voice,
            speed=speed,
            instructions=config_service.get_tts_instructions(),
        )
        put_cached(PROJECT_ROOT, "got_it", got_it_key, data)
        voice_info(
            "got_it_audio_prewarmed",
            client_id_prefix=cid[:12],
            voice=voice,
            bytes=len(data),
        )
    one_moment_key = _one_moment_cache_key(cid)
    if not get_cached(PROJECT_ROOT, "one_moment", one_moment_key):
        voice = one_moment_key[2]
        speed = one_moment_key[3]
        data = _synthesize_tts_clip(
            ONE_MOMENT_PHRASE,
            voice=voice,
            speed=speed,
            instructions=config_service.get_tts_instructions(),
        )
        put_cached(PROJECT_ROOT, "one_moment", one_moment_key, data)
        voice_info(
            "one_moment_audio_prewarmed",
            client_id_prefix=cid[:12],
            voice=voice,
            bytes=len(data),
        )


def warm_client_voice_cache(client_id: str) -> None:
    """Pre-generate greeting, got-it, and one-moment clips for a tenant."""
    cid = (client_id or "").strip()
    if not cid or cid == "default":
        return
    try:
        _ensure_greeting_audio_cached(cid)
        _warm_auxiliary_voice_cache(cid)
    except Exception as e:
        voice_warning(
            "voice_cache_prewarm_failed",
            client_id_prefix=cid[:12],
            error_type=type(e).__name__,
        )
        logger.warning(
            "warm_client_voice_cache failed client_id=%s: %s", cid, e, exc_info=True
        )


def _warm_all_tenant_voice_caches() -> None:
    """Prewarm voice clips at startup.

    Single-tenant/dev: warm the one configured runtime.client (cheap). Multi-tenant:
    sweeping every tenant at boot is O(tenants × 3 TTS calls) and does not scale
    (minutes of OpenAI calls at 60+ tenants, blocking nothing but burning quota
    and a worker). It is therefore LAZY by default — greeting/got-it/one-moment
    clips synthesize on the first call and are re-warmed on config save. Set
    VOICE_PREWARM_ALL_TENANTS=1 to opt into the full boot sweep for small fleets.
    """
    if not runtime.USE_DB:
        cid = (CLIENT_ID or "").strip()
        if cid and cid != "default":
            warm_client_voice_cache(cid)
        return
    if (os.getenv("VOICE_PREWARM_ALL_TENANTS") or "").strip().lower() not in (
        "1",
        "true",
        "yes",
    ):
        logger.info(
            "voice_cache_startup_prewarm skipped (lazy; set VOICE_PREWARM_ALL_TENANTS=1 to enable)"
        )
        return
    try:
        tenants = database.db_tenant_list_all()
    except Exception as e:
        logger.warning("voice_cache_startup_prewarm list failed: %s", e)
        return
    for tenant in tenants:
        cid = (tenant.get("client_id") or "").strip()
        phone = (tenant.get("twilio_phone_number") or "").strip()
        if not cid or not phone:
            continue
        try:
            warm_client_voice_cache(cid)
        except Exception as e:
            logger.warning(
                "voice_cache_startup_prewarm failed client_id=%s: %s", cid, e
            )


async def _warm_client_voice_cache_async(client_id: str) -> None:
    await asyncio.to_thread(warm_client_voice_cache, client_id)


async def _warm_auxiliary_voice_cache_async(client_id: str) -> None:
    await asyncio.to_thread(_warm_auxiliary_voice_cache, client_id)


def _greeting_audio_cache_key(client_id: str) -> tuple:
    """Cache key from fully resolved spoken text (includes tenant name fallback for placeholders)."""
    info = config_service.get_business_info()
    tenant = _tenant_for_call_recording()
    payload = build_phone_greeting_payload(info, tenant)
    try:
        speed_key = round(float(info.get("speed", 1.0)), 2)
    except (TypeError, ValueError):
        speed_key = 1.0
    return (
        client_id,
        payload["spoken_text"],
        (payload.get("voice") or "fable").strip(),
        speed_key,
    ) + _tts_variant_suffix()


# ===== STT provider selection (cut 3) =====


def _voice_stt_use_deepgram() -> bool:
    """Nova-3 live STT via Twilio Media Streams when env and credentials are present."""
    try:
        from voice.stt_runtime import deepgram_stt_active
    except ImportError:
        return False
    return deepgram_stt_active(
        twilio_available=TWILIO_AVAILABLE, twilio_client=runtime.twilio_client
    )


def uses_non_latin_script(language_name: str) -> bool:
    """
    Check if a language uses a non-Latin script (where Twilio transcription struggles).
    Returns True for languages like Japanese, Punjabi, Chinese, Arabic, Hindi, etc.
    """
    non_latin_languages = {
        "Japanese",
        "Punjabi",
        "Chinese",
        "Hindi",
        "Arabic",
        "Russian",
        "Korean",
        "Thai",
        "Vietnamese",
        "Bengali",
        "Tamil",
        "Telugu",
        "Gujarati",
        "Kannada",
        "Malayalam",
        "Marathi",
        "Urdu",
        "Hebrew",
        "Greek",
        "Georgian",
        "Armenian",
        "Khmer",
        "Lao",
        "Myanmar",
        "Tibetan",
        "Mongolian",
        "Nepali",
        "Sinhala",
    }
    return language_name in non_latin_languages


def _text_looks_latin(text: str) -> bool:
    """True when transcript is mostly basic Latin letters (English booking phrases, names, etc.)."""
    letters = [c for c in (text or "") if c.isalpha()]
    if not letters:
        return True
    latin = sum(1 for c in letters if ord(c) < 128)
    return latin / len(letters) >= 0.85


def _conversation_prefers_english_stt(call_data: dict) -> bool:
    """Use Deepgram/Gather English path when recent caller speech is Latin script."""
    lang = (call_data.get("detected_language") or "English").strip()
    if lang == "English" or not uses_non_latin_script(lang):
        return True
    for msg in reversed(call_data.get("conversation_history") or []):
        if msg.get("role") == "user":
            return _text_looks_latin(str(msg.get("content") or ""))
    return False


# ===== call-session context (cut 4; uses runtime.call_store) =====


def _persist_call_session(call_sid: str, data: Optional[dict] = None) -> None:
    """Write session back to Redis after in-place mutations (no-op for memory store)."""
    if isinstance(runtime.call_store, MemoryCallSessionStore):
        return
    sid = (call_sid or "").strip()
    if not sid or not runtime.call_store.exists(sid):
        return
    payload = data if data is not None else runtime.call_store.get(sid)
    if payload is not None:
        runtime.call_store.save(sid, payload)


def _merge_call_session(call_sid: str, updates: dict[str, Any]) -> None:
    """Persist partial session updates (safe on Redis and memory)."""
    if not call_sid or not updates:
        return
    runtime.call_store.merge_session(call_sid, updates)


def _merge_history_into(latest: dict, snapshot: dict) -> None:
    """Merge the GPT snapshot's conversation_history INTO the latest stored history so a
    caller turn that arrived while the response was generating is never lost. Appends any
    snapshot message not already present (e.g. the new assistant reply) onto the latest."""
    snap_hist = snapshot.get("conversation_history")
    if not isinstance(snap_hist, list):
        return
    latest_hist = latest.get("conversation_history")
    merged = list(latest_hist) if isinstance(latest_hist, list) else []
    for msg in snap_hist:
        if msg not in merged:
            merged.append(msg)
    snapshot["conversation_history"] = merged


async def persist_generated_session_locked(call_sid: str, call_data: dict) -> None:
    """Persist the GPT background task's session WITHOUT clobbering a caller turn that
    arrived while the (possibly slow) response was generating.

    The utterance handler writes the session under the per-call utterance lock; the GPT
    task runs AFTER that lock is released and was doing a full-overwrite of a stale
    snapshot — losing any newer caller turn and making the AI re-ask for info already
    given. This acquires the same lock, re-reads the latest session, merges the snapshot's
    history into it, then saves. No-op for the in-memory store (call_data is live)."""
    if isinstance(runtime.call_store, MemoryCallSessionStore):
        return
    sid = (call_sid or "").strip()
    if not sid:
        return
    try:
        async with runtime.call_store.utterance_lock(sid):
            latest = runtime.call_store.get(sid)
            if latest is None:
                if runtime.call_store.exists(sid):
                    runtime.call_store.save(sid, call_data)
                return
            snap_len = len(call_data.get("conversation_history") or [])
            latest_len = len(latest.get("conversation_history") or [])
            _merge_history_into(latest, call_data)
            merged_len = len(call_data.get("conversation_history") or [])
            # DIAGNOSTIC: rescued_turn=True means a caller turn arrived during generation
            # and the merge preserved it (would have been lost by the old full-overwrite).
            voice_info(
                "session_history_merge",
                call_sid=sid,
                snapshot_len=snap_len,
                latest_len=latest_len,
                merged_len=merged_len,
                rescued_turn=bool(merged_len > snap_len),
            )
            runtime.call_store.save(sid, call_data)
    except UtteranceLockError:
        # Lock contended past timeout: still merge best-effort rather than drop the turn.
        try:
            latest = runtime.call_store.get(sid)
            if latest is not None:
                _merge_history_into(latest, call_data)
        except Exception:
            pass
        _persist_call_session(call_sid, call_data)
    except Exception:
        _persist_call_session(call_sid, call_data)


def _call_sid_from_form(form_data: Any) -> str:
    """Normalize Twilio CallSid from webhook form body; empty string if invalid."""
    from voice.call_sid import normalize_call_sid

    return normalize_call_sid(str(form_data.get("CallSid") or "").strip())


def _restore_call_context(call_sid: str) -> bool:
    """Restore request client_id from call session for downstream phone handlers. Returns True if found."""
    if call_sid and runtime.call_store.exists(call_sid):
        cid = str((runtime.call_store.get(call_sid) or {}).get("client_id") or "").strip()
        if not cid:
            return False
        database.set_request_client_id(cid)
        return True
    return False


def _get_client_id_from_call(request: Request) -> Optional[str]:
    """Resolve client_id from call_sid query param (call session)."""
    call_sid = request.query_params.get("call_sid")
    if call_sid and runtime.call_store.exists(call_sid):
        return (
            str((runtime.call_store.get(call_sid) or {}).get("client_id") or "").strip() or None
        )
    return None


# ===== call log (cut 5; in-memory + file/DB persistence) =====

call_log_entries = {}  # call_sid -> {from_number, to_number, start_iso, outcome, ...}

CALL_LOG_MAX_ENTRIES = 5000


def call_log_start(call_sid: str, from_number: str, to_number: str):
    """Record call start. Outcome set when we forward or in status callback."""
    call_log_entries[call_sid] = {
        "call_sid": call_sid,
        "from_number": from_number,
        "to_number": to_number,
        "start_iso": datetime.now().isoformat(),
        "outcome": None,
        "end_iso": None,
        "duration_sec": None,
        "category": None,
        "recording_sid": None,
        "recording_url": None,
        "recording_duration_sec": None,
        "recording_status": None,
        "call_summary": None,
    }
    deps.audit_log(
        "voice",
        "call_started",
        resource_type="call",
        resource_id=call_sid,
        client_id=database._client_id() if runtime.USE_DB else None,
        details={
            "from_masked": mask_phone_e164(from_number or ""),
            "to_masked": mask_phone_e164(to_number or ""),
        },
    )


def call_log_merge_recording(call_sid: str, **kwargs) -> None:
    """Merge recording / summary fields into in-memory call log entry."""
    ent = call_log_entries.get(call_sid)
    if not ent:
        return
    for k, v in kwargs.items():
        if v is not None:
            ent[k] = v


def _file_call_log_merge_recording(call_sid: str, **kwargs) -> None:
    """Best-effort merge into clients/<id>/call_log.json when not using DB."""
    data_dir = config_service.get_client_data_dir()
    if not data_dir:
        return
    path = data_dir / "call_log.json"
    log_list: List[dict] = []
    if path.exists():
        try:
            with open(path, "r", encoding="utf-8") as f:
                log_list = json.load(f)
        except Exception:
            return
    for e in reversed(log_list):
        if e.get("call_sid") == call_sid:
            for k, v in kwargs.items():
                if v is not None:
                    e[k] = v
            break
    else:
        return
    try:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(log_list, f, indent=2)
    except Exception as ex:
        print(f"Failed to merge recording into file call log: {ex}")


def call_log_set_outcome(call_sid: str, outcome: str):
    """Set outcome: 'forwarded', 'answered_by_ai', 'missed', 'error', 'no-answer'."""
    if call_sid in call_log_entries:
        call_log_entries[call_sid]["outcome"] = outcome


def call_log_end(call_sid: str):
    """Write completed call to persistent log and remove from in-memory."""
    if call_sid not in call_log_entries:
        return
    entry = call_log_entries[call_sid].copy()
    entry["end_iso"] = datetime.now().isoformat()
    start_s = entry.get("start_iso")
    if start_s:
        try:
            start_dt = datetime.fromisoformat(start_s.replace("Z", "+00:00"))
            end_dt = datetime.fromisoformat(entry["end_iso"].replace("Z", "+00:00"))
            entry["duration_sec"] = int((end_dt - start_dt).total_seconds())
        except Exception:
            pass
    if not entry.get("outcome"):
        entry["outcome"] = "answered_by_ai"
    deps.audit_log(
        "voice",
        "call_ended",
        resource_type="call",
        resource_id=call_sid,
        client_id=database._client_id() if runtime.USE_DB else None,
        details={
            "outcome": entry.get("outcome"),
            "duration_sec": entry.get("duration_sec"),
            "recording_status": entry.get("recording_status"),
        },
    )
    if runtime.USE_DB:
        database.db_call_log_append(entry)
    else:
        data_dir = config_service.get_client_data_dir()
        if data_dir:
            path = data_dir / "call_log.json"
            log_list = []
            if path.exists():
                try:
                    with open(path, "r", encoding="utf-8") as f:
                        log_list = json.load(f)
                except Exception:
                    pass
            log_list.append(entry)
            if len(log_list) > CALL_LOG_MAX_ENTRIES:
                log_list = log_list[-CALL_LOG_MAX_ENTRIES:]
            try:
                with open(path, "w", encoding="utf-8") as f:
                    json.dump(log_list, f, indent=2)
            except Exception as e:
                print(f"Failed to save call log: {e}")
    del call_log_entries[call_sid]


# ===== call recording: SSRF-guarded fetch + Whisper/GPT summary (cut 6) =====


def _is_trusted_twilio_media_url(url: str) -> bool:
    """True only for https URLs on a Twilio-owned host.

    Recording/media URLs arrive in webhook bodies, which an attacker could forge
    if a signature check is ever bypassed. We never attach Twilio credentials to
    any other host — this is the SSRF / credential-exfil trust boundary, enforced
    at every credentialed fetch site below.
    """
    try:
        parsed = urlparse((url or "").strip())
    except Exception:
        return False
    if parsed.scheme != "https":
        return False
    host = (parsed.hostname or "").lower()
    if not host:
        return False
    return host == "twilio.com" or host.endswith(".twilio.com")


def _fetch_twilio_recording_bytes(recording_url: str) -> tuple:
    import httpx

    if not _is_trusted_twilio_media_url(recording_url):
        logger.error("[Recording] Refusing to fetch recording from untrusted host")
        return 0, b""
    r = httpx.get(
        recording_url,
        auth=(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN),
        timeout=120.0,
    )
    return r.status_code, r.content


def _summarize_call_recording_sync(
    call_sid: str, client_id: str, recording_url: str, duration_sec: Optional[int]
) -> None:
    """Download Twilio recording, Whisper transcribe, short GPT summary; persist call_summary."""
    if not recording_url or not TWILIO_ACCOUNT_SID or not TWILIO_AUTH_TOKEN:
        return
    if not _is_trusted_twilio_media_url(recording_url):
        logger.error(
            "[Recording] Refusing summary fetch from untrusted host call_sid=%s",
            call_sid,
        )
        return
    try:
        cap = int(os.getenv("CALL_SUMMARY_MAX_DURATION_SEC", "1800"))
    except ValueError:
        cap = 1800
    if duration_sec is not None and duration_sec > cap:
        logger.info(
            "[Recording] Skip summary (duration %s sec > cap %s)", duration_sec, cap
        )
        return
    if (os.getenv("TWILIO_INTELLIGENCE_SERVICE_SID") or "").strip():
        logger.info(
            "[Recording] TWILIO_INTELLIGENCE_SERVICE_SID is set; Phase 1 still uses OpenAI Whisper+GPT"
        )
    try:
        import httpx

        with httpx.Client(timeout=120.0) as http:
            r = http.get(recording_url, auth=(TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN))
        if r.status_code != 200:
            logger.error(
                "[Recording] Download failed status=%s call_sid=%s",
                r.status_code,
                call_sid,
            )
            return
        audio_data = r.content
        runtime._ensure_openai_client()
        bio = io.BytesIO(audio_data)
        bio.name = "recording.mp3"
        transcript = runtime.client.audio.transcriptions.create(model="whisper-1", file=bio)
        text = (getattr(transcript, "text", None) or "").strip()
        if not text:
            return
        resp = runtime.client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {
                    "role": "system",
                    "content": "Summarize this phone call in 2–4 clear sentences for a business owner dashboard. Mention caller intent (e.g. appointment, question, complaint) if clear. Be factual; do not invent details.",
                },
                {"role": "user", "content": text[:12000]},
            ],
            max_tokens=350,
            temperature=0.3,
        )
        summary = (resp.choices[0].message.content or "").strip()
        if not summary:
            return
        database.set_request_client_id(client_id)
        if runtime.USE_DB:
            database.db_call_log_update_summary(call_sid, client_id, summary)
        call_log_merge_recording(call_sid, call_summary=summary)
        if not runtime.USE_DB:
            _file_call_log_merge_recording(call_sid, call_summary=summary)
    except Exception:
        logger.exception("[Recording] Summarize failed call_sid=%s", call_sid)


async def _schedule_recording_summary(
    call_sid: str, client_id: str, recording_url: str, duration_sec: Optional[int]
) -> None:
    try:
        await asyncio.to_thread(
            _summarize_call_recording_sync,
            call_sid,
            client_id,
            recording_url,
            duration_sec,
        )
    except Exception:
        logger.exception("[Recording] Summary task failed call_sid=%s", call_sid)


# ===== voice call flow: TwiML handoffs, forwarding, language detection (cut 7) =====


def setup_transfers_to_store_after_message(info: Optional[dict] = None) -> bool:
    """
    True when inbound calls should play the setup message then dial the store:
    store phone is set but the team roster is not ready yet.
    """
    data = info if info is not None else config_service.get_business_info()
    return config_service.forwarding_phone_ready(data) and not config_service.staff_roster_ready_for_booking(data)


def setup_not_ready_call_message(info: Optional[dict] = None) -> str:
    """Spoken when the AI receptionist is not fully configured (before optional store transfer)."""
    data = info if info is not None else config_service.get_business_info()
    roster_ok = config_service.staff_roster_ready_for_booking(data)
    phone_ok = config_service.forwarding_phone_ready(data)
    if not roster_ok and phone_ok:
        return (
            "Sorry, your AI receptionist cannot work until the owner adds team members "
            "to their roster online. I will transfer you to the store now."
        )
    if not roster_ok and not phone_ok:
        return (
            "Sorry, I won't be able to function until the owner updates their settings online, "
            "including team members on the roster and a store phone number."
        )
    if not phone_ok:
        return (
            "Sorry, I won't be able to function until the owner adds a store phone number "
            "and completes their setup online."
        )
    return ""


def _normalize_dial_number(forwarding_phone: str) -> str:
    clean = "".join(c for c in (forwarding_phone or "") if c.isdigit() or c == "+")
    if not clean.startswith("+"):
        if len(clean) == 10:
            clean = f"+1{clean}"
        elif len(clean) == 11 and clean.startswith("1"):
            clean = f"+{clean}"
        else:
            clean = f"+1{clean}"
    return clean


def append_dial_forwarding_only(response: VoiceResponse, forwarding_phone: str) -> None:
    """Dial the store after a custom message (no extra 'please hold' TTS)."""
    clean_phone = _normalize_dial_number(forwarding_phone)
    voice_trace("dial_forwarding_only", dial_to=mask_phone(clean_phone))
    response.dial(clean_phone, timeout=30, record=False)
    response.say(
        "I'm sorry, no one is available right now. Please try again later or leave a message.",
        voice="alice",
    )
    response.hangup()


def twiml_setup_not_ready_handoff(
    base_url: str, biz_info: dict, call_sid: str = ""
) -> VoiceResponse:
    """
    Play setup-not-ready message. Transfer to the store only when store phone is set but roster is not
    (roster-only gap). If store phone is missing, end the call after the message.
    """
    response = VoiceResponse()
    message = setup_not_ready_call_message(biz_info)
    if message:
        msg_encoded = quote(message)
        response.play(
            f"{base_url}/api/phone/tts-audio?text={msg_encoded}&voice={config_service.get_tts_voice()}"
        )
    forwarding_phone = (biz_info.get("forwarding_phone") or "").strip()
    if setup_transfers_to_store_after_message(biz_info) and forwarding_phone:
        append_dial_forwarding_only(response, forwarding_phone)
        if call_sid:
            call_log_set_outcome(call_sid, "forwarded")
    else:
        response.say(
            "Please ask the business to complete their setup online. Goodbye.",
            voice="alice",
        )
        response.hangup()
        if call_sid:
            call_log_set_outcome(call_sid, "error")
    return response


def twiml_roster_not_ready_handoff(
    base_url: str, biz_info: dict, call_sid: str = ""
) -> VoiceResponse:
    """Backward-compatible alias for setup-not-ready handoff TwiML."""
    return twiml_setup_not_ready_handoff(base_url, biz_info, call_sid=call_sid)


def parse_transfer_to(ai_text: str) -> Optional[str]:
    """If AI responded with TRANSFER_TO: Name, return the name; else None."""
    if not ai_text:
        return None
    t = ai_text.strip()
    prefix = "TRANSFER_TO:"
    if t.upper().startswith(prefix):
        return t[len(prefix) :].strip()
    return None


def parse_message_directive(ai_text: str) -> Optional[str]:
    """If AI emitted a MESSAGE: <text> line (anywhere in the reply), return the message
    body; else None. The directive carries a third-person summary of what the caller
    wants relayed to the business."""
    if not ai_text or "MESSAGE:" not in ai_text.upper():
        return None
    m = re.search(r"(?i)MESSAGE:\s*([^\n]+)", ai_text)
    if not m:
        return None
    body = m.group(1).strip()
    return body or None


def get_twilio_language_code(language_name: str) -> str:
    """
    Map language name to Twilio language code for speech recognition.
    Returns Twilio language code (e.g., 'es-ES', 'en-US', 'hi-IN').
    Defaults to 'en-US' if language not supported.
    """
    lang = language_name
    if lang is None or (isinstance(lang, str) and not lang.strip()):
        lang = "English"
    elif not isinstance(lang, str):
        lang = str(lang)
    language_map = {
        "English": "en-US",
        "Spanish": "es-ES",
        "French": "fr-FR",
        "German": "de-DE",
        "Italian": "it-IT",
        "Portuguese": "pt-PT",
        "Chinese": "zh-CN",
        "Japanese": "ja-JP",
        "Korean": "ko-KR",
        "Hindi": "hi-IN",
        "Punjabi": "pa-IN",  # Punjabi (Gurmukhi)
        "Arabic": "ar-SA",
        "Russian": "ru-RU",
        "Dutch": "nl-NL",
        "Polish": "pl-PL",
        "Turkish": "tr-TR",
        "Swedish": "sv-SE",
        "Norwegian": "nb-NO",
        "Danish": "da-DK",
        "Finnish": "fi-FI",
        "Greek": "el-GR",
        "Czech": "cs-CZ",
        "Romanian": "ro-RO",
        "Hungarian": "hu-HU",
        "Thai": "th-TH",
        "Vietnamese": "vi-VN",
        "Indonesian": "id-ID",
        "Malay": "ms-MY",
    }

    # Try exact match first
    if lang in language_map:
        return language_map[lang]

    # Try case-insensitive match
    for key, code in language_map.items():
        if key.lower() == lang.lower():
            return code

    # Default to English if not found
    return "en-US"


def should_forward_to_human(
    user_input: str,
    ai_response: str,
    *,
    call_sid: str = "",
    client_id: str = "",
) -> bool:
    """
    Detect if the user wants to talk to a real person or if we should forward the call.
    Checks both user input and AI response for forwarding signals.
    """
    if not user_input:
        return False

    user_lower = user_input.lower()
    ai_lower = ai_response.lower() if ai_response else ""

    # Keywords that indicate user wants to talk to a person
    forward_keywords = [
        "talk to a person",
        "speak to someone",
        "talk to someone",
        "real person",
        "human",
        "agent",
        "representative",
        "transfer me",
        "connect me",
        "forward me",
        "can i speak to",
        "i want to speak to",
        "let me talk to",
        "put me through",
        "i need to talk to",
        "operator",
        "manager",
        "supervisor",
    ]

    matched_keyword = next((k for k in forward_keywords if k in user_lower), None)
    # AI response may itself signal a transfer intent even if the caller's words didn't match.
    ai_transfer_intent = matched_keyword is None and (
        "transfer" in ai_lower and ("you" in ai_lower or "connect" in ai_lower)
    )
    if not matched_keyword and not ai_transfer_intent:
        return False

    # A human handoff was requested. "Take a message instead" wins over any dial number: the
    # business opted out of live transfers, so capture a message (prompt's NO LIVE TRANSFER LINE
    # branch) instead of dialing. Only log this on an actual human request, not every turn.
    if config_service.transfer_takes_message():
        voice_forward(
            "human_request_takes_message",
            call_sid=call_sid,
            client_id=client_id,
            forward_kind="take_message",
            matched_keyword=matched_keyword or "ai_transfer_intent",
        )
        return False

    if matched_keyword:
        voice_forward(
            "caller_requested_human",
            call_sid=call_sid,
            client_id=client_id,
            forward_kind="fallback",
            matched_keyword=matched_keyword,
            input_len=len(user_input or ""),
        )
    else:
        voice_forward(
            "ai_transfer_intent_in_reply",
            call_sid=call_sid,
            client_id=client_id,
            forward_kind="fallback",
            reply_preview=(ai_response or "")[:80],
        )
    return True


def append_forward_call_verbs(
    response: VoiceResponse,
    forwarding_phone: str,
    base_url: str,
    detected_lang: str = "English",
) -> None:
    """Append handoff TTS, Dial, and no-answer fallback to an existing TwiML response."""
    if detected_lang == "Spanish":
        message = "Conectándote con alguien ahora. Por favor espera."
    elif detected_lang == "French":
        message = "Je vous connecte maintenant. Veuillez patienter."
    else:
        message = "Connecting you with someone now. Please hold."

    message_encoded = quote(message)
    tts_url = (
        f"{base_url}/api/phone/tts-audio?text={message_encoded}&voice={config_service.get_tts_voice()}"
    )
    response.play(tts_url)

    clean_phone = "".join(c for c in forwarding_phone if c.isdigit() or c == "+")
    if not clean_phone.startswith("+"):
        if len(clean_phone) == 10:
            clean_phone = f"+1{clean_phone}"
        elif len(clean_phone) == 11 and clean_phone.startswith("1"):
            clean_phone = f"+{clean_phone}"
        else:
            clean_phone = f"+1{clean_phone}"

    voice_trace("dial_fallback_appended", dial_to=mask_phone(clean_phone))
    response.dial(clean_phone, timeout=30, record=False)
    response.say(
        "I'm sorry, no one is available right now. Please try again later or leave a message.",
        voice="alice",
    )
    response.hangup()


def forward_call_to_business(
    forwarding_phone: str, base_url: str, detected_lang: str = "English"
) -> VoiceResponse:
    """
    Forward the call to the business's actual phone number using Twilio Dial.
    """
    response = VoiceResponse()
    append_forward_call_verbs(response, forwarding_phone, base_url, detected_lang)
    return response


def detect_language(text: str) -> str:
    """
    Detect the language of the input text using OpenAI's intelligence.
    Returns language name in English (e.g., 'Spanish', 'Punjabi', 'English', 'French', etc.).
    This function is called on EVERY speech input to support dynamic language switching.
    Relies on OpenAI to detect any language automatically - no hardcoded word lists.
    """
    if not text or len(text.strip()) < 3:
        return "English"

    # Use OpenAI to detect language - it can detect any language automatically
    try:
        # No detection without an OpenAI key (runtime.client is a lazy proxy, never None).
        if not os.getenv("OPENAI_API_KEY"):
            return "English"

        # Use OpenAI to intelligently detect the language
        # This works for any language, not just hardcoded ones
        detection_prompt = f"""Detect the language of this text and respond with ONLY the language name in English (e.g., 'Spanish', 'Punjabi', 'English', 'French', 'German', 'Chinese', 'Hindi', 'Italian', 'Portuguese', 'Japanese', 'Korean', 'Arabic', 'Russian', etc.). 

Text: {text[:200]}

Respond with just the language name, nothing else."""

        detection_response = runtime.client.chat.completions.create(
            model="gpt-3.5-turbo",
            messages=[{"role": "user", "content": detection_prompt}],
            max_tokens=15,
            temperature=0,  # Low temperature for consistent language detection
        )
        detected_lang = detection_response.choices[0].message.content.strip()

        # Clean up response (remove quotes, extra words, periods)
        detected_lang = (
            detected_lang.replace('"', "").replace("'", "").replace(".", "").strip()
        )

        # Extract just the language name (in case GPT adds extra text)
        # Take the first word which should be the language name
        detected_lang = (
            detected_lang.split()[0] if detected_lang.split() else detected_lang
        )

        # Capitalize first letter (e.g., "spanish" -> "Spanish")
        if detected_lang:
            detected_lang = detected_lang.capitalize()

        if detected_lang and len(detected_lang) < 30:  # Sanity check
            return detected_lang
    except Exception as e:
        print(f"Language detection error: {e}")
        import traceback

        traceback.print_exc()

    # Default to English if detection fails
    return "English"


def invalidate_voice_cache(client_id: Optional[str] = None) -> None:
    """Clear greeting/got-it audio cache when voice, speed, greeting, name, or receptionist changes."""
    from voice.tts_cache import invalidate_client

    if client_id:
        invalidate_client(PROJECT_ROOT, client_id)
    else:
        for d in (PROJECT_ROOT / "clients").glob("*/voice_cache"):
            if d.is_dir():
                for p in d.glob("*.mp3"):
                    try:
                        p.unlink()
                    except OSError:
                        pass
        from voice.tts_cache import clear_all_memory

        clear_all_memory()
