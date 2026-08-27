"""Clerk user/tenant linking + invites, and the admin access-debug snapshot.

Extracted from main.py so the onboarding pipeline (and admin routes) can link
Clerk owners to tenants without importing main. Clerk-API calls use httpx;
JWT/metadata helpers live in deps (deps._clerk_fetch_user_link /
deps._clerk_patch_user_tenant_metadata); DB access is module-qualified.
"""

from __future__ import annotations

from typing import List, Optional
from urllib.parse import quote
import os

import database
import deps
import runtime


def _clerk_api_json_list(resp) -> list:
    """Clerk list endpoints may return {data: [...]} or a bare list."""
    if getattr(resp, "status_code", 500) >= 400:
        return []
    try:
        body = resp.json()
    except Exception:
        return []
    if isinstance(body, list):
        return body
    if isinstance(body, dict):
        data = body.get("data")
        return data if isinstance(data, list) else []
    return []


def _clerk_revoke_active_sessions(user_id: str, headers: dict) -> None:
    """Force a fresh Clerk session so JWT public_metadata (tenant_id) is current."""
    import httpx

    try:
        sessions_resp = httpx.get(
            f"https://api.clerk.com/v1/sessions?user_id={user_id}&status=active",
            headers=headers,
            timeout=10.0,
        )
        for session in _clerk_api_json_list(sessions_resp):
            sid = session.get("id") if isinstance(session, dict) else None
            if not sid:
                continue
            httpx.post(
                f"https://api.clerk.com/v1/sessions/{sid}/revoke",
                headers=headers,
                timeout=10.0,
            )
        print(f"[Admin] Revoked active Clerk sessions for user {user_id}")
    except Exception as e:
        print(f"[Admin] Error revoking sessions for Clerk user {user_id}: {e}")


def _emails_on_clerk_user(row: dict) -> List[str]:
    """Every email address on a Clerk user object, lowercased."""
    out: List[str] = []
    for entry in row.get("email_addresses") or []:
        if isinstance(entry, dict):
            addr = str(entry.get("email_address") or "").strip().lower()
            if addr:
                out.append(addr)
    return out


def _clerk_user_ids_from_api(email: str, headers: dict) -> List[str]:
    """Clerk user IDs whose address really is `email`.

    Every row is checked against the address we asked for, because the filter cannot
    be trusted. Clerk silently ignores query parameters it doesn't recognise and
    returns the whole user list, so a request whose filter didn't take looks exactly
    like "these all matched". That result then flowed into
    _clerk_link_email_to_tenant, which took [0] and made that person the sole owner of
    the store — inviting someone with no account handed their shop to an unrelated
    account. Verifying here is what makes that impossible, whatever the API does.
    """
    import httpx

    raw = (email or "").strip()
    if not raw or "@" not in raw:
        return []
    wanted = raw.lower()
    ids: List[str] = []
    seen: set = set()
    for candidate in {raw, raw.lower()}:
        email_q = quote(candidate, safe="")
        for url in (
            f"https://api.clerk.com/v1/users?email_address[]={email_q}&limit=20",
            f"https://api.clerk.com/v1/users?email_address={email_q}&limit=20",
        ):
            try:
                users_resp = httpx.get(url, headers=headers, timeout=10.0)
            except Exception as e:
                print(f"[Admin] Clerk user lookup request failed: {e}")
                continue
            if users_resp.status_code >= 400:
                print(
                    f"[Admin] Clerk user lookup {users_resp.status_code} url={url}: "
                    f"{(users_resp.text or '')[:160]}"
                )
                continue
            users = users_resp.json()
            user_list = users if isinstance(users, list) else users.get("data", [])
            returned = 0
            for row in user_list or []:
                if not isinstance(row, dict) or not row.get("id"):
                    continue
                returned += 1
                if wanted not in _emails_on_clerk_user(row):
                    continue  # the filter didn't hold; this is somebody else
                uid = str(row["id"])
                if uid not in seen:
                    seen.add(uid)
                    ids.append(uid)
            if returned and not ids:
                # Rows came back and none of them own the address — the filter was
                # ignored. Loud, because it means this URL shape doesn't work.
                print(
                    f"[Admin] Clerk user lookup returned {returned} row(s) but none "
                    f"match the requested address; ignoring. url_shape={url.split('?')[1].split('=')[0]}"
                )
            if ids:
                return ids
    return ids


