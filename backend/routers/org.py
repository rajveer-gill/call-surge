"""Multi-store oversight API — one login watching several stores.

A franchise or multi-shop owner gets an org containing their stores and an
org_members row saying they may oversee it. These routes answer "who am I" and
"how are my stores doing"; drilling into a single store reuses every existing
dashboard endpoint, with the store named by the X-Store-Id header (see
deps.require_tenant).

Auth here is require_user, NOT require_tenant. An overseer generally owns no store
themselves, so they have no tenant_members row and require_tenant would 403 them.
"""

from __future__ import annotations

import logging
import re
from typing import Optional
from uuid import uuid4

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, EmailStr, Field

import clerk_service
import config_service
import database
import deps
import runtime
from subscription_access import evaluate_billing, get_tenant_subscription_state

logger = logging.getLogger("nuvatra")
router = APIRouter()


def _require_org_manager(user_id: str, org_id: Optional[str] = None) -> dict:
    """The caller must manage an org before they can change anything in it.

    A viewer oversees stores read-only; only a manager provisions. When org_id is
    omitted and they manage exactly one org, that's the one — the common case, since
    a regional manager has a single group.
    """
    # org_wide: a manager invited to a single store is an org member, but adding or
    # changing stores across the group is not theirs to do.
    memberships = [
        m
        for m in database.db_org_memberships_org_wide(user_id)
        if database.org_role_at_least(m.get("role"), "manager")
    ]
    if not memberships:
        raise HTTPException(
            status_code=403, detail="Your account cannot add or change stores in this group."
        )
    if org_id:
        for m in memberships:
            if m["org_id"] == str(org_id):
                return m
        raise HTTPException(status_code=403, detail="You do not manage that group.")
    if len(memberships) > 1:
        raise HTTPException(
            status_code=400, detail="You manage several groups — specify which one."
        )
    return memberships[0]


def _store_setup_state(store: dict) -> dict:
    """What a store still needs before it can answer calls.

    Three gates, in the order a manager hits them:
      1. an AI line to forward to (provisioned automatically once the group pays)
      2. the receptionist's own setup — team, services, a way to reach a human
      3. call forwarding actually switched on at the carrier

    Forwarding is *confirmed*, not merely claimed: config_service stamps
    forwarding_verified_at the first time a real forwarded call arrives carrying
    Twilio's ForwardedFrom. Until that happens we say "waiting for the first call"
    rather than pretending it's done.
    """
    cid = (store.get("client_id") or "").strip()
    has_number = bool((store.get("twilio_phone_number") or "").strip())
    try:
        cfg = config_service._read_raw_client_config(cid) or {}
    except Exception:
        cfg = {}
    info = config_service._config_data_to_business_info(cfg) if cfg else {}
    byon = (cfg.get("number_mode") or "new") == "existing"
    verified = bool((cfg.get("forwarding_verified_at") or "").strip())
    ready = bool(info) and config_service.voice_receptionist_ready(info)
    if store.get("demo_mode"):
        # A demo is a preview, not a half-built store. Saying "we're setting up your
        # phone line" would be a lie — it gets one when they activate, not before.
        step = "demo"
    elif not has_number:
        step = "needs_number"
    elif not ready:
        step = "needs_setup"          # team roster / services / human handoff
    elif byon and not verified:
        step = "needs_forwarding"     # the carrier switch is the last mile
    else:
        step = "live"
    return {
        "setup_step": step,
        "has_number": has_number,
        "receptionist_ready": ready,
        "forwarding_required": byon,
        "forwarding_verified": verified,
        "existing_business_number": (cfg.get("existing_business_number") or "").strip(),
        # Free — cfg is already in hand. Blank when it matches the record name.
        "public_name": (cfg.get("public_name") or "").strip(),
    }


def _consume_pending_org_invites(user_id: str) -> list:
    """Materialize any org invites waiting on this user's verified emails.

    Reuses the Clerk user lookup deps already uses for tenant-invite consumption, so
    the email list is authoritative (a user can only claim an invite for an address
    Clerk has verified as theirs). Best-effort — a Clerk hiccup just means they'll be
    picked up on the next load. Returns the orgs joined.
    """
    try:
        link = deps._clerk_fetch_user_link(user_id)
        emails = (link or {}).get("emails") or []
        if not emails:
            return []
        return database.db_org_invites_consume_for_emails(user_id, emails)
    except Exception as e:
        logger.warning("org_invite_consume_failed user=%s err=%s", user_id, type(e).__name__)
        return []


