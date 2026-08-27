"""Shared FastAPI dependencies and cross-cutting request helpers.

Owns the auth/tenant-resolution stack that routers depend on: ``require_tenant``,
``require_admin``, ``require_active_subscription``, plus ``audit_log`` and the
Clerk/tenant-context helpers they use. Routers import these as
``from deps import require_active_subscription`` (the dependency *object* is what
``Depends`` and ``app.dependency_overrides`` key on) and call body helpers as
``deps.audit_log(...)``. main.py re-exports these names so ``from main import X``
keeps working for existing tests.

This module imports only leaf modules (runtime, database, auth, observability,
subscription_access) — never main — so the import graph stays acyclic.
"""

from __future__ import annotations

import asyncio
import logging
import os
import time
from typing import Any, List, Optional
from urllib.parse import urlparse

from fastapi import Depends, HTTPException, Request

import database
import runtime
from auth import get_bearer_token, verify_clerk_token
from observability import usage_warning
from security.webhooks import validate_twilio_webhook as validate_twilio_signature
from subscription_access import get_tenant_subscription_state

try:
    from twilio.request_validator import RequestValidator as _RequestValidator  # noqa: F401
    TWILIO_AVAILABLE = True
except ImportError:  # pragma: no cover
    TWILIO_AVAILABLE = False


logger = logging.getLogger("nuvatra")


def _server_error(
    context: str,
    exc: Exception,
    *,
    status_code: int = 500,
    public_detail: str = "Internal server error",
) -> HTTPException:
    """Log the real exception server-side; return a client-safe HTTPException.

    Raw exception strings from the DB driver, Stripe, OpenAI, or Twilio can embed
    connection strings, partial keys, or internal hostnames — never echo str(e)
    to clients. Routers do `raise deps._server_error("context", e)`.
    """
    logger.error("%s: %s", context, exc, exc_info=True)
    return HTTPException(status_code=status_code, detail=public_detail)


def _public_base_url() -> str:
    """HTTPS origin Twilio can reach for webhooks (use NGROK_URL or PUBLIC_BASE_URL)."""
    return (
        (os.getenv("NGROK_URL") or os.getenv("PUBLIC_BASE_URL") or "")
        .strip()
        .rstrip("/")
    )


def _derived_public_base_from_request(request: Request) -> str:
    """When PUBLIC_BASE_URL is unset, derive https://host from the inbound webhook (Render/proxies send X-Forwarded-*)."""
    host = (
        (request.headers.get("x-forwarded-host") or request.headers.get("host") or "")
        .split(",")[0]
        .strip()
    )
    if not host:
        return ""
    proto = (
        (request.headers.get("x-forwarded-proto") or "").split(",")[0].strip().lower()
    )
    if proto not in ("https", "http"):
        proto = (request.url.scheme or "https").lower()
        if proto not in ("http", "https"):
            proto = "https"
    return f"{proto}://{host}".rstrip("/")


def _twilio_base_url(request: Request) -> str:
    """Absolute base URL for Twilio <Play>, <Gather action>, etc. (Twilio rejects relative URLs)."""
    bu = _public_base_url()
    if bu:
        return bu
    d = _derived_public_base_from_request(request)
    if d:
        return d
    try:
        ru = urlparse(str(request.url))
        if ru.hostname and "ngrok" in ru.hostname.lower():
            return f"{ru.scheme}://{ru.netloc}".rstrip("/")
    except Exception:
        pass
    return ""


_background_tasks: "set[asyncio.Task]" = set()


def create_tracked_task(coro: Any, *, name: str) -> "asyncio.Task":
    """Create background task with standardized failure logging and lifecycle cleanup."""
    task = asyncio.create_task(coro, name=name)
    _background_tasks.add(task)

    def _done(t: "asyncio.Task") -> None:
        _background_tasks.discard(t)
        try:
            _ = t.result()
        except asyncio.CancelledError:
            logger.info("background_task_cancelled name=%s", name)
        except Exception as e:
            logger.exception("background_task_failed name=%s", name)
            try:
                import alerts

                alerts.notify_failure("task", name, None, str(e), sms=False)
            except Exception:
                pass

    task.add_done_callback(_done)
    return task