def _clerk_user_ids_from_tenant_members(email: str, headers: dict) -> List[str]:
    """Find Clerk users by comparing emails on existing tenant memberships (API lookup fallback)."""
    if not runtime.USE_DB:
        return []
    target = (email or "").strip().lower()
    if not target or "@" not in target:
        return []
    matched: List[str] = []
    for clerk_user_id in database.db_tenant_all_member_clerk_ids():
        link = deps._clerk_fetch_user_link(clerk_user_id)
        if not link:
            continue
        for em in link.get("emails") or []:
            if (em or "").strip().lower() == target:
                matched.append(clerk_user_id)
                break
    return matched


def _clerk_user_ids_for_email(email: str, headers: dict) -> List[str]:
    """Clerk user IDs for an email — API first, then scan known tenant members."""
    ids = _clerk_user_ids_from_api(email, headers)
    if ids:
        return ids
    member_ids = _clerk_user_ids_from_tenant_members(email, headers)
    if member_ids:
        print(
            f"[Admin] Clerk API missed email {email!r}; matched via tenant_members "
            f"({len(member_ids)} user(s))"
        )
    return member_ids


def _clerk_relink_users_to_tenant(
    user_ids: List[str],
    tenant_id: str,
    email: str,
    headers: dict,
) -> tuple[List[str], List[str], Optional[str]]:
    """Re-link Clerk users to tenant. Returns (linked_ids, displaced_ids, error_message)."""
    linked: List[str] = []
    displaced_all: List[str] = []
    link_errors: List[str] = []
    for uid in user_ids:
        try:
            displaced = _clerk_relink_user_to_tenant(uid, tenant_id, headers)
            displaced_all.extend(displaced or [])
            linked.append(uid)
            print(
                f"[Admin] Re-linked existing user {uid} to tenant {tenant_id} (email={email})"
            )
        except Exception as e:
            link_errors.append(f"{uid}: {e}")
            print(f"[Admin] Error re-linking user {uid}: {e}")
    err: Optional[str] = None
    if link_errors and not linked:
        err = f"Re-link failed: {'; '.join(link_errors[:3])}"
    elif link_errors:
        err = (
            f"Linked {len(linked)} of {len(user_ids)} Clerk account(s); "
            f"failures: {'; '.join(link_errors[:2])}"
        )
    return linked, displaced_all, err


def _clerk_invite_error_message(status_code: int, body: str) -> str:
    """Short admin-facing message for Clerk invitation failures."""
    if status_code == 422 and "form_identifier_exists" in body:
        return (
            "That email already has a Clerk account. Use Resend invite again — we will link "
            "the existing account to this business (no new invitation email)."
        )
    return f"Clerk API {status_code}: {body[:240]}"


def _clerk_clear_tenant_access(clerk_user_id: str, headers: dict) -> None:
    """Remove tenant from Clerk user and force re-auth (used when displacing a tenant owner)."""
    import httpx

    try:
        httpx.patch(
            f"https://api.clerk.com/v1/users/{clerk_user_id}",
            headers=headers,
            json={"public_metadata": {"tenant_id": None}},
            timeout=10.0,
        )
    except Exception as e:
        print(f"[Admin] Error clearing Clerk metadata for {clerk_user_id}: {e}")
    _clerk_revoke_active_sessions(clerk_user_id, headers)


def _clerk_relink_user_to_tenant(
    clerk_user_id: str, tenant_id: str, headers: dict
) -> List[str]:
    """
    Make this user the sole owner of the tenant; clear access for anyone else on that tenant.
    Returns displaced clerk_user_ids (previous owners).
    """
    displaced = database.db_tenant_member_assign_owner(clerk_user_id, tenant_id)
    if displaced is None:
        raise RuntimeError(
            f"Database membership update failed for {clerk_user_id} (tenant_id={tenant_id})"
        )
    if not deps._clerk_patch_user_tenant_metadata(clerk_user_id, tenant_id):
        raise RuntimeError(
            f"Clerk metadata patch failed for {clerk_user_id} (tenant_id={tenant_id})"
        )
    for uid in displaced:
        _clerk_clear_tenant_access(uid, headers)
    _clerk_revoke_active_sessions(clerk_user_id, headers)
    return displaced