def _unique_client_id(name: str) -> str:
    """Slugify a store name into a stable, unique client_id. Mirrors the self-serve
    signup slug so org-created stores look identical to any other tenant."""
    base = re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-")[:40] or "store"
    candidate, n = base, 2
    while database.db_tenant_get_by_client_id(candidate) is not None:
        candidate = f"{base}-{n}"
        n += 1
        if n > 200:
            candidate = f"{base}-{uuid4().hex[:6]}"
            break
    return candidate


@router.get("/api/org/me")
def get_org_me(user_id: str = Depends(deps.require_user)):
    """Whether this user oversees any stores, and at what role.

    The dashboard calls this to decide whether to show the store switcher. A normal
    store owner gets is_org_member=false and nothing changes for them.
    """
    if not runtime.USE_DB:
        return {"is_org_member": False, "orgs": [], "store_count": 0}
    orgs = database.db_org_memberships(user_id)
    if not orgs:
        # Not a member yet — this may be an invited user's first sign-in. Consuming
        # pending invites here (rather than on every request) means the one Clerk
        # email lookup happens only for users who aren't members of anything: an
        # invited overseer pays it once, then this branch is skipped forever after.
        if _consume_pending_org_invites(user_id):
            orgs = database.db_org_memberships(user_id)
        if not orgs:
            logger.info(
                "org_me user=%s memberships=none (no rows, no pending invite matched)"
                " -> is_org_member=False",
                (user_id or "")[:14],
            )
            return {"is_org_member": False, "orgs": [], "store_count": 0}
    # A manager invited to one store is an org member in the database, but they are
    # not an overseer: no switcher, no rollup, no group billing. They see that single
    # store and it resolves like any other dashboard, so report them as a normal owner.
    scoped_only = [o for o in orgs if o.get("tenant_id")]
    orgs = [o for o in orgs if not o.get("tenant_id")]
    if not orgs:
        # This decides whether someone is shown their group or asked to create a
        # business, and the answer is invisible from the outside. Log which of the
        # two "no" cases it is: no rows at all (wrong account, invite never applied)
        # versus rows that are all store-scoped (invited to a store, not the group).
        logger.info(
            "org_me user=%s memberships=0_org_wide store_scoped=%s -> is_org_member=False",
            (user_id or "")[:14], len(scoped_only),
        )
        return {"is_org_member": False, "orgs": [], "store_count": 0}
    stores = database.db_org_stores_for_user(user_id)
    # Attach billing state per org so the UI can prompt a manager to set up payment
    # before their stores can take calls. Only a manager needs (or is shown) this.
    for o in orgs:
        billing = database.db_org_get_by_id(o["org_id"]) or {}
        o["subscription_status"] = billing.get("subscription_status")
        o["billing_active"] = evaluate_billing(billing)["active"] if billing else False
        o["store_count"] = database.db_org_store_count(o["org_id"])
    logger.info(
        "org_me user=%s org_wide=%s -> is_org_member=True", (user_id or "")[:14], len(orgs)
    )
    return {
        "is_org_member": True,
        "orgs": orgs,
        "store_count": len(stores),
        # Convenience for the UI: the weakest role wins for hiding write controls.
        # Rank, not equality: an owner outranks a manager and must not read as
        # less. This hid the "Your stores" link from the head account.
        "can_edit_any": any(database.org_role_at_least(o.get("role"), "manager") for o in orgs),
    }


