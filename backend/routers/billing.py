"""Billing: subscription state + Stripe checkout/portal/webhook."""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel

import database
import deps
import runtime
from observability import _stable_sha256
from security.webhooks import verify_stripe_event
from subscription_access import evaluate_billing, get_tenant_subscription_state

try:
    from plans import get_plan_limits
except ImportError:  # pragma: no cover
    get_plan_limits = None  # type: ignore

try:
    import stripe

    STRIPE_AVAILABLE = True
except ImportError:  # pragma: no cover
    stripe = None
    STRIPE_AVAILABLE = False

logger = logging.getLogger("nuvatra")
router = APIRouter()


@router.get("/api/subscription")
def get_subscription(tenant: Optional[dict] = Depends(deps.require_tenant)):
    """Return subscription state, plan limits, and usage for the current tenant."""
    state = get_tenant_subscription_state(tenant)
    if get_plan_limits:
        state["limits"] = get_plan_limits(tenant)
    # Use the tenant's client_id directly — a contextvar set inside the sync
    # require_tenant dependency does not survive into this sync endpoint, so
    # database._client_id() would fall back to "default" and show zero usage.
    cid = ((tenant or {}).get("client_id") or "").strip() or database._client_id()
    if runtime.USE_DB and cid and cid != "default":
        month = datetime.now(timezone.utc).strftime("%Y-%m")
        usage = database.db_usage_get(cid, month)
        state["usage"] = {
            "voice_minutes": usage.get("voice_minutes") or 0,
            "sms_count": usage.get("sms_count") or 0,
            "month": month,
        }
    else:
        state["usage"] = {
            "voice_minutes": 0,
            "sms_count": 0,
            "month": datetime.now(timezone.utc).strftime("%Y-%m"),
        }
    if deps._settings_load_debug_enabled():
        cid = (tenant or {}).get("client_id") if tenant else None
        prefix = (str(cid)[:10] + "…") if cid else "none"
        logger.info(
            "settings_load_debug GET /api/subscription client_id_prefix=%s keys=%s can_use_app=%s",
            prefix,
            sorted(state.keys()) if isinstance(state, dict) else type(state).__name__,
            (state.get("can_use_app") if isinstance(state, dict) else None),
        )
    return state


# ---------- Stripe billing ----------
def _stripe_price_id(plan: str) -> Optional[str]:
    key = f"STRIPE_{plan.upper()}_PRICE_ID"
    return (os.getenv(key) or os.getenv("STRIPE_PRICE_ID") or "").strip() or None


def _org_price_id(org: dict, plan: str) -> Optional[str]:
    """The price this group is billed at, honouring a partner rate.

    "$50 off each store" cannot be a coupon: amount_off comes off the invoice, and an
    org is one subscription with quantity = store count, so a fixed discount lands
    once whether that is 2 stores or 43. A percentage is per-store but changes value
    with the plan. A discounted PRICE is per-store by construction and stays $50
    across plans, because quantity multiplies the unit price.

    Falls back to the standard env price whenever an override is absent or malformed,
    so a bad value bills at list price rather than failing a checkout — and every
    customer without an override is untouched.
    """
    overrides = (org or {}).get("price_overrides") or {}
    if isinstance(overrides, dict):
        candidate = str(overrides.get((plan or "").strip().lower()) or "").strip()
        # Only accept something that looks like a Stripe price. A typo here would
        # otherwise reach Stripe as an opaque failure at the worst moment.
        if candidate.startswith("price_"):
            return candidate
        if candidate:
            logger.warning(
                "org_price_override_ignored org=%s plan=%s reason=not_a_price_id",
                (org or {}).get("id"), plan,
            )
    return _stripe_price_id(plan)