def clerk_user_id_for_email(email: str) -> Optional[str]:
    """The Clerk user id for an address, or None if they have no account yet.

    Public wrapper so callers can check who an invite would land on BEFORE writing.
    Returns None on any failure — the caller then proceeds as if the account is new,
    which is the existing behaviour and never blocks an invite on a Clerk hiccup.
    """
    secret = os.getenv("CLERK_SECRET_KEY", "").strip()
    if not secret:
        return None
    try:
        ids = _clerk_user_ids_for_email(
            email, {"Authorization": f"Bearer {secret}", "Content-Type": "application/json"}
        )
        return ids[0] if ids else None
    except Exception:
        return None


def _clerk_revoke_invitations_for_email(email: str, headers: dict) -> dict:
    """Revoke any PENDING Clerk invitation for this address.

    Cancelling used to delete only our own pending row, leaving Clerk's invitation
    alive. Two consequences, both silent: the old email kept working as a way in
    after access was withdrawn, and re-inviting the same address hit Clerk's
    duplicate check so no new mail was ever sent.

    Best-effort by design — the caller has already withdrawn access on our side, and
    failing the whole request because Clerk was unreachable would leave the user
    unable to cancel at all.
    """
    import httpx

    out = {"revoked": 0, "error": None}
    target = (email or "").strip().lower()
    if not target:
        return out
    try:
        resp = httpx.get(
            "https://api.clerk.com/v1/invitations",
            headers=headers,
            params={"status": "pending", "limit": 100},
            timeout=10.0,
        )
        if resp.status_code >= 400:
            out["error"] = f"list {resp.status_code}"
            return out
        rows = resp.json()
        if isinstance(rows, dict):
            rows = rows.get("data") or []
        for row in rows or []:
            if str(row.get("email_address", "")).strip().lower() != target:
                continue
            inv_id = row.get("id")
            if not inv_id:
                continue
            r = httpx.post(
                f"https://api.clerk.com/v1/invitations/{inv_id}/revoke",
                headers=headers,
                timeout=10.0,
            )
            if r.status_code < 400:
                out["revoked"] += 1
            else:
                out["error"] = f"revoke {r.status_code}"
    except Exception as e:
        out["error"] = str(e)[:200]
    return out


def revoke_clerk_invitations(email: str) -> dict:
    """Public wrapper: revoke pending Clerk invitations for an address."""
    secret = os.getenv("CLERK_SECRET_KEY", "").strip()
    if not secret:
        return {"revoked": 0, "error": "CLERK_SECRET_KEY is not set"}
    return _clerk_revoke_invitations_for_email(
        email, {"Authorization": f"Bearer {secret}", "Content-Type": "application/json"}
    )


