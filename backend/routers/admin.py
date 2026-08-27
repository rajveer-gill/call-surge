"""Admin API — tenant management, invites, members, legal holds, billing exemptions,
Twilio-number assignment, and ops self-check.

Admin-only (Depends(deps.require_admin)). Backed by clerk_service (user linking),
database (tenant/legal-hold CRUD), deps (auth/audit), config_service. Helpers are
module-qualified so monkeypatches target the owning module.
"""

from __future__ import annotations

import logging
import os
import re
import shutil
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field

import clerk_service
import config_service
import database
import deps
import runtime

logger = logging.getLogger("nuvatra")

TWILIO_ACCOUNT_SID = os.getenv("TWILIO_ACCOUNT_SID")
TWILIO_AUTH_TOKEN = os.getenv("TWILIO_AUTH_TOKEN")

router = APIRouter()


class AdminTenantTwilioUpdate(BaseModel):
    twilio_phone_number: str


class BillingExemptUpdate(BaseModel):
    exempt_until: Optional[str] = None
    extend_months: Optional[int] = None
    extend_trial_months: Optional[int] = None


class OrgPriceOverridesUpdate(BaseModel):
    """Per-plan Stripe price IDs for a partner rate. Omit or send "" to clear one."""
    starter: Optional[str] = None
    growth: Optional[str] = None
    pro: Optional[str] = None


class OrgPartnerDiscountUpdate(BaseModel):
    """Dollars off each store, per month. 0 clears the rate."""
    amount_off_per_store: float = Field(..., ge=0, le=100000)


class AccountPausedUpdate(BaseModel):
    paused: bool


class ReferralCodeCreate(BaseModel):
    code: str = Field(..., min_length=2, max_length=40)
    referrer_name: str = Field(..., min_length=1, max_length=200)
    referrer_contact: Optional[str] = Field(default=None, max_length=200)


class ReferralCodeActiveUpdate(BaseModel):
    active: bool


class CommissionPaidUpdate(BaseModel):
    paid: bool = True


class FailedEventResolveUpdate(BaseModel):
    resolved: bool = True


class AdminCreateTenantRequest(BaseModel):
    client_id: str
    name: str
    twilio_phone_number: str
    email: str
    plan: Optional[str] = "starter"
    business_vertical: str = "salon_chair"


class AdminResendInviteRequest(BaseModel):
    email: str


class AdminLegalHoldRequest(BaseModel):
    client_id: str = Field(..., min_length=1, max_length=120)
    reason: Optional[str] = Field(default=None, max_length=2000)
    hold_until: Optional[datetime] = None


def _cron_stale_jobs(last_success: dict) -> List[str]:
    """Daily cron jobs with no successful run in the last 36 hours."""
    stale: List[str] = []
    cutoff = datetime.now(timezone.utc) - timedelta(hours=36)
    for job in database.DAILY_CRON_JOBS:
        ts = last_success.get(job)
        if not ts:
            stale.append(job)
            continue
        try:
            finished = datetime.fromisoformat(ts.replace("Z", "+00:00"))
            if finished.tzinfo is None:
                finished = finished.replace(tzinfo=timezone.utc)
            if finished < cutoff:
                stale.append(job)
        except Exception:
            stale.append(job)
    return stale


def _extend_trial_through_exempt(tenant_id: str, exempt_until: datetime) -> None:
    """Keep trial_ends_at at or past billing_exempt_until so admin/client dates stay aligned."""
    tenant = database.db_tenant_get_by_id(tenant_id)
    if not tenant:
        return
    now = datetime.now(timezone.utc)
    trial_ends_at = tenant.get("trial_ends_at")
    try:
        if trial_ends_at:
            trial_dt = (
                datetime.fromisoformat(trial_ends_at.replace("Z", "+00:00"))
                if isinstance(trial_ends_at, str)
                else trial_ends_at
            )
            if trial_dt.tzinfo is None:
                trial_dt = trial_dt.replace(tzinfo=timezone.utc)
        else:
            trial_dt = now
    except Exception:
        trial_dt = now
    if exempt_until > trial_dt:
        database.db_tenant_extend_trial(tenant_id, exempt_until)


def _membership_diagnosis(
    user_id: str,
    jwt_tid: Optional[str],
    link: Optional[dict],
    tenant: Optional[dict],
    memberships: List[dict],
) -> dict:
    """Explain likely dashboard routing for support (no secrets)."""
    clerk_tid = (link or {}).get("tenant_id")
    db_tid = str((tenant or {}).get("id") or "")
    issues: List[str] = []
    if len(memberships) > 1:
        issues.append("multiple_db_memberships")
    if not memberships:
        issues.append("no_db_membership")
    if jwt_tid and db_tid and str(jwt_tid) != db_tid:
        issues.append("jwt_metadata_tenant_id_differs_from_db")
    if clerk_tid and db_tid and str(clerk_tid) != db_tid:
        issues.append("clerk_metadata_tenant_id_differs_from_db")
    if memberships and tenant and len(memberships) == 1:
        only = memberships[0]
        if only.get("tenant_id") != db_tid:
            issues.append("resolved_tenant_not_newest_membership")
    recommended = "ok"
    if "no_db_membership" in issues:
        recommended = "admin_resend_invite_exact_sign_in_email"
    elif issues:
        recommended = "sign_out_all_devices_then_sign_in_again"
    return {"issues": issues, "recommended_action": recommended}


def _admin_resolve_email_debug(email: str) -> dict:
    """Admin-only: where an email appears across invites, Clerk, and memberships."""
    email = (email or "").strip()
    if not email or "@" not in email:
        return {"error": "valid_email_required"}
    clerk_secret = os.getenv("CLERK_SECRET_KEY", "").strip()
    headers = (
        {"Authorization": f"Bearer {clerk_secret}", "Content-Type": "application/json"}
        if clerk_secret
        else None
    )
    invite_tid = database.db_tenant_invite_peek(email) if runtime.USE_DB else None
    invite_tenant = database.db_tenant_get_by_id(invite_tid) if invite_tid and runtime.USE_DB else None
    api_ids = clerk_service._clerk_user_ids_from_api(email, headers) if headers else []
    member_ids = clerk_service._clerk_user_ids_from_tenant_members(email, headers) if headers else []
    all_ids = list(dict.fromkeys(api_ids + member_ids))
    users: List[dict] = []
    for uid in all_ids:
        link = deps._clerk_fetch_user_link(uid) if headers else None
        users.append(
            {
                "clerk_user_id": uid,
                "clerk_emails": (link or {}).get("emails") or [],
                "clerk_metadata_tenant_id": (link or {}).get("tenant_id"),
                "db_memberships": database.db_tenant_memberships_for_user(uid) if runtime.USE_DB else [],
            }
        )
    tenants_by_client: List[dict] = []
    if runtime.USE_DB:
        for t in database.db_tenant_list_all():
            tid = str(t.get("id") or "")
            if database.db_tenant_get_invite_email(tid) == database._normalize_invite_email(email):
                tenants_by_client.append(
                    {
                        "tenant_id": tid,
                        "client_id": t.get("client_id"),
                        "role": "pending_invite_on_tenant",
                    }
                )
            for uid in database.db_tenant_get_members(tid):
                link = deps._clerk_fetch_user_link(uid) if headers else None
                for em in (link or {}).get("emails") or []:
                    if (em or "").strip().lower() == email.strip().lower():
                        tenants_by_client.append(
                            {
                                "tenant_id": tid,
                                "client_id": t.get("client_id"),
                                "role": "active_member",
                                "clerk_user_id": uid,
                            }
                        )
    return {
        "email": email,
        "pending_invite_tenant": (
            {
                "tenant_id": invite_tid,
                "client_id": (invite_tenant or {}).get("client_id"),
            }
            if invite_tid
            else None
        ),
        "clerk_user_ids_api": api_ids,
        "clerk_user_ids_member_scan": member_ids,
        "clerk_users": users,
        "tenant_roles_for_email": tenants_by_client,
    }