@router.get("/api/org/stores")
def get_org_stores(
    user_id: str = Depends(deps.require_user),
    days: int = Query(7, ge=1, le=90),
):
    """Every store this user oversees, with headline numbers for the last N days.

    This is the rollup: one row per shop, so a regional manager can see at a glance
    which store is missing calls. Metrics are aggregated across all stores in three
    queries rather than per-store loops.
    """
    if not runtime.USE_DB:
        raise HTTPException(status_code=503, detail="Database required")
    stores = database.db_org_stores_for_user(user_id)
    if not stores:
        return {"stores": [], "totals": {}, "days": days}
    metrics = database.db_org_store_metrics([s["client_id"] for s in stores], days=days)
    out = []
    for s in stores:
        cid = s["client_id"]
        m = metrics.get(cid) or {}
        state = get_tenant_subscription_state(s)
        setup = _store_setup_state(s)
        out.append(
            {
                "client_id": cid,
                "tenant_id": s.get("id"),
                "name": s.get("name"),
                "public_name": setup.get("public_name") or "",
                "org_id": s.get("org_id"),
                "org_name": s.get("org_name"),
                "role": s.get("org_role"),
                "phone": s.get("twilio_phone_number"),
                "plan": state.get("plan"),
                # Surfaced so a lapsed store is obvious in the list rather than only
                # discovered when someone drills in and finds it dead.
                "can_use_app": state.get("can_use_app"),
                "subscription_status": state.get("subscription_status"),
                "demo_mode": state.get("demo_mode"),
                # Self-serve setup checklist: what this store still needs before it can
                # answer a call. Lets a manager work through their locations without
                # anyone hand-holding them.
                **setup,
                "calls": m.get("calls", 0),
                "missed": m.get("missed", 0),
                "answered": m.get("answered", 0),
                "bookings": m.get("bookings", 0),
                "upcoming": m.get("upcoming", 0),
                "unread_messages": m.get("unread_messages", 0),
                "prev_calls": m.get("prev_calls", 0),
                "prev_bookings": m.get("prev_bookings", 0),
            }
        )
    totals = {
        k: sum(int(s.get(k) or 0) for s in out)
        for k in (
            "calls", "missed", "answered", "bookings", "upcoming",
            "unread_messages", "prev_calls", "prev_bookings",
        )
    }
    totals["stores"] = len(out)
    # Stores that can't take a call yet — the owner's actual to-do list. Demos are
    # excluded: nothing is outstanding on them until the owner chooses to activate.
    totals["needs_attention"] = sum(
        1 for s in out if s.get("setup_step") not in (None, "live", "demo")
    )
    totals["inactive"] = sum(1 for s in out if not s.get("can_use_app"))
    # Percentage change vs the immediately preceding window of the same length.
    totals["calls_change_pct"] = _pct_change(totals["calls"], totals["prev_calls"])
    totals["bookings_change_pct"] = _pct_change(totals["bookings"], totals["prev_bookings"])
    return {"stores": out, "totals": totals, "days": days}


def _pct_change(now: int, before: int) -> Optional[int]:
    """Whole-percent change, or None when there's no prior period to compare against
    (showing "+100%" against a zero baseline would be noise, not signal)."""
    if not before:
        return None
    return round(((now - before) / before) * 100)


class CreateOrgStoreRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=120)
    org_id: Optional[str] = None
    business_vertical: str = "salon_chair"
    # "new" = we give this store a number to publish; "existing" = it keeps its own
    # published number and forwards to the (hidden) AI line.
    number_mode: str = Field(default="new")
    existing_number: Optional[str] = None
    # Optional: invite the store's manager in the same step.
    manager_email: Optional[EmailStr] = None