def _clerk_invite_email_to_org(
    email: str, org_id: str, role: str = "viewer", tenant_id: Optional[str] = None
) -> dict:
    """Invite someone to oversee/manage a group by email.

    If they already have a Clerk account, add them to org_members right now — no email
    needed. Otherwise store a pending org invite and send a Clerk invitation; the
    invite is turned into membership the first time they sign in with this email
    (routers/org.get_org_me -> database.db_org_invites_consume_for_emails).

    tenant_id scopes the membership to one store (an invited store manager) instead of
    the whole group. Either way this is additive — nobody is displaced, and the
    invitee keeps every other store they already had.

    Returns {invite_sent, user_added, pending_invite_stored, clerk_error}.
    """
    email = (email or "").strip()
    role = (role or "viewer").strip().lower()
    if not email or "@" not in email:
        return {
            "invite_sent": False,
            "user_added": False,
            "pending_invite_stored": False,
            "clerk_error": "Valid email required",
        }
    lowered = email.lower()
    if lowered.endswith(("@example.com", "@example.org", "@test.com")):
        # Still store the pending invite (a test may sign up later) but be honest that
        # no mail can go to a placeholder address.
        database.db_org_invite_upsert(email, org_id, role, tenant_id)
        return {
            "invite_sent": False,
            "user_added": False,
            "pending_invite_stored": True,
            "clerk_error": f"{email} is a placeholder address and cannot receive mail.",
        }
    clerk_secret = os.getenv("CLERK_SECRET_KEY", "").strip()
    if not clerk_secret:
        database.db_org_invite_upsert(email, org_id, role, tenant_id)
        return {
            "invite_sent": False,
            "user_added": False,
            "pending_invite_stored": True,
            "clerk_error": "CLERK_SECRET_KEY is not set on the backend. Invite email not sent.",
        }
    import httpx

    headers = {"Authorization": f"Bearer {clerk_secret}", "Content-Type": "application/json"}
    # Already have an account? Add them directly — no invite email, immediate access.
    existing = _clerk_user_ids_for_email(email, headers)
    if existing:
        if len(existing) > 1:
            print(f"[Admin] {email!r} matches {len(existing)} Clerk users; adding the first to org.")
        database.db_org_member_add(existing[0], org_id, role, tenant_id)
        deps._admin_access_log("invite_email_to_org", tenant_id=org_id, email=email, user_added=True)
        return {
            "invite_sent": False,
            "user_added": True,
            "pending_invite_stored": False,
            "clerk_error": None,
            "clerk_user_id": existing[0],
        }
    # No account yet: queue the invite and email them.
    database.db_org_invite_upsert(email, org_id, role, tenant_id)
    invite_sent = False
    clerk_error: Optional[str] = None
    try:
        resp = httpx.post(
            "https://api.clerk.com/v1/invitations",
            headers=headers,
            json={
                "email_address": email,
                # Carried for reference only — membership is materialized by matching
                # the verified email at login, not by trusting this metadata.
                "public_metadata": {"org_id": org_id, "org_role": role},
                # Must land on the SIGN-UP page. Clerk appends __clerk_ticket to this
                # URL, and the ticket can only be redeemed by the sign-up flow. This
                # pointed straight at a protected dashboard route, so an invitee with
                # no account was bounced to sign-in and told "new sign ups are
                # currently restricted" — the invite was real and unusable.
                #
                # `next` carries where they belong afterwards: a store manager has no
                # rollup to land on, a group member does.
                "redirect_url": os.getenv("FRONTEND_URL", "https://call-surge.com")
                + "/sign-up?invited=1&next="
                + ("%2Fdashboard" if tenant_id else "%2Fdashboard%2Fstores"),
            },
            timeout=10.0,
        )
        if resp.status_code < 400:
            invite_sent = True
        else:
            body = resp.text or ""
            # A race: the account was created between the lookup and here. Add directly.
            if resp.status_code == 422 and "form_identifier_exists" in body:
                retry = _clerk_user_ids_for_email(email, headers)[:1]
                if retry:
                    database.db_org_member_add(retry[0], org_id, role, tenant_id)
                    database.db_org_invite_delete(email, org_id)
                    return {
                        "invite_sent": False,
                        "user_added": True,
                        "pending_invite_stored": False,
                        "clerk_error": None,
                        "clerk_user_id": retry[0],
                    }
            elif "duplicate_record" in body or "already" in body.lower():
                # A pending invitation from a previous send blocks a new one, so a
                # cancelled address could never be re-invited. Clear it and retry.
                rev = _clerk_revoke_invitations_for_email(email, headers)
                if rev.get("revoked"):
                    again = httpx.post(
                        "https://api.clerk.com/v1/invitations",
                        headers=headers,
                        json={
                            "email_address": email,
                            "public_metadata": {"org_id": org_id, "org_role": role},
                            "redirect_url": os.getenv("FRONTEND_URL", "https://call-surge.com")
                            + "/sign-up?invited=1&next="
                            + ("%2Fdashboard" if tenant_id else "%2Fdashboard%2Fstores"),
                        },
                        timeout=10.0,
                    )
                    if again.status_code < 400:
                        invite_sent = True
                        body = ""
            if not invite_sent:
                clerk_error = _clerk_invite_error_message(resp.status_code, body)
                print(f"[Admin] Clerk org invite failed: {resp.status_code} {body[:200]}")
    except Exception as e:
        clerk_error = str(e)[:240]
        print(f"[Admin] Clerk org invite error: {e}")
    deps._admin_access_log(
        "invite_email_to_org", tenant_id=org_id, email=email, invite_sent=invite_sent,
        clerk_error=(clerk_error or "")[:120] if clerk_error else None,
    )
    return {
        "invite_sent": invite_sent,
        "user_added": False,
        "pending_invite_stored": True,
        "clerk_error": clerk_error,
    }