def _admin_tenant_with_access_email(tenant: dict) -> dict:
    """Attach dashboard owner / pending invite email for admin UI."""
    tid = str(tenant.get("id") or "")
    pending: Optional[str] = None
    owner_email: Optional[str] = None
    try:
        pending = database.db_tenant_get_invite_email(tid) if tid else None
        members = database.db_tenant_get_members(tid) if tid else []
        if members:
            link = deps._clerk_fetch_user_link(members[0])
            emails = (link or {}).get("emails") or []
            if emails:
                owner_email = str(emails[0]).strip()
    except Exception as e:
        print(
            f"[Admin] tenant access email lookup failed client_id={tenant.get('client_id')}: {e}"
        )
    allocated = owner_email or pending
    if owner_email and pending and owner_email.lower() != pending.lower():
        access_status = "active_pending_mismatch"
    elif owner_email:
        access_status = "active"
    elif pending:
        access_status = "pending_invite"
    else:
        access_status = "none"
    return {
        **tenant,
        "owner_email": owner_email,
        "pending_invite_email": pending,
        "allocated_email": allocated,
        "access_status": access_status,
    }


@router.get("/api/admin/session")
def admin_session(request: Request):
    """True if the bearer token user id is in ADMIN_CLERK_USER_IDS. No tenant required."""
    token = deps.get_bearer_token(request)
    if not token:
        return {"is_admin": False}
    try:
        user_id, _ = deps.verify_clerk_token(token)
    except HTTPException:
        return {"is_admin": False}
    admin_ids = [
        x.strip()
        for x in (os.getenv("ADMIN_CLERK_USER_IDS") or "").split(",")
        if x.strip()
    ]
    if not admin_ids:
        return {"is_admin": False}
    return {"is_admin": user_id in admin_ids}


@router.post("/api/admin/tenants")
def admin_create_tenant(
    req: AdminCreateTenantRequest,
    request: Request,
    admin_user_id: str = Depends(deps.require_admin),
):
    """Create tenant and send Clerk invite. Requires admin auth."""
    if not runtime.USE_DB:
        raise HTTPException(
            status_code=503, detail="Database required for multi-tenant"
        )
    bv = (req.business_vertical or "salon_chair").strip()
    if bv not in config_service.ALLOWED_BUSINESS_VERTICALS:
        raise HTTPException(status_code=400, detail="Invalid business_vertical")
    # New tenants get 7-day trial (plan=free, subscription_status=trialing); no paid plan at creation
    tenant = database.db_tenant_create(
        req.client_id, req.name, req.twilio_phone_number, "free", bv
    )
    if not tenant:
        raise HTTPException(
            status_code=409, detail="Tenant already exists or create failed"
        )
    cfg = config_service._default_client_config_data(req.client_id, tenant.get("plan") or "free")
    config_service.save_raw_client_config(req.client_id, cfg)
    link = clerk_service._clerk_link_email_to_tenant(req.email, tenant["id"])
    invite_sent = bool(link.get("invite_sent"))
    user_relinked = bool(link.get("user_relinked"))
    deps.audit_log(
        "admin",
        "tenant_created",
        actor_id=admin_user_id,
        resource_type="tenant",
        resource_id=tenant["id"],
        client_id=tenant["client_id"],
        details={"name": req.name, **link},
        request=request,
    )
    return {
        "success": True,
        "tenant": tenant,
        "invite_sent": invite_sent,
        "user_relinked": user_relinked,
        "clerk_error": link.get("clerk_error"),
        "linked_clerk_user_id": link.get("linked_clerk_user_id"),
    }


@router.post("/api/admin/tenants/{tenant_id}/resend-invite")
def admin_resend_invite(
    tenant_id: str,
    req: AdminResendInviteRequest,
    request: Request,
    admin_user_id: str = Depends(deps.require_admin),
):
    """Re-queue pending invite by email and send a new Clerk invitation (existing tenants)."""
    if not runtime.USE_DB:
        raise HTTPException(
            status_code=503, detail="Database required for multi-tenant"
        )
    tenant = database.db_tenant_get_by_id(tenant_id)
    if not tenant:
        raise HTTPException(status_code=404, detail="Tenant not found")
    email = (req.email or "").strip()
    if not email or "@" not in email:
        raise HTTPException(status_code=400, detail="Valid email required")
    link = clerk_service._clerk_link_email_to_tenant(email, tenant_id)
    invite_sent = bool(link.get("invite_sent"))
    user_relinked = bool(link.get("user_relinked"))
    deps.audit_log(
        "admin",
        "tenant_invite_resent",
        actor_id=admin_user_id,
        resource_type="tenant",
        resource_id=tenant_id,
        client_id=tenant.get("client_id"),
        details={"email": email, **link},
        request=request,
    )
    return {"success": True, **link}


@router.get("/api/admin/tenants")
def admin_list_tenants(_: str = Depends(deps.require_admin)):
    """List all tenants. Requires admin auth."""
    if not runtime.USE_DB:
        return {"tenants": [], "db_enabled": False}
    try:
        tenants = database.db_tenant_list_all()
    except Exception as e:
        logger.error("admin_list_tenants_failed err=%s: %s", type(e).__name__, e)
        raise HTTPException(
            status_code=500, detail="Failed to load tenants from database"
        ) from e
    # Group name per store, so the admin list can show a franchise's locations
    # together instead of scattered through one flat list. One query for the lot
    # rather than one per store.
    org_names: dict = {}
    try:
        for o in database.db_org_list_all():
            org_names[str(o.get("id"))] = o.get("name") or ""
    except Exception as e:
        # Cosmetic only — a store without its group name still lists correctly.
        logger.warning("admin_org_name_lookup_failed err=%s: %s", type(e).__name__, e)
    enriched: List[dict] = []
    for t in tenants:
        t = {**t, "org_name": org_names.get(str(t.get("org_id") or "")) or None}
        try:
            enriched.append(_admin_tenant_with_access_email(t))
        except Exception as e:
            print(f"[Admin] enrich tenant {t.get('client_id')} failed: {e}")
            enriched.append(
                {
                    **t,
                    "owner_email": None,
                    "pending_invite_email": None,
                    "allocated_email": None,
                    "access_status": "none",
                }
            )
    return {"tenants": enriched, "db_enabled": True}


# --- Orgs (multi-store oversight) ---------------------------------------------


class AdminOrgCreate(BaseModel):
    name: str = Field(..., min_length=1, max_length=200)


class AdminOrgMemberAdd(BaseModel):
    # Supply one of the two. clerk_user_id is exact; email is resolved via Clerk.
    clerk_user_id: Optional[str] = None
    email: Optional[str] = None
    # viewer = read-only oversight (the default, and what a franchise usually wants).
    role: str = Field(default="viewer")


class AdminOrgAttach(BaseModel):
    tenant_ids: List[str] = Field(default_factory=list)


@router.get("/api/admin/orgs")
def admin_list_orgs(_: str = Depends(deps.require_admin)):
    """Every org with its stores and overseers.

    A failure here must not answer 200 with an empty list. The console renders that
    as "Group not found / 0 stores", which is what a pool blip looked like to
    someone who had just changed that group's billing.
    """
    if not runtime.USE_DB:
        return {"orgs": [], "db_enabled": False}
    try:
        orgs = database.db_org_list_all()
    except database.DatabaseUnavailable as e:
        logger.error("admin_list_orgs_unavailable err=%s", e)
        raise HTTPException(
            status_code=503,
            detail="Could not read groups from the database. Nothing was changed — retry in a moment.",
        ) from e
    logger.info("admin_list_orgs count=%s", len(orgs))
    return {"orgs": orgs, "db_enabled": True}


@router.post("/api/admin/orgs")
def admin_create_org(req: AdminOrgCreate, request: Request, admin: str = Depends(deps.require_admin)):
    """Create an org — the group a multi-store account oversees."""
    if not runtime.USE_DB:
        raise HTTPException(status_code=503, detail="Database required")
    org = database.db_org_create(req.name)
    if not org:
        raise HTTPException(status_code=500, detail="Could not create org")
    deps.audit_log(
        "admin", "org_created", actor_id=admin, resource_type="org",
        resource_id=org["id"], details={"name": org["name"]}, request=request,
    )
    return org