def _validate_twilio_webhook(request: Request, form_data: dict) -> bool:
    """Validate X-Twilio-Signature so only Twilio can trigger webhooks."""
    allow_insecure = (os.getenv("ALLOW_INSECURE_WEBHOOKS") or "").strip().lower() in (
        "1",
        "true",
        "yes",
    )
    strict_required = bool(runtime.USE_DB) and not allow_insecure
    return validate_twilio_signature(
        request,
        form_data,
        auth_token=os.getenv("TWILIO_AUTH_TOKEN"),
        twilio_available=TWILIO_AVAILABLE,
        strict_required=strict_required,
    )


def _settings_load_debug_enabled() -> bool:
    """Set SETTINGS_LOAD_DEBUG=1 on Render to log Settings API diagnostics (keys/types only, no PII)."""
    return os.getenv("SETTINGS_LOAD_DEBUG", "").strip().lower() in ("1", "true", "yes")


def _admin_access_debug_enabled() -> bool:
    """ADMIN_ACCESS_DEBUG=1 — INFO logs for invite/relink; extra fields on admin debug API responses."""
    return os.getenv("ADMIN_ACCESS_DEBUG", "").strip().lower() in ("1", "true", "yes")


def _admin_access_log(event: str, **fields) -> None:
    if not _admin_access_debug_enabled():
        return
    parts = [f"{k}={v!r}" for k, v in fields.items() if v is not None]
    print(f"[ADMIN_ACCESS] {event} " + " ".join(parts))


def audit_log(
    actor_type: str,
    action: str,
    *,
    actor_id: Optional[str] = None,
    resource_type: Optional[str] = None,
    resource_id: Optional[str] = None,
    client_id: Optional[str] = None,
    details: Optional[dict] = None,
    request: Optional[Request] = None,
) -> None:
    """Append an audit event. No full PII (e.g. no message bodies)."""
    if not runtime.USE_DB:
        return
    try:
        ip = request.client.host if request and request.client else None
        request_id = getattr(request.state, "request_id", None) if request else None
        database.db_audit_append(
            actor_type=actor_type,
            action=action,
            actor_id=actor_id,
            resource_type=resource_type,
            resource_id=resource_id,
            client_id=client_id,
            details=details,
            ip=ip,
            request_id=request_id,
        )
    except Exception:
        pass


def maybe_alert_usage_cap(
    *,
    client_id: str,
    month: str,
    channel: str,
    voice_minutes: int,
    voice_cap: int,
    sms_count: int,
    sms_cap: int,
    request: Optional[Request] = None,
) -> None:
    """Emit a one-per-tenant-per-month operator alert when a usage cap is crossed.

    Alert-only policy: service is NOT cut off — this just notifies the operator (who can
    manually pause the account if needed). Idempotent via usage_alert_sent. Best-effort;
    never raises into the call/SMS path."""
    if not runtime.USE_DB or not client_id:
        return
    try:
        if database.db_usage_alert_exists(client_id, month):
            return
        audit_log(
            "usage",
            "usage_cap_alert",
            client_id=client_id,
            details={
                "month": month,
                "channel": channel,
                "voice_minutes": voice_minutes,
                "voice_cap": voice_cap,
                "sms_count": sms_count,
                "sms_cap": sms_cap,
            },
            request=request,
        )
        try:
            import email_notify

            subject = f"[Nuvatra] Tenant {client_id} crossed its {channel} usage cap"
            html = (
                f"<p>Tenant <strong>{client_id}</strong> has exceeded its plan cap for {month}.</p>"
                f"<p>Voice minutes: {voice_minutes} / {voice_cap}<br>"
                f"SMS: {sms_count} / {sms_cap}</p>"
                f"<p>Service continues (overage is billed). Pause the account from the admin "
                f"console if this looks like abuse.</p>"
            )
            email_notify.send_operator_alert(subject, html)
        except Exception:
            pass
        # Record after attempting the alert so we don't re-alert every call this month.
        database.db_usage_alert_insert(client_id, month)
    except Exception:
        pass


def _ensure_db_ready() -> None:
    """Block briefly to let background init_db finish if it hasn't yet."""
    if runtime.USE_DB or not runtime._db_imported or not os.getenv("DATABASE_URL"):
        return
    for _ in range(20):
        if runtime.USE_DB:
            return
        time.sleep(0.5)
    # Last resort: try init synchronously
    try:
        runtime.USE_DB = database.init_db()
    except Exception:
        pass