def _clerk_link_email_to_tenant(email: str, tenant_id: str) -> dict:
    """
    Queue pending invite by email and either re-link an existing Clerk user or send a new invitation.
    When multiple Clerk users share the email, link all of them (common after OAuth + email test accounts).
    """
    email = (email or "").strip()
    if not email or "@" not in email:
        return {
            "invite_sent": False,
            "user_relinked": False,
            "pending_invite_stored": False,
            "clerk_error": "Valid email required",
        }
    lowered = email.lower()
    if (
        lowered.endswith("@example.com")
        or lowered.endswith("@example.org")
        or lowered.endswith("@test.com")
    ):
        return {
            "invite_sent": False,
            "user_relinked": False,
            "pending_invite_stored": True,
            "clerk_error": (
                f"{email} is a placeholder address and cannot receive mail. "
                "Use the client's real email (must match how they sign in)."
            ),
        }
    database.db_tenant_invite_upsert(email, tenant_id)
    invite_sent = False
    user_relinked = False
    clerk_error: Optional[str] = None
    linked_clerk_user_id: Optional[str] = None
    linked_clerk_user_ids: List[str] = []
    clerk_users_matched_count = 0
    clerk_secret = os.getenv("CLERK_SECRET_KEY", "").strip()
    if not clerk_secret:
        return {
            "invite_sent": False,
            "user_relinked": False,
            "pending_invite_stored": True,
            "clerk_error": "CLERK_SECRET_KEY is not set on the backend (Render). Invites cannot be sent.",
        }
    import httpx

    headers = {
        "Authorization": f"Bearer {clerk_secret}",
        "Content-Type": "application/json",
    }
    existing_user_ids = _clerk_user_ids_for_email(email, headers)
    clerk_users_matched_count = len(existing_user_ids)
    if clerk_users_matched_count > 1:
        print(
            f"[Admin] Clerk returned {clerk_users_matched_count} users for {email!r}; "
            "using the first account only (one dashboard user per tenant)"
        )
        clerk_error = (
            clerk_error
            or f"Multiple Clerk accounts share {email}; linked the first only. "
            "Remove duplicate accounts in Clerk or sign in with the linked account."
        )
    link_user_ids = existing_user_ids[:1] if existing_user_ids else []

    def _apply_relink(ids_to_link: List[str]) -> None:
        nonlocal user_relinked, linked_clerk_user_id, linked_clerk_user_ids, clerk_error
        if not ids_to_link:
            return
        linked, displaced_all, relink_err = _clerk_relink_users_to_tenant(
            ids_to_link, tenant_id, email, headers
        )
        if displaced_all:
            print(
                f"[Admin] Removed prior tenant owner(s) from tenant {tenant_id}: "
                f"{', '.join(displaced_all[:5])}"
            )
        if linked:
            database.db_tenant_invite_delete(email)
            user_relinked = True
            linked_clerk_user_ids = linked
            linked_clerk_user_id = linked[0]
            if not relink_err:
                clerk_error = None
            elif not clerk_error:
                clerk_error = relink_err
        elif relink_err and not clerk_error:
            clerk_error = relink_err

    if link_user_ids:
        _apply_relink(link_user_ids)
    else:
        try:
            resp = httpx.post(
                "https://api.clerk.com/v1/invitations",
                headers=headers,
                json={
                    "email_address": email,
                    "public_metadata": {"tenant_id": tenant_id},
                    "redirect_url": os.getenv("FRONTEND_URL", "https://call-surge.com")
                    + "/",
                },
                timeout=10.0,
            )
            if resp.status_code < 400:
                invite_sent = True
            else:
                body = resp.text or ""
                print(f"[Admin] Clerk invite failed: {resp.status_code} {body[:240]}")
                if resp.status_code == 422 and "form_identifier_exists" in body:
                    retry_ids = _clerk_user_ids_for_email(email, headers)[:1]
                    if retry_ids:
                        _apply_relink(retry_ids)
                        if user_relinked:
                            clerk_error = None
                        else:
                            clerk_error = (
                                "Clerk account exists for this email but re-link failed. "
                                "Confirm the email matches sign-in (including Google), then try again."
                            )
                    else:
                        clerk_error = (
                            "Clerk says this email is already registered, but we could not find "
                            "the account to link. Check Clerk Dashboard → Users for the exact email."
                        )
                else:
                    clerk_error = _clerk_invite_error_message(resp.status_code, body)
        except Exception as e:
            clerk_error = str(e)[:240]
            print(f"[Admin] Clerk invite error: {e}")
    result = {
        "invite_sent": invite_sent,
        "user_relinked": user_relinked,
        "pending_invite_stored": True,
        "clerk_error": clerk_error,
        "linked_clerk_user_id": linked_clerk_user_id,
        "linked_clerk_user_ids": linked_clerk_user_ids,
        "clerk_users_matched_count": clerk_users_matched_count,
    }
    deps._admin_access_log(
        "link_email_to_tenant",
        tenant_id=tenant_id,
        email=email,
        invite_sent=invite_sent,
        user_relinked=user_relinked,
        linked_ids=linked_clerk_user_ids,
        clerk_users_matched_count=clerk_users_matched_count,
        clerk_error=(clerk_error or "")[:120] if clerk_error else None,
    )
    if deps._admin_access_debug_enabled():
        result["access_debug"] = _admin_tenant_access_debug_snapshot(tenant_id)
    return result