@router.post("/api/org/stores")
def create_org_store(
    req: CreateOrgStoreRequest, request: Request, user_id: str = Depends(deps.require_user)
):
    """Add a store to the group you manage.

    This exists because the self-serve signup path cannot do it: it makes the creator
    a tenant_member, and one-user-one-tenant means her second store would silently
    hand back her first (business.py's already_existed guard). Here she is never a
    member of the stores she creates — her access comes from the org — so she can
    create as many as she likes without touching her own account.

    The store is created with no number; the number arrives when the group's
    subscription covers it. Access comes from the org's subscription, so the store
    never needs a card of its own.
    """
    if not runtime.USE_DB:
        raise HTTPException(status_code=503, detail="Database required")
    membership = _require_org_manager(user_id, req.org_id)
    org_id = membership["org_id"]
    bv = (req.business_vertical or "salon_chair").strip()
    if bv not in config_service.ALLOWED_BUSINESS_VERTICALS:
        raise HTTPException(status_code=400, detail="Invalid business type")
    if req.number_mode not in ("new", "existing"):
        raise HTTPException(status_code=400, detail="Invalid number mode")
    existing_number = ""
    if req.number_mode == "existing":
        digits = re.sub(r"\D", "", req.existing_number or "")
        if len(digits) < 10:
            raise HTTPException(
                status_code=400,
                detail="Enter this store's current phone number to forward calls from.",
            )
        existing_number = f"+1{digits}" if len(digits) == 10 else f"+{digits}"

    org = database.db_org_get_by_id(org_id)
    if not org:
        raise HTTPException(status_code=404, detail="Group not found")
    name = req.name.strip()
    client_id = _unique_client_id(name)
    # plan mirrors the org's, because plans.get_plan_limits reads the tenant's own
    # plan — a store in a Pro group must literally carry plan='pro'.
    tenant = database.db_tenant_create_pending(client_id, name, org.get("plan") or "pro", bv)
    if not tenant:
        raise HTTPException(status_code=409, detail="Could not create store; please try again")
    if not database.db_org_attach_tenant(tenant["id"], org_id):
        # An orphaned store would be invisible to her and billed to nobody.
        database.db_tenant_delete(tenant["id"])
        raise HTTPException(status_code=500, detail="Could not add the store to your group")
    database.set_request_client_id(client_id)
    cfg = config_service._default_client_config_data(client_id, org.get("plan") or "pro")
    cfg["business_name"] = name
    cfg["name"] = name
    cfg["number_mode"] = req.number_mode
    if req.number_mode == "existing":
        cfg["existing_business_number"] = existing_number
    config_service.save_raw_client_config(client_id, cfg)

    # The group is billed per store, so a new store must move the quantity. Imported
    # here rather than at module scope to keep org <-> billing from importing circularly.
    quantity, provisioned = {}, {}
    try:
        from routers import billing as billing_router

        quantity = billing_router.sync_org_subscription_quantity(org_id)
        # Give the new store its AI line straight away when the group is already
        # paying, so the manager can set up forwarding without waiting on anyone.
        # No-op for an unpaid group — the numbers arrive when they check out.
        provisioned = billing_router.provision_missing_org_store_numbers(org_id, request)
    except Exception as e:
        logger.error("org_store_post_create_failed org=%s err=%s", org_id, e)

    invite: dict = {}
    if req.manager_email:
        # Scoped to this store, and additive — see invite_store_manager for why this
        # is an org membership rather than a tenant_members row.
        invite = clerk_service._clerk_invite_email_to_org(
            str(req.manager_email), org_id, role="manager", tenant_id=str(tenant["id"])
        )
    deps.audit_log(
        "user",
        "org_store_created",
        actor_id=user_id,
        resource_type="tenant",
        resource_id=tenant["id"],
        client_id=client_id,
        details={"org_id": org_id, "name": name, "invited": bool(req.manager_email)},
        request=request,
    )
    # Re-read AFTER provisioning so the response carries the new number.
    fresh = database.db_tenant_get_by_id(tenant["id"]) or tenant
    return {
        "store": fresh,
        "invite_sent": bool(invite.get("invite_sent")),
        "clerk_error": invite.get("clerk_error"),
        "store_count": database.db_org_store_count(org_id),
        # False when the group has no subscription yet (they pay once, after adding
        # stores) or when Stripe rejected the change — the UI should say which.
        "billing_synced": bool(quantity.get("synced")),
        # True once the store has its own AI line to forward calls to.
        "number_provisioned": bool((fresh or {}).get("twilio_phone_number")),
        "provisioning": provisioned,
    }


class InviteStoreManagerRequest(BaseModel):
    email: EmailStr


@router.post("/api/org/stores/{store_ref}/invite")
def invite_store_manager(
    store_ref: str,
    req: InviteStoreManagerRequest,
    request: Request,
    user_id: str = Depends(deps.require_user),
):
    """Invite the person who runs this store.

    They get an org membership scoped to this one store: no rollup, no switcher, no
    group billing, and no access to the group's other stores. require_tenant's
    single-store fallback lands them in it.

    Deliberately NOT a tenant_members row. That path runs through
    db_tenant_member_assign_owner ("make this user the sole owner"), which deletes
    every other member of the store AND every other membership the invitee had — so
    inviting someone who already had an account silently took a store away from them.
    Making it non-destructive at the invite site alone doesn't hold either, because
    db_tenant_get_for_user collapses multi-membership on read. Org membership is
    exempt from all of that by design, which is why it's the right home for this.
    """
    if not runtime.USE_DB:
        raise HTTPException(status_code=503, detail="Database required")
    # Resolve the store through org membership — same join that guards every other
    # org read, so she cannot invite someone into a store she doesn't manage.
    scoped = database.db_org_store_for_user(user_id, store_ref)
    if not scoped:
        raise HTTPException(status_code=403, detail="You do not have access to that store.")
    if not database.org_role_at_least(scoped.get("role"), "manager"):
        raise HTTPException(
            status_code=403, detail="Your account can view this store but not change it."
        )
    tenant = scoped["tenant"]
    org_of_store = str(tenant.get("org_id") or "")
    if not org_of_store:
        # Every store reached through this endpoint belongs to an org by construction;
        # without one there is nothing to scope the membership to.
        raise HTTPException(status_code=409, detail="That store is not part of a group.")
    # org_members is PRIMARY KEY (clerk_user_id, org_id): one row per person per
    # group. Inviting someone who already manages a DIFFERENT store in this group
    # would upsert that row and move them — they would lose the other store, with no
    # error and nothing on screen to say so. Refuse instead, and name the store they
    # are already on so the person inviting can decide.
    existing_uid = clerk_service.clerk_user_id_for_email(str(req.email))
    if existing_uid:
        prior = database.db_org_member_scope(existing_uid, org_of_store)
        prior_tenant = (prior or {}).get("tenant_id")
        if prior and prior_tenant and str(prior_tenant) != str(tenant["id"]):
            other = database.db_tenant_get_by_id(str(prior_tenant)) or {}
            raise HTTPException(
                status_code=409,
                detail=(
                    "That person already manages "
                    + (other.get("name") or "another store")
                    + " in this group, and an account can manage only one store. "
                    "Remove them from that store first, or give them group access instead."
                ),
            )
    link = clerk_service._clerk_invite_email_to_org(
        str(req.email), org_of_store, role="manager", tenant_id=str(tenant["id"])
    )
    deps.audit_log(
        "user",
        "org_store_manager_invited",
        actor_id=user_id,
        resource_type="tenant",
        resource_id=tenant["id"],
        client_id=tenant.get("client_id"),
        details={"email": str(req.email), **{k: v for k, v in link.items() if k != "email"}},
        request=request,
    )
    # user_added is a success with no email: they already had an account and now have
    # the store. Only a genuine failure to reach anyone is a 502.
    if link.get("clerk_error") and not link.get("invite_sent") and not link.get("user_added"):
        # The pending invite row is still stored, so they'd be linked if they sign up
        # anyway — but say so plainly rather than reporting a success that isn't one.
        raise HTTPException(status_code=502, detail=str(link.get("clerk_error")))
    return {
        "ok": True,
        "invite_sent": bool(link.get("invite_sent")),
        "user_added": bool(link.get("user_added")),
    }