def _clerk_fetch_user_link(clerk_user_id: str) -> Optional[dict]:
    """Clerk Backend API: public_metadata.tenant_id and verified email addresses."""
    clerk_secret = os.getenv("CLERK_SECRET_KEY", "").strip()
    if not clerk_secret:
        return None
    try:
        import httpx

        resp = httpx.get(
            f"https://api.clerk.com/v1/users/{clerk_user_id}",
            headers={"Authorization": f"Bearer {clerk_secret}"},
            timeout=5.0,
        )
        if resp.status_code != 200:
            return None
        data = resp.json()
        emails: List[str] = []
        for item in data.get("email_addresses") or []:
            addr = (item.get("email_address") or "").strip()
            if addr:
                emails.append(addr)
        primary_id = data.get("primary_email_address_id")
        if primary_id:
            for item in data.get("email_addresses") or []:
                if item.get("id") == primary_id:
                    addr = (item.get("email_address") or "").strip()
                    if addr and addr not in emails:
                        emails.insert(0, addr)
        tenant_id = (data.get("public_metadata") or {}).get("tenant_id")
        return {"tenant_id": tenant_id, "emails": emails}
    except Exception as e:
        print(f"[Auth] Clerk user lookup failed for {clerk_user_id}: {e}")
    return None


def _clerk_patch_user_tenant_metadata(clerk_user_id: str, tenant_id: str) -> bool:
    clerk_secret = os.getenv("CLERK_SECRET_KEY", "").strip()
    if not clerk_secret:
        return False
    try:
        import httpx

        resp = httpx.patch(
            f"https://api.clerk.com/v1/users/{clerk_user_id}",
            headers={
                "Authorization": f"Bearer {clerk_secret}",
                "Content-Type": "application/json",
            },
            json={"public_metadata": {"tenant_id": tenant_id}},
            timeout=10.0,
        )
        return resp.status_code < 400
    except Exception as e:
        print(f"[Auth] Clerk metadata patch failed for {clerk_user_id}: {e}")
        return False


# Header a multi-store overseer sends to say which store this request is about.
STORE_HEADER = "X-Store-Id"
# Methods a read-only org viewer may use. Anything else is a write.
_READ_ONLY_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})


def _admin_user_ids() -> list:
    """Admin allowlist from the environment. Empty means admin is not configured."""
    return [x.strip() for x in (os.getenv("ADMIN_CLERK_USER_IDS") or "").split(",") if x.strip()]


def is_admin_user(user_id: str) -> bool:
    return bool(user_id) and user_id in _admin_user_ids()


def _enforce_org_write_role(request: Request, role: str, user_id: str, client_id: str) -> None:
    """Block a read-only org viewer from anything that isn't a read.

    Shared by both routes into an org store — the X-Store-Id path and require_tenant's
    single-store fallback — because a gate that only one of them applies is a gate you
    can walk around by omitting a header.
    """
    # Rank, not equality. This read == "manager", so adding a stronger role made
    # the OWNER read-only everywhere — the one account that must never be.
    if database.org_role_at_least(role, "manager"):
        return
    if request.method.upper() in _READ_ONLY_METHODS:
        return
    audit_log(
        "user",
        "auth_failure",
        actor_id=user_id,
        details={
            "reason": "org_viewer_write_blocked",
            "method": request.method,
            "client_id": client_id,
        },
        request=request,
    )
    raise HTTPException(
        status_code=403,
        detail="Your account can view this store but not change it.",
    )