def _admin_tenant_access_debug_snapshot(tenant_id: str) -> dict:
    """Admin-only: how dashboard access is wired for one tenant."""
    tenant = database.db_tenant_get_by_id(tenant_id) if runtime.USE_DB else None
    if not tenant:
        return {"found": False, "tenant_id": tenant_id}
    tid = str(tenant.get("id") or "")
    pending_invite = database.db_tenant_get_invite_email(tid) if runtime.USE_DB else None
    member_ids = database.db_tenant_get_members(tid) if runtime.USE_DB else []
    clerk_secret = os.getenv("CLERK_SECRET_KEY", "").strip()
    headers = (
        {"Authorization": f"Bearer {clerk_secret}", "Content-Type": "application/json"}
        if clerk_secret
        else None
    )
    members_debug: List[dict] = []
    for uid in member_ids:
        link = deps._clerk_fetch_user_link(uid) if headers else None
        memberships = database.db_tenant_memberships_for_user(uid) if runtime.USE_DB else []
        members_debug.append(
            {
                "clerk_user_id": uid,
                "clerk_emails": (link or {}).get("emails") or [],
                "clerk_metadata_tenant_id": (link or {}).get("tenant_id"),
                "all_db_memberships": memberships,
            }
        )
    clerk_api_ids: List[str] = []
    lookup_source = None
    if pending_invite and headers:
        clerk_api_ids = _clerk_user_ids_from_api(pending_invite, headers)
        lookup_source = "clerk_api" if clerk_api_ids else None
    if pending_invite and headers and not clerk_api_ids:
        clerk_api_ids = _clerk_user_ids_from_tenant_members(pending_invite, headers)
        lookup_source = "tenant_members_scan" if clerk_api_ids else lookup_source
    return {
        "found": True,
        "tenant_id": tid,
        "client_id": tenant.get("client_id"),
        "name": tenant.get("name"),
        "twilio_phone_number": tenant.get("twilio_phone_number"),
        "pending_invite_email": pending_invite,
        "member_clerk_user_ids": member_ids,
        "members": members_debug,
        "clerk_lookup_for_pending_email": {
            "email": pending_invite,
            "user_ids": clerk_api_ids,
            "source": lookup_source,
        },
        "one_email_per_tenant": True,
    }