# ---------------------------------------------------------------------------
# Group membership — who oversees the whole group, and at what level
# ---------------------------------------------------------------------------
# Until now only a Nuvatra admin could add someone at group level. A franchise with
# a head office and two regional managers had to email us to change their own team,
# which does not scale past the pilot.
#
# owner   — the head account. May do anything here, including changing owners.
# manager — may run the group and manage viewers and other managers, but may not
#           remove an owner, demote an owner, or create one. That last restriction
#           was not in the original ask: without it a manager can mint an owner they
#           control and then outrank everyone, which makes "cannot remove the owner"
#           decorative.
# viewer  — read-only, including here.
#
# The last owner can never be removed or demoted, by anyone, including themselves.
# Otherwise a group locks itself out of its own account and only we can fix it.


class OrgMemberInvite(BaseModel):
    email: EmailStr
    role: str = Field(default="manager")
    org_id: Optional[str] = None


class OrgMemberRoleUpdate(BaseModel):
    role: str
    org_id: Optional[str] = None


def _resolve_org_for_user(user_id: str, org_id: Optional[str]) -> str:
    """Which group this request is about, and proof the caller belongs to it.

    Most customers oversee exactly one group, so org_id is optional and inferred.
    Someone who oversees several must name one — guessing would be a way to act on
    the wrong company's account.
    """
    memberships = database.db_org_memberships_org_wide(user_id)
    if not memberships:
        raise HTTPException(status_code=403, detail="You do not oversee a group.")
    if org_id:
        wanted = str(org_id).strip()
        for m in memberships:
            if str(m.get("org_id")) == wanted:
                return wanted
        raise HTTPException(status_code=403, detail="You do not oversee that group.")
    if len(memberships) > 1:
        raise HTTPException(
            status_code=400,
            detail="You oversee more than one group — say which with org_id.",
        )
    return str(memberships[0]["org_id"])


def _actor_role(user_id: str, org_id: str, minimum: str, request: Request) -> str:
    role = database.db_org_member_role(user_id, org_id)
    if not database.org_role_at_least(role, minimum):
        deps.audit_log(
            "user", "auth_failure", actor_id=user_id, resource_type="org",
            resource_id=org_id,
            details={"reason": "org_role_insufficient", "have": role, "need": minimum},
            request=request,
        )
        raise HTTPException(status_code=403, detail="This needs " + minimum + " access to the group.")
    return (role or "").strip().lower()


def _guard_target(
    actor_role: str,
    target_role: Optional[str],
    org_id: str,
    granting: Optional[str] = None,
) -> None:
    """Refuse changes that would escalate the actor or orphan the group."""
    target = (target_role or "").strip().lower()
    if actor_role != "owner":
        if target == "owner":
            raise HTTPException(status_code=403, detail="Only an owner can change an owner.")
        if granting == "owner":
            raise HTTPException(status_code=403, detail="Only an owner can make someone an owner.")
    if target == "owner" and granting != "owner":
        # Removing an owner, or demoting one. Fine unless they are the last.
        if database.db_org_owner_count(org_id) <= 1:
            raise HTTPException(
                status_code=409,
                detail="This is the only owner of the group. Make someone else an owner first.",
            )