@router.patch("/api/admin/orgs/{org_id}/partner-discount")
def admin_org_partner_discount(
    org_id: str,
    req: OrgPartnerDiscountUpdate,
    request: Request,
    admin_user_id: str = Depends(deps.require_admin),
):
    """Give a group a flat amount off every store, on every plan.

    The discount has to be a price rather than a coupon — a group is one subscription
    with quantity = store count, so a coupon's amount_off would come off the invoice
    once instead of off each store. This does the translation: read each standard
    price, subtract, reuse or create the matching Stripe price, and store the ids.

    A group that already subscribed is repriced too. A Stripe Price is immutable,
    but which price a subscription ITEM points at is not — without that step the
    discount silently never reaches anyone who signed up before it was configured,
    and every store they add afterwards bumps quantity on the undiscounted price.
    """
    if not runtime.USE_DB:
        raise HTTPException(status_code=503, detail="Database required")
    org = database.db_org_get_by_id(org_id)
    if not org:
        raise HTTPException(status_code=404, detail="Group not found")
    from routers import billing as billing_router

    cents = int(round(float(req.amount_off_per_store) * 100))
    if cents <= 0:
        database.db_org_set_price_overrides(org_id, None)
        # Symmetry matters: if applying a rate moves the subscription, clearing it has
        # to move them back, or "cleared" would leave them on the partner price.
        moved = billing_router.move_org_subscription_to_price(org_id)
        deps.audit_log(
            "admin", "org_partner_discount", actor_id=admin_user_id,
            resource_type="org", resource_id=org_id,
            details={"amount_off_per_store": 0, "cleared": True, "subscription": moved},
            request=request,
        )
        return {
            "success": True, "cleared": True, "price_overrides": {}, "subscription": moved,
        }

    built = billing_router.build_partner_prices(cents)
    errors = built.pop("_errors", [])
    if not built:
        raise HTTPException(
            status_code=502,
            detail="Could not create the discounted prices: " + "; ".join(errors or ["unknown error"]),
        )
    if not database.db_org_set_price_overrides(org_id, built):
        raise HTTPException(status_code=500, detail="Prices created but could not be saved")
    # Reprice an existing subscription. Best-effort and reported, never fatal: the
    # override is already saved, so a Stripe hiccup here must not read as "the
    # discount did not save" when it did.
    moved = billing_router.move_org_subscription_to_price(org_id)
    deps.audit_log(
        "admin", "org_partner_discount", actor_id=admin_user_id,
        resource_type="org", resource_id=org_id,
        details={
            "amount_off_per_store": req.amount_off_per_store,
            "price_overrides": built,
            "subscription": moved,
        },
        request=request,
    )
    return {
        "success": True,
        "amount_off_per_store": req.amount_off_per_store,
        "price_overrides": built,
        # Named plans that could not be priced, rather than a silent partial success.
        "errors": errors,
        # What happened to a subscription they already had, so the UI can say so
        # instead of implying every group is now paying the new rate.
        "subscription": moved,
    }


@router.patch("/api/admin/orgs/{org_id}/price-overrides")
def admin_org_price_overrides(
    org_id: str,
    req: OrgPriceOverridesUpdate,
    request: Request,
    admin_user_id: str = Depends(deps.require_admin),
):
    """Put a group on partner pricing.

    "$50 off each store" is a price, not a coupon: a coupon's amount_off comes off the
    invoice, and a group is one subscription with quantity = store count, so a fixed
    discount lands once whether that is 2 stores or 43. A discounted price is per-store
    by construction and holds its value across plans.

    Only affects the NEXT checkout — an existing subscription keeps the price it was
    created with, which Stripe treats as immutable on the item. Changing an active
    subscription's price is a deliberate act in Stripe, not a side effect of this.
    """
    if not runtime.USE_DB:
        raise HTTPException(status_code=503, detail="Database required")
    org = database.db_org_get_by_id(org_id)
    if not org:
        raise HTTPException(status_code=404, detail="Group not found")
    overrides: Dict[str, str] = {}
    bad: List[str] = []
    for plan in ("starter", "growth", "pro"):
        raw = (getattr(req, plan, None) or "").strip()
        if not raw:
            continue
        # Reject anything that is not a price id here rather than at checkout, where
        # the failure would be an opaque Stripe error in front of a paying customer.
        if not raw.startswith("price_"):
            bad.append(plan)
            continue
        overrides[plan] = raw[:120]
    if bad:
        raise HTTPException(
            status_code=400,
            detail=f"Not a Stripe price ID (must start with 'price_'): {', '.join(bad)}",
        )
    if not database.db_org_set_price_overrides(org_id, overrides or None):
        raise HTTPException(status_code=500, detail="Failed to save price overrides")
    deps.audit_log(
        "admin",
        "org_price_overrides",
        actor_id=admin_user_id,
        resource_type="org",
        resource_id=org_id,
        details={"overrides": overrides or None},
        request=request,
    )
    return {"success": True, "price_overrides": overrides}


@router.delete("/api/admin/orgs/{org_id}")
def admin_delete_org(
    org_id: str,
    request: Request,
    force: bool = False,
    cancel_subscription: bool = False,
    delete_stores: bool = False,
    admin: str = Depends(deps.require_admin),
):
    """Delete a group. Members and pending invites go with it; stores do NOT.

    Two guards, both bypassable with ?force=true, because both describe damage that
    is easy to cause and hard to notice:

    - **Still has stores.** Deleting would detach them (org_id -> NULL), so they'd
      become independent stores that no longer inherit the group's subscription —
      i.e. a paying customer's locations quietly stop being paid for.
    - **Still has a Stripe subscription.** Deleting the org loses our only link to
      it, and Stripe keeps billing a subscription nobody can now find or cancel.

    Detach the stores and cancel the subscription first, or pass force if you know
    the group is disposable (a test leftover, typically).
    """
    if not runtime.USE_DB:
        raise HTTPException(status_code=503, detail="Database required")
    org = database.db_org_get_by_id(org_id)
    if not org:
        raise HTTPException(status_code=404, detail="Org not found")
    store_count = database.db_org_store_count(org_id)
    sub_id = (org.get("stripe_subscription_id") or "").strip()
    if not force and not delete_stores:
        if store_count:
            raise HTTPException(
                status_code=409,
                detail=(
                    f"This group still has {store_count} store(s). Detaching them here would "
                    "stop them inheriting its subscription. Move or delete the stores first."
                ),
            )
        if sub_id:
            # Name the subscription. "Cancel it in Stripe" is not actionable without
            # knowing which one, and the console previously told the admin to "retry
            # with force=true" — a parameter the UI had no way to send.
            raise HTTPException(
                status_code=409,
                detail=(
                    f"This group still has an active Stripe subscription ({sub_id}). "
                    "Deleting the group would leave it billing with nothing pointing at it. "
                    "Delete it with cancel_subscription=true to cancel that subscription "
                    "as part of the delete, or cancel it in Stripe first."
                ),
            )
    cancelled: Optional[dict] = None
    if sub_id and cancel_subscription:
        # Cancel BEFORE deleting the group. The other order loses the only record
        # linking us to the subscription, so a failure there would leave it billing
        # with nothing left to find it by — the exact outcome the guard exists for.
        from routers import billing as billing_router

        cancelled = billing_router.cancel_org_subscription(org_id, sub_id)
        if not cancelled.get("ok"):
            raise HTTPException(
                status_code=502,
                detail=(
                    "Could not cancel the subscription, so the group was left alone: "
                    + str(cancelled.get("error") or "unknown error")
                ),
            )
    deleted_stores: List[str] = []
    if delete_stores and store_count:
        # Reuse the tenant-delete route rather than writing a second deletion. It
        # releases the Twilio number, archives the operational data, cascades
        # tenant_members, and clears each member's Clerk metadata and sessions —
        # which is what makes a later sign-in behave like a new account rather than
        # dropping someone back into a store that no longer has a group.
        #
        # Detaching alone leaves live stores with a phone number, no owner and no
        # billing. "Delete the group" should not quietly mean "keep its locations".
        for st in database.db_org_stores(org_id) or []:
            tid = str(st.get("id") or "")
            if not tid:
                continue
            admin_delete_tenant(tenant_id=tid, request=request, admin_user_id=admin)
            deleted_stores.append(st.get("client_id") or tid)

    ok = database.db_org_delete(org_id)
    deps.audit_log(
        "admin",
        "org_deleted",
        actor_id=admin,
        resource_type="org",
        resource_id=org_id,
        details={
            "cancelled_subscription": (cancelled or {}).get("subscription_id"),
            "deleted_stores": deleted_stores,
            "name": org.get("name"),
            "stores_detached": store_count,
            "had_subscription": bool(sub_id),
            "forced": bool(force),
            "ok": ok,
        },
        request=request,
    )
    if not ok:
        raise HTTPException(status_code=500, detail="Could not delete the group")
    return {"ok": True, "stores_detached": store_count}