def _plain(obj):
    """A Stripe API response as plain nested dicts.

    The SDK's objects used to subclass dict, so `.get()` worked on anything an API
    call returned. Current versions (we build on 15) do not, and `.get()` raises
    AttributeError. requirements.txt asked for `stripe>=8.0.0` with no ceiling, so a
    routine rebuild moved us across that line and every `.get()` on a Stripe response
    started failing — quietly, because both call sites caught Exception and logged
    only the type name.

    Normalising at the boundary keeps the call sites readable and works on either
    side of the change, so this does not become a flag day if the pin moves again.
    """
    if isinstance(obj, dict):
        return {k: _plain(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_plain(v) for v in obj]
    to_dict = getattr(obj, "to_dict", None)
    if callable(to_dict):
        return _plain(to_dict())
    return obj


def build_partner_prices(amount_off_cents: int) -> dict:
    """Find or create prices that are `amount_off_cents` below each standard plan.

    The admin should be able to say "$50 off per store" and be done. Turning that into
    Stripe price IDs by hand means reading three prices, doing three subtractions and
    pasting three ids — three chances to paste a product where a price goes.

    Reuses an existing price with the same product, amount, currency and interval
    rather than creating a second identical one, because a duplicate price is
    invisible in the dashboard until you are looking at two of them.

    Returns {plan: price_id, ...} plus a "_errors" key naming any plan it could not
    price, so the caller can report a partial result instead of implying success.
    """
    out: dict = {}
    errors: list = []
    if not (STRIPE_AVAILABLE and stripe):
        return {"_errors": ["Stripe library not available"]}
    stripe.api_key = (os.getenv("STRIPE_SECRET_KEY") or "").strip()
    if not stripe.api_key:
        return {"_errors": ["STRIPE_SECRET_KEY is not set"]}
    for plan in ("starter", "growth", "pro"):
        base_id = _stripe_price_id(plan)
        if not base_id:
            continue  # plan not configured at all; nothing to discount
        try:
            base = _plain(stripe.Price.retrieve(base_id))
            unit = int(base.get("unit_amount") or 0) - amount_off_cents
            if unit <= 0:
                errors.append(f"{plan}: discount is not smaller than the price")
                continue
            currency = base.get("currency")
            interval = ((base.get("recurring") or {}).get("interval")) or "month"
            product = base.get("product")
            found = None
            listed = _plain(stripe.Price.list(product=product, active=True, limit=100))
            for p in listed.get("data", []):
                if (
                    int(p.get("unit_amount") or -1) == unit
                    and p.get("currency") == currency
                    and ((p.get("recurring") or {}).get("interval")) == interval
                ):
                    found = p.get("id")
                    break
            if not found:
                created = _plain(stripe.Price.create(
                    product=product,
                    unit_amount=unit,
                    currency=currency,
                    recurring={"interval": interval},
                    nickname=f"Partner rate — {amount_off_cents / 100:.0f} off {plan}",
                ))
                found = created.get("id")
                logger.info(
                    "partner_price_created plan=%s price=%s unit=%s", plan, found, unit
                )
            else:
                logger.info("partner_price_reused plan=%s price=%s unit=%s", plan, found, unit)
            out[plan] = found
        except Exception as e:
            logger.warning(
                "partner_price_failed plan=%s err=%s: %s", plan, type(e).__name__, e
            )
            errors.append(f"{plan}: {type(e).__name__}")
    if errors:
        out["_errors"] = errors
    return out


def _subscription_status_and_trial(sub_id: Optional[str]):
    """Read a Stripe subscription's real status + trial end so the tenant mirrors it.

    A self-serve signup starts a 7-day trial, so Stripe reports status 'trialing'
    with a trial_end. Recording that (instead of a hardcoded 'active') is what makes
    _is_trial_active true and unlocks full Pro-tier features during the trial.
    Falls back to ('active', None) if the subscription can't be read.
    """
    if not sub_id or not (STRIPE_AVAILABLE and stripe):
        return "active", None
    try:
        stripe.api_key = (os.getenv("STRIPE_SECRET_KEY") or "").strip()
        sub = stripe.Subscription.retrieve(sub_id)
        status = getattr(sub, "status", None)
        if not isinstance(status, str) or not status:
            status = "active"
        trial_ends_at = None
        t_end = getattr(sub, "trial_end", None)
        if isinstance(t_end, (int, float)):
            trial_ends_at = datetime.fromtimestamp(int(t_end), tz=timezone.utc)
        return status, trial_ends_at
    except Exception as e:
        logger.warning("stripe_subscription_retrieve_failed sub=%s err=%s", sub_id, type(e).__name__)
        return "active", None


def _plan_from_price_id(price_id: Optional[str]) -> Optional[str]:
    """Reverse-map a Stripe price ID to a plan name. Used for Customer-Portal plan
    switches, where the new plan is carried in the subscription's line items (not our
    metadata). Returns None for an unrecognized price so we never guess."""
    pid = (price_id or "").strip()
    if not pid:
        return None
    for plan in ("starter", "growth", "pro"):
        if (os.getenv(f"STRIPE_{plan.upper()}_PRICE_ID") or "").strip() == pid:
            return plan
    return None


def _subscription_plan_from_obj(obj: dict) -> Optional[str]:
    """Derive the plan from a Stripe subscription object's first line-item price."""
    try:
        items = ((obj.get("items") or {}).get("data")) or []
        if items:
            return _plan_from_price_id((items[0].get("price") or {}).get("id"))
    except Exception:
        pass
    return None


class CreateCheckoutSessionRequest(BaseModel):
    plan: Literal["starter", "growth", "pro"]
    # Self-serve signup: preferred area code for the number provisioned after checkout.
    area_code: Optional[str] = None
    # Optional referral code; validated server-side in the webhook (free month is granted
    # only after the card/email anti-abuse check passes).
    referral_code: Optional[str] = None


@router.post("/api/create-checkout-session")
def create_checkout_session(
    req: CreateCheckoutSessionRequest, tenant: Optional[dict] = Depends(deps.require_tenant)
):
    """Create a Stripe Checkout session for the given plan. Returns { url } for redirect."""
    if not STRIPE_AVAILABLE or not stripe:
        raise HTTPException(status_code=503, detail="Billing not configured")
    if not tenant or not runtime.USE_DB:
        raise HTTPException(status_code=403, detail="Tenant required")
    secret = (os.getenv("STRIPE_SECRET_KEY") or "").strip()
    if not secret:
        raise HTTPException(status_code=503, detail="Stripe not configured")
    stripe.api_key = secret
    # Every account is an org, so billing belongs to the org: one subscription priced
    # per store, whether they have one location or thirty-four. Adding a location later
    # just moves the quantity — no migration onto a different kind of account. The
    # tenant-level path below remains for any store that isn't in an org.
    org_id = (tenant.get("org_id") or "").strip()
    if org_id:
        org = database.db_org_get_by_id(org_id)
        if org:
            return _build_org_checkout(org, req.plan)
        logger.warning("checkout_org_missing tenant=%s org=%s", tenant.get("id"), org_id)
    price_id = _stripe_price_id(req.plan)
    if not price_id:
        raise HTTPException(
            status_code=503, detail=f"Price not configured for plan: {req.plan}"
        )
    frontend = (
        (os.getenv("FRONTEND_URL") or "http://localhost:3000").strip().rstrip("/")
    )
    success_url = f"{frontend}/dashboard?session_id={{CHECKOUT_SESSION_ID}}"
    cancel_url = f"{frontend}/dashboard"
    tenant_id = tenant.get("id")
    stripe_customer_id = tenant.get("stripe_customer_id")
    if not stripe_customer_id:
        try:
            cust = stripe.Customer.create(
                metadata={
                    "tenant_id": str(tenant_id),
                    "client_id": tenant.get("client_id", ""),
                },
                email=None,
            )
            stripe_customer_id = cust.id
            database.db_tenant_update_subscription(
                tenant_id, stripe_customer_id=stripe_customer_id
            )
        except Exception as e:
            logger.error("Stripe customer create failed: %s", e)
            raise HTTPException(
                status_code=500, detail="Could not create billing customer"
            )
    # A tenant with no number yet is a fresh self-serve signup — give it the card-on-file
    # free trial; existing tenants upgrading from trial get charged normally.
    needs_trial = not (tenant.get("twilio_phone_number") or "").strip()
    ref_code = (req.referral_code or "").strip().upper()
    subscription_data: dict = {
        "metadata": {"tenant_id": str(tenant_id), "plan": req.plan, "referral_code": ref_code}
    }
    if needs_trial:
        # Normal 7-day trial here; a valid referral extends it to a free month in the
        # webhook, AFTER the card/email anti-abuse check.
        subscription_data["trial_period_days"] = 7
    try:
        session = stripe.checkout.Session.create(
            customer=stripe_customer_id,
            mode="subscription",
            line_items=[{"price": price_id, "quantity": 1}],
            # Let the customer enter a Stripe promo code at checkout (e.g. a partner/intro
            # discount). The coupon + code are created in the Stripe dashboard; this just
            # surfaces the input field.
            allow_promotion_codes=True,
            success_url=success_url,
            cancel_url=cancel_url,
            metadata={
                "tenant_id": str(tenant_id),
                "plan": req.plan,
                "area_code": (req.area_code or "").strip(),
                "referral_code": ref_code,
            },
            subscription_data=subscription_data,
        )
        return {"url": session.url}
    except Exception as e:
        raise deps._server_error("Stripe checkout session failed", e)


@router.get("/api/referral/validate")
def validate_referral_code(code: str, _user_id: str = Depends(deps.require_user)):
    """Signed-in check so the signup page can confirm a code before checkout. Uses
    require_user (NOT require_tenant) because the user has no tenant yet mid-signup.
    Returns the MINIMUM (valid + referrer first name) — never contact or payout terms."""
    if not runtime.USE_DB:
        return {"valid": False}
    rc = database.db_referral_code_get_by_code(code, active_only=True)
    if not rc:
        return {"valid": False}
    first_name = (rc.get("referrer_name") or "").strip().split(" ")[0]
    return {"valid": True, "referrer_first_name": first_name}


@router.post("/api/create-portal-session")
def create_portal_session(tenant: Optional[dict] = Depends(deps.require_tenant)):
    """Create a Stripe Customer Portal session for managing subscription. Returns { url }."""
    if not STRIPE_AVAILABLE or not stripe:
        raise HTTPException(status_code=503, detail="Billing not configured")
    if not tenant or not runtime.USE_DB:
        raise HTTPException(status_code=403, detail="Tenant required")
    stripe_customer_id = tenant.get("stripe_customer_id")
    if not stripe_customer_id:
        # Trial users may not have a Stripe customer yet; create one so they can use the portal
        secret = (os.getenv("STRIPE_SECRET_KEY") or "").strip()
        if not secret:
            raise HTTPException(status_code=503, detail="Stripe not configured")
        stripe.api_key = secret
        try:
            cust = stripe.Customer.create(
                metadata={
                    "tenant_id": str(tenant.get("id")),
                    "client_id": tenant.get("client_id", ""),
                },
                email=None,
            )
            stripe_customer_id = cust.id
            database.db_tenant_update_subscription(
                tenant.get("id"), stripe_customer_id=stripe_customer_id
            )
        except Exception as e:
            logger.error("Stripe customer create failed for portal: %s", e)
            raise HTTPException(
                status_code=500, detail="Could not create billing account"
            )
    secret = (os.getenv("STRIPE_SECRET_KEY") or "").strip()
    if not secret:
        raise HTTPException(status_code=503, detail="Stripe not configured")
    stripe.api_key = secret
    frontend = (
        (os.getenv("FRONTEND_URL") or "http://localhost:3000").strip().rstrip("/")
    )
    return_url = f"{frontend}/dashboard"
    try:
        session = stripe.billing_portal.Session.create(
            customer=stripe_customer_id,
            return_url=return_url,
        )
        return {"url": session.url}
    except Exception as e:
        raise deps._server_error("Stripe portal session failed", e)


# ---------- Org billing: one subscription, quantity = number of stores ----------


def _org_subscription_line(sub_id: str) -> dict:
    """The subscription's first line item: {item_id, quantity, price_id}.

    Empty dict if it cannot be read. The price id matters because a group put on a
    partner rate after it subscribed is still pointing at the price it signed up on.
    """
    try:
        sub = stripe.Subscription.retrieve(sub_id)
        items = _plain(getattr(sub, "items", None) or {}).get("data") or []
        if items:
            it = items[0]
            return {
                "item_id": it.get("id"),
                "quantity": it.get("quantity"),
                "price_id": (it.get("price") or {}).get("id"),
            }
    except Exception as e:
        logger.warning(
            "org_sub_item_lookup_failed sub=%s err=%s: %s", sub_id, type(e).__name__, e
        )
    return {}


def _org_subscription_item(sub_id: str):
    """The subscription's first line item — the thing whose quantity is the store
    count. Returns (item_id, quantity) or (None, None)."""
    line = _org_subscription_line(sub_id)
    return line.get("item_id"), line.get("quantity")


def move_org_subscription_to_price(org_id: str) -> dict:
    """Point an existing subscription at the group's current (possibly partner) price.

    Without this, a partner rate only ever reaches groups that had not subscribed
    yet: the admin types "$50 off each store", the prices are created, the override
    is saved — and the group keeps paying list price forever, because nothing moves
    the subscription onto the new price. Adding stores makes it worse rather than
    better, since each one bumps quantity on the undiscounted price.

    The reason this was not done before is a misreading worth naming: a Stripe Price
    is immutable, but which price a subscription ITEM points at is not. Repricing is
    an ordinary Subscription.modify.

    proration_behavior="none" is deliberate. The discount takes effect from the next
    invoice; it does not retroactively credit the part of the period already paid.
    Anything else would hand out refunds nobody authorised as a side effect of an
    admin typing a number.

    Quantity is sent explicitly, and must be. Changing an item's price without
    naming a quantity does not leave the old one in place — Stripe applies the
    default of 1. The first version of this omitted it on the reasoning that
    quantity belongs to the store-count sync, and silently rebilled a 2-store group
    as 1 store. Store count is the source of truth, so send that: it also repairs a
    subscription that has already drifted.
    """
    out = {
        "repriced": False, "reason": None, "from_price": None, "to_price": None,
        "quantity": None,
    }
    if not (STRIPE_AVAILABLE and stripe) or not runtime.USE_DB:
        out["reason"] = "billing_unavailable"
        return out
    org = database.db_org_get_by_id(org_id)
    if not org:
        out["reason"] = "org_not_found"
        return out
    sub_id = (org.get("stripe_subscription_id") or "").strip()
    if not sub_id:
        # Not paying yet. Checkout will read the override, so there is nothing to fix.
        out["reason"] = "no_subscription"
        return out
    plan = (org.get("plan") or "").strip().lower() or "starter"
    target = _org_price_id(org, plan)
    if not target:
        out["reason"] = "no_price_for_plan"
        logger.warning("org_reprice_skipped org=%s plan=%s reason=no_price", org_id, plan)
        return out
    try:
        stripe.api_key = (os.getenv("STRIPE_SECRET_KEY") or "").strip()
        line = _org_subscription_line(sub_id)
        item_id = line.get("item_id")
        current = line.get("price_id")
        out["from_price"] = current
        out["to_price"] = target
        if not item_id:
            out["reason"] = "no_subscription_item"
            logger.warning("org_reprice_skipped org=%s sub=%s reason=no_item", org_id, sub_id)
            return out
        # Never omit this — see the docstring. Prefer the real store count so a
        # subscription that has already drifted comes back in line.
        was_qty = line.get("quantity")
        try:
            qty = max(1, int(database.db_org_store_count(org_id) or 0))
        except Exception:
            qty = None
        if not qty:
            qty = int(was_qty) if isinstance(was_qty, int) and was_qty > 0 else 1
        out["quantity"] = qty
        if current == target and was_qty == qty:
            out["reason"] = "already_on_price"
            logger.info(
                "org_reprice_noop org=%s sub=%s plan=%s price=%s qty=%s",
                org_id, sub_id, plan, target, qty,
            )
            return out
        stripe.Subscription.modify(
            sub_id,
            items=[{"id": item_id, "price": target, "quantity": qty}],
            proration_behavior="none",
        )
        out["repriced"] = True
        logger.info(
            "org_repriced org=%s sub=%s plan=%s from=%s to=%s qty=%s->%s stores=%s "
            "proration=none",
            org_id, sub_id, plan, current, target, was_qty, qty, qty,
        )
        return out
    except Exception as e:
        out["reason"] = f"{type(e).__name__}: {e}"
        logger.error(
            "org_reprice_failed org=%s sub=%s plan=%s target=%s err=%s: %s",
            org_id, sub_id, plan, target, type(e).__name__, e,
        )
        try:
            import alerts

            alerts.notify_failure(
                "billing", "org_reprice_failed", org_id,
                f"Org {org_id} kept its old price after a partner rate was applied",
                payload={"error": str(e), "target_price": target},
            )
        except Exception:
            pass
        return out


def cancel_org_subscription(org_id: str, sub_id: str) -> dict:
    """Cancel a group's subscription outright, for deleting the group.

    Immediate, unlike the empty-group path: the group is about to stop existing, so
    there is nothing left to un-cancel and no period worth preserving. Already-
    cancelled counts as success — the goal is "no longer billing", not "I was the
    one who stopped it".
    """
    out = {"ok": False, "subscription_id": sub_id, "error": None}
    if not (STRIPE_AVAILABLE and stripe):
        out["error"] = "Stripe library not available"
        return out
    try:
        stripe.api_key = (os.getenv("STRIPE_SECRET_KEY") or "").strip()
        if not stripe.api_key:
            out["error"] = "STRIPE_SECRET_KEY is not set"
            return out
        sub = _plain(stripe.Subscription.retrieve(sub_id))
        if sub.get("status") in ("canceled", "incomplete_expired"):
            out["ok"] = True
            out["already"] = True
            return out
        stripe.Subscription.cancel(sub_id)
        out["ok"] = True
        logger.info("org_subscription_cancelled org=%s sub=%s reason=group_deleted", org_id, sub_id)
    except Exception as e:
        out["error"] = f"{type(e).__name__}: {e}"
        logger.error(
            "org_cancel_failed org=%s sub=%s err=%s: %s", org_id, sub_id, type(e).__name__, e
        )
    return out


def _stop_billing_empty_org(org_id: str, sub_id: str) -> dict:
    """Schedule cancellation for a group with no stores left.

    At period end, not immediately: they have paid for this period, an immediate
    cancel raises refund questions nobody asked, and a store added back before the
    period ends simply un-schedules it. Reversible is the right default for an
    action taken automatically on the customer's behalf.
    """
    out = {"synced": True, "quantity": 0, "cancel_scheduled": False}
    try:
        stripe.api_key = (os.getenv("STRIPE_SECRET_KEY") or "").strip()
        sub = _plain(stripe.Subscription.retrieve(sub_id))
        if sub.get("status") in ("canceled", "incomplete_expired"):
            return out
        if sub.get("cancel_at_period_end"):
            out["cancel_scheduled"] = True
            return out
        stripe.Subscription.modify(sub_id, cancel_at_period_end=True)
        out["cancel_scheduled"] = True
        logger.info("org_billing_stopped org=%s sub=%s reason=no_stores_left", org_id, sub_id)
    except Exception as e:
        logger.error(
            "org_stop_billing_failed org=%s sub=%s err=%s: %s",
            org_id, sub_id, type(e).__name__, e,
        )
        try:
            import alerts

            alerts.notify_failure(
                "billing", "org_stop_billing_failed", org_id,
                f"Org {org_id} has no stores but its subscription is still billing",
                payload={"error": str(e), "subscription": sub_id},
            )
        except Exception:
            pass
    return out


def sync_org_subscription_quantity(org_id: str) -> dict:
    """Point the org's subscription quantity at its real store count.

    Called whenever stores are added or removed. Best-effort: a failure here means
    they're billed for the wrong number of stores, which is a money bug, so it's
    logged and alerted rather than swallowed — but it never breaks the caller, since
    refusing to create a store because Stripe hiccuped would be worse.
    """
    out = {"synced": False, "quantity": None}
    if not (STRIPE_AVAILABLE and stripe) or not runtime.USE_DB:
        return out
    org = database.db_org_get_by_id(org_id)
    if not org:
        return out
    sub_id = (org.get("stripe_subscription_id") or "").strip()
    if not sub_id:
        return out  # not paying yet — quantity is set at checkout
    raw_count = database.db_org_store_count(org_id)
    if raw_count <= 0:
        # No stores left. The floor below used to keep this at 1, so a group that
        # closed its last location went on paying for a store it did not have.
        # Schedule the cancellation rather than cancelling outright: they keep what
        # they already paid for until the period ends, and adding a store back before
        # then puts it straight back (see the un-schedule below).
        return _stop_billing_empty_org(org_id, sub_id)
    count = max(1, raw_count)
    try:
        stripe.api_key = (os.getenv("STRIPE_SECRET_KEY") or "").strip()
        item_id, current = _org_subscription_item(sub_id)
        if not item_id:
            return out
        if current == count:
            return {"synced": True, "quantity": count}
        # If billing was stopped when the group emptied, a store coming back has to
        # un-schedule that cancellation — otherwise they have stores again and the
        # subscription still dies at period end.
        try:
            existing = _plain(stripe.Subscription.retrieve(sub_id))
            if existing.get("cancel_at_period_end"):
                stripe.Subscription.modify(sub_id, cancel_at_period_end=False)
                logger.info("org_billing_resumed org=%s sub=%s", org_id, sub_id)
        except Exception as e:
            logger.warning(
                "org_resume_billing_check_failed org=%s err=%s: %s",
                org_id, type(e).__name__, e,
            )
        stripe.Subscription.modify(
            sub_id,
            items=[{"id": item_id, "quantity": count}],
            # They pay the difference for the rest of the period rather than a full
            # month for a store added on the 28th.
            proration_behavior="create_prorations",
        )
        logger.info("org_quantity_synced org=%s from=%s to=%s", org_id, current, count)
        return {"synced": True, "quantity": count}
    except Exception as e:
        logger.error("org_quantity_sync_failed org=%s err=%s", org_id, e)
        try:
            import alerts

            alerts.notify_failure(
                "billing", "org_quantity_sync_failed", org_id,
                f"Org {org_id} is billed for the wrong number of stores (should be {count})",
                payload={"error": str(e)},
            )
        except Exception:
            pass
        return out


class CreateOrgCheckoutRequest(BaseModel):
    plan: Literal["starter", "growth", "pro"] = "pro"
    org_id: Optional[str] = None


def _resolve_managed_org(user_id: str, org_id: Optional[str]) -> dict:
    """The caller must manage the org they're trying to pay for."""
    # org_wide: the group's subscription belongs to whoever oversees the group, never
    # to a manager who was invited to one store inside it.
    managed = [
        m
        for m in database.db_org_memberships_org_wide(user_id)
        if database.org_role_at_least(m.get("role"), "manager")
    ]
    if not managed:
        raise HTTPException(status_code=403, detail="Your account cannot manage billing.")
    if org_id:
        if not any(m["org_id"] == str(org_id) for m in managed):
            raise HTTPException(status_code=403, detail="You do not manage that group.")
        target = str(org_id)
    elif len(managed) > 1:
        raise HTTPException(status_code=400, detail="You manage several groups — specify which one.")
    else:
        target = managed[0]["org_id"]
    org = database.db_org_get_by_id(target)
    if not org:
        raise HTTPException(status_code=404, detail="Group not found")
    return org


@router.post("/api/org/create-checkout-session")
def create_org_checkout_session(
    req: CreateOrgCheckoutRequest, user_id: str = Depends(deps.require_user)
):
    """One subscription for the whole group, billed per store.

    Quantity is the store count at checkout and is kept in step afterwards by
    sync_org_subscription_quantity. Every store in the group inherits access from
    this one subscription, so no store ever enters a card.
    """
    if not STRIPE_AVAILABLE or not stripe:
        raise HTTPException(status_code=503, detail="Billing not configured")
    if not runtime.USE_DB:
        raise HTTPException(status_code=503, detail="Database required")
    secret = (os.getenv("STRIPE_SECRET_KEY") or "").strip()
    if not secret:
        raise HTTPException(status_code=503, detail="Stripe not configured")
    stripe.api_key = secret
    org = _resolve_managed_org(user_id, req.org_id)
    return _build_org_checkout(org, req.plan)


def _build_org_checkout(org: dict, plan: str) -> dict:
    """Create the group's Stripe Checkout session. Shared by the org endpoint and by
    ordinary signup, since every account is an org — one billing path, not two."""
    price_id = _org_price_id(org, plan)
    if not price_id:
        raise HTTPException(status_code=503, detail=f"Price not configured for plan: {plan}")
    customer_id = (org.get("stripe_customer_id") or "").strip()
    if not customer_id:
        try:
            cust = stripe.Customer.create(
                metadata={"org_id": org["id"], "org_name": org.get("name") or ""}, email=None
            )
            customer_id = cust.id
            database.db_org_update_subscription(org["id"], stripe_customer_id=customer_id)
        except Exception as e:
            logger.error("Stripe customer create failed for org %s: %s", org["id"], e)
            raise HTTPException(status_code=500, detail="Could not create billing customer")
    quantity = max(1, database.db_org_store_count(org["id"]))
    frontend = (os.getenv("FRONTEND_URL") or "http://localhost:3000").strip().rstrip("/")
    # A one-location account goes straight into its store; a multi-store group lands on
    # the rollup. Sending a solo owner to a list of one would be a strange first sight.
    landing = "/dashboard" if quantity <= 1 else "/dashboard/stores"
    subscription_data: dict = {"metadata": {"org_id": org["id"], "plan": plan}}
    # First subscription for the group gets the same 7-day trial a single store does.
    if not (org.get("stripe_subscription_id") or "").strip():
        subscription_data["trial_period_days"] = 7
    try:
        session = stripe.checkout.Session.create(
            customer=customer_id,
            mode="subscription",
            line_items=[{"price": price_id, "quantity": quantity}],
            allow_promotion_codes=True,
            success_url=f"{frontend}{landing}?session_id={{CHECKOUT_SESSION_ID}}",
            cancel_url=f"{frontend}{landing}",
            metadata={"org_id": org["id"], "plan": plan},
            subscription_data=subscription_data,
        )
        return {"url": session.url, "quantity": quantity}
    except Exception as e:
        raise deps._server_error("Stripe checkout session failed", e)


@router.post("/api/org/create-portal-session")
def create_org_portal_session(
    req: CreateOrgCheckoutRequest, user_id: str = Depends(deps.require_user)
):
    """Stripe Customer Portal for the group's subscription (card, invoices, cancel)."""
    if not STRIPE_AVAILABLE or not stripe:
        raise HTTPException(status_code=503, detail="Billing not configured")
    if not runtime.USE_DB:
        raise HTTPException(status_code=503, detail="Database required")
    secret = (os.getenv("STRIPE_SECRET_KEY") or "").strip()
    if not secret:
        raise HTTPException(status_code=503, detail="Stripe not configured")
    stripe.api_key = secret
    org = _resolve_managed_org(user_id, req.org_id)
    customer_id = (org.get("stripe_customer_id") or "").strip()
    if not customer_id:
        raise HTTPException(status_code=400, detail="This group has no billing set up yet.")
    frontend = (os.getenv("FRONTEND_URL") or "http://localhost:3000").strip().rstrip("/")
    try:
        session = stripe.billing_portal.Session.create(
            customer=customer_id, return_url=f"{frontend}/dashboard/stores"
        )
        return {"url": session.url}
    except Exception as e:
        raise deps._server_error("Stripe portal session failed", e)


def _resolve_org_for_subscription(meta: dict, sub_id: Optional[str]) -> Optional[dict]:
    """Is this Stripe subscription a group's rather than a single store's?

    Checked before the tenant path on every subscription event, because an org
    subscription has no tenant_id and would otherwise fall through to code that
    looks one up and finds nothing. Portal-initiated events carry no metadata, so
    the stored subscription id is the fallback — same shape as the tenant lookup.
    """
    if not runtime.USE_DB:
        return None
    org_id = (meta or {}).get("org_id")
    if org_id:
        return database.db_org_get_by_id(str(org_id))
    if sub_id:
        return database.db_org_get_by_stripe_subscription_id(sub_id)
    return None


def _handle_org_checkout_completed(obj: dict, meta: dict, request: Optional[Request]) -> None:
    """The group paid: record the subscription and light up every store in it.

    Stores don't need touching to gain access — subscription_access reads the org at
    request time — but their plan column is stamped so plans.get_plan_limits (which
    only ever looks at the tenant's own plan) gives them the tier they paid for.
    """
    org_id = meta.get("org_id")
    plan = meta.get("plan") or "pro"
    sub_id = obj.get("subscription")
    customer_id = obj.get("customer")
    if not org_id:
        return
    sub_status, trial_ends_at = _subscription_status_and_trial(sub_id)
    database.db_org_update_subscription(
        org_id,
        stripe_customer_id=customer_id,
        stripe_subscription_id=sub_id,
        subscription_status=sub_status,
        plan=plan,
        trial_ends_at=trial_ends_at,
    )
    synced = database.db_org_sync_store_plans(org_id, plan)
    # A demo account paying for the first time: clear its sample data before any store
    # gets a phone number, exactly as the single-store path does. Without this, a demo
    # that signs up through the org path would go live with invented services.
    for store in database.db_tenants_for_org(org_id):
        if store.get("demo_mode"):
            _deactivate_demo_if_needed(store, plan, request)
    deps.audit_log(
        "stripe",
        "org_checkout_completed",
        resource_type="org",
        resource_id=org_id,
        details={"plan": plan, "subscription_id": sub_id, "stores_synced": synced},
        request=request,
    )
    # Stores created before checkout aren't counted in the session's quantity.
    sync_org_subscription_quantity(org_id)
    # The group is paid now, so every store it already holds gets its AI line.
    provision_missing_org_store_numbers(org_id, request)


def _deactivate_demo_if_needed(tenant: dict, plan: str, request: Optional[Request] = None) -> None:
    """A demo tenant that just paid becomes a real one: purge every seeded sample row
    and reset the business config to a blank slate.

    The config reset is the whole point. A demo is pre-filled with invented services
    ("Haircut $28") and staff who don't exist; if any of that survived activation,
    their live receptionist would quote sample prices to real callers and book them
    with imaginary stylists. Handing the owner an empty Settings page is strictly
    better than a plausible wrong one — the setup gate already walks them through
    filling it in.

    Their actual onboarding choice (number_mode / existing_business_number) is the one
    thing carried across: it's a real answer they gave, not sample data.

    Best-effort — never raises into the webhook handler.
    """
    if not tenant.get("demo_mode"):
        return
    try:
        import config_service

        cid = (tenant.get("client_id") or "").strip()
        if not cid:
            return
        old = config_service._read_raw_client_config(cid) or {}
        fresh = config_service._default_client_config_data(cid, plan)
        fresh["business_name"] = tenant.get("name") or ""
        fresh["name"] = tenant.get("name") or ""
        fresh["number_mode"] = old.get("number_mode") or "new"
        if fresh["number_mode"] == "existing":
            fresh["existing_business_number"] = old.get("existing_business_number") or ""
        res = database.db_tenant_deactivate_demo(tenant["id"], fresh)
        if not res:
            # Not a demo any more — a Stripe redelivery of the same event. Nothing to do.
            return
        # Keep the local config file in step with the DB (dev only; on Render the
        # DB is authoritative and this is a no-op that may fail harmlessly).
        try:
            config_service.save_raw_client_config(cid, fresh)
        except Exception:
            pass
        deps.audit_log(
            "stripe",
            "demo_converted_to_paid",
            resource_type="tenant",
            resource_id=tenant["id"],
            client_id=cid,
            details={"plan": plan, "purged": res.get("deleted")},
            request=request,
        )
        logger.info("demo_converted cid=%s purged=%s", cid, res.get("deleted"))
    except Exception as e:
        # A tenant stuck in demo_mode still can't take calls until a number is
        # provisioned below, but their dashboard would keep showing sample data —
        # alert so it can be cleared by hand.
        logger.error("demo_deactivate_failed tenant=%s err=%s", tenant.get("id"), e)
        try:
            import alerts

            alerts.notify_failure(
                "billing", "demo_deactivate_failed", tenant.get("id"),
                f"Demo data purge failed for {tenant.get('client_id')} after payment",
                payload={"error": str(e)},
            )
        except Exception:
            pass


def provision_missing_org_store_numbers(org_id: str, request: Optional[Request] = None) -> dict:
    """Give every store in a paid org its own AI line.

    Stores added by an org manager are created without a number (there's no per-store
    checkout to hang provisioning off — the group pays once). Without this, a manager
    could add all their stores and none of them could receive a call.

    Only runs when the org's billing is actually active, so an unpaid group can't
    provision phone numbers. Safe to call repeatedly: stores that already have a number
    are skipped, so it doubles as a backfill.
    """
    out = {"provisioned": 0, "skipped": 0, "failed": 0}
    if not runtime.USE_DB:
        return out
    org = database.db_org_get_by_id(org_id)
    if not org or not evaluate_billing(org)["active"]:
        return out
    for store in database.db_tenants_for_org(org_id):
        if (store.get("twilio_phone_number") or "").strip():
            out["skipped"] += 1
            continue
        # Match the store's own area code where we can, so the AI line looks local to
        # the callers being forwarded to it.
        area = _area_code_for_store(store)
        try:
            _provision_number_for_tenant(store, area_code=area, request=request)
            fresh = database.db_tenant_get_by_id(store["id"]) or {}
            if (fresh.get("twilio_phone_number") or "").strip():
                out["provisioned"] += 1
            else:
                out["failed"] += 1
        except Exception as e:
            logger.error("org_store_provision_failed store=%s err=%s", store.get("client_id"), e)
            out["failed"] += 1
    logger.info("org_store_numbers org=%s %s", org_id, out)
    return out


def _area_code_for_store(store: dict) -> Optional[str]:
    """The store's own area code, taken from the existing business number they're
    forwarding from. None when we can't tell — Twilio then picks any available number."""
    try:
        import config_service

        cfg = config_service._read_raw_client_config((store.get("client_id") or "").strip()) or {}
        digits = "".join(c for c in (cfg.get("existing_business_number") or "") if c.isdigit())
        if len(digits) >= 10:
            return digits[-10:-7]  # area code of a US number
    except Exception:
        pass
    return None


def _provision_number_for_tenant(tenant: dict, area_code: Optional[str], request: Request) -> None:
    """Self-serve: buy and wire a Twilio number (+ A2P enroll) for a tenant that has
    none yet, after checkout succeeds. Non-fatal — logged + audited on failure so the
    operator can provision manually from the admin console."""
    import twilio_provision

    acct = (os.getenv("TWILIO_ACCOUNT_SID") or "").strip()
    tok = (os.getenv("TWILIO_AUTH_TOKEN") or "").strip()
    base = (os.getenv("PUBLIC_BASE_URL") or "").strip()
    if not (acct and tok and base):
        logger.error("self_serve_provision_skipped: missing Twilio/base config tenant=%s", tenant.get("id"))
        return
    res = twilio_provision.purchase_number(
        account_sid=acct,
        auth_token=tok,
        base_url=base,
        area_code=area_code,
        # Label it with the store, so the Twilio console is readable at 34 locations.
        label=(tenant.get("client_id") or tenant.get("name") or "").strip() or None,
    )
    if res.get("ok") and res.get("phone_e164"):
        database.db_tenant_set_twilio_phone(tenant["id"], res["phone_e164"])
        # Store the number SID so we can release it reliably on churn without a lookup.
        if res.get("number_sid"):
            database.db_tenant_set_twilio_number_sid(tenant["id"], res["number_sid"])
        deps.audit_log(
            "system",
            "self_serve_number_provisioned",
            resource_type="tenant",
            resource_id=tenant["id"],
            client_id=tenant.get("client_id"),
            details={"phone_e164": res["phone_e164"], "a2p_enrolled": res.get("messaging_service_enrolled")},
            request=request,
        )
    else:
        logger.error("self_serve_provision_failed tenant=%s errors=%s", tenant.get("id"), res.get("errors"))
        deps.audit_log(
            "system",
            "self_serve_number_provision_failed",
            resource_type="tenant",
            resource_id=tenant["id"],
            client_id=tenant.get("client_id"),
            details={"errors": res.get("errors")},
            request=request,
        )
        # A paying customer with no phone line is urgent — alert so it can be fixed manually.
        try:
            import alerts

            alerts.notify_failure(
                "provision", "number_purchase_failed", tenant.get("id"),
                f"Self-serve number provisioning failed for {tenant.get('client_id')}",
                payload={"errors": res.get("errors")},
            )
        except Exception:
            pass


def _release_tenant_twilio_number(tenant: Optional[dict], request: Optional[Request] = None) -> None:
    """Release a churned tenant's Twilio number (remove from A2P service + delete) and
    clear it from the tenant row. Best-effort — never raises into a webhook handler."""
    if not tenant:
        return
    phone = (tenant.get("twilio_phone_number") or "").strip()
    if not phone:
        return  # pending tenant that never got a number — nothing to release
    import twilio_provision

    acct = (os.getenv("TWILIO_ACCOUNT_SID") or "").strip()
    tok = (os.getenv("TWILIO_AUTH_TOKEN") or "").strip()
    if not (acct and tok):
        logger.error("twilio_release_skipped: missing Twilio creds tenant=%s", tenant.get("id"))
        return
    try:
        res = twilio_provision.release_number(
            account_sid=acct,
            auth_token=tok,
            phone_e164=phone,
            number_sid=(tenant.get("twilio_number_sid") or None),
        )
        database.db_tenant_clear_twilio(tenant["id"])
        deps.audit_log(
            "system",
            "twilio_number_released",
            resource_type="tenant",
            resource_id=tenant["id"],
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
        logger.exception("twilio_release_unexpected tenant=%s: %s", tenant.get("id"), e)


def _referral_card_fingerprint_and_email(session_obj: dict, sub_id, customer_id):
    """Best-effort: return (card_fingerprint, email, subscription_obj). Null-safe."""
    fp = None
    email = None
    sub_obj = None
    try:
        cd = session_obj.get("customer_details") or {}
        email = (cd.get("email") or "").strip() or None
    except Exception:
        pass
    try:
        if sub_id:
            sub_obj = stripe.Subscription.retrieve(sub_id, expand=["default_payment_method"])
            pm = getattr(sub_obj, "default_payment_method", None)
            card = getattr(pm, "card", None) if pm else None
            fp = getattr(card, "fingerprint", None) if card else None
        if not fp and customer_id:
            pms = stripe.PaymentMethod.list(customer=customer_id, type="card")
            data = getattr(pms, "data", None) or []
            if data:
                card = getattr(data[0], "card", None)
                fp = getattr(card, "fingerprint", None) if card else None
        if not email and customer_id:
            cust = stripe.Customer.retrieve(customer_id)
            email = (getattr(cust, "email", None) or "").strip() or None
    except Exception as e:
        logger.warning("referral_fingerprint_lookup_failed sub=%s: %s", sub_id, e)
    return fp, email, sub_obj


def _process_referral_on_checkout(session_obj, meta, tenant_id, sub_id, customer_id, plan, request):
    """Record the signup's card/email (global anti-abuse ledger) and, if a valid referral
    code was used, grant the free month or flag the redemption. Best-effort; never raises."""
    try:
        fp, email, sub_obj = _referral_card_fingerprint_and_email(session_obj, sub_id, customer_id)
        # Always record the signup fingerprint/email so future signups can be deduped.
        try:
            database.db_signup_payment_method_record(tenant_id, fp, email)
        except Exception:
            pass

        code = (meta.get("referral_code") or "").strip().upper()
        if not code:
            return
        rc = database.db_referral_code_get_by_code(code, active_only=True)
        if not rc:
            deps.audit_log(
                "stripe", "referral_code_invalid", resource_type="tenant",
                resource_id=tenant_id, details={"code": code}, request=request,
            )
            return
        red_id = database.db_referral_redemption_create(
            tenant_id, rc["id"], code, rc["referrer_name"], plan, sub_id
        )
        if not red_id:
            return
        database.db_referral_redemption_update(red_id, card_fingerprint=fp, signup_email=email)

        # Anti-abuse: a card or email already used by a DIFFERENT prior signup blocks the
        # free month (exclude our own just-recorded row via exclude_tenant_id).
        dup_card = bool(fp) and database.db_signup_fingerprint_seen(fp, exclude_tenant_id=tenant_id)
        dup_email = bool(email) and database.db_signup_email_seen(email, exclude_tenant_id=tenant_id)
        if dup_card or dup_email:
            reason = "duplicate_card" if dup_card else "duplicate_email"
            database.db_referral_redemption_update(red_id, status="flagged", flagged_reason=reason, free_month_granted=False)
            deps.audit_log(
                "stripe", "referral_redemption_flagged", resource_type="tenant",
                resource_id=tenant_id, details={"code": code, "reason": reason}, request=request,
            )
            return

        # Grant the free month by extending the Stripe trial to ~30 days from start, so
        # the customer is genuinely not charged. Anchored off the subscription start.
        from plans import REFERRAL_FREE_MONTH_DAYS

        now_ts = int(datetime.now(timezone.utc).timestamp())
        started = int(getattr(sub_obj, "created", 0) or now_ts) if sub_obj else now_ts
        trial_end = max(started, now_ts) + REFERRAL_FREE_MONTH_DAYS * 86400
        if trial_end <= now_ts + 60:
            trial_end = now_ts + REFERRAL_FREE_MONTH_DAYS * 86400
        stripe.Subscription.modify(sub_id, trial_end=trial_end, proration_behavior="none")
        database.db_referral_redemption_update(red_id, status="granted", free_month_granted=True)
        deps.audit_log(
            "stripe", "referral_free_month_granted", resource_type="tenant",
            resource_id=tenant_id, details={"code": code, "days": REFERRAL_FREE_MONTH_DAYS}, request=request,
        )
    except Exception as e:
        logger.exception("referral_checkout_processing_failed tenant=%s: %s", tenant_id, e)


def _process_referral_commission(invoice_obj, sub_id, request):
    """On a real paid invoice for a referred subscription, create the $200 signup bounty
    (once, on the first paid charge) and a 25%-of-plan-price commission for the month
    (capped at 12 months / 1 year). Idempotent via DB unique constraints. Never raises."""
    try:
        from plans import REFERRAL_SIGNUP_BOUNTY_CENTS, REFERRAL_MRR_MONTHS_CAP, referral_mrr_commission_cents

        red = database.db_referral_redemption_get_by_subscription(sub_id)
        if not red or red.get("status") not in ("granted", "converted"):
            return  # no redemption, or flagged → never earns a payout
        red_id = red["id"]
        # Use the tenant's CURRENT plan so upgrades/downgrades follow the price.
        tenant = database.db_tenant_get_by_id(red["tenant_id"]) if red.get("tenant_id") else None
        plan = (tenant or {}).get("plan") or red.get("plan_at_signup") or "starter"
        invoice_id = invoice_obj.get("id") or "unknown"
        now = datetime.now(timezone.utc)

        first_paid_dt = None
        if red.get("first_paid_at"):
            try:
                first_paid_dt = datetime.fromisoformat(red["first_paid_at"].replace("Z", "+00:00"))
            except Exception:
                first_paid_dt = None

        # First paid charge → set converted + create the $200 signup bounty (idempotent).
        if not first_paid_dt:
            database.db_referral_redemption_update(red_id, status="converted", first_paid_at=now)
            first_paid_dt = now
            database.db_referral_commission_insert(
                red_id, "signup_bounty", "signup", REFERRAL_SIGNUP_BOUNTY_CENTS,
                plan, red["code_snapshot"], red["referrer_name_snapshot"],
            )
            deps.audit_log(
                "stripe", "referral_bounty_earned", resource_type="tenant",
                resource_id=red.get("tenant_id"), details={"amount_cents": REFERRAL_SIGNUP_BOUNTY_CENTS}, request=request,
            )

        # Recurring 25% MRR for this paid month — capped at 12 entries / within 1 year.
        if first_paid_dt and now > first_paid_dt + timedelta(days=365):
            return
        if database.db_referral_commission_count_mrr(red_id) >= REFERRAL_MRR_MONTHS_CAP:
            return
        amount = referral_mrr_commission_cents(plan)
        inserted = database.db_referral_commission_insert(
            red_id, "mrr", invoice_id, amount, plan, red["code_snapshot"], red["referrer_name_snapshot"],
        )
        if inserted:
            deps.audit_log(
                "stripe", "referral_mrr_earned", resource_type="tenant",
                resource_id=red.get("tenant_id"), details={"amount_cents": amount, "invoice": invoice_id}, request=request,
            )
    except Exception as e:
        logger.exception("referral_commission_processing_failed sub=%s: %s", sub_id, e)


@router.post("/api/stripe-webhook")
async def stripe_webhook(request: Request):
    """Handle Stripe webhooks: subscription and payment events. Raw body required for signature verification."""
    if not STRIPE_AVAILABLE or not stripe:
        raise HTTPException(status_code=503, detail="Billing not configured")
    payload = await request.body()
    sig = request.headers.get("stripe-signature", "")
    secret = (os.getenv("STRIPE_WEBHOOK_SECRET") or "").strip()
    event, verr = verify_stripe_event(
        payload, sig, webhook_secret=secret, stripe_module=stripe
    )
    if verr:
        code = 503 if verr == "Webhook secret not configured" else 400
        # Log the reason. Stripe only surfaces the status code on its side, so without
        # this a rejected delivery is an unexplained 400 in the access log — which is
        # exactly how a signing-secret mismatch stays invisible for hours. The secret
        # fingerprint (never the secret) tells you WHICH secret is loaded, so you can
        # tell "wrong endpoint's secret" from "no secret at all".
        logger.error(
            "stripe_webhook_rejected status=%s reason=%s secret_present=%s "
            "secret_fingerprint=%s sig_header_present=%s payload_bytes=%s",
            code,
            verr,
            bool(secret),
            (_stable_sha256(secret)[:8] if secret else "none"),
            bool(sig),
            len(payload or b""),
        )
        raise HTTPException(status_code=code, detail=verr)
    assert event is not None
    if not runtime.USE_DB:
        return {"received": True}
    # Work off the verified raw payload as a plain dict — robust across Stripe SDK /
    # API-version differences (the typed event object's dict access can vary and was
    # 500ing the handler). Signature is already verified above.
    try:
        evt = json.loads(payload)
    except Exception:
        evt = {}
    etype = evt.get("type") or getattr(event, "type", "") or ""
    obj = ((evt.get("data") or {}).get("object")) or {}

    # Never 500 on a processing error — that makes Stripe retry the event forever.
    try:
        if etype == "checkout.session.completed":
            meta = obj.get("metadata") or {}
            tenant_id = meta.get("tenant_id")
            plan = meta.get("plan") or "starter"
            sub_id = obj.get("subscription")
            customer_id = obj.get("customer")
            # A group's subscription covers many stores and has no tenant_id at all.
            if meta.get("org_id"):
                _handle_org_checkout_completed(obj, meta, request)
            elif tenant_id and (sub_id or customer_id):
                # Mirror Stripe's real subscription state: a fresh signup is on a
                # 7-day trial ('trialing' + trial_end), which unlocks full Pro-tier
                # features. Hardcoding 'active' here previously dropped trial users
                # to their paid plan immediately.
                sub_status, trial_ends_at = _subscription_status_and_trial(sub_id)
                database.db_tenant_update_subscription(
                    tenant_id,
                    stripe_customer_id=customer_id,
                    stripe_subscription_id=sub_id,
                    subscription_status=sub_status,
                    plan=plan,
                    trial_ends_at=trial_ends_at,
                )
                tenant = database.db_tenant_get_by_id(tenant_id)
                deps.audit_log(
                    "stripe",
                    "checkout.session.completed",
                    resource_type="tenant",
                    resource_id=tenant_id,
                    client_id=tenant["client_id"] if tenant else None,
                    details={"plan": plan, "subscription_id": sub_id},
                    request=request,
                )
                # Demo -> paid: clear the sample data BEFORE a number exists. Once the
                # line is live, real calls can write real rows, and the purge deletes
                # by client_id — so it has to run while the tenant is still unreachable.
                if tenant:
                    _deactivate_demo_if_needed(tenant, plan, request)
                # Self-serve: provision the number now that payment is set up.
                if tenant and not (tenant.get("twilio_phone_number") or "").strip():
                    _provision_number_for_tenant(
                        tenant, area_code=(meta.get("area_code") or "").strip() or None, request=request
                    )
                # Referral: record the signup's card/email and (if a valid code) grant the
                # free month or flag for abuse. Runs after provisioning so a Stripe call
                # here can never delay number setup. Best-effort; never breaks the webhook.
                _process_referral_on_checkout(
                    obj, meta, tenant_id, sub_id, customer_id, plan, request
                )
        elif etype == "customer.subscription.updated":
            sub_id = obj.get("id")
            meta = obj.get("metadata") or {}
            tenant_id = meta.get("tenant_id")
            status = obj.get("status")
            org = _resolve_org_for_subscription(meta, sub_id)
            if org:
                org_plan = meta.get("plan") or _subscription_plan_from_obj(obj)
                trial_ends_at = None
                t_end = obj.get("trial_end")
                if t_end:
                    trial_ends_at = datetime.fromtimestamp(int(t_end), tz=timezone.utc)
                database.db_org_update_subscription(
                    org["id"],
                    stripe_subscription_id=sub_id,
                    subscription_status=status,
                    plan=org_plan,
                    trial_ends_at=trial_ends_at,
                )
                if org_plan:
                    database.db_org_sync_store_plans(org["id"], org_plan)
                deps.audit_log(
                    "stripe", "org_subscription_updated", resource_type="org",
                    resource_id=org["id"], details={"status": status, "plan": org_plan},
                    request=request,
                )
                return {"received": True}
            # Customer-Portal-initiated events often carry no tenant_id metadata;
            # resolve by the stored subscription id instead (mirrors payment_failed).
            if not tenant_id and sub_id:
                t = database.db_tenant_get_by_stripe_subscription_id(sub_id)
                if t:
                    tenant_id = t.get("id")
            if tenant_id and sub_id:
                # Prefer metadata.plan (set at checkout); for a Customer-Portal plan
                # switch there's no metadata, so derive the plan from the subscription's
                # line-item price. Falls back to None (leave plan untouched) only when the
                # price is unrecognized — never silently downgrades to starter.
                plan = meta.get("plan") or _subscription_plan_from_obj(obj)
                trial_ends_at = None
                t_end = obj.get("trial_end")
                if t_end:
                    trial_ends_at = datetime.fromtimestamp(int(t_end), tz=timezone.utc)
                database.db_tenant_update_subscription(
                    tenant_id,
                    stripe_subscription_id=sub_id,
                    subscription_status=status,
                    plan=plan,
                    trial_ends_at=trial_ends_at,
                )
                tenant = database.db_tenant_get_by_id(tenant_id)
                deps.audit_log(
                    "stripe",
                    "customer.subscription.updated",
                    resource_type="tenant",
                    resource_id=tenant_id,
                    client_id=tenant["client_id"] if tenant else None,
                    details={"status": status, "plan": plan},
                    request=request,
                )
        elif etype == "customer.subscription.deleted":
            sub_id = obj.get("id")
            tenant_id = (obj.get("metadata") or {}).get("tenant_id")
            org = _resolve_org_for_subscription(obj.get("metadata") or {}, sub_id)
            if org:
                # Cancelling the group's subscription stops every store in it: each one
                # was only live by inheriting this. No numbers are released here — that
                # would be irreversible on a billing blip, and the stores go dark anyway.
                database.db_org_update_subscription(org["id"], subscription_status="canceled")
                deps.audit_log(
                    "stripe", "org_subscription_deleted", resource_type="org",
                    resource_id=org["id"],
                    details={"stores": database.db_org_store_count(org["id"])},
                    request=request,
                )
                return {"received": True}
            # Portal/Stripe-initiated cancellations may lack metadata; resolve by sub id.
            if not tenant_id and sub_id:
                t = database.db_tenant_get_by_stripe_subscription_id(sub_id)
                if t:
                    tenant_id = t.get("id")
            if tenant_id:
                tenant = database.db_tenant_get_by_id(tenant_id)
                database.db_tenant_update_subscription(tenant_id, subscription_status="canceled")
                deps.audit_log(
                    "stripe",
                    "customer.subscription.deleted",
                    resource_type="tenant",
                    resource_id=tenant_id,
                    client_id=tenant["client_id"] if tenant else None,
                    details={},
                    request=request,
                )
                # Stripe dunning is exhausted at this point — release the Twilio number
                # so we stop paying for a churned tenant. Best-effort; never 500s.
                _release_tenant_twilio_number(tenant, request=request)
        elif etype == "invoice.payment_failed":
            sub_id = obj.get("subscription")
            if sub_id:
                tenant = database.db_tenant_get_by_stripe_subscription_id(sub_id)
                if tenant:
                    database.db_tenant_update_subscription(
                        tenant["id"], subscription_status="past_due"
                    )
                    deps.audit_log(
                        "stripe",
                        "invoice.payment_failed",
                        resource_type="tenant",
                        resource_id=tenant["id"],
                        client_id=tenant.get("client_id"),
                        details={"subscription_id": sub_id},
                        request=request,
                    )
        elif etype == "invoice.payment_succeeded":
            # A real (non-trial) payment cleared → referral commission(s) may be due.
            amount_paid = obj.get("amount_paid") or 0
            inv_sub_id = obj.get("subscription")
            if amount_paid > 0 and inv_sub_id:
                _process_referral_commission(obj, inv_sub_id, request)
    except Exception as e:
        logger.exception("stripe_webhook handler error event_type=%s: %s", etype, e)
        # Record + alert: a swallowed Stripe failure can mean a missed cancellation,
        # un-released number, or unrecorded payout — never let it vanish into logs.
        try:
            import alerts

            alerts.notify_failure("stripe", etype, (evt.get("id") if isinstance(evt, dict) else None), str(e))
        except Exception:
            pass
    return {"received": True}