@router.get("/api/org/members")
def list_org_members(
    user_id: str = Depends(deps.require_user),
    org_id: Optional[str] = Query(None),
):
    """Everyone who oversees the group, plus invites not yet accepted."""
    if not runtime.USE_DB:
        raise HTTPException(status_code=503, detail="Database required")
    oid = _resolve_org_for_user(user_id, org_id)
    me = database.db_org_member_role(user_id, oid)
    members = database.db_org_members(oid)
    # Resolve each Clerk id to the email that person signs in with. A team screen
    # listing user_3A6L7yXuDUCH... is not a team screen — nobody can tell which
    # colleague to remove. Best-effort per row: if Clerk is unreachable the id is
    # still shown, which is worse to read but never wrong.
    for m in members:
        m["email"] = None
        m["is_you"] = m.get("clerk_user_id") == user_id
        try:
            link = deps._clerk_fetch_user_link(m.get("clerk_user_id") or "")
            emails = (link or {}).get("emails") or []
            if emails:
                m["email"] = str(emails[0]).strip()
        except Exception as e:
            logger.warning(
                "org_member_email_lookup_failed org=%s err=%s: %s", oid, type(e).__name__, e
            )
    return {
        "org_id": oid,
        "your_role": me,
        "members": members,
        "pending_invites": database.db_org_invites_for_org(oid),
        # So the UI can hide controls that would only earn a 403.
        "can_manage": database.org_role_at_least(me, "manager"),
        "can_manage_owners": database.org_role_at_least(me, "owner"),
    }


@router.post("/api/org/members")
def invite_org_member(
    req: OrgMemberInvite, request: Request, user_id: str = Depends(deps.require_user)
):
    """Invite someone to oversee the whole group."""
    if not runtime.USE_DB:
        raise HTTPException(status_code=503, detail="Database required")
    oid = _resolve_org_for_user(user_id, req.org_id)
    actor = _actor_role(user_id, oid, "manager", request)
    role = (req.role or "manager").strip().lower()
    if role not in database.ORG_ROLES:
        raise HTTPException(status_code=400, detail="Unknown role.")
    if role == "owner":  # role-value-check: is the REQUESTED role owner, not a rank
        # Inviting straight to owner would hand the business to an address that has
        # not accepted anything yet — and would leave two owners if it were honoured.
        raise HTTPException(
            status_code=400,
            detail="Invite them as a manager, then transfer ownership once they have signed in.",
        )
    _guard_target(actor, None, oid, granting=role)
    link = clerk_service._clerk_invite_email_to_org(str(req.email), oid, role)
    deps.audit_log(
        "user", "org_member_invited", actor_id=user_id, resource_type="org",
        resource_id=oid,
        details={"email": str(req.email), "role": role, "by_role": actor,
                 "user_added": bool(link.get("user_added")),
                 "invite_sent": bool(link.get("invite_sent"))},
        request=request,
    )
    if link.get("clerk_error") and not link.get("invite_sent") \
            and not link.get("user_added") and not link.get("pending_invite_stored"):
        raise HTTPException(status_code=502, detail=str(link.get("clerk_error")))
    return {
        "ok": True,
        "added": bool(link.get("user_added")),
        "invite_sent": bool(link.get("invite_sent")),
        "pending": bool(link.get("pending_invite_stored")) and not link.get("user_added"),
        "role": role,
        "org_id": oid,
    }


@router.patch("/api/org/members/{clerk_user_id}")
def update_org_member_role(
    clerk_user_id: str,
    req: OrgMemberRoleUpdate,
    request: Request,
    user_id: str = Depends(deps.require_user),
):
    """Change what someone may do in the group."""
    if not runtime.USE_DB:
        raise HTTPException(status_code=503, detail="Database required")
    oid = _resolve_org_for_user(user_id, req.org_id)
    actor = _actor_role(user_id, oid, "manager", request)
    role = (req.role or "").strip().lower()
    if role not in database.ORG_ROLES:
        raise HTTPException(status_code=400, detail="Unknown role.")
    target_role = database.db_org_member_role(clerk_user_id, oid)
    if target_role is None:
        raise HTTPException(status_code=404, detail="That person does not oversee this group.")
    if role == "owner":  # role-value-check: is the REQUESTED role owner, not a rank
        # A group has exactly one owner. Promoting a second would leave two, and
        # "the owner" stops meaning anything. Ownership moves by transfer, which
        # demotes the incumbent in the same transaction.
        raise HTTPException(
            status_code=400,
            detail="A group has one owner. Use transfer-ownership to hand it over.",
        )
    _guard_target(actor, target_role, oid, granting=role)
    if not database.db_org_member_add(clerk_user_id, oid, role):
        raise HTTPException(status_code=500, detail="Could not change that role.")
    deps.audit_log(
        "user", "org_member_role_changed", actor_id=user_id, resource_type="org",
        resource_id=oid,
        details={"clerk_user_id": clerk_user_id, "from": target_role, "to": role,
                 "by_role": actor},
        request=request,
    )
    return {"ok": True, "clerk_user_id": clerk_user_id, "role": role, "org_id": oid}