@router.post("/api/admin/orgs/{org_id}/stores")
def admin_attach_stores(
    org_id: str, req: AdminOrgAttach, request: Request, admin: str = Depends(deps.require_admin)
):
    """Put existing stores into an org. Idempotent; re-attaching is a no-op."""
    if not runtime.USE_DB:
        raise HTTPException(status_code=503, detail="Database required")
    if not database.db_org_get_by_id(org_id):
        raise HTTPException(status_code=404, detail="Org not found")
    attached, failed = [], []
    for tid in req.tenant_ids:
        (attached if database.db_org_attach_tenant(tid, org_id) else failed).append(tid)
    deps.audit_log(
        "admin", "org_stores_attached", actor_id=admin, resource_type="org",
        resource_id=org_id, details={"attached": attached, "failed": failed}, request=request,
    )
    return {"attached": attached, "failed": failed}


@router.delete("/api/admin/orgs/{org_id}/stores/{tenant_id}")
def admin_detach_store(
    org_id: str, tenant_id: str, request: Request, admin: str = Depends(deps.require_admin)
):
    """Make a store independent again. Its own data and owner are untouched."""
    if not runtime.USE_DB:
        raise HTTPException(status_code=503, detail="Database required")
    ok = database.db_org_attach_tenant(tenant_id, None)
    deps.audit_log(
        "admin", "org_store_detached", actor_id=admin, resource_type="org",
        resource_id=org_id, details={"tenant_id": tenant_id, "ok": ok}, request=request,
    )
    return {"ok": ok}


@router.post("/api/admin/orgs/{org_id}/members")
def admin_add_org_member(
    org_id: str, req: AdminOrgMemberAdd, request: Request, admin: str = Depends(deps.require_admin)
):
    """Grant a user oversight of an org's stores.

    Pass clerk_user_id to add an exact account immediately. Pass an email and they're
    added now if they already have an account, or emailed an invite and added the
    first time they sign in with that address.
    """
    if not runtime.USE_DB:
        raise HTTPException(status_code=503, detail="Database required")
    if not database.db_org_get_by_id(org_id):
        raise HTTPException(status_code=404, detail="Org not found")
    # A group has exactly one owner, and this path writes the role directly. Without
    # this guard the invariant the customer-facing rules depend on can be broken from
    # the admin side, which is the one place it would go unnoticed.
    if (req.role or "").strip().lower() == "owner":
        raise HTTPException(
            status_code=400,
            detail="Add them as a manager, then use POST /api/admin/orgs/{org_id}/owner to hand over ownership.",
        )
    uid = (req.clerk_user_id or "").strip()
    if uid:
        if not database.db_org_member_add(uid, org_id, req.role):
            raise HTTPException(status_code=500, detail="Could not add org member")
        deps.audit_log(
            "admin", "org_member_added", actor_id=admin, resource_type="org",
            resource_id=org_id, details={"clerk_user_id": uid, "role": req.role}, request=request,
        )
        return {"ok": True, "added": True, "clerk_user_id": uid, "org_id": org_id, "role": req.role}
    email = (req.email or "").strip()
    if not email:
        raise HTTPException(status_code=400, detail="Provide clerk_user_id or email")
    link = clerk_service._clerk_invite_email_to_org(email, org_id, req.role)
    deps.audit_log(
        "admin", "org_member_invited", actor_id=admin, resource_type="org",
        resource_id=org_id,
        details={"email": email, "role": req.role,
                 "user_added": bool(link.get("user_added")),
                 "invite_sent": bool(link.get("invite_sent"))},
        request=request,
    )
    # Only a hard failure with nothing queued is an error; a stored-but-unsent invite
    # (e.g. Clerk key missing) still links them if they sign up, so report it, don't 500.
    if link.get("clerk_error") and not link.get("invite_sent") and not link.get("user_added") \
            and not link.get("pending_invite_stored"):
        raise HTTPException(status_code=502, detail=str(link.get("clerk_error")))
    return {
        "ok": True,
        "added": bool(link.get("user_added")),
        "invite_sent": bool(link.get("invite_sent")),
        "pending": bool(link.get("pending_invite_stored")) and not link.get("user_added"),
        "clerk_error": link.get("clerk_error"),
        "org_id": org_id,
        "role": req.role,
    }


class AdminOrgInviteRevoke(BaseModel):
    email: str


@router.delete("/api/admin/orgs/{org_id}/invites")
def admin_revoke_org_invite(
    org_id: str, req: AdminOrgInviteRevoke, request: Request, admin: str = Depends(deps.require_admin)
):
    """Cancel a pending (not-yet-accepted) org invite."""
    if not runtime.USE_DB:
        raise HTTPException(status_code=503, detail="Database required")
    ok = database.db_org_invite_delete(req.email, org_id)
    deps.audit_log(
        "admin", "org_invite_revoked", actor_id=admin, resource_type="org",
        resource_id=org_id, details={"email": req.email, "ok": ok}, request=request,
    )
    return {"ok": ok}


@router.delete("/api/admin/orgs/{org_id}/members/{clerk_user_id}")
def admin_remove_org_member(
    org_id: str, clerk_user_id: str, request: Request, admin: str = Depends(deps.require_admin)
):
    """Revoke a user's oversight of an org."""
    if not runtime.USE_DB:
        raise HTTPException(status_code=503, detail="Database required")
    ok = database.db_org_member_remove(clerk_user_id, org_id)
    deps.audit_log(
        "admin", "org_member_removed", actor_id=admin, resource_type="org",
        resource_id=org_id, details={"clerk_user_id": clerk_user_id, "ok": ok}, request=request,
    )
    return {"ok": ok}


@router.post("/api/admin/tenants/bulk", deprecated=True)
def admin_bulk_create_tenants(_admin: str = Depends(deps.require_admin)):
    """Deprecated. The synchronous bulk-create timed out and left tenants
    half-provisioned at scale. Use the background provisioning pipeline:
    POST /api/admin/provisioning/jobs (idempotent, resumable, auto-purchases
    Twilio numbers)."""
    raise HTTPException(
        status_code=410,
        detail="Deprecated. Use POST /api/admin/provisioning/jobs for bulk onboarding.",
    )


@router.get("/api/admin/ops/self-check")
def admin_ops_self_check(_: str = Depends(deps.require_admin)):
    """Production safety checks for webhook and auth hardening."""
    from voice.redis_ops_health import redis_ops_health

    cron_secret_set = bool((os.getenv("CRON_SECRET") or "").strip())
    twilio_auth_token_set = bool((os.getenv("TWILIO_AUTH_TOKEN") or "").strip())
    public_base_url_set = bool((os.getenv("PUBLIC_BASE_URL") or "").strip())
    client_id_set = bool((os.getenv("CLIENT_ID") or "").strip())
    last_cron_runs = database.db_cron_runs_last_success() if runtime.USE_DB else {}
    stale_cron_jobs = (
        _cron_stale_jobs(last_cron_runs)
        if runtime.USE_DB and cron_secret_set
        else list(database.DAILY_CRON_JOBS)
    )
    stt_provider = (os.getenv("VOICE_STT_PROVIDER") or "twilio").strip().lower()
    deepgram_key_set = bool((os.getenv("DEEPGRAM_API_KEY") or "").strip())
    redis_health = redis_ops_health()
    # Reported as a VALUE, not a boolean. Every invitation link is built from this,
    # so a set-but-wrong FRONTEND_URL sends invitees to the other environment's app
    # and a different Clerk instance — the invite arrives, looks right, and cannot
    # be redeemed. "Is it set" cannot catch that; only reading it can. It is the
    # site's own public address, so there is nothing here to leak to an admin.
    frontend_url = (os.getenv("FRONTEND_URL") or "").strip().rstrip("/")
    frontend_url_ok = bool(
        frontend_url
        and frontend_url.startswith("https://")
        and frontend_url.count("/") == 2  # scheme + host only, no path
    )
    return {
        "frontend_url": frontend_url or None,
        "frontend_url_ok": frontend_url_ok,
        "public_base_url_set": public_base_url_set,
        "twilio_signature_validation_enabled": twilio_auth_token_set,
        "cron_secret_set": cron_secret_set,
        "multi_tenant_client_id_env_ok": not client_id_set,
        "database_enabled": bool(runtime.USE_DB),
        **redis_health,
        "clerk_issuer_set": bool((os.getenv("CLERK_ISSUER") or "").strip()),
        "clerk_audience_set": bool((os.getenv("CLERK_AUDIENCE") or "").strip()),
        "deepgram_ready": stt_provider != "deepgram" or deepgram_key_set,
        "last_cron_runs": last_cron_runs,
        "stale_cron_jobs": stale_cron_jobs,
        "cron_jobs_healthy": len(stale_cron_jobs) == 0,
    }