def _resolve_org_store(request: Request, user_id: str):
    """Resolve the store an org overseer is asking for, or None if they aren't one.

    Returns the tenant dict. Raises 403 if they asked for a store they don't oversee,
    or if a read-only viewer tried to write.

    This is the only place org access is granted, which is what makes it auditable:
    db_org_store_for_user validates membership inside the fetch query, so there is no
    window where an unauthorized store is loaded and then checked.
    """
    store_ref = (request.headers.get(STORE_HEADER) or "").strip()
    if not store_ref or not runtime.USE_DB or not user_id:
        return None
    scoped = database.db_org_store_for_user(user_id, store_ref)
    if not scoped and is_admin_user(user_id):
        # Support access: an admin can open any store's dashboard to set it up or see
        # what a customer is seeing. Deliberately NOT a membership row — nothing to
        # create, nothing to forget to remove, and it disappears the moment the user
        # leaves ADMIN_CLERK_USER_IDS. Admins can already delete a tenant outright
        # from the admin panel, so this grants no new trust; it grants convenience.
        #
        # Audited on EVERY request, not just the first, because "who looked at this
        # customer's data and when" is the question that matters afterwards.
        tenant = database.db_tenant_get_by_client_id(store_ref) or (
            database.db_tenant_get_by_id(store_ref) if len(store_ref) > 20 else None
        )
        if tenant:
            audit_log(
                "admin",
                "admin_store_access",
                actor_id=user_id,
                resource_type="tenant",
                resource_id=str(tenant.get("id") or ""),
                client_id=tenant.get("client_id"),
                details={"method": request.method, "path": str(getattr(request, "url", ""))[:200]},
                request=request,
            )
            database.set_request_client_id(tenant["client_id"])
            return tenant
    if not scoped:
        # Miss. Distinguish the two reasons, because they deserve opposite answers:
        # an overseer reaching for a store outside their org is a real 403, but a
        # normal owner whose browser kept a stale header from some other session is
        # not — 403ing them would lock them out of their own dashboard. Only the
        # former has any org membership at all.
        if database.db_org_memberships(user_id):
            audit_log(
                "user",
                "auth_failure",
                actor_id=user_id,
                details={"reason": "store_not_in_org", "store_ref": store_ref[:64]},
                request=request,
            )
            # Machine-readable code so the client can tell "this store is gone, drop
            # your saved selection and retry" from a real permission problem. Without
            # it, a store that gets deleted while someone has it selected locks them
            # out of the whole app until they clear browser storage.
            raise HTTPException(
                status_code=403,
                detail={
                    "code": "STORE_NOT_ACCESSIBLE",
                    "message": "You do not have access to that store.",
                },
            )
        return None  # not an overseer — ignore the header, resolve them normally
    _enforce_org_write_role(
        request,
        scoped.get("role") or "viewer",
        user_id,
        (scoped.get("tenant") or {}).get("client_id"),
    )
    return scoped.get("tenant")


def require_tenant(request: Request):
    """
    Dependency: multi-tenant mode requires Bearer token; single-tenant uses CLIENT_ID env.
    Sets request client_id context for database operations.
    """
    jwks_url = os.getenv("CLERK_JWKS_URL", "").strip()
    if not jwks_url:
        return None
    token = get_bearer_token(request)
    if not token:
        audit_log(
            "user", "auth_failure", details={"reason": "no_token"}, request=request
        )
        raise HTTPException(status_code=401, detail="Authorization required")
    user_id, tenant_id_from_meta = verify_clerk_token(token)
    _ensure_db_ready()
    # Multi-store overseer picking a store. Checked first and returned early: their
    # access comes from org membership, so none of the tenant_members resolution
    # (or its one-tenant-per-user collapsing) below should run for them.
    org_store = _resolve_org_store(request, user_id)
    if org_store:
        database.set_request_client_id(org_store["client_id"])
        return org_store
    tenant = None
    preferred_tid = str(tenant_id_from_meta or "").strip() or None
    link = None
    # DB membership is authoritative — JWT public_metadata can be stale after tenant delete/relink.
    if runtime.USE_DB and user_id:
        tenant = database.db_tenant_get_for_user(
            user_id, preferred_tenant_id=preferred_tid
        )
    if not tenant and tenant_id_from_meta and runtime.USE_DB:
        tenant = database.db_tenant_get_by_id(str(tenant_id_from_meta))
        if tenant and user_id:
            database.db_tenant_member_set_single(user_id, tenant["id"])
    if not tenant and runtime.USE_DB:
        # JWT often omits public_metadata; resolve via Clerk API + pending invite email.
        link = _clerk_fetch_user_link(user_id)
        if link:
            api_tenant_id = link.get("tenant_id")
            if api_tenant_id:
                preferred_tid = preferred_tid or str(api_tenant_id)
                tenant = database.db_tenant_get_by_id(str(api_tenant_id))
                if tenant:
                    database.db_tenant_member_set_single(user_id, tenant["id"])
                    print(
                        f"[Auth] Auto-linked user {user_id} to tenant {tenant['id']} via Clerk metadata"
                    )
            if not tenant:
                for em in link.get("emails") or []:
                    invited_tid = database.db_tenant_invite_consume(em)
                    if not invited_tid:
                        continue
                    tenant = database.db_tenant_get_by_id(invited_tid)
                    if tenant:
                        database.db_tenant_member_set_single(user_id, tenant["id"])
                        _clerk_patch_user_tenant_metadata(user_id, tenant["id"])
                        print(
                            f"[Auth] Auto-linked user {user_id} to tenant {tenant['id']} via invite email {em}"
                        )
                        break
    elif runtime.USE_DB and user_id and not preferred_tid:
        link = _clerk_fetch_user_link(user_id)
        if link and link.get("tenant_id"):
            preferred_tid = str(link.get("tenant_id"))
            if tenant and str(tenant.get("id")) != preferred_tid:
                alt = database.db_tenant_get_by_id(preferred_tid)
                if alt and preferred_tid in database.db_tenant_membership_tenant_ids(
                    user_id
                ):
                    tenant = alt
    if tenant and user_id:
        tid = str(tenant.get("id") or "").strip()
        meta_tid = str(tenant_id_from_meta or "").strip()
        if tid and meta_tid != tid:
            _clerk_patch_user_tenant_metadata(user_id, tid)
    if not tenant and runtime.USE_DB and user_id:
        # Last resort: an org member who isn't a tenant_member of anything — e.g. a
        # manager whose stores were all created through the org. With exactly one
        # store, land them in it rather than making them pick from a list of one.
        # With several, fall through to the 403; the dashboard sends them to the
        # store list to choose. Only runs when normal resolution found nothing, so
        # it costs a query for org managers, not for everyone.
        org_stores = database.db_org_stores_for_user(user_id)
        if len(org_stores) == 1:
            tenant = org_stores[0]
            # This path skips _resolve_org_store, so it applies the same role gate —
            # otherwise a read-only viewer with exactly one store gets write access
            # simply by not sending the store header.
            _enforce_org_write_role(
                request,
                tenant.get("org_role") or "viewer",
                user_id,
                tenant.get("client_id"),
            )
    if not tenant:
        print(
            f"[Auth] no_tenant user_id={user_id} jwt_metadata_tenant_id={tenant_id_from_meta!r}"
        )
        audit_log(
            "user",
            "auth_failure",
            actor_id=user_id,
            details={"reason": "no_tenant"},
            request=request,
        )
        raise HTTPException(
            status_code=403,
            detail=(
                "No tenant assigned to your account. Open the invite link from your email to finish sign-up, "
                "or ask your administrator to resend the invite using the exact email you use to sign in."
            ),
        )
    database.set_request_client_id(tenant["client_id"])
    return tenant