@router.delete("/api/org/members/{clerk_user_id}")
def remove_org_member(
    clerk_user_id: str,
    request: Request,
    user_id: str = Depends(deps.require_user),
    org_id: Optional[str] = Query(None),
):
    """Revoke someone's oversight of the group."""
    if not runtime.USE_DB:
        raise HTTPException(status_code=503, detail="Database required")
    oid = _resolve_org_for_user(user_id, org_id)
    actor = _actor_role(user_id, oid, "manager", request)
    target_role = database.db_org_member_role(clerk_user_id, oid)
    if target_role is None:
        raise HTTPException(status_code=404, detail="That person does not oversee this group.")
    _guard_target(actor, target_role, oid)
    ok = database.db_org_member_remove(clerk_user_id, oid)
    deps.audit_log(
        "user", "org_member_removed", actor_id=user_id, resource_type="org",
        resource_id=oid,
        details={"clerk_user_id": clerk_user_id, "was": target_role, "by_role": actor,
                 "ok": ok},
        request=request,
    )
    return {"ok": ok, "clerk_user_id": clerk_user_id, "org_id": oid}


class OrgOwnershipTransfer(BaseModel):
    org_id: Optional[str] = None


@router.post("/api/org/members/{clerk_user_id}/transfer-ownership")
def transfer_org_ownership(
    clerk_user_id: str,
    req: OrgOwnershipTransfer,
    request: Request,
    user_id: str = Depends(deps.require_user),
):
    """Hand the head account to another group member.

    The caller stops being owner in the same transaction the target becomes one, so
    the group is never briefly ownerless and never briefly has two.
    """
    if not runtime.USE_DB:
        raise HTTPException(status_code=503, detail="Database required")
    oid = _resolve_org_for_user(user_id, req.org_id)
    _actor_role(user_id, oid, "owner", request)
    if (clerk_user_id or "").strip() == user_id:
        raise HTTPException(status_code=400, detail="You are already the owner.")
    target_role = database.db_org_member_role(clerk_user_id, oid)
    if target_role is None:
        raise HTTPException(
            status_code=404,
            detail="They need to be in the group first. Invite them, then transfer once they have signed in.",
        )
    if not database.db_org_transfer_ownership(oid, user_id, clerk_user_id):
        raise HTTPException(status_code=500, detail="Could not transfer ownership.")
    deps.audit_log(
        "user", "org_ownership_transferred", actor_id=user_id, resource_type="org",
        resource_id=oid,
        details={"to": clerk_user_id, "their_previous_role": target_role},
        request=request,
    )
    # "manager" is the stored value; "Group admin" is what the person reading this
    # sees everywhere else. Returning the internal name invited the UI to print it.
    return {
        "ok": True,
        "org_id": oid,
        "owner": clerk_user_id,
        "you_are_now": "manager",
        "you_are_now_label": "Group admin",
    }