@router.get("/api/admin/legal-holds")
def admin_list_legal_holds(_: str = Depends(deps.require_admin)):
    if not runtime.USE_DB:
        raise HTTPException(status_code=503, detail="Database required")
    return {"holds": database.db_legal_hold_list_active()}


@router.post("/api/admin/legal-holds")
def admin_upsert_legal_hold(
    req: AdminLegalHoldRequest,
    request: Request,
    admin_user_id: str = Depends(deps.require_admin),
):
    if not runtime.USE_DB:
        raise HTTPException(status_code=503, detail="Database required")
    ok = database.db_legal_hold_set(
        req.client_id.strip(),
        reason=(req.reason or "").strip() or None,
        hold_until=req.hold_until,
        created_by=admin_user_id,
    )
    if not ok:
        raise HTTPException(status_code=500, detail="Failed to set legal hold")
    deps.audit_log(
        "admin",
        "legal_hold_upserted",
        actor_id=admin_user_id,
        resource_type="tenant",
        client_id=req.client_id.strip(),
        details={
            "reason": (req.reason or "").strip()[:200],
            "hold_until": req.hold_until.isoformat() if req.hold_until else None,
        },
        request=request,
    )
    return {"success": True}


@router.delete("/api/admin/legal-holds/{client_id}")
def admin_clear_legal_hold(
    client_id: str,
    request: Request,
    admin_user_id: str = Depends(deps.require_admin),
):
    if not runtime.USE_DB:
        raise HTTPException(status_code=503, detail="Database required")
    cleared = database.db_legal_hold_clear(client_id)
    deps.audit_log(
        "admin",
        "legal_hold_cleared",
        actor_id=admin_user_id,
        resource_type="tenant",
        client_id=(client_id or "").strip(),
        details={"cleared": bool(cleared)},
        request=request,
    )
    return {"success": True, "cleared": bool(cleared)}


@router.get("/api/admin/tenants/{tenant_id}/access-debug")
def admin_tenant_access_debug(tenant_id: str, _: str = Depends(deps.require_admin)):
    """Admin: full access wiring snapshot for one tenant (invite, DB member, Clerk metadata)."""
    if not runtime.USE_DB:
        raise HTTPException(status_code=503, detail="Database required")
    return clerk_service._admin_tenant_access_debug_snapshot(tenant_id)


@router.get("/api/admin/debug/resolve-email")
def admin_resolve_email_debug(email: str, _: str = Depends(deps.require_admin)):
    """Admin: find an email across pending invites, Clerk users, and tenant memberships."""
    if not runtime.USE_DB:
        raise HTTPException(status_code=503, detail="Database required")
    return _admin_resolve_email_debug(email)


@router.patch("/api/admin/tenants/{tenant_id}/twilio-phone")
def admin_update_tenant_twilio_phone(
    tenant_id: str,
    req: AdminTenantTwilioUpdate,
    request: Request,
    admin_user_id: str = Depends(deps.require_admin),
):
    """Set the tenant's inbound Twilio number so SMS/voice webhooks resolve the tenant (E.164)."""
    if not runtime.USE_DB:
        raise HTTPException(status_code=503, detail="Database required")
    phone = (req.twilio_phone_number or "").strip()
    if not any(c.isdigit() for c in phone):
        raise HTTPException(
            status_code=400,
            detail="twilio_phone_number must contain digits (E.164 or US local is fine)",
        )
    tenant = database.db_tenant_get_by_id(tenant_id)
    if not tenant:
        raise HTTPException(status_code=404, detail="Tenant not found")
    if not database.db_tenant_set_twilio_phone(tenant_id, phone):
        raise HTTPException(status_code=500, detail="Failed to update Twilio number")
    updated = database.db_tenant_get_by_id(tenant_id)
    from twilio_provision import (
        configure_webhooks,
        public_webhook_result,
        enroll_in_messaging_service,
    )

    base = deps._public_base_url()
    webhook_config = configure_webhooks(
        account_sid=TWILIO_ACCOUNT_SID or "",
        auth_token=TWILIO_AUTH_TOKEN or "",
        phone=(updated or {}).get("twilio_phone_number") or phone,
        base_url=base,
    )
    public_config = public_webhook_result(webhook_config)
    # Store the number SID so the line can be released reliably on churn/deletion.
    if webhook_config.get("number_sid"):
        database.db_tenant_set_twilio_number_sid(tenant_id, webhook_config["number_sid"])
        updated = database.db_tenant_get_by_id(tenant_id)
        # A2P: enroll the number in the Messaging Service so outbound SMS from it is
        # campaign-registered (else carrier 30034). The purchase flow already does this;
        # doing it here too means admin-assigned numbers are covered without a Twilio
        # console trip. Best-effort and idempotent; no-ops when the env is unset.
        if runtime.twilio_client:
            enroll_in_messaging_service(runtime.twilio_client, webhook_config["number_sid"])
    deps.audit_log(
        "admin",
        "tenant_twilio_phone_updated",
        actor_id=admin_user_id,
        resource_type="tenant",
        resource_id=tenant_id,
        client_id=tenant.get("client_id"),
        details={
            "twilio_phone_number": (updated or {}).get("twilio_phone_number") or phone,
            "webhook_config": public_config,
        },
        request=request,
    )
    return {"success": True, "tenant": updated, "webhook_config": public_config}


def _release_tenant_twilio_number_for_delete(tenant: dict, admin_user_id: str, request: Request) -> None:
    """Release the tenant's Twilio number on admin delete. Best-effort; never raises."""
    phone = (tenant.get("twilio_phone_number") or "").strip()
    if not phone:
        return
    acct = (TWILIO_ACCOUNT_SID or "").strip()
    tok = (TWILIO_AUTH_TOKEN or "").strip()
    if not (acct and tok):
        print(f"[Admin] twilio_release_skipped: missing Twilio creds tenant={tenant.get('id')}")
        return
    try:
        from twilio_provision import release_number

        res = release_number(
            account_sid=acct,
            auth_token=tok,
            phone_e164=phone,
            number_sid=(tenant.get("twilio_number_sid") or None),
        )
        deps.audit_log(
            "admin",
            "twilio_number_released",
            actor_id=admin_user_id,
            resource_type="tenant",
            resource_id=tenant.get("id"),
            client_id=tenant.get("client_id"),
            details={
                "phone_e164": phone,
                "released": res.get("released"),
                "removed_from_messaging_service": res.get("removed_from_messaging_service"),
                "errors": res.get("errors"),
            },
            request=request,
        )
    except Exception as e:
        print(f"[Admin] twilio_release_unexpected tenant={tenant.get('id')}: {e}")