def require_admin(request: Request):
    """Dependency: require Bearer token and admin user (user_id in ADMIN_CLERK_USER_IDS)."""
    token = get_bearer_token(request)
    if not token:
        audit_log(
            "admin", "auth_failure", details={"reason": "no_token"}, request=request
        )
        raise HTTPException(status_code=401, detail="Authorization required")
    user_id, _ = verify_clerk_token(token)
    admin_ids = _admin_user_ids()
    if not admin_ids:
        audit_log(
            "admin",
            "auth_failure",
            actor_id=user_id,
            details={"reason": "admin_not_configured"},
            request=request,
        )
        raise HTTPException(status_code=403, detail="Admin not configured")
    if user_id not in admin_ids:
        audit_log(
            "admin",
            "auth_failure",
            actor_id=user_id,
            details={"reason": "not_admin"},
            request=request,
        )
        raise HTTPException(status_code=403, detail="Admin access required")
    return user_id


def require_user(request: Request) -> str:
    """Authenticate the Clerk user WITHOUT requiring a tenant — for self-serve signup,
    before any tenant exists. Returns the Clerk user_id."""
    token = get_bearer_token(request)
    if not token:
        audit_log("user", "auth_failure", details={"reason": "no_token"}, request=request)
        raise HTTPException(status_code=401, detail="Authorization required")
    user_id, _ = verify_clerk_token(token)
    if not user_id:
        raise HTTPException(status_code=401, detail="Invalid or expired token")
    return user_id


def require_active_subscription(tenant: Optional[dict] = Depends(require_tenant)):
    """Dependency: after require_tenant, require that tenant can use the app (trial or paid or exempt)."""
    state = get_tenant_subscription_state(tenant)
    if not state.get("can_use_app"):
        cid = (tenant or {}).get("client_id") if tenant else None
        usage_warning(
            "app_access_denied_subscription",
            client_id=cid,
            subscription_status=state.get("subscription_status"),
            plan=state.get("plan"),
        )
        raise HTTPException(
            status_code=403,
            detail={
                "code": "SUBSCRIPTION_REQUIRED",
                "message": "Subscription required. Your trial has ended. Please choose a plan to continue.",
            },
            headers={"X-Subscription-Required": "true"},
        )
    return tenant


def _bind_tenant_db_context(tenant: Optional[dict]) -> str:
    """Pin tenant client_id for DB queries (shared connection + async can lose context vars)."""
    cid = ((tenant or {}).get("client_id") or "").strip() or database._client_id()
    database.set_request_client_id(cid)
    return cid