@router.delete("/api/org/invites")
def revoke_org_invite(
    request: Request,
    email: str = Query(...),
    user_id: str = Depends(deps.require_user),
    org_id: Optional[str] = Query(None),
):
    """Cancel an invitation that has not been accepted yet.

    Invite existed with no way to un-invite, which matters more than it sounds: a
    pending invite is a standing offer of access to every store in the group, and
    the only way to withdraw it was to ask us. It also blocks re-inviting the same
    address after a bad send.

    No owner guard needed — an invite can never carry the owner role, so revoking
    one can never remove the group's owner.
    """
    if not runtime.USE_DB:
        raise HTTPException(status_code=503, detail="Database required")
    oid = _resolve_org_for_user(user_id, org_id)
    actor = _actor_role(user_id, oid, "manager", request)
    ok = database.db_org_invite_delete(email, oid)
    # Also kill the invitation on Clerk's side. Deleting only our row left the
    # emailed link working as a way in after access was withdrawn, and made the
    # address un-invitable afterwards because Clerk still held a pending invite.
    revoked = clerk_service.revoke_clerk_invitations(email)
    deps.audit_log(
        "user", "org_invite_revoked", actor_id=user_id, resource_type="org",
        resource_id=oid,
        details={"email": email, "by_role": actor, "ok": ok,
                 "clerk_revoked": revoked.get("revoked"),
                 "clerk_error": revoked.get("error")},
        request=request,
    )
    if not ok:
        raise HTTPException(status_code=404, detail="No pending invite for that address.")
    return {
        "ok": True,
        "email": email,
        "org_id": oid,
        # Surfaced so the UI can say the emailed link may still work rather than
        # implying the invitation is definitively dead.
        "link_revoked": bool(revoked.get("revoked")) and not revoked.get("error"),
        "clerk_error": revoked.get("error"),
    }


@router.get("/api/org/stores/{store_ref}/managers")
def list_store_managers(
    store_ref: str,
    user_id: str = Depends(deps.require_user),
):
    """Who runs this one store.

    Inviting a store manager shipped with no way to see who had been invited or to
    take them off again. A standing invitation to a store is access to that store's
    calls, messages and customers, and it could only be withdrawn by asking us.
    """
    if not runtime.USE_DB:
        raise HTTPException(status_code=503, detail="Database required")
    scoped = database.db_org_store_for_user(user_id, store_ref)
    if not scoped:
        raise HTTPException(status_code=403, detail="You do not have access to that store.")
    tenant = scoped["tenant"]
    org_of_store = str(tenant.get("org_id") or "")
    if not org_of_store:
        return {"managers": [], "pending_invites": [], "can_manage": False}
    managers = database.db_store_managers(org_of_store, str(tenant["id"]))
    for m in managers:
        m["email"] = None
        m["is_you"] = m.get("clerk_user_id") == user_id
        try:
            link = deps._clerk_fetch_user_link(m.get("clerk_user_id") or "")
            emails = (link or {}).get("emails") or []
            if emails:
                m["email"] = str(emails[0]).strip()
        except Exception as e:
            logger.warning(
                "store_manager_email_lookup_failed store=%s err=%s: %s",
                tenant.get("client_id"), type(e).__name__, e,
            )
    pending = [
        inv
        for inv in database.db_org_invites_for_org(org_of_store)
        if str(inv.get("tenant_id") or "") == str(tenant["id"])
    ]
    return {
        "managers": managers,
        "pending_invites": pending,
        "can_manage": database.org_role_at_least(scoped.get("role"), "manager"),
    }


@router.delete("/api/org/stores/{store_ref}/managers/{clerk_user_id}")
def remove_store_manager(
    store_ref: str,
    clerk_user_id: str,
    request: Request,
    user_id: str = Depends(deps.require_user),
):
    """Take someone off this store."""
    if not runtime.USE_DB:
        raise HTTPException(status_code=503, detail="Database required")
    scoped = database.db_org_store_for_user(user_id, store_ref)
    if not scoped:
        raise HTTPException(status_code=403, detail="You do not have access to that store.")
    if not database.org_role_at_least(scoped.get("role"), "manager"):
        raise HTTPException(
            status_code=403, detail="Your account can view this store but not change it."
        )
    tenant = scoped["tenant"]
    org_of_store = str(tenant.get("org_id") or "")
    existing = database.db_org_member_scope(clerk_user_id, org_of_store)
    if not existing or str(existing.get("tenant_id") or "") != str(tenant["id"]):
        raise HTTPException(status_code=404, detail="They are not a manager of this store.")
    # Refuse to strip a whole-group person from here. Their row covers every store,
    # so deleting it from one store's screen would quietly revoke the entire group —
    # a much larger action than the button appears to offer.
    if existing.get("tenant_id") is None:
        raise HTTPException(
            status_code=409,
            detail="They oversee the whole group. Remove them from the group's Team instead.",
        )
    ok = database.db_org_member_remove(clerk_user_id, org_of_store)
    deps.audit_log(
        "user", "org_store_manager_removed", actor_id=user_id, resource_type="tenant",
        resource_id=tenant["id"],
        details={"clerk_user_id": clerk_user_id, "client_id": tenant.get("client_id"), "ok": ok},
        request=request,
    )
    if not ok:
        raise HTTPException(status_code=500, detail="Could not remove that manager.")
    return {"ok": True, "clerk_user_id": clerk_user_id}