@router.delete("/api/admin/tenants/{tenant_id}")
def admin_delete_tenant(
    tenant_id: str, request: Request, admin_user_id: str = Depends(deps.require_admin)
):
    """Delete a tenant and revoke access for its members.

    Steps:
      1. Look up all tenant_members (clerk_user_ids) before any destructive work.
      2. Archive all client_id-scoped operational data to tenant_removed_archive, then delete live rows
         (so a new tenant reusing the same client_id does not see old runtime.appointments, etc.; archive supports retention).
      3. Remove clients/<client_id> on-disk config if present.
      4. Delete the tenant row (cascades to tenant_members).
      5. For each former member via Clerk API: clear public_metadata tenant_id and revoke sessions.
      Users are NOT banned — they can be re-invited to a new tenant later.
    """
    if not runtime.USE_DB:
        raise HTTPException(status_code=503, detail="Database required")
    tenant = database.db_tenant_get_by_id(tenant_id)
    if not tenant:
        raise HTTPException(status_code=404, detail="Tenant not found")
    member_ids = database.db_tenant_get_members(tenant_id)
    # Release the Twilio number BEFORE deleting the tenant row, while we still know it.
    # Best-effort: a Twilio hiccup must not block tenant deletion.
    _release_tenant_twilio_number_for_delete(tenant, admin_user_id, request)
    archive_id = database.db_archive_purge_and_delete_tenant(
        tenant_id, tenant, actor_clerk_id=admin_user_id
    )
    if archive_id is None:
        raise HTTPException(
            status_code=500,
            detail="Failed to archive tenant operational data; tenant was not removed. Retry or check database logs.",
        )
    client_slug = (tenant.get("client_id") or "").strip()
    if client_slug:
        client_dir = config_service.PROJECT_ROOT / "clients" / client_slug
        try:
            if client_dir.is_dir():
                shutil.rmtree(client_dir, ignore_errors=True)
        except Exception as e:
            print(f"[Admin] Could not remove client directory {client_dir}: {e}")
    revoked_users: list[str] = []
    clerk_secret = os.getenv("CLERK_SECRET_KEY", "").strip()
    if clerk_secret and member_ids:
        import httpx

        headers = {
            "Authorization": f"Bearer {clerk_secret}",
            "Content-Type": "application/json",
        }
        for uid in member_ids:
            try:
                httpx.patch(
                    f"https://api.clerk.com/v1/users/{uid}",
                    headers=headers,
                    json={"public_metadata": {"tenant_id": None}},
                    timeout=10.0,
                )
                sessions_resp = httpx.get(
                    f"https://api.clerk.com/v1/sessions?user_id={uid}&status=active",
                    headers=headers,
                    timeout=10.0,
                )
                for session in clerk_service._clerk_api_json_list(sessions_resp):
                    sid = session.get("id") if isinstance(session, dict) else None
                    if not sid:
                        continue
                    httpx.post(
                        f"https://api.clerk.com/v1/sessions/{sid}/revoke",
                        headers=headers,
                        timeout=10.0,
                    )
                revoked_users.append(uid)
            except Exception as e:
                print(f"[Admin] Error revoking access for Clerk user {uid}: {e}")
    deps.audit_log(
        "admin",
        "tenant_deleted",
        actor_id=admin_user_id,
        resource_type="tenant",
        resource_id=tenant_id,
        client_id=tenant.get("client_id"),
        details={"name": tenant.get("name"), "data_archive_id": archive_id},
        request=request,
    )
    return {
        "success": True,
        "deleted_tenant": tenant,
        "revoked_users": revoked_users,
        "data_archive_id": archive_id,
    }


def _defer_stripe_billing(tenant: dict, until: datetime) -> dict:
    """Push the subscription's trial_end in Stripe so it stops invoicing until `until`.

    Extending a trial or granting free months used to change only our own database, so
    a customer with a live subscription kept being charged on renewal while the admin
    screen said they were exempt. Stripe's own trial is the right mechanism: while
    trial_end is in the future it does not invoice, and it reverts to normal billing
    afterwards without anything else to remember.

    Returns {applied, reason, error} and never raises. A Stripe failure must never be
    reported as success — "they won't be charged" is the entire claim being made.
    """
    sub_id = (tenant.get("stripe_subscription_id") or "").strip()
    if not sub_id:
        # Nothing is billing them; the database grant is the whole story.
        return {"applied": False, "reason": "no_subscription", "error": None}
    key = (os.getenv("STRIPE_SECRET_KEY") or "").strip()
    if not key:
        return {
            "applied": False, "reason": "not_configured",
            "error": "STRIPE_SECRET_KEY is not set on this backend.",
        }
    try:
        import stripe as _stripe

        _stripe.api_key = key
        _stripe.Subscription.modify(
            sub_id,
            trial_end=int(until.timestamp()),
            # Moving an active subscription back into a trial must not credit or
            # invoice for the part-period; we are deferring billing, not refunding.
            proration_behavior="none",
        )
        return {"applied": True, "reason": "trial_end_set", "error": None}
    except Exception as e:
        print(f"[Admin] stripe trial_end update failed sub={sub_id}: {type(e).__name__}: {e}")
        return {
            "applied": False, "reason": "stripe_error",
            "error": f"{type(e).__name__}: {e}"[:200],
        }


@router.get("/api/admin/tenants/{tenant_id}/stripe-status")
def admin_tenant_stripe_status(
    tenant_id: str,
    admin_user_id: str = Depends(deps.require_admin),
):
    """What Stripe currently thinks, read live rather than from our own column.

    Our subscription_status is a copy kept up to date by webhooks, and a webhook that
    silently fails leaves it wrong in exactly the situation an admin is investigating:
    a customer who was charged while the app still asks them to pick a plan. Showing
    our own copy back would confirm the mistake instead of exposing it, so this asks
    Stripe.

    Never raises for a Stripe problem — the admin screen must still render.
    """
    if not runtime.USE_DB:
        raise HTTPException(status_code=503, detail="Database required")
    tenant = database.db_tenant_get_by_id(tenant_id)
    if not tenant:
        raise HTTPException(status_code=404, detail="Tenant not found")
    sub_id = (tenant.get("stripe_subscription_id") or "").strip()
    ours = (tenant.get("subscription_status") or "").strip() or None
    if not sub_id:
        return {
            "has_subscription": False,
            "ours": ours,
            "stripe": None,
            "in_sync": None,
            "message": "No Stripe subscription on file for this tenant.",
        }
    try:
        import stripe as _stripe

        _stripe.api_key = (os.getenv("STRIPE_SECRET_KEY") or "").strip()
        if not _stripe.api_key:
            return {
                "has_subscription": True, "ours": ours, "stripe": None, "in_sync": None,
                "message": "STRIPE_SECRET_KEY is not set on this backend.",
            }
        sub = _stripe.Subscription.retrieve(sub_id)
    except Exception as e:
        print(f"[Admin] stripe status read failed sub={sub_id}: {type(e).__name__}")
        return {
            "has_subscription": True, "ours": ours, "stripe": None, "in_sync": None,
            "message": f"Could not read Stripe ({type(e).__name__}).",
        }

    def _ts(v):
        try:
            return datetime.fromtimestamp(int(v), tz=timezone.utc).isoformat() if v else None
        except Exception:
            return None

    live = str(getattr(sub, "status", "") or "") or None
    return {
        "has_subscription": True,
        "subscription_id": sub_id,
        "ours": ours,
        "stripe": live,
        # The whole point of the screen: say plainly when the two disagree.
        "in_sync": (ours == live) if (ours and live) else None,
        "cancel_at_period_end": bool(getattr(sub, "cancel_at_period_end", False)),
        "current_period_end": _ts(getattr(sub, "current_period_end", None)),
        "trial_end": _ts(getattr(sub, "trial_end", None)),
    }


@router.patch("/api/admin/tenants/{tenant_id}/billing-exempt")
def admin_tenant_billing_exempt(
    tenant_id: str,
    req: BillingExemptUpdate,
    request: Request,
    admin_user_id: str = Depends(deps.require_admin),
):
    """Set billing exemption or extend trial for a tenant. Admin only."""
    if not runtime.USE_DB:
        raise HTTPException(status_code=503, detail="Database required")
    tenant = database.db_tenant_get_by_id(tenant_id)
    if not tenant:
        raise HTTPException(status_code=404, detail="Tenant not found")
    now = datetime.now(timezone.utc)
    if req.extend_trial_months is not None and req.extend_trial_months >= 0:
        trial_ends_at = tenant.get("trial_ends_at")
        try:
            if trial_ends_at:
                trial_dt = (
                    datetime.fromisoformat(trial_ends_at.replace("Z", "+00:00"))
                    if isinstance(trial_ends_at, str)
                    else trial_ends_at
                )
                if trial_dt.tzinfo is None:
                    trial_dt = trial_dt.replace(tzinfo=timezone.utc)
                base = max(trial_dt, now)
            else:
                base = now
            new_ends = base + timedelta(days=30 * req.extend_trial_months)
            # Extending a free trial must also restore `trialing` status, not just the date.
            # get_plan_limits only grants full (pro-tier) trial access when
            # subscription_status == "trialing"; if a tenant was charged (status "active")
            # or canceled/refunded, pushing trial_ends_at alone leaves them gated to starter.
            # Set both together so an admin-granted trial is always the full experience.
            if database.db_tenant_update_subscription(
                tenant_id, subscription_status="trialing", trial_ends_at=new_ends
            ):
                deps.audit_log(
                    "admin",
                    "billing_exempt",
                    actor_id=admin_user_id,
                    resource_type="tenant",
                    resource_id=tenant_id,
                    client_id=tenant.get("client_id"),
                    details={
                        "action": "extend_trial_months",
                        "months": req.extend_trial_months,
                        "trial_ends_at": new_ends.isoformat(),
                        "subscription_status": "trialing",
                    },
                    request=request,
                )
                stripe_result = _defer_stripe_billing(tenant, new_ends)
                return {
                    "success": True,
                    "trial_ends_at": new_ends.isoformat(),
                    "subscription_status": "trialing",
                    "stripe": stripe_result,
                }
        except Exception as e:
            raise deps._server_error(
                "trial extension failed",
                e,
                status_code=400,
                public_detail="Could not update trial",
            )
    if req.extend_months is not None and req.extend_months >= 0:
        exempt_until = now + timedelta(days=30 * req.extend_months)
        if database.db_tenant_set_billing_exempt(tenant_id, exempt_until):
            _extend_trial_through_exempt(tenant_id, exempt_until)
            deps.audit_log(
                "admin",
                "billing_exempt",
                actor_id=admin_user_id,
                resource_type="tenant",
                resource_id=tenant_id,
                client_id=tenant.get("client_id"),
                details={
                    "action": "extend_months",
                    "months": req.extend_months,
                    "exempt_until": exempt_until.isoformat(),
                },
                request=request,
            )
            stripe_result = _defer_stripe_billing(tenant, exempt_until)
            return {
                "success": True,
                "billing_exempt_until": exempt_until.isoformat(),
                "stripe": stripe_result,
            }
    if req.exempt_until:
        try:
            exempt_dt = datetime.fromisoformat(req.exempt_until.replace("Z", "+00:00"))
            if exempt_dt.tzinfo is None:
                exempt_dt = exempt_dt.replace(tzinfo=timezone.utc)
            if database.db_tenant_set_billing_exempt(tenant_id, exempt_dt):
                _extend_trial_through_exempt(tenant_id, exempt_dt)
                deps.audit_log(
                    "admin",
                    "billing_exempt",
                    actor_id=admin_user_id,
                    resource_type="tenant",
                    resource_id=tenant_id,
                    client_id=tenant.get("client_id"),
                    details={
                        "action": "exempt_until",
                        "exempt_until": exempt_dt.isoformat(),
                    },
                    request=request,
                )
                stripe_result = _defer_stripe_billing(tenant, exempt_dt)
                return {
                    "success": True,
                    "billing_exempt_until": exempt_dt.isoformat(),
                    "stripe": stripe_result,
                }
        except ValueError as e:
            raise HTTPException(
                status_code=400, detail=f"Invalid exempt_until date: {e}"
            )
    raise HTTPException(
        status_code=400,
        detail="Provide exempt_until, extend_months, or extend_trial_months",
    )


@router.patch("/api/admin/tenants/{tenant_id}/account-paused")
def admin_tenant_account_paused(
    tenant_id: str,
    req: AccountPausedUpdate,
    request: Request,
    admin_user_id: str = Depends(deps.require_admin),
):
    """Manually pause/resume a tenant. While paused, the tenant's voice and SMS
    webhooks decline service (via the shared subscription-access gate). Admin only."""
    if not runtime.USE_DB:
        raise HTTPException(status_code=503, detail="Database required")
    tenant = database.db_tenant_get_by_id(tenant_id)
    if not tenant:
        raise HTTPException(status_code=404, detail="Tenant not found")
    if not database.db_tenant_set_account_paused(tenant_id, req.paused):
        raise HTTPException(status_code=500, detail="Failed to update pause state")
    deps.audit_log(
        "admin",
        "account_paused",
        actor_id=admin_user_id,
        resource_type="tenant",
        resource_id=tenant_id,
        client_id=tenant.get("client_id"),
        details={"paused": bool(req.paused)},
        request=request,
    )
    return {"success": True, "account_paused": bool(req.paused)}


# --- Referral program ---

@router.post("/api/admin/referral-codes")
def admin_create_referral_code(
    req: ReferralCodeCreate, request: Request, admin_user_id: str = Depends(deps.require_admin)
):
    """Create a shareable referral code tied to a referrer. Admin only."""
    if not runtime.USE_DB:
        raise HTTPException(status_code=503, detail="Database required")
    code = (req.code or "").strip().upper()
    if not re.fullmatch(r"[A-Z0-9-]{2,40}", code):
        raise HTTPException(status_code=400, detail="Code must be 2–40 letters, numbers, or hyphens")
    code_id = database.db_referral_code_create(
        code, req.referrer_name.strip(), (req.referrer_contact or "").strip() or None, admin_user_id
    )
    if not code_id:
        raise HTTPException(status_code=409, detail="That code already exists")
    deps.audit_log(
        "admin", "referral_code_created", actor_id=admin_user_id,
        resource_type="referral_code", resource_id=str(code_id),
        details={"code": code, "referrer_name": req.referrer_name.strip()}, request=request,
    )
    return {"success": True, "id": code_id, "code": code}


@router.get("/api/admin/referral-codes")
def admin_list_referral_codes(_: str = Depends(deps.require_admin)):
    """List referral codes with signup/conversion counts. Admin only."""
    if not runtime.USE_DB:
        return {"codes": [], "db_enabled": False}
    return {"codes": database.db_referral_codes_list_with_counts(), "db_enabled": True}


@router.patch("/api/admin/referral-codes/{code_id}")
def admin_update_referral_code(
    code_id: int, req: ReferralCodeActiveUpdate, request: Request,
    admin_user_id: str = Depends(deps.require_admin),
):
    """Activate/deactivate a referral code. Admin only."""
    if not runtime.USE_DB:
        raise HTTPException(status_code=503, detail="Database required")
    if not database.db_referral_code_set_active(code_id, req.active):
        raise HTTPException(status_code=404, detail="Code not found")
    deps.audit_log(
        "admin", "referral_code_updated", actor_id=admin_user_id,
        resource_type="referral_code", resource_id=str(code_id),
        details={"active": bool(req.active)}, request=request,
    )
    return {"success": True, "active": bool(req.active)}


@router.get("/api/admin/referral-commissions")
def admin_list_referral_commissions(_: str = Depends(deps.require_admin)):
    """List referral payout line items + totals. Admin only."""
    if not runtime.USE_DB:
        return {"commissions": [], "unpaid_total_cents": 0, "paid_total_cents": 0, "db_enabled": False}
    items = database.db_referral_commissions_list_all(include_paid=True)
    unpaid = sum(c["amount_cents"] for c in items if not c["paid"])
    paid = sum(c["amount_cents"] for c in items if c["paid"])
    return {
        "commissions": items,
        "unpaid_total_cents": unpaid,
        "paid_total_cents": paid,
        "db_enabled": True,
    }


@router.patch("/api/admin/referral-commissions/{commission_id}")
def admin_mark_referral_commission_paid(
    commission_id: int, req: CommissionPaidUpdate, request: Request,
    admin_user_id: str = Depends(deps.require_admin),
):
    """Mark a referral payout line item as paid (record-keeping only — sends no money)."""
    if not runtime.USE_DB:
        raise HTTPException(status_code=503, detail="Database required")
    if not req.paid:
        raise HTTPException(status_code=400, detail="Only marking paid is supported")
    if not database.db_referral_commission_mark_paid(commission_id):
        raise HTTPException(status_code=500, detail="Failed to mark paid")
    deps.audit_log(
        "admin", "referral_commission_paid", actor_id=admin_user_id,
        resource_type="referral_commission", resource_id=str(commission_id),
        details={}, request=request,
    )
    return {"success": True, "paid": True}


# --- System health / incidents ---

@router.post("/api/admin/test-alert")
def admin_send_test_alert(request: Request, admin_user_id: str = Depends(deps.require_admin)):
    """Send a test operator alert (email + SMS) so you can confirm alerting is wired up.
    Reports which channels actually delivered so missing config is obvious. Admin only."""
    import alerts
    import email_notify

    subject = "Test alert"
    msg = "This is a test operator alert from Call Surge. If you received this, alerting works."
    email_ok = False
    try:
        email_ok = email_notify.send_operator_alert(f"[Call Surge] {subject}", f"<p>{msg}</p>", msg)
    except Exception as e:
        print(f"[Admin] test-alert email failed: {e}")
    sms_ok = False
    try:
        sms_ok = alerts._send_alert_sms(f"[Call Surge] {msg}")
    except Exception as e:
        print(f"[Admin] test-alert sms failed: {e}")
    deps.audit_log(
        "admin", "test_alert_sent", actor_id=admin_user_id,
        details={"email_sent": email_ok, "sms_sent": sms_ok}, request=request,
    )
    return {
        "email_sent": email_ok,
        "sms_sent": sms_ok,
        "email_target_set": bool((os.getenv("OPERATOR_ALERT_EMAIL") or "").strip()),
        "sms_target_set": bool((os.getenv("OPERATOR_ALERT_SMS") or "").strip()),
    }


@router.get("/api/admin/failed-events")
def admin_list_failed_events(include_resolved: bool = False, _: str = Depends(deps.require_admin)):
    """List recorded failures (webhook/cron/task) for the System Health panel. Admin only."""
    if not runtime.USE_DB:
        return {"events": [], "unresolved_count": 0, "db_enabled": False}
    return {
        "events": database.db_failed_events_list(include_resolved=include_resolved),
        "unresolved_count": database.db_failed_events_unresolved_count(),
        "db_enabled": True,
    }


@router.patch("/api/admin/failed-events/{event_id}")
def admin_resolve_failed_event(
    event_id: int, req: FailedEventResolveUpdate, request: Request,
    admin_user_id: str = Depends(deps.require_admin),
):
    """Mark an incident resolved once you've handled it. Admin only."""
    if not runtime.USE_DB:
        raise HTTPException(status_code=503, detail="Database required")
    if not req.resolved:
        raise HTTPException(status_code=400, detail="Only resolving is supported")
    if not database.db_failed_event_resolve(event_id):
        raise HTTPException(status_code=500, detail="Failed to resolve")
    deps.audit_log(
        "admin", "failed_event_resolved", actor_id=admin_user_id,
        resource_type="failed_event", resource_id=str(event_id),
        details={}, request=request,
    )
    return {"success": True, "resolved": True}


@router.post("/api/admin/tenants/{tenant_id}/members")
def admin_add_tenant_member(
    tenant_id: str,
    req: AdminResendInviteRequest,
    request: Request,
    admin_user_id: str = Depends(deps.require_admin),
):
    """Link a Clerk user to a tenant by email (re-link existing account or send invite)."""
    if not runtime.USE_DB:
        raise HTTPException(status_code=503, detail="Database required")
    tenant = database.db_tenant_get_by_id(tenant_id)
    if not tenant:
        raise HTTPException(status_code=404, detail="Tenant not found")
    email = (req.email or "").strip()
    if not email or "@" not in email:
        raise HTTPException(status_code=400, detail="Valid email required")
    link = clerk_service._clerk_link_email_to_tenant(email, tenant_id)
    deps.audit_log(
        "admin",
        "tenant_member_add_attempt",
        actor_id=admin_user_id,
        resource_type="tenant",
        resource_id=tenant_id,
        client_id=tenant.get("client_id"),
        details={"email": email, **link},
        request=request,
    )
    return {"success": True, **link}


# ===== access-debug route (moved from main; uses _membership_diagnosis) =====

@router.get("/api/me/access")
def me_access(request: Request):
    """
    Debug helper for dashboard access issues: shows which Clerk user is signed in,
    which emails Clerk has on file, and whether a tenant membership exists in the DB.
    """
    token = deps.get_bearer_token(request)
    if not token:
        return {"signed_in": False}
    try:
        user_id, jwt_tid = deps.verify_clerk_token(token)
    except HTTPException:
        return {"signed_in": False, "token_invalid": True}
    deps._ensure_db_ready()
    tenant = database.db_tenant_get_for_user(user_id) if runtime.USE_DB else None
    link = deps._clerk_fetch_user_link(user_id) if runtime.USE_DB else None
    memberships = database.db_tenant_memberships_for_user(user_id) if runtime.USE_DB else []
    admin_ids = [
        x.strip()
        for x in (os.getenv("ADMIN_CLERK_USER_IDS") or "").split(",")
        if x.strip()
    ]
    primary_email = ((link or {}).get("emails") or [None])[0]
    pending_invite_tid = (
        database.db_tenant_invite_peek(primary_email) if runtime.USE_DB and primary_email else None
    )
    diagnosis = _membership_diagnosis(user_id, jwt_tid, link, tenant, memberships)
    return {
        "signed_in": True,
        "user_id": user_id,
        "is_admin": user_id in admin_ids,
        "jwt_metadata_tenant_id": jwt_tid,
        "clerk_api_tenant_id": (link or {}).get("tenant_id"),
        "clerk_emails": (link or {}).get("emails") or [],
        "db_tenant_client_id": (tenant or {}).get("client_id"),
        "db_tenant_id": (tenant or {}).get("id"),
        "db_tenant_name": (tenant or {}).get("name"),
        "has_tenant_membership": tenant is not None,
        "db_memberships": memberships,
        "pending_invite_for_primary_email": pending_invite_tid,
        "diagnosis": diagnosis,
    }


class AdminOrgOwnerSet(BaseModel):
    clerk_user_id: str


@router.post("/api/admin/orgs/{org_id}/owner")
def admin_set_org_owner(
    org_id: str, req: AdminOrgOwnerSet, request: Request, admin: str = Depends(deps.require_admin)
):
    """Make someone the group's owner, demoting whoever holds it now.

    This is the recovery path when a group cannot fix itself: the owner has left the
    company, lost the account, or never existed because the group predates owners.
    Customers cannot do this to each other — an owner hands over voluntarily via
    /api/org/members/{id}/transfer-ownership, and a manager cannot promote anyone.
    Overriding that has to be a deliberate, audited act by us.

    Demotion of the incumbent is the point, not a side effect: leaving two owners
    would break the single-owner rule that every other guard here relies on.
    """
    if not runtime.USE_DB:
        raise HTTPException(status_code=503, detail="Database required")
    if not database.db_org_get_by_id(org_id):
        raise HTTPException(status_code=404, detail="Org not found")
    uid = (req.clerk_user_id or "").strip()
    if not uid:
        raise HTTPException(status_code=400, detail="clerk_user_id is required")
    if database.db_org_member_role(uid, org_id) is None:
        raise HTTPException(
            status_code=404,
            detail="They do not oversee this group yet. Add them as a manager first.",
        )
    members = database.db_org_members(org_id)
    current = next((m["clerk_user_id"] for m in members if (m.get("role") or "") == "owner"), None)
    if current == uid:
        return {"ok": True, "org_id": org_id, "owner": uid, "changed": False}
    if current:
        if not database.db_org_transfer_ownership(org_id, current, uid):
            raise HTTPException(status_code=500, detail="Could not transfer ownership")
    else:
        # No owner at all — a group created before owners existed, or one whose
        # backfill found no whole-group manager to promote.
        if not database.db_org_member_add(uid, org_id, "owner"):
            raise HTTPException(status_code=500, detail="Could not set the owner")
    deps.audit_log(
        "admin", "org_owner_reassigned", actor_id=admin, resource_type="org",
        resource_id=org_id,
        details={"new_owner": uid, "previous_owner": current, "had_owner": bool(current)},
        request=request,
    )
    return {"ok": True, "org_id": org_id, "owner": uid, "previous_owner": current, "changed": True}
