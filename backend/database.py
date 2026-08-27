"""
PostgreSQL database layer for production. Used when DATABASE_URL is set.
Tables: appointments, messages, call_log, caller_memory, booked_slots, tenants, tenant_members
"""
import os
import json
import hashlib
import uuid
import contextvars
import logging
import threading
import time as _time
from datetime import date, datetime, timedelta, timezone
from typing import Any, Optional, List, Tuple
from pathlib import Path

# Request-scoped client_id (set by auth middleware or webhook)
_request_client_id: contextvars.ContextVar[Optional[str]] = contextvars.ContextVar("request_client_id", default=None)
_log = logging.getLogger("nuvatra")

def set_request_client_id(client_id: Optional[str]) -> None:
    """Set the current request's client_id for all DB operations in this context."""
    if client_id is not None:
        _request_client_id.set(client_id)
    else:
        try:
            _request_client_id.set(None)
        except LookupError:
            pass

def clear_request_client_id() -> None:
    """Clear the request client_id context (e.g. after request completes)."""
    try:
        _request_client_id.reset(_request_client_id.get())
    except LookupError:
        pass

class DatabaseUnavailable(RuntimeError):
    """The query could not run — as distinct from running and finding nothing.

    Returning [] for both is what let a pool blip render as "Group not found /
    0 stores" on a group with two stores, at HTTP 200. Read paths that a human
    reads a COUNT off should raise this instead.
    """


# Connection pool (ThreadedConnectionPool) — one borrowed conn per thread per request
_pool = None
_use_db = False
_thread_local = threading.local()


def _ensure_pool():
    global _pool
    if _pool is not None:
        return _pool
    url = os.getenv("DATABASE_URL")
    if not url:
        return None
    from psycopg2 import pool

    minconn = max(1, int((os.getenv("DB_POOL_MIN") or "2").strip()))
    maxconn = max(minconn, int((os.getenv("DB_POOL_MAX") or "10").strip()))
    _pool = pool.ThreadedConnectionPool(minconn, maxconn, url, connect_timeout=10)
    return _pool


# psycopg2's pool raises the moment it is empty rather than waiting, so a burst of
# concurrent requests fails instantly instead of queueing for the connection that is
# about to come back. A short bounded wait absorbs the burst; past it we still fail
# rather than pile up unbounded.
_POOL_WAIT_SECONDS = float((os.getenv("DB_POOL_WAIT_SECONDS") or "3").strip() or 3)


def _getconn_waiting(pool):
    """pool.getconn(), retried briefly while the pool is exhausted."""
    deadline = _time.monotonic() + _POOL_WAIT_SECONDS
    waited = False
    while True:
        try:
            conn = pool.getconn()
            if waited:
                _log.info("db_pool_getconn_recovered after_wait=true")
            return conn
        except Exception:
            if _time.monotonic() >= deadline:
                raise
            waited = True
            _time.sleep(0.05)


def _get_conn():
    global _use_db
    if not _use_db:
        return None
    conn = getattr(_thread_local, "conn", None)
    if conn is not None and not conn.closed:
        return conn  # already validated earlier this request
    pool = _ensure_pool()
    if not pool:
        return None
    # Pool pre-ping: a pooled connection may have been dropped server-side while
    # idle (Render Starter Postgres closes idle conns), and psycopg2's conn.closed
    # won't catch that. Validate with a cheap SELECT 1 on checkout; if it's dead,
    # discard and try one more. Runs once per request — the live connection is then
    # cached in _thread_local for the rest of the request's queries.
    last_err = None
    for _ in range(2):
        try:
            conn = _getconn_waiting(pool)
        except Exception as e:
            _log.warning(
                "db_pool_getconn_failed after %.1fs: %s | pool_max=%s in_use=%s",
                _POOL_WAIT_SECONDS, e, getattr(pool, "maxconn", "?"),
                len(getattr(pool, "_used", {}) or {}),
            )
            return None
        try:
            cur = conn.cursor()
            cur.execute("SELECT 1")
            cur.close()
            _thread_local.conn = conn
            return conn
        except Exception as e:
            last_err = e
            try:
                pool.putconn(conn, close=True)
            except Exception:
                try:
                    conn.close()
                except Exception:
                    pass
    _log.warning("db_get_conn_preping_failed: %s", last_err)
    return None


def db_release_thread_connection() -> None:
    """Return borrowed connection to pool (call after HTTP request or background DB work)."""
    conn = getattr(_thread_local, "conn", None)
    if conn is None:
        return
    pool = _ensure_pool()
    if pool:
        try:
            pool.putconn(conn)
        except Exception as e:
            _log.warning("db_pool_putconn_failed: %s", e)
    _thread_local.conn = None

def _discard_thread_connection() -> None:
    """Drop the thread-local connection from the pool without reusing it.

    Needed because psycopg2's `conn.closed` only reflects client-side closure —
    a connection the server silently dropped (idle timeout, restart) still reads
    as open and fails on first use. We discard it so the next _get_conn() borrows
    a fresh one.
    """
    conn = getattr(_thread_local, "conn", None)
    _thread_local.conn = None
    if conn is None:
        return
    try:
        pool = _ensure_pool()
        if pool:
            pool.putconn(conn, close=True)
        else:
            conn.close()
    except Exception:
        try:
            conn.close()
        except Exception:
            pass


# ---------------------------------------------------------------------------
# Per-call connection scoping  (the cross-request transaction-bleed fix)
# ---------------------------------------------------------------------------
# The hazard: async request handlers call the sync db_* functions directly on the
# event-loop thread. The old model cached the borrowed connection in _thread_local
# and only released it at request end (middleware), so a connection HELD ACROSS AN
# `await` was visible to whatever other request ran on the loop thread in the
# meantime — cross-request transaction bleed (see docs/DB-CONCURRENCY.md).
#
# The fix: every *top-level* db_* call borrows a dedicated pooled connection and
# returns it — rolled back — the instant it returns. A db_* body is synchronous and
# contains no `await`, so the connection is never held across a yield point; concurrent
# async requests therefore can never share a live connection. Borrow and release happen
# in the SAME synchronous call frame (the wrapper's finally), so there is no reliance on
# framework/middleware teardown running in the right context — the exact failure mode of
# the earlier contextvar attempt. Reentrant depth-counting lets nested db_* calls (a db_*
# that calls another) share one connection; only the outermost frame releases.
import functools as _functools

# db_-prefixed names that are NOT query functions and must not be scope-wrapped.
_SCOPE_EXCLUDE = {"db_release_thread_connection"}


def _conn_scope_exit() -> None:
    """Return the call's borrowed connection to the pool, rolled back to clear any
    open read snapshot / aborted transaction so the next borrower starts clean."""
    conn = getattr(_thread_local, "conn", None)
    _thread_local.conn = None
    if conn is None:
        return
    pool = _ensure_pool()
    try:
        if not conn.closed:
            conn.rollback()  # committed writes already persisted; clears idle-in-tx reads
    except Exception:
        # Connection is unusable — discard it rather than return a poisoned conn.
        try:
            if pool:
                pool.putconn(conn, close=True)
            else:
                conn.close()
        except Exception:
            pass
        return
    if pool:
        try:
            pool.putconn(conn)
        except Exception as e:
            _log.warning("db_pool_putconn_failed: %s", e)
    else:
        try:
            conn.close()
        except Exception:
            pass


def _scoped(fn):
    """Wrap a db_* query function so each top-level call borrows + releases its own
    connection. Reentrant: nested db_* calls share the outermost frame's connection."""

    @_functools.wraps(fn)
    def wrapper(*args, **kwargs):
        if not _use_db:
            return fn(*args, **kwargs)  # in-memory / no-DB path borrows nothing
        depth = getattr(_thread_local, "depth", 0)
        _thread_local.depth = depth + 1
        try:
            return fn(*args, **kwargs)
        finally:
            new_depth = getattr(_thread_local, "depth", 1) - 1
            _thread_local.depth = max(0, new_depth)
            if new_depth <= 0:
                _conn_scope_exit()

    wrapper._db_scoped = True
    return wrapper


def db_ping() -> bool:
    """Return True if DB is reachable (for health check).

    Retries once with a fresh connection so a stale pooled connection (Render
    Starter Postgres closes idle connections) doesn't produce a false 503 and
    flap the instance. A genuine outage still fails both attempts -> False.
    """
    for _ in range(2):
        conn = _get_conn()
        if not conn:
            return False
        try:
            cur = conn.cursor()
            cur.execute("SELECT 1")
            cur.close()
            return True
        except Exception:
            _discard_thread_connection()
    return False

def init_db() -> bool:
    """Initialize database: create tables if not exist. Returns True if DB is used.

    NOTE: Schema changes now go through Alembic, not here — do not add new
    CREATE TABLE / ALTER TABLE statements to this function. Add an Alembic
    revision instead (see docs/MIGRATIONS.md). The Alembic 0001_baseline
    revision mirrors the DDL below exactly.
    """
    global _use_db
    url = os.getenv("DATABASE_URL")
    if not url:
        _log.error("db_init_no_url — DATABASE_URL is not set; starting without a database")
        return False
    try:
        import psycopg2
        print("[DB] Connecting to PostgreSQL...")
        # Retry the FIRST connect. USE_DB is decided once, at startup, and never
        # revisited — so a single slow connect here disables the database for the
        # life of the process even after it comes back. That is exactly what a cold
        # free-tier Postgres does: staging booted while its database was waking,
        # timed out at 10s, and then reported "no database" for hours with the
        # database sitting there healthy. Worth waiting ~30s at boot to avoid.
        conn = None
        last_err = None
        for attempt in range(4):
            try:
                conn = psycopg2.connect(url, connect_timeout=10)
                if attempt:
                    _log.warning("db_init_connected_after_retries attempts=%s", attempt + 1)
                break
            except Exception as e:
                last_err = e
                _log.warning(
                    "db_init_connect_failed attempt=%s err=%s: %s",
                    attempt + 1, type(e).__name__, e,
                )
                if attempt < 3:
                    _time.sleep(2 * (attempt + 1))
        if conn is None:
            raise last_err if last_err else RuntimeError("could not connect")
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE IF NOT EXISTS appointments (
                id SERIAL PRIMARY KEY,
                client_id TEXT NOT NULL DEFAULT 'default',
                name TEXT NOT NULL,
                email TEXT,
                phone TEXT,
                date TEXT NOT NULL,
                time TEXT,
                reason TEXT,
                status TEXT NOT NULL DEFAULT 'pending',
                source TEXT DEFAULT 'manual',
                created_at TIMESTAMPTZ DEFAULT NOW()
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS messages (
                id SERIAL PRIMARY KEY,
                client_id TEXT NOT NULL DEFAULT 'default',
                caller_name TEXT,
                caller_phone TEXT,
                message TEXT,
                urgency TEXT DEFAULT 'normal',
                status TEXT DEFAULT 'unread',
                created_at TIMESTAMPTZ DEFAULT NOW()
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS call_log (
                id SERIAL PRIMARY KEY,
                client_id TEXT NOT NULL DEFAULT 'default',
                call_sid TEXT UNIQUE NOT NULL,
                from_number TEXT,
                to_number TEXT,
                start_iso TEXT,
                end_iso TEXT,
                outcome TEXT,
                duration_sec INTEGER,
                category TEXT,
                created_at TIMESTAMPTZ DEFAULT NOW()
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS caller_memory (
                phone TEXT NOT NULL,
                client_id TEXT NOT NULL DEFAULT 'default',
                name TEXT,
                call_count INTEGER DEFAULT 0,
                last_call_iso TEXT,
                last_reason TEXT,
                data JSONB,
                updated_at TIMESTAMPTZ DEFAULT NOW(),
                PRIMARY KEY (phone, client_id)
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS booked_slots (
                id SERIAL PRIMARY KEY,
                client_id TEXT NOT NULL DEFAULT 'default',
                date TEXT NOT NULL,
                time TEXT NOT NULL,
                appointment_id INTEGER NOT NULL,
                duration_minutes INTEGER DEFAULT 30,
                created_at TIMESTAMPTZ DEFAULT NOW()
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS tenants (
                id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                client_id TEXT UNIQUE NOT NULL,
                name TEXT NOT NULL,
                twilio_phone_number TEXT NOT NULL,
                plan TEXT NOT NULL DEFAULT 'starter',
                created_at TIMESTAMPTZ DEFAULT NOW()
            )
        """)
        # Migration: add subscription/trial columns if missing (PostgreSQL 9.5+)
        for col, typ in [
            ("trial_ends_at", "TIMESTAMPTZ"),
            ("subscription_status", "TEXT"),
            ("stripe_customer_id", "TEXT"),
            ("stripe_subscription_id", "TEXT"),
            ("billing_exempt_until", "TIMESTAMPTZ"),
            ("business_vertical", "TEXT"),
            ("billing_period_anchor_at", "TIMESTAMPTZ"),
            ("business_config", "JSONB"),
            ("account_paused", "BOOLEAN NOT NULL DEFAULT FALSE"),
            ("twilio_number_sid", "TEXT"),
            ("demo_mode", "BOOLEAN NOT NULL DEFAULT FALSE"),
        ]:
            try:
                cur.execute(f"ALTER TABLE tenants ADD COLUMN IF NOT EXISTS {col} {typ}")
            except Exception:
                pass
        # Self-serve signup creates a pending tenant before its number is provisioned
        # (the number arrives on Stripe checkout completion), so the column is nullable.
        try:
            cur.execute("ALTER TABLE tenants ALTER COLUMN twilio_phone_number DROP NOT NULL")
        except Exception:
            pass
        try:
            cur.execute(
                "UPDATE tenants SET business_vertical = 'salon_chair' WHERE business_vertical IS NULL OR trim(business_vertical) = ''"
            )
        except Exception:
            pass
        cur.execute("""
            CREATE TABLE IF NOT EXISTS tenant_members (
                clerk_user_id TEXT NOT NULL,
                tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
                role TEXT NOT NULL DEFAULT 'member',
                created_at TIMESTAMPTZ DEFAULT NOW(),
                PRIMARY KEY (clerk_user_id, tenant_id)
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS tenant_invites (
                email TEXT PRIMARY KEY,
                tenant_id UUID NOT NULL REFERENCES tenants(id) ON DELETE CASCADE,
                created_at TIMESTAMPTZ DEFAULT NOW()
            )
        """)
        # Multi-store oversight: an org groups stores, org_members says who may watch
        # them. Deliberately separate from tenant_members — an org member is not a
        # member of any store, so the one-user-one-tenant rules below stay intact.
        cur.execute("""
            CREATE TABLE IF NOT EXISTS orgs (
                id UUID PRIMARY KEY DEFAULT gen_random_uuid(),
                name TEXT NOT NULL,
                created_at TIMESTAMPTZ DEFAULT NOW()
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS org_members (
                clerk_user_id TEXT NOT NULL,
                org_id UUID NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
                role TEXT NOT NULL DEFAULT 'viewer',
                created_at TIMESTAMPTZ DEFAULT NOW(),
                PRIMARY KEY (clerk_user_id, org_id)
            )
        """)
        # Invite an org overseer/manager by email before they have an account. Mirrors
        # tenant_invites; consumed at first matching sign-in (routers/org.get_org_me).
        cur.execute("""
            CREATE TABLE IF NOT EXISTS org_invites (
                email TEXT NOT NULL,
                org_id UUID NOT NULL REFERENCES orgs(id) ON DELETE CASCADE,
                role TEXT NOT NULL DEFAULT 'viewer',
                created_at TIMESTAMPTZ DEFAULT NOW(),
                PRIMARY KEY (email, org_id)
            )
        """)
        try:
            cur.execute("CREATE INDEX IF NOT EXISTS idx_org_invites_email ON org_invites(email)")
        except Exception:
            pass
        # An org membership is normally the whole group (a regional manager). Setting
        # tenant_id narrows it to one store, which is what a store manager gets — see
        # 0012_org_member_store_scope for why they aren't tenant_members.
        for tbl in ("org_members", "org_invites"):
            try:
                cur.execute(
                    f"ALTER TABLE {tbl} ADD COLUMN IF NOT EXISTS tenant_id UUID "
                    "REFERENCES tenants(id) ON DELETE CASCADE"
                )
            except Exception:
                pass
        try:
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_org_members_tenant ON org_members(tenant_id)"
            )
        except Exception:
            pass
        # Org-level billing: one subscription covering N stores. Mirrors the tenant
        # billing columns so subscription_access can judge an org with the same rules.
        for col, typ in [
            ("price_overrides", "JSONB"),
            ("plan", "TEXT NOT NULL DEFAULT 'pro'"),
            ("subscription_status", "TEXT"),
            ("stripe_customer_id", "TEXT"),
            ("stripe_subscription_id", "TEXT"),
            ("trial_ends_at", "TIMESTAMPTZ"),
            ("billing_exempt_until", "TIMESTAMPTZ"),
        ]:
            try:
                cur.execute(f"ALTER TABLE orgs ADD COLUMN IF NOT EXISTS {col} {typ}")
            except Exception:
                pass
        try:
            # Every group needs an owner. This lives here, not only in Alembic,
            # because schema in this project arrives through init_db's
            # ADD COLUMN IF NOT EXISTS — Alembic is not run on deploy, so a
            # migration that adds a column still lands while one that BACKFILLS
            # DATA silently does nothing. 0014 shipped and no owner ever appeared.
            #
            # Idempotent and self-healing by construction: it only touches orgs that
            # have no owner, and promotes the longest-standing whole-group manager —
            # the same call we would make by hand. An org that already has an owner
            # is never altered, so re-running on every boot is a no-op.
            cur.execute(
                """
                WITH ownerless AS (
                    SELECT o.id FROM orgs o
                    WHERE NOT EXISTS (
                        SELECT 1 FROM org_members m
                        WHERE m.org_id = o.id AND m.role = 'owner' AND m.tenant_id IS NULL
                    )
                ), promote AS (
                    SELECT DISTINCT ON (m.org_id) m.org_id, m.clerk_user_id
                    FROM org_members m
                    JOIN ownerless w ON w.id = m.org_id
                    WHERE m.role = 'manager' AND m.tenant_id IS NULL
                    ORDER BY m.org_id, m.created_at ASC, m.clerk_user_id ASC
                )
                UPDATE org_members t SET role = 'owner'
                FROM promote p
                WHERE t.org_id = p.org_id AND t.clerk_user_id = p.clerk_user_id
                  AND t.tenant_id IS NULL
                """
            )
            if cur.rowcount:
                print(f"[DB] promoted {cur.rowcount} group(s) to have an owner", flush=True)
        except Exception as e:
            print(f"[DB] owner backfill skipped: {e}", flush=True)
        try:
            # NULL org_id = an independent store (every tenant, until grouped).
            cur.execute(
                "ALTER TABLE tenants ADD COLUMN IF NOT EXISTS org_id UUID "
                "REFERENCES orgs(id) ON DELETE SET NULL"
            )
            cur.execute("CREATE INDEX IF NOT EXISTS idx_tenants_org ON tenants(org_id)")
            cur.execute(
                "CREATE INDEX IF NOT EXISTS idx_org_members_user ON org_members(clerk_user_id)"
            )
        except Exception:
            pass
        try:
            cur.execute(
                """
                DELETE FROM tenant_invites a
                USING tenant_invites b
                WHERE a.tenant_id = b.tenant_id
                  AND a.email <> b.email
                  AND (
                    a.created_at < b.created_at
                    OR (
                      a.created_at IS NOT DISTINCT FROM b.created_at
                      AND a.email < b.email
                    )
                  )
                """
            )
            cur.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_tenant_invites_one_email_per_tenant "
                "ON tenant_invites (tenant_id)"
            )
        except Exception:
            pass
        _dedupe_tenant_members_one_per_tenant(cur)
        try:
            cur.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_tenant_members_one_per_tenant "
                "ON tenant_members (tenant_id)"
            )
        except Exception:
            pass
        cur.execute("CREATE INDEX IF NOT EXISTS idx_tenants_twilio_phone ON tenants(twilio_phone_number)")
        for col, typ in [
            ("recording_sid", "TEXT"),
            ("recording_url", "TEXT"),
            ("recording_duration_sec", "INTEGER"),
            ("recording_status", "TEXT"),
            ("call_summary", "TEXT"),
        ]:
            try:
                cur.execute(f"ALTER TABLE call_log ADD COLUMN IF NOT EXISTS {col} {typ}")
            except Exception:
                pass
        cur.execute("""
            CREATE TABLE IF NOT EXISTS sms_sessions (
                phone TEXT NOT NULL,
                client_id TEXT NOT NULL,
                messages JSONB DEFAULT '[]',
                appointment_id INT,
                updated_at TIMESTAMPTZ DEFAULT NOW(),
                PRIMARY KEY (phone, client_id)
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS sms_opt_out (
                phone TEXT NOT NULL,
                client_id TEXT NOT NULL,
                created_at TIMESTAMPTZ DEFAULT NOW(),
                PRIMARY KEY (phone, client_id)
            )
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_sms_opt_out_client ON sms_opt_out(client_id)")
        cur.execute("""
            CREATE TABLE IF NOT EXISTS sms_consent (
                id BIGSERIAL PRIMARY KEY,
                phone TEXT NOT NULL,
                client_id TEXT NOT NULL DEFAULT 'default',
                consent_type TEXT NOT NULL DEFAULT 'service',
                source TEXT NOT NULL,
                detail JSONB,
                ip TEXT,
                created_at TIMESTAMPTZ DEFAULT NOW()
            )
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_sms_consent_phone_client ON sms_consent(phone, client_id, created_at DESC)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_sms_consent_client_created ON sms_consent(client_id, created_at DESC)")
        cur.execute("""
            CREATE TABLE IF NOT EXISTS audit_events (
                id BIGSERIAL PRIMARY KEY,
                occurred_at TIMESTAMPTZ DEFAULT NOW(),
                actor_type TEXT NOT NULL,
                actor_id TEXT,
                action TEXT NOT NULL,
                resource_type TEXT,
                resource_id TEXT,
                client_id TEXT,
                details JSONB,
                ip TEXT,
                request_id TEXT
            )
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_audit_events_client_occurred ON audit_events(client_id, occurred_at)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_audit_events_action_occurred ON audit_events(action, occurred_at)")
        cur.execute("""
            CREATE TABLE IF NOT EXISTS tenant_removed_archive (
                id BIGSERIAL PRIMARY KEY,
                archived_at TIMESTAMPTZ DEFAULT NOW(),
                former_tenant_id UUID NOT NULL,
                client_id TEXT NOT NULL,
                actor_clerk_id TEXT,
                bundle JSONB NOT NULL
            )
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_tenant_removed_archive_client ON tenant_removed_archive(client_id)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_tenant_removed_archive_time ON tenant_removed_archive(archived_at)")
        cur.execute("""
            CREATE TABLE IF NOT EXISTS legal_holds (
                id BIGSERIAL PRIMARY KEY,
                client_id TEXT NOT NULL,
                reason TEXT,
                hold_until TIMESTAMPTZ,
                created_by TEXT,
                created_at TIMESTAMPTZ DEFAULT NOW(),
                updated_at TIMESTAMPTZ DEFAULT NOW(),
                UNIQUE(client_id)
            )
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_legal_holds_client ON legal_holds(client_id)")
        cur.execute("""
            CREATE TABLE IF NOT EXISTS backup_exports (
                id BIGSERIAL PRIMARY KEY,
                export_key TEXT NOT NULL UNIQUE,
                destination_path TEXT NOT NULL,
                sha256 TEXT NOT NULL,
                created_at TIMESTAMPTZ DEFAULT NOW()
            )
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_backup_exports_created ON backup_exports(created_at)")
        # Plan tier tables
        cur.execute("""
            CREATE TABLE IF NOT EXISTS tenant_usage (
                id SERIAL PRIMARY KEY,
                client_id TEXT NOT NULL,
                month TEXT NOT NULL CHECK (month ~ '^\\d{4}-\\d{2}$'),
                voice_minutes INTEGER NOT NULL DEFAULT 0 CHECK (voice_minutes >= 0),
                sms_count INTEGER NOT NULL DEFAULT 0 CHECK (sms_count >= 0),
                created_at TIMESTAMPTZ DEFAULT NOW(),
                updated_at TIMESTAMPTZ DEFAULT NOW(),
                UNIQUE(client_id, month)
            )
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_tenant_usage_client_month ON tenant_usage(client_id, month)")
        cur.execute("""
            CREATE TABLE IF NOT EXISTS leads (
                id SERIAL PRIMARY KEY,
                client_id TEXT NOT NULL,
                name TEXT,
                phone TEXT NOT NULL,
                reason TEXT,
                source TEXT CHECK (source IN ('call', 'sms')),
                created_at TIMESTAMPTZ DEFAULT NOW()
            )
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_leads_client_created ON leads(client_id, created_at DESC)")
        cur.execute("""
            CREATE TABLE IF NOT EXISTS feedback (
                id SERIAL PRIMARY KEY,
                client_id TEXT,
                user_email TEXT,
                category TEXT NOT NULL DEFAULT 'other' CHECK (category IN ('bug', 'idea', 'other')),
                message TEXT NOT NULL,
                page_url TEXT,
                user_agent TEXT,
                status TEXT NOT NULL DEFAULT 'new',
                created_at TIMESTAMPTZ DEFAULT NOW()
            )
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_feedback_created ON feedback(created_at DESC)")
        cur.execute("""
            CREATE TABLE IF NOT EXISTS sms_automations (
                id SERIAL PRIMARY KEY,
                client_id TEXT NOT NULL,
                trigger TEXT NOT NULL CHECK (trigger IN ('after_inquiry', 'post_call')),
                template TEXT NOT NULL,
                enabled BOOLEAN DEFAULT true,
                created_at TIMESTAMPTZ DEFAULT NOW()
            )
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_sms_automations_client ON sms_automations(client_id)")
        cur.execute("""
            CREATE TABLE IF NOT EXISTS overage_processed (
                client_id TEXT NOT NULL,
                month TEXT NOT NULL CHECK (month ~ '^\\d{4}-\\d{2}$'),
                created_at TIMESTAMPTZ DEFAULT NOW(),
                PRIMARY KEY (client_id, month)
            )
        """)
        # Dedup guard so a usage-cap alert fires at most once per tenant per month.
        cur.execute("""
            CREATE TABLE IF NOT EXISTS usage_alert_sent (
                client_id TEXT NOT NULL,
                month TEXT NOT NULL CHECK (month ~ '^\\d{4}-\\d{2}$'),
                created_at TIMESTAMPTZ DEFAULT NOW(),
                PRIMARY KEY (client_id, month)
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS conversational_sms_period_usage (
                client_id TEXT NOT NULL,
                billing_period_key TEXT NOT NULL,
                session_count INTEGER NOT NULL DEFAULT 0 CHECK (session_count >= 0),
                updated_at TIMESTAMPTZ DEFAULT NOW(),
                PRIMARY KEY (client_id, billing_period_key)
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS conversational_sms_session_keys (
                client_id TEXT NOT NULL,
                billing_period_key TEXT NOT NULL,
                phone_normalized TEXT NOT NULL,
                created_at TIMESTAMPTZ DEFAULT NOW(),
                PRIMARY KEY (client_id, billing_period_key, phone_normalized)
            )
        """)
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_conv_sms_sessions_client_period "
            "ON conversational_sms_session_keys(client_id, billing_period_key)"
        )
        # --- Referral program ---
        # Global anti-abuse ledger: one row per completed signup (referred or not), so a
        # card/email reused across ANY prior signup can be detected.
        cur.execute("""
            CREATE TABLE IF NOT EXISTS signup_payment_methods (
                id BIGSERIAL PRIMARY KEY,
                tenant_id TEXT,
                card_fingerprint TEXT,
                signup_email TEXT,
                created_at TIMESTAMPTZ DEFAULT NOW()
            )
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_signup_pm_fingerprint ON signup_payment_methods(card_fingerprint)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_signup_pm_email ON signup_payment_methods(signup_email)")
        cur.execute("""
            CREATE TABLE IF NOT EXISTS referral_codes (
                id BIGSERIAL PRIMARY KEY,
                code TEXT NOT NULL UNIQUE,
                referrer_name TEXT NOT NULL,
                referrer_contact TEXT,
                active BOOLEAN NOT NULL DEFAULT TRUE,
                created_by TEXT,
                created_at TIMESTAMPTZ DEFAULT NOW()
            )
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_referral_codes_active ON referral_codes(active)")
        cur.execute("""
            CREATE TABLE IF NOT EXISTS referral_redemptions (
                id BIGSERIAL PRIMARY KEY,
                tenant_id TEXT NOT NULL UNIQUE,
                referral_code_id BIGINT REFERENCES referral_codes(id) ON DELETE SET NULL,
                code_snapshot TEXT NOT NULL,
                referrer_name_snapshot TEXT NOT NULL,
                plan_at_signup TEXT,
                card_fingerprint TEXT,
                signup_email TEXT,
                status TEXT NOT NULL DEFAULT 'pending'
                    CHECK (status IN ('pending','granted','flagged','converted')),
                free_month_granted BOOLEAN NOT NULL DEFAULT FALSE,
                flagged_reason TEXT,
                stripe_subscription_id TEXT,
                first_paid_at TIMESTAMPTZ,
                created_at TIMESTAMPTZ DEFAULT NOW()
            )
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_referral_redemptions_sub ON referral_redemptions(stripe_subscription_id)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_referral_redemptions_fp ON referral_redemptions(card_fingerprint)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_referral_redemptions_email ON referral_redemptions(signup_email)")
        cur.execute("""
            CREATE TABLE IF NOT EXISTS referral_commissions (
                id BIGSERIAL PRIMARY KEY,
                redemption_id BIGINT REFERENCES referral_redemptions(id) ON DELETE SET NULL,
                kind TEXT NOT NULL CHECK (kind IN ('signup_bounty','mrr')),
                period_key TEXT NOT NULL,
                amount_cents INTEGER NOT NULL CHECK (amount_cents >= 0),
                plan_snapshot TEXT,
                code_snapshot TEXT NOT NULL,
                referrer_name_snapshot TEXT NOT NULL,
                paid BOOLEAN NOT NULL DEFAULT FALSE,
                paid_at TIMESTAMPTZ,
                created_at TIMESTAMPTZ DEFAULT NOW(),
                UNIQUE (redemption_id, kind, period_key)
            )
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_referral_commissions_unpaid ON referral_commissions(paid)")
        cur.execute("CREATE INDEX IF NOT EXISTS idx_referral_commissions_redemption ON referral_commissions(redemption_id)")
        # Dead-letter / incident log: swallowed failures (webhooks, crons, tasks) land here
        # so they're visible + retryable instead of disappearing into logs.
        cur.execute("""
            CREATE TABLE IF NOT EXISTS failed_events (
                id BIGSERIAL PRIMARY KEY,
                source TEXT NOT NULL,
                event_type TEXT,
                ref TEXT,
                error TEXT,
                payload JSONB,
                resolved BOOLEAN NOT NULL DEFAULT FALSE,
                resolved_at TIMESTAMPTZ,
                created_at TIMESTAMPTZ DEFAULT NOW()
            )
        """)
        cur.execute("CREATE INDEX IF NOT EXISTS idx_failed_events_unresolved ON failed_events(resolved, created_at DESC)")
        cur.execute("""
            CREATE TABLE IF NOT EXISTS cron_runs (
                id SERIAL PRIMARY KEY,
                job_name TEXT NOT NULL,
                started_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                finished_at TIMESTAMPTZ,
                status TEXT NOT NULL DEFAULT 'running',
                summary JSONB
            )
        """)
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_cron_runs_job_finished "
            "ON cron_runs(job_name, finished_at DESC)"
        )
        cur.execute("""
            CREATE TABLE IF NOT EXISTS provisioning_jobs (
                id TEXT PRIMARY KEY,
                created_by TEXT,
                total INTEGER NOT NULL DEFAULT 0,
                status TEXT NOT NULL DEFAULT 'pending',
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """)
        cur.execute("""
            CREATE TABLE IF NOT EXISTS provisioning_tasks (
                id SERIAL PRIMARY KEY,
                job_id TEXT NOT NULL,
                client_id TEXT NOT NULL,
                name TEXT,
                email TEXT,
                area_code TEXT,
                plan TEXT NOT NULL DEFAULT 'free',
                status TEXT NOT NULL DEFAULT 'pending',
                phone_e164 TEXT,
                steps_done JSONB NOT NULL DEFAULT '[]'::jsonb,
                error TEXT,
                attempts INTEGER NOT NULL DEFAULT 0,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            )
        """)
        cur.execute(
            "CREATE INDEX IF NOT EXISTS idx_provisioning_tasks_job "
            "ON provisioning_tasks(job_id, status)"
        )
        try:
            cur.execute(
                "UPDATE tenants SET billing_period_anchor_at = created_at "
                "WHERE billing_period_anchor_at IS NULL"
            )
        except Exception:
            pass
        try:
            cur.execute("ALTER TABLE appointments ADD COLUMN IF NOT EXISTS reminder_sent_at TIMESTAMPTZ")
        except Exception:
            pass
        for col, typ in [
            ("staff_id", "TEXT"),
            ("owner_decline_reason", "TEXT"),
            ("confirmation_sms_failed", "BOOLEAN NOT NULL DEFAULT false"),
        ]:
            try:
                cur.execute(f"ALTER TABLE appointments ADD COLUMN IF NOT EXISTS {col} {typ}")
            except Exception:
                pass
        try:
            cur.execute("ALTER TABLE booked_slots ADD COLUMN IF NOT EXISTS staff_id TEXT")
        except Exception:
            pass
        # Concurrency: a unique calendar hold per (tenant, date, time, staff) so two
        # simultaneous bookings can never double-book the same slot. Dedupe any existing
        # duplicates first (keep the lowest id) or the unique index creation would fail.
        try:
            cur.execute(
                """
                DELETE FROM booked_slots a USING booked_slots b
                WHERE a.id < b.id
                  AND a.client_id = b.client_id AND a.date = b.date AND a.time = b.time
                  AND COALESCE(a.staff_id, '') = COALESCE(b.staff_id, '')
                """
            )
            cur.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_booked_slots_unique "
                "ON booked_slots (client_id, date, time, (COALESCE(staff_id, '')))"
            )
        except Exception:
            pass
        cur.execute("CREATE INDEX IF NOT EXISTS idx_appointments_status_date ON appointments(client_id, status)")
        conn.commit()
        cur.close()
        conn.close()
        _use_db = True
        print("[DB] PostgreSQL initialized successfully")
        return True
    except Exception as e:
        print(f"[DB] Failed to init PostgreSQL: {e}")
        return False

def _client_id() -> str:
    """Current request's client_id from context, or CLIENT_ID env, or 'default'."""
    ctx = _request_client_id.get(None)
    if ctx:
        return ctx
    return os.getenv("CLIENT_ID", "").strip() or "default"

def _normalize_e164(phone: str) -> str:
    """Convert to E.164 for Twilio lookup."""
    d = "".join(c for c in (phone or "") if c.isdigit())
    if len(d) == 10:
        return f"+1{d}"
    if len(d) == 11 and d.startswith("1"):
        return f"+{d}"
    return f"+{d}" if d else phone

# --- Tenants ---
def db_tenant_create(
    client_id: str,
    name: str,
    twilio_phone_number: str,
    plan: str = "free",
    business_vertical: str = "salon_chair",
) -> Optional[dict]:
    """Create a tenant with 7-day trial. Returns tenant dict or None on conflict."""
    conn = _get_conn()
    if not conn:
        return None
    phone_store = _normalize_e164((twilio_phone_number or "").strip())
    if not phone_store or not any(c.isdigit() for c in phone_store):
        print("[DB] db_tenant_create: invalid twilio_phone_number")
        return None
    try:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO tenants (client_id, name, twilio_phone_number, plan, subscription_status, trial_ends_at, business_vertical)
            VALUES (%s, %s, %s, %s, 'trialing', NOW() + INTERVAL '7 days', %s)
            ON CONFLICT (client_id) DO NOTHING
            RETURNING id, client_id, name, twilio_phone_number, plan, created_at,
                trial_ends_at, subscription_status, stripe_customer_id, stripe_subscription_id, billing_exempt_until, business_vertical
        """, (client_id, name, phone_store, plan, business_vertical))
        row = cur.fetchone()
        conn.commit()
        cur.close()
        if not row:
            return None
        return _row_to_tenant(row)
    except Exception as e:
        print(f"[DB] Failed to create tenant: {e}")
        return None


def db_tenant_create_pending(
    client_id: str,
    name: str,
    plan: str = "starter",
    business_vertical: str = "salon_chair",
) -> Optional[dict]:
    """Create a self-serve tenant with NO number yet and status 'incomplete' — the
    number is provisioned when Stripe checkout completes, and the status flips to
    active/trialing then. Returns the tenant dict, or None on client_id conflict."""
    conn = _get_conn()
    if not conn:
        return None
    try:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO tenants (client_id, name, twilio_phone_number, plan, subscription_status, business_vertical)
            VALUES (%s, %s, NULL, %s, 'incomplete', %s)
            ON CONFLICT (client_id) DO NOTHING
            RETURNING id, client_id, name, twilio_phone_number, plan, created_at,
                trial_ends_at, subscription_status, stripe_customer_id, stripe_subscription_id, billing_exempt_until, business_vertical
        """, (client_id, name, plan, business_vertical))
        row = cur.fetchone()
        conn.commit()
        cur.close()
        if not row:
            return None
        return _row_to_tenant(row)
    except Exception as e:
        print(f"[DB] Failed to create pending tenant: {e}")
        return None


def _row_to_tenant(row) -> dict:
    """Map tenant SELECT row to dict (includes business_vertical when present)."""
    base = {"id": str(row[0]), "client_id": row[1], "name": row[2], "twilio_phone_number": row[3], "plan": row[4]}
    base["created_at"] = row[5].isoformat() if len(row) > 5 and row[5] else None
    if len(row) >= 11:
        base["trial_ends_at"] = row[6].isoformat() if row[6] else None
        base["subscription_status"] = row[7] or "trialing"
        base["stripe_customer_id"] = row[8]
        base["stripe_subscription_id"] = row[9]
        base["billing_exempt_until"] = row[10].isoformat() if row[10] else None
    else:
        base["trial_ends_at"] = None
        base["subscription_status"] = "trialing"
        base["stripe_customer_id"] = None
        base["stripe_subscription_id"] = None
        base["billing_exempt_until"] = None
    if len(row) >= 12 and row[11] is not None and str(row[11]).strip():
        base["business_vertical"] = str(row[11]).strip()
    else:
        base["business_vertical"] = "salon_chair"
    if len(row) >= 13 and row[12]:
        base["billing_period_anchor_at"] = row[12].isoformat() if hasattr(row[12], "isoformat") else row[12]
    else:
        base["billing_period_anchor_at"] = base.get("created_at")
    base["account_paused"] = bool(row[13]) if len(row) >= 14 else False
    base["twilio_number_sid"] = row[14] if len(row) >= 15 else None
    base["demo_mode"] = bool(row[15]) if len(row) >= 16 else False
    # NULL for an independent store. When set, subscription_access lets the store
    # inherit access from the org's single subscription.
    base["org_id"] = str(row[16]) if len(row) >= 17 and row[16] else None
    return base

# Order is load-bearing: _row_to_tenant reads this row positionally.
_TENANT_COLS = (
    "id", "client_id", "name", "twilio_phone_number", "plan", "created_at",
    "trial_ends_at", "subscription_status", "stripe_customer_id",
    "stripe_subscription_id", "billing_exempt_until", "business_vertical",
    "billing_period_anchor_at", "account_paused", "twilio_number_sid", "demo_mode",
    "org_id",
)


def _tenant_select_cols(prefix: str = "") -> str:
    """Tenant column list for a SELECT, ordered as _row_to_tenant expects.

    Pass a table alias when joining anything that shares a column name — both
    tenants and org_members have created_at, so an unqualified list is ambiguous.
    """
    p = f"{prefix}." if prefix else ""
    return ", ".join(p + c for c in _TENANT_COLS)

def db_tenant_get_by_phone(twilio_phone_number: str) -> Optional[dict]:
    """Look up tenant by Twilio phone number (E.164). Returns tenant or None."""
    conn = _get_conn()
    if not conn:
        return None
    cur = conn.cursor()
    raw = (twilio_phone_number or "").strip()
    normalized = _normalize_e164(raw)
    digits_in = "".join(c for c in raw if c.isdigit())
    digits_norm = "".join(c for c in normalized if c.isdigit())
    cur.execute(f"""
        SELECT {_tenant_select_cols()}
        FROM tenants
        WHERE twilio_phone_number IN (%s, %s)
           OR regexp_replace(coalesce(twilio_phone_number, ''), '[^0-9]', '', 'g') IN (%s, %s)
        LIMIT 1
    """, (raw, normalized, digits_in, digits_norm))
    row = cur.fetchone()
    cur.close()
    if not row:
        return None
    return _row_to_tenant(row)

def db_tenant_get_by_id(tenant_id: str) -> Optional[dict]:
    """Look up tenant by UUID."""
    conn = _get_conn()
    if not conn:
        return None
    cur = conn.cursor()
    cur.execute(f"SELECT {_tenant_select_cols()} FROM tenants WHERE id = %s", (tenant_id,))
    row = cur.fetchone()
    cur.close()
    if not row:
        return None
    return _row_to_tenant(row)

def _dedupe_tenant_members_one_per_tenant(cur) -> None:
    """Keep the newest membership row per tenant before unique index on tenant_id."""
    try:
        cur.execute(
            """
            DELETE FROM tenant_members a
            USING tenant_members b
            WHERE a.tenant_id = b.tenant_id
              AND a.clerk_user_id <> b.clerk_user_id
              AND (
                a.created_at < b.created_at
                OR (
                  a.created_at IS NOT DISTINCT FROM b.created_at
                  AND a.clerk_user_id < b.clerk_user_id
                )
              )
            """
        )
    except Exception as e:
        print(f"[DB] tenant_members dedupe skipped: {e}")


def db_tenant_member_add(clerk_user_id: str, tenant_id: str) -> bool:
    """Assign sole owner for tenant (one email / one Clerk user per tenant)."""
    return db_tenant_member_assign_owner(clerk_user_id, tenant_id) is not None


def db_tenant_member_assign_owner(clerk_user_id: str, tenant_id: str) -> Optional[List[str]]:
    """
    Make clerk_user_id the only member of tenant_id and remove their other tenant memberships.
    Returns clerk_user_ids displaced from this tenant (previous owners), excluding the new owner.
    """
    conn = _get_conn()
    if not conn or not clerk_user_id or not tenant_id:
        return None
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT clerk_user_id FROM tenant_members WHERE tenant_id = %s::uuid",
            (tenant_id,),
        )
        displaced = [str(r[0]) for r in cur.fetchall() if r and r[0] and str(r[0]) != clerk_user_id]
        cur.execute("DELETE FROM tenant_members WHERE tenant_id = %s::uuid", (tenant_id,))
        cur.execute("DELETE FROM tenant_members WHERE clerk_user_id = %s", (clerk_user_id,))
        cur.execute(
            """
            INSERT INTO tenant_members (clerk_user_id, tenant_id)
            VALUES (%s, %s::uuid)
            """,
            (clerk_user_id, tenant_id),
        )
        cur.close()
        conn.commit()
        return displaced
    except Exception as e:
        print(f"[DB] Failed to assign tenant owner: {e}")
        return None


def db_tenant_member_set_single(clerk_user_id: str, tenant_id: str) -> bool:
    """Replace all tenant memberships for a user with one tenant (admin re-link / invite accept)."""
    return db_tenant_member_assign_owner(clerk_user_id, tenant_id) is not None


def db_tenant_member_remove(clerk_user_id: str, tenant_id: str) -> bool:
    """Remove one tenant membership. Returns True if a row was deleted."""
    conn = _get_conn()
    if not conn:
        return False
    try:
        cur = conn.cursor()
        cur.execute(
            "DELETE FROM tenant_members WHERE clerk_user_id = %s AND tenant_id = %s::uuid",
            (clerk_user_id, tenant_id),
        )
        deleted = cur.rowcount > 0
        cur.close()
        conn.commit()
        return deleted
    except Exception as e:
        print(f"[DB] Failed to remove tenant member: {e}")
        return False


def db_tenant_membership_tenant_ids(clerk_user_id: str) -> List[str]:
    """All tenant UUIDs this Clerk user belongs to (may be multiple if legacy rows exist)."""
    conn = _get_conn()
    if not conn:
        return []
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT tenant_id::text FROM tenant_members
            WHERE clerk_user_id = %s
            ORDER BY created_at DESC NULLS LAST
            """,
            (clerk_user_id,),
        )
        rows = cur.fetchall()
        cur.close()
        return [str(r[0]) for r in rows if r and r[0]]
    except Exception as e:
        print(f"[DB] Failed to list tenant memberships: {e}")
        return []


def _normalize_invite_email(email: str) -> str:
    return (email or "").strip().lower()


def db_tenant_invite_upsert(email: str, tenant_id: str) -> bool:
    """Record pending invite; only one email may be queued per tenant."""
    conn = _get_conn()
    if not conn:
        return False
    em = _normalize_invite_email(email)
    if not em or not tenant_id:
        return False
    try:
        cur = conn.cursor()
        cur.execute(
            "DELETE FROM tenant_invites WHERE tenant_id = %s::uuid AND email <> %s",
            (tenant_id, em),
        )
        cur.execute(
            """
            INSERT INTO tenant_invites (email, tenant_id)
            VALUES (%s, %s::uuid)
            ON CONFLICT (email) DO UPDATE SET tenant_id = EXCLUDED.tenant_id, created_at = NOW()
            """,
            (em, tenant_id),
        )
        cur.close()
        conn.commit()
        return True
    except Exception as e:
        print(f"[DB] Failed to upsert tenant invite: {e}")
        return False


def db_tenant_invite_delete(email: str) -> None:
    conn = _get_conn()
    if not conn:
        return
    em = _normalize_invite_email(email)
    if not em:
        return
    try:
        cur = conn.cursor()
        cur.execute("DELETE FROM tenant_invites WHERE email = %s", (em,))
        cur.close()
        conn.commit()
    except Exception as e:
        print(f"[DB] Failed to delete tenant invite: {e}")


def db_tenant_invite_consume(email: str) -> Optional[str]:
    """Return tenant_id for a pending invite email and remove the row."""
    conn = _get_conn()
    if not conn:
        return None
    em = _normalize_invite_email(email)
    if not em:
        return None
    try:
        cur = conn.cursor()
        cur.execute(
            "DELETE FROM tenant_invites WHERE email = %s RETURNING tenant_id",
            (em,),
        )
        row = cur.fetchone()
        cur.close()
        conn.commit()
        return str(row[0]) if row else None
    except Exception as e:
        print(f"[DB] Failed to consume tenant invite: {e}")
        return None


def db_tenant_get_for_user(
    clerk_user_id: str,
    preferred_tenant_id: Optional[str] = None,
) -> Optional[dict]:
    """
    Get the tenant for a Clerk user (from tenant_members).
    Users should have at most one membership; legacy duplicates are collapsed to preferred or newest.
    """
    membership_ids = db_tenant_membership_tenant_ids(clerk_user_id)
    if not membership_ids:
        return None
    pref = (preferred_tenant_id or "").strip()
    chosen = pref if pref and pref in membership_ids else membership_ids[0]
    if len(membership_ids) > 1:
        print(
            f"[Auth] multiple_tenant_memberships user_id={clerk_user_id} "
            f"tenant_ids={membership_ids} collapsing_to={chosen}"
        )
        db_tenant_member_assign_owner(clerk_user_id, chosen)
    return db_tenant_get_by_id(chosen)

# --- Orgs (multi-store oversight) ---------------------------------------------
# An org groups stores under one login. Org membership is separate from
# tenant_members on purpose: a regional manager overseeing 12 shops is not an
# owner of any of them, so none of the one-user-one-tenant collapsing above
# applies to them, and store owners are unaffected.

# viewer = read-only oversight (what a franchise asked for); manager = may also
# change store settings; owner = the group's head account. Enforced at the auth seam
# in deps.require_tenant, and for membership changes in routers/org.py.
#
# Ordered weakest to strongest — ORG_ROLE_RANK depends on this order, so append
# rather than reorder.
ORG_ROLES = ("viewer", "manager", "owner")
ORG_ROLE_RANK = {r: i for i, r in enumerate(ORG_ROLES)}


def org_role_rank(role: Optional[str]) -> int:
    """Strength of a role. Unknown or missing reads as the weakest, never as strong,
    so a typo or a NULL cannot grant privilege."""
    return ORG_ROLE_RANK.get((role or "").strip().lower(), 0)


def org_role_at_least(role: Optional[str], minimum: str) -> bool:
    return org_role_rank(role) >= org_role_rank(minimum)


def db_org_create(name: str) -> Optional[dict]:
    """Create an org (a group of stores). Returns {id, name} or None."""
    clean = (name or "").strip()
    if not clean:
        return None
    conn = _get_conn()
    if not conn:
        return None
    try:
        cur = conn.cursor()
        cur.execute(
            f"INSERT INTO orgs (name) VALUES (%s) RETURNING {', '.join(_ORG_BILLING_COLS)}",
            (clean[:200],),
        )
        row = cur.fetchone()
        conn.commit()
        cur.close()
        return _row_to_org(row) if row else None
    except Exception as e:
        print(f"[DB] Failed to create org: {e}")
        return None


# The org's billing columns, deliberately the same shape as a tenant's so
# subscription_access can evaluate either with one code path.
_ORG_BILLING_COLS = (
    "id", "name", "plan", "subscription_status", "stripe_customer_id",
    "stripe_subscription_id", "trial_ends_at", "billing_exempt_until",
    # {"starter": "price_...", ...}. NULL/absent = the standard env price, so an
    # untouched org bills exactly as before. See 0013_org_price_overrides.
    "price_overrides",
)


def _row_to_org(row) -> dict:
    return {
        "id": str(row[0]),
        "name": row[1],
        "plan": row[2] or "pro",
        "subscription_status": row[3],
        "stripe_customer_id": row[4],
        "stripe_subscription_id": row[5],
        "trial_ends_at": row[6].isoformat() if row[6] else None,
        "billing_exempt_until": row[7].isoformat() if row[7] else None,
        "price_overrides": row[8] if len(row) > 8 and isinstance(row[8], dict) else {},
    }


def db_org_set_price_overrides(org_id: str, overrides: Optional[dict]) -> bool:
    """Set a group's per-plan price IDs. Pass None or {} to fall back to the standard
    env prices — that fallback is what keeps every other customer unaffected."""
    conn = _get_conn()
    if not conn or not org_id:
        return False
    try:
        import json as _json

        cur = conn.cursor()
        cur.execute(
            "UPDATE orgs SET price_overrides = %s::jsonb WHERE id = %s::uuid",
            (_json.dumps(overrides) if overrides else None, org_id),
        )
        changed = cur.rowcount
        conn.commit()
        cur.close()
        return changed > 0
    except Exception as e:
        print(f"[DB] Failed to set org price overrides: {e}")
        return False


def db_org_get_by_id(org_id: str) -> Optional[dict]:
    conn = _get_conn()
    if not conn or not org_id:
        return None
    try:
        cur = conn.cursor()
        cur.execute(
            f"SELECT {', '.join(_ORG_BILLING_COLS)} FROM orgs WHERE id = %s::uuid", (org_id,)
        )
        row = cur.fetchone()
        cur.close()
        return _row_to_org(row) if row else None
    except Exception as e:
        print(f"[DB] Failed to get org: {e}")
        return None


def db_org_get_by_stripe_subscription_id(sub_id: str) -> Optional[dict]:
    """Resolve an org from a Stripe subscription — Customer-Portal events often carry
    no metadata, same problem the tenant path already solves this way."""
    conn = _get_conn()
    if not conn or not (sub_id or "").strip():
        return None
    try:
        cur = conn.cursor()
        cur.execute(
            f"SELECT {', '.join(_ORG_BILLING_COLS)} FROM orgs WHERE stripe_subscription_id = %s",
            (sub_id.strip(),),
        )
        row = cur.fetchone()
        cur.close()
        return _row_to_org(row) if row else None
    except Exception as e:
        print(f"[DB] Failed to get org by subscription: {e}")
        return None


def db_tenants_for_org(org_id: str) -> List[dict]:
    """Every store in an org, as full tenant dicts (admin/system use — no user scoping;
    for a user-scoped read use db_org_stores_for_user)."""
    conn = _get_conn()
    if not conn or not org_id:
        return []
    try:
        cur = conn.cursor()
        cur.execute(
            f"SELECT {_tenant_select_cols()} FROM tenants WHERE org_id = %s::uuid ORDER BY name",
            (org_id,),
        )
        rows = cur.fetchall()
        cur.close()
        return [_row_to_tenant(r) for r in rows]
    except Exception as e:
        print(f"[DB] Failed to list tenants for org: {e}")
        return []


def db_org_store_count(org_id: str) -> int:
    """Stores in this org — the quantity the org's subscription is billed on."""
    conn = _get_conn()
    if not conn or not org_id:
        return 0
    try:
        cur = conn.cursor()
        cur.execute("SELECT COUNT(*) FROM tenants WHERE org_id = %s::uuid", (org_id,))
        row = cur.fetchone()
        cur.close()
        return int(row[0]) if row else 0
    except Exception as e:
        print(f"[DB] Failed to count org stores: {e}")
        return 0


def db_org_update_subscription(
    org_id: str,
    *,
    plan: Optional[str] = None,
    subscription_status: Optional[str] = None,
    stripe_customer_id: Optional[str] = None,
    stripe_subscription_id: Optional[str] = None,
    trial_ends_at: Optional[datetime] = None,
    billing_exempt_until: Optional[datetime] = None,
    clear_trial: bool = False,
) -> bool:
    """Update an org's billing. Only non-None fields are written, so a partial update
    can't blank the rest. clear_trial explicitly nulls trial_ends_at (None can't, since
    None means 'leave alone')."""
    sets, vals = [], []
    for col, val in (
        ("plan", plan),
        ("subscription_status", subscription_status),
        ("stripe_customer_id", stripe_customer_id),
        ("stripe_subscription_id", stripe_subscription_id),
        ("trial_ends_at", trial_ends_at),
        ("billing_exempt_until", billing_exempt_until),
    ):
        if val is not None:
            sets.append(f"{col} = %s")
            vals.append(val)
    if clear_trial:
        sets.append("trial_ends_at = NULL")
    if not sets:
        return True
    conn = _get_conn()
    if not conn:
        return False
    try:
        cur = conn.cursor()
        vals.append(org_id)
        cur.execute(f"UPDATE orgs SET {', '.join(sets)} WHERE id = %s::uuid", tuple(vals))
        conn.commit()
        cur.close()
        return True
    except Exception as e:
        print(f"[DB] Failed to update org subscription: {e}")
        return False


def db_org_stores(org_id: str) -> List[dict]:
    """Every store attached to a group: [{id, client_id, name}].

    Used when deleting a group that should take its locations with it.
    """
    if not org_id:
        return []
    conn = _get_conn()
    if not conn:
        raise DatabaseUnavailable("no connection for db_org_stores")
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT id, client_id, name FROM tenants WHERE org_id = %s::uuid ORDER BY created_at",
            (org_id,),
        )
        rows = cur.fetchall()
        cur.close()
        return [{"id": str(r[0]), "client_id": r[1], "name": r[2]} for r in rows]
    except DatabaseUnavailable:
        raise
    except Exception as e:
        _log.error("db_org_stores_failed org=%s err=%s: %s", org_id, type(e).__name__, e)
        raise DatabaseUnavailable(f"could not list org stores: {type(e).__name__}") from e


def db_org_sync_store_plans(org_id: str, plan: str) -> int:
    """Stamp the org's plan onto every store in it.

    Keeps plans.get_plan_limits working untouched: it reads the tenant's own plan, so
    a store in a Pro org must literally carry plan='pro'. Returns rows updated.
    """
    p = (plan or "").strip()
    if not p or not org_id:
        return 0
    conn = _get_conn()
    if not conn:
        return 0
    try:
        cur = conn.cursor()
        cur.execute("UPDATE tenants SET plan = %s WHERE org_id = %s::uuid", (p, org_id))
        n = cur.rowcount
        conn.commit()
        cur.close()
        return n
    except Exception as e:
        print(f"[DB] Failed to sync org store plans: {e}")
        return 0


def db_org_delete(org_id: str) -> bool:
    """Delete an org. Its members and pending invites cascade away with it.

    Any store still attached has its org_id set to NULL (the FK is ON DELETE SET
    NULL), which makes it an independent store again — it does NOT delete stores.
    That's survivable but usually not what you want, so callers should check the
    store count first; the admin route refuses unless explicitly forced.
    """
    conn = _get_conn()
    if not conn or not org_id:
        return False
    try:
        cur = conn.cursor()
        cur.execute("DELETE FROM orgs WHERE id = %s::uuid", (org_id,))
        ok = cur.rowcount > 0
        conn.commit()
        cur.close()
        return ok
    except Exception as e:
        print(f"[DB] Failed to delete org: {e}")
        return False


def db_org_member_add(
    clerk_user_id: str,
    org_id: str,
    role: str = "viewer",
    tenant_id: Optional[str] = None,
) -> bool:
    """Add (or re-role) a user in an org. Unknown roles fall back to viewer — the
    least-privileged option, so a typo can never grant write access.

    tenant_id None = the whole group; set = that one store, which is what an invited
    store manager gets. Adding is never destructive: unlike the tenant_members path,
    nobody is displaced and the user keeps whatever else they had."""
    uid = (clerk_user_id or "").strip()
    if not uid or not org_id:
        return False
    r = (role or "viewer").strip().lower()
    if r not in ORG_ROLES:
        r = "viewer"
    conn = _get_conn()
    if not conn:
        return False
    try:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO org_members (clerk_user_id, org_id, role, tenant_id) "
            "VALUES (%s, %s::uuid, %s, %s) "
            "ON CONFLICT (clerk_user_id, org_id) DO UPDATE SET role = EXCLUDED.role, "
            "tenant_id = EXCLUDED.tenant_id",
            (uid, org_id, r, tenant_id),
        )
        conn.commit()
        cur.close()
        return True
    except Exception as e:
        print(f"[DB] Failed to add org member: {e}")
        return False


def db_org_member_role(clerk_user_id: str, org_id: str) -> Optional[str]:
    """This user's role over the WHOLE group, or None.

    tenant_id IS NULL deliberately: a manager invited to one store is an org_members
    row too, and must not read as oversight of the group.
    """
    uid = (clerk_user_id or "").strip()
    if not uid or not org_id:
        return None
    conn = _get_conn()
    if not conn:
        return None
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT role FROM org_members WHERE clerk_user_id = %s AND org_id = %s::uuid "
            "AND tenant_id IS NULL",
            (uid, org_id),
        )
        row = cur.fetchone()
        cur.close()
        return row[0] if row else None
    except Exception as e:
        _log.warning("db_org_member_role_failed org=%s err=%s: %s", org_id, type(e).__name__, e)
        return None


def db_store_managers(org_id: str, tenant_id: str) -> List[dict]:
    """Everyone scoped to ONE store — the managers invited from that store's row.

    Separate from db_org_members, which deliberately lists only whole-group people.
    A store manager is not an overseer of the group and must not appear as one.
    """
    if not org_id or not tenant_id:
        return []
    conn = _get_conn()
    if not conn:
        raise DatabaseUnavailable("no connection for db_store_managers")
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT clerk_user_id, role, created_at FROM org_members "
            "WHERE org_id = %s::uuid AND tenant_id = %s::uuid ORDER BY created_at",
            (org_id, tenant_id),
        )
        rows = cur.fetchall()
        cur.close()
        return [
            {
                "clerk_user_id": r[0],
                "role": r[1],
                "created_at": r[2].isoformat() if r[2] else None,
            }
            for r in rows
        ]
    except DatabaseUnavailable:
        raise
    except Exception as e:
        _log.error("db_store_managers_failed org=%s err=%s: %s", org_id, type(e).__name__, e)
        raise DatabaseUnavailable(f"could not list store managers: {type(e).__name__}") from e


def db_org_member_scope(clerk_user_id: str, org_id: str) -> Optional[dict]:
    """This user's single row in the group: {role, tenant_id} or None.

    org_members is PRIMARY KEY (clerk_user_id, org_id) — one row per person per
    group — so a person is scoped to the whole group OR to exactly one store, never
    to two. Callers need to see the existing scope before writing, because the
    upsert would otherwise MOVE them silently.
    """
    uid = (clerk_user_id or "").strip()
    if not uid or not org_id:
        return None
    conn = _get_conn()
    if not conn:
        return None
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT role, tenant_id FROM org_members WHERE clerk_user_id = %s AND org_id = %s::uuid",
            (uid, org_id),
        )
        row = cur.fetchone()
        cur.close()
        if not row:
            return None
        return {"role": row[0], "tenant_id": str(row[1]) if row[1] else None}
    except Exception as e:
        _log.warning("db_org_member_scope_failed org=%s err=%s: %s", org_id, type(e).__name__, e)
        return None


def db_org_members(org_id: str) -> List[dict]:
    """Whole-group members of an org, strongest role first.

    tenant_id IS NULL only: a manager invited to a single store is an org_members row
    too, and listing them here would imply oversight of the group they do not have.
    """
    if not org_id:
        return []
    conn = _get_conn()
    if not conn:
        raise DatabaseUnavailable("no connection for db_org_members")
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT clerk_user_id, role, created_at FROM org_members "
            "WHERE org_id = %s::uuid AND tenant_id IS NULL",
            (org_id,),
        )
        rows = cur.fetchall()
        cur.close()
        out = [
            {
                "clerk_user_id": r[0],
                "role": r[1],
                "created_at": r[2].isoformat() if r[2] else None,
            }
            for r in rows
        ]
        out.sort(key=lambda m: (-org_role_rank(m["role"]), m.get("created_at") or ""))
        return out
    except DatabaseUnavailable:
        raise
    except Exception as e:
        _log.error("db_org_members_failed org=%s err=%s: %s", org_id, type(e).__name__, e)
        raise DatabaseUnavailable(f"could not list org members: {type(e).__name__}") from e


def db_org_owner_count(org_id: str) -> int:
    """How many whole-group owners the org has. Guards the last-owner rule."""
    if not org_id:
        return 0
    conn = _get_conn()
    if not conn:
        raise DatabaseUnavailable("no connection for db_org_owner_count")
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT COUNT(*) FROM org_members WHERE org_id = %s::uuid AND role = 'owner' "
            "AND tenant_id IS NULL",
            (org_id,),
        )
        n = cur.fetchone()[0]
        cur.close()
        return int(n or 0)
    except DatabaseUnavailable:
        raise
    except Exception as e:
        # Raise rather than return 0: a 0 here would read as "no owners left to
        # protect" and let the last one be removed on a transient error.
        _log.error("db_org_owner_count_failed org=%s err=%s: %s", org_id, type(e).__name__, e)
        raise DatabaseUnavailable(f"could not count org owners: {type(e).__name__}") from e


def db_org_transfer_ownership(org_id: str, from_user: str, to_user: str) -> bool:
    """Hand the head account to someone else. One statement, one transaction.

    Transfer rather than grant: a group has exactly one owner, so making a second
    person owner has to demote the first in the same breath. Done as two updates in
    one transaction because the in-between state — two owners, or none — is the
    state every rule here exists to prevent, and a crash must not be able to leave
    the group sitting in it.

    The target must already oversee the whole group. Handing the business to an
    address that has not accepted an invite would be a way to lose it.
    """
    src = (from_user or "").strip()
    dst = (to_user or "").strip()
    if not org_id or not src or not dst or src == dst:
        return False
    conn = _get_conn()
    if not conn:
        return False
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT clerk_user_id, role FROM org_members "
            "WHERE org_id = %s::uuid AND tenant_id IS NULL "
            "AND clerk_user_id IN (%s, %s) FOR UPDATE",
            (org_id, src, dst),
        )
        found = {r[0]: r[1] for r in cur.fetchall()}
        if found.get(src) != "owner" or dst not in found:
            conn.rollback()
            cur.close()
            return False
        cur.execute(
            "UPDATE org_members SET role = 'manager' "
            "WHERE org_id = %s::uuid AND clerk_user_id = %s AND tenant_id IS NULL",
            (org_id, src),
        )
        cur.execute(
            "UPDATE org_members SET role = 'owner' "
            "WHERE org_id = %s::uuid AND clerk_user_id = %s AND tenant_id IS NULL",
            (org_id, dst),
        )
        conn.commit()
        cur.close()
        _log.info("org_ownership_transferred org=%s from=%s to=%s", org_id, src, dst)
        return True
    except Exception as e:
        try:
            conn.rollback()
        except Exception:
            pass
        _log.error("org_transfer_failed org=%s err=%s: %s", org_id, type(e).__name__, e)
        return False


def db_org_member_remove(clerk_user_id: str, org_id: str) -> bool:
    conn = _get_conn()
    if not conn:
        return False
    try:
        cur = conn.cursor()
        cur.execute(
            "DELETE FROM org_members WHERE clerk_user_id = %s AND org_id = %s::uuid",
            ((clerk_user_id or "").strip(), org_id),
        )
        ok = cur.rowcount > 0
        conn.commit()
        cur.close()
        return ok
    except Exception as e:
        print(f"[DB] Failed to remove org member: {e}")
        return False


def db_org_memberships(clerk_user_id: str) -> List[dict]:
    """Orgs this user oversees: [{org_id, name, role, tenant_id}]. Empty for a normal owner.

    tenant_id is None for someone who oversees the whole group, or a store id for an
    invited store manager. Callers deciding anything group-level (the rollup, billing,
    adding stores) must require tenant_id is None — see db_org_memberships_org_wide.
    """
    uid = (clerk_user_id or "").strip()
    if not uid:
        return []
    conn = _get_conn()
    if not conn:
        return []
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT o.id, o.name, om.role, om.tenant_id FROM org_members om "
            "JOIN orgs o ON o.id = om.org_id WHERE om.clerk_user_id = %s ORDER BY o.name",
            (uid,),
        )
        rows = cur.fetchall()
        cur.close()
        return [
            {
                "org_id": str(r[0]),
                "name": r[1],
                "role": r[2],
                "tenant_id": str(r[3]) if r[3] else None,
            }
            for r in rows
        ]
    except Exception as e:
        print(f"[DB] Failed to load org memberships: {e}")
        return []


def db_org_memberships_org_wide(clerk_user_id: str) -> List[dict]:
    """Only memberships covering the whole group.

    The gate for anything group-level: the rollup, billing, adding stores. A manager
    invited to one store is an org member too, and must not reach any of it.
    """
    return [m for m in db_org_memberships(clerk_user_id) if not m.get("tenant_id")]


def db_org_invite_upsert(
    email: str, org_id: str, role: str = "viewer", tenant_id: Optional[str] = None
) -> bool:
    """Queue (or re-role) a pending org invite by email. Unknown roles -> viewer."""
    em = _normalize_invite_email(email)
    if not em or not org_id:
        return False
    r = (role or "viewer").strip().lower()
    if r not in ORG_ROLES:
        r = "viewer"
    conn = _get_conn()
    if not conn:
        return False
    try:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO org_invites (email, org_id, role, tenant_id) "
            "VALUES (%s, %s::uuid, %s, %s) "
            "ON CONFLICT (email, org_id) DO UPDATE SET role = EXCLUDED.role, "
            "tenant_id = EXCLUDED.tenant_id",
            (em, org_id, r, tenant_id),
        )
        conn.commit()
        cur.close()
        return True
    except Exception as e:
        print(f"[DB] Failed to upsert org invite: {e}")
        return False


def db_org_invite_delete(email: str, org_id: str) -> bool:
    conn = _get_conn()
    if not conn:
        return False
    try:
        cur = conn.cursor()
        cur.execute(
            "DELETE FROM org_invites WHERE email = %s AND org_id = %s::uuid",
            (_normalize_invite_email(email), org_id),
        )
        ok = cur.rowcount > 0
        conn.commit()
        cur.close()
        return ok
    except Exception as e:
        print(f"[DB] Failed to delete org invite: {e}")
        return False


def db_org_invites_for_org(org_id: str) -> List[dict]:
    """Pending (not-yet-accepted) invites for an org — admin display."""
    conn = _get_conn()
    if not conn or not org_id:
        return []
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT email, role FROM org_invites WHERE org_id = %s::uuid ORDER BY email",
            (org_id,),
        )
        rows = cur.fetchall()
        cur.close()
        return [{"email": r[0], "role": r[1]} for r in rows]
    except Exception as e:
        print(f"[DB] Failed to list org invites: {e}")
        return []


def db_org_invites_consume_for_emails(clerk_user_id: str, emails: List[str]) -> List[dict]:
    """Turn any pending org invites for these verified emails into memberships.

    Called the first time an invited person signs in. Each match becomes an
    org_members row and the invite is deleted, in one transaction, so a redelivery
    (or a second concurrent request on first login) can't double-add or leave a
    dangling invite. Returns the orgs the user just joined: [{org_id, role}].

    If they're already a member the invite is still cleared, but their existing role
    is left alone — a stray leftover invite must never silently demote a manager.
    """
    uid = (clerk_user_id or "").strip()
    ems = [_normalize_invite_email(e) for e in (emails or []) if _normalize_invite_email(e)]
    if not uid or not ems:
        return []
    conn = _get_conn()
    if not conn:
        return []
    try:
        cur = conn.cursor()
        # Claim the matching invites by deleting them and returning what was pending.
        # Doing the DELETE first makes the claim atomic: a concurrent call finds no
        # rows and does nothing.
        cur.execute(
            "DELETE FROM org_invites WHERE email = ANY(%s) RETURNING org_id, role, tenant_id",
            (ems,),
        )
        claimed = cur.fetchall()
        joined = []
        for org_id, role, tenant_id in claimed:
            # tenant_id rides along, so a store manager's invite becomes access to that
            # store rather than to the whole group.
            cur.execute(
                "INSERT INTO org_members (clerk_user_id, org_id, role, tenant_id) "
                "VALUES (%s, %s, %s, %s) "
                "ON CONFLICT (clerk_user_id, org_id) DO NOTHING",
                (uid, str(org_id), role, tenant_id),
            )
            joined.append(
                {
                    "org_id": str(org_id),
                    "role": role,
                    "tenant_id": str(tenant_id) if tenant_id else None,
                }
            )
        conn.commit()
        cur.close()
        return joined
    except Exception as e:
        print(f"[DB] Failed to consume org invites: {e}")
        try:
            conn.rollback()
        except Exception:
            pass
        return []


def db_org_attach_tenant(tenant_id: str, org_id: Optional[str]) -> bool:
    """Put a store in an org, or pass org_id=None to make it independent again."""
    conn = _get_conn()
    if not conn:
        return False
    try:
        cur = conn.cursor()
        if org_id:
            cur.execute(
                "UPDATE tenants SET org_id = %s::uuid WHERE id = %s::uuid", (org_id, tenant_id)
            )
        else:
            cur.execute("UPDATE tenants SET org_id = NULL WHERE id = %s::uuid", (tenant_id,))
        ok = cur.rowcount > 0
        conn.commit()
        cur.close()
        return ok
    except Exception as e:
        print(f"[DB] Failed to attach tenant to org: {e}")
        return False


def db_org_stores_for_user(clerk_user_id: str) -> List[dict]:
    """Every store this user oversees, across all their orgs. Each dict is a tenant
    plus the org name and the user's role in that org."""
    uid = (clerk_user_id or "").strip()
    if not uid:
        return []
    conn = _get_conn()
    if not conn:
        return []
    try:
        cur = conn.cursor()
        cur.execute(
            f"""
            SELECT {_tenant_select_cols('t')}, o.name, om.role
            FROM tenants t
            JOIN org_members om
              ON om.org_id = t.org_id
             -- Same scope rule as db_org_store_for_user: a store-scoped membership
             -- lists that store only, never the rest of the group.
             AND (om.tenant_id IS NULL OR om.tenant_id = t.id)
            JOIN orgs o ON o.id = t.org_id
            WHERE om.clerk_user_id = %s
            ORDER BY t.name
            """,
            (uid,),
        )
        rows = cur.fetchall()
        cur.close()
        n = len(_TENANT_COLS)
        out = []
        for r in rows:
            tenant = _row_to_tenant(r[:n])  # org_id comes through here
            tenant["org_name"] = r[n]
            tenant["org_role"] = r[n + 1]
            out.append(tenant)
        return out
    except Exception as e:
        print(f"[DB] Failed to load org stores: {e}")
        return []


def db_org_store_for_user(clerk_user_id: str, store_ref: str) -> Optional[dict]:
    """Resolve one store a user may oversee, by client_id or tenant UUID.

    Returns {"tenant": {...}, "role": "viewer"|"manager"} or None when the user has
    no org membership covering that store.

    The membership check IS the join — there is no path that fetches the store first
    and authorizes second, so a caller cannot read a store outside their org by
    guessing an id. Every caller must treat None as 403.
    """
    uid = (clerk_user_id or "").strip()
    ref = (store_ref or "").strip()
    if not uid or not ref:
        return None
    conn = _get_conn()
    if not conn:
        return None
    try:
        cur = conn.cursor()
        # id is cast to text (not the parameter) so a non-UUID ref can't raise.
        cur.execute(
            f"""
            SELECT {_tenant_select_cols('t')}, om.role
            FROM tenants t
            JOIN org_members om
              ON om.org_id = t.org_id
             -- NULL tenant_id = oversees the whole group; set = that one store only,
             -- which is what an invited store manager gets.
             AND (om.tenant_id IS NULL OR om.tenant_id = t.id)
            WHERE om.clerk_user_id = %s AND (t.client_id = %s OR t.id::text = %s)
            -- Someone can hold both an org-wide and a store-scoped row; the stronger
            -- role wins rather than whichever Postgres happens to return first.
            ORDER BY CASE om.role WHEN 'owner' THEN 0 WHEN 'manager' THEN 1 ELSE 2 END
            LIMIT 1
            """,
            (uid, ref, ref),
        )
        row = cur.fetchone()
        cur.close()
        if not row:
            return None
        n = len(_TENANT_COLS)
        return {"tenant": _row_to_tenant(row[:n]), "role": row[n]}
    except Exception as e:
        print(f"[DB] Failed to resolve org store: {e}")
        return None


def db_org_list_all() -> List[dict]:
    """Every org with its stores and overseers — the admin console view.

    Raises DatabaseUnavailable rather than returning [] — see that class.
    """
    conn = _get_conn()
    if not conn:
        raise DatabaseUnavailable("no database connection available for db_org_list_all")
    try:
        cur = conn.cursor()
        # price_overrides comes along so the admin console can SHOW a partner rate,
        # not just set one — a form that renders empty for a group that has pricing
        # invites someone to overwrite it with blanks.
        cur.execute("SELECT id, name, created_at, price_overrides FROM orgs ORDER BY name")
        orgs = [
            {
                "id": str(r[0]),
                "name": r[1],
                "created_at": r[2].isoformat() if r[2] else None,
                "price_overrides": r[3] if isinstance(r[3], dict) else {},
                "stores": [],
                "members": [],
                "pending_invites": [],
            }
            for r in cur.fetchall()
        ]
        if not orgs:
            cur.close()
            return []
        by_id = {o["id"]: o for o in orgs}
        cur.execute("SELECT org_id, id, client_id, name FROM tenants WHERE org_id IS NOT NULL")
        for org_id, tid, cid, name in cur.fetchall():
            org = by_id.get(str(org_id))
            if org:
                org["stores"].append({"tenant_id": str(tid), "client_id": cid, "name": name})
        cur.execute("SELECT org_id, clerk_user_id, role FROM org_members")
        for org_id, uid, role in cur.fetchall():
            org = by_id.get(str(org_id))
            if org:
                org["members"].append({"clerk_user_id": uid, "role": role})
        cur.execute("SELECT org_id, email, role FROM org_invites")
        for org_id, email, role in cur.fetchall():
            org = by_id.get(str(org_id))
            if org:
                org["pending_invites"].append({"email": email, "role": role})
        cur.close()
        return orgs
    except Exception as e:
        _log.error("db_org_list_all_failed err=%s: %s", type(e).__name__, e)
        raise DatabaseUnavailable(f"could not list orgs: {type(e).__name__}") from e


def db_org_store_metrics(client_ids: List[str], days: int = 7) -> dict:
    """Headline metrics per store for the oversight rollup, keyed by client_id.

    Aggregated in SQL across all stores at once rather than looping per store, so a
    franchise with 50 shops is still three queries. Filters use the real created_at
    timestamp, not call_log.start_iso — that column is TEXT and would be a string
    comparison. appointments.date is also TEXT, but it's a zero-padded YYYY-MM-DD, so
    lexicographic order matches date order there.
    """
    cids = [c for c in (client_ids or []) if c]
    if not cids:
        return {}
    conn = _get_conn()
    if not conn:
        return {}
    out = {
        c: {
            "calls": 0, "missed": 0, "answered": 0, "bookings": 0,
            "upcoming": 0, "unread_messages": 0, "prev_calls": 0, "prev_bookings": 0,
        }
        for c in cids
    }
    window = timedelta(days=max(1, days))
    since = datetime.now(timezone.utc) - window
    prev_since = since - window  # same-length window immediately before, for the trend
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT client_id,
                   COUNT(*) FILTER (WHERE created_at >= %s) AS calls,
                   COUNT(*) FILTER (WHERE created_at >= %s AND outcome IN ('missed', 'no_answer')) AS missed,
                   COUNT(*) FILTER (WHERE created_at >= %s AND outcome = 'answered_by_ai') AS answered,
                   COUNT(*) FILTER (WHERE created_at >= %s AND created_at < %s) AS prev_calls
            FROM call_log WHERE client_id = ANY(%s) GROUP BY client_id
            """,
            (since, since, since, prev_since, since, cids),
        )
        for cid, calls, missed, answered, prev_calls in cur.fetchall():
            out[cid]["calls"] = calls or 0
            out[cid]["missed"] = missed or 0
            out[cid]["answered"] = answered or 0
            out[cid]["prev_calls"] = prev_calls or 0
        cur.execute(
            """
            SELECT client_id,
                   COUNT(*) FILTER (WHERE created_at >= %s AND source = 'receptionist') AS bookings,
                   COUNT(*) FILTER (WHERE date >= %s AND status NOT IN ('cancelled', 'completed')) AS upcoming,
                   COUNT(*) FILTER (WHERE created_at >= %s AND created_at < %s AND source = 'receptionist') AS prev_bookings
            FROM appointments WHERE client_id = ANY(%s) GROUP BY client_id
            """,
            (since, today, prev_since, since, cids),
        )
        for cid, bookings, upcoming, prev_bookings in cur.fetchall():
            out[cid]["bookings"] = bookings or 0
            out[cid]["upcoming"] = upcoming or 0
            out[cid]["prev_bookings"] = prev_bookings or 0
        cur.execute(
            """
            SELECT client_id, COUNT(*) FILTER (WHERE status = 'unread') AS unread
            FROM messages WHERE client_id = ANY(%s) GROUP BY client_id
            """,
            (cids,),
        )
        for cid, unread in cur.fetchall():
            out[cid]["unread_messages"] = unread or 0
        cur.close()
    except Exception as e:
        print(f"[DB] Failed to load org store metrics: {e}")
    return out


def db_tenant_get_members(tenant_id: str) -> List[str]:
    """Get all clerk_user_ids for a tenant."""
    conn = _get_conn()
    if not conn:
        return []
    cur = conn.cursor()
    cur.execute("SELECT clerk_user_id FROM tenant_members WHERE tenant_id = %s::uuid", (tenant_id,))
    rows = cur.fetchall()
    cur.close()
    return [r[0] for r in rows]


def db_tenant_all_member_clerk_ids() -> List[str]:
    """All Clerk user IDs with a tenant membership (admin email lookup fallback)."""
    conn = _get_conn()
    if not conn:
        return []
    try:
        cur = conn.cursor()
        cur.execute("SELECT DISTINCT clerk_user_id FROM tenant_members ORDER BY clerk_user_id")
        rows = cur.fetchall()
        cur.close()
        return [str(r[0]) for r in rows if r and r[0]]
    except Exception as e:
        print(f"[DB] Failed to list tenant member clerk ids: {e}")
        return []


def db_tenant_invite_peek(email: str) -> Optional[str]:
    """Pending invite tenant_id for an email (does not consume the row)."""
    conn = _get_conn()
    if not conn:
        return None
    em = _normalize_invite_email(email)
    if not em:
        return None
    try:
        cur = conn.cursor()
        cur.execute("SELECT tenant_id::text FROM tenant_invites WHERE email = %s LIMIT 1", (em,))
        row = cur.fetchone()
        cur.close()
        return str(row[0]) if row and row[0] else None
    except Exception as e:
        print(f"[DB] Failed to peek tenant invite: {e}")
        return None


def db_tenant_memberships_for_user(clerk_user_id: str) -> List[dict]:
    """All tenant memberships for a Clerk user (normally 0 or 1)."""
    conn = _get_conn()
    if not conn:
        return []
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT t.id::text, t.client_id, t.name, m.created_at
            FROM tenant_members m
            JOIN tenants t ON t.id = m.tenant_id
            WHERE m.clerk_user_id = %s
            ORDER BY m.created_at DESC NULLS LAST
            """,
            (clerk_user_id,),
        )
        rows = cur.fetchall()
        cur.close()
        return [
            {
                "tenant_id": str(r[0]),
                "client_id": r[1],
                "name": r[2],
                "member_since": r[3].isoformat() if r[3] and hasattr(r[3], "isoformat") else None,
            }
            for r in rows
        ]
    except Exception as e:
        print(f"[DB] Failed to list memberships for user: {e}")
        return []


def db_tenant_get_invite_email(tenant_id: str) -> Optional[str]:
    """Pending invite email for this tenant (at most one per tenant)."""
    conn = _get_conn()
    if not conn:
        return None
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT email FROM tenant_invites WHERE tenant_id = %s::uuid LIMIT 1",
            (tenant_id,),
        )
        row = cur.fetchone()
        cur.close()
        return str(row[0]).strip() if row and row[0] else None
    except Exception as e:
        print(f"[DB] Failed to get tenant invite email: {e}")
        return None

def db_tenant_delete(tenant_id: str) -> bool:
    """Delete a tenant by UUID. Cascades to tenant_members. Returns True on success."""
    conn = _get_conn()
    if not conn:
        return False
    try:
        cur = conn.cursor()
        cur.execute("DELETE FROM tenants WHERE id = %s", (tenant_id,))
        deleted = cur.rowcount > 0
        conn.commit()
        cur.close()
        return deleted
    except Exception as e:
        print(f"[DB] Failed to delete tenant: {e}")
        return False


# Operational tables keyed by tenant client_id (purge when tenant is removed; snapshot retained for compliance).
_CLIENT_SCOPED_TABLES = (
    "overage_processed",
    "tenant_usage",
    "sms_automations",
    "leads",
    "sms_opt_out",
    "sms_sessions",
    "booked_slots",
    "caller_memory",
    "messages",
    "call_log",
    "appointments",
    "audit_events",
)


def _serialize_cell(val):
    if isinstance(val, (datetime, date)):
        return val.isoformat()
    if isinstance(val, memoryview):
        return bytes(val).decode("utf-8", errors="replace")
    if isinstance(val, bytes):
        return val.decode("utf-8", errors="replace")
    return val


def _fetch_all_rows_for_client(cur, table: str, client_id: str) -> List[dict]:
    if table not in _CLIENT_SCOPED_TABLES:
        raise ValueError(f"Invalid table name: {table}")
    cur.execute(f"SELECT * FROM {table} WHERE client_id = %s", (client_id,))
    desc = cur.description
    if not desc:
        return []
    cols = [d[0] for d in desc]
    out: List[dict] = []
    for row in cur.fetchall():
        out.append({cols[i]: _serialize_cell(row[i]) for i in range(len(cols))})
    return out


def db_archive_purge_and_delete_tenant(
    tenant_id: str,
    tenant: dict,
    *,
    actor_clerk_id: Optional[str] = None,
) -> Optional[int]:
    """
    One transaction: snapshot all client_id-scoped rows into tenant_removed_archive, delete those live rows,
    then delete the tenant row (tenant_members CASCADE). Retains an auditable bundle for disputes/retention.
    Returns archive row id, or None on failure (nothing committed).
    """
    conn = _get_conn()
    if not conn:
        return None
    tid = (tenant.get("id") or "").strip()
    cid = (tenant.get("client_id") or "").strip()
    tid_n = str(tid).replace("-", "").lower()
    path_n = str(tenant_id or "").replace("-", "").lower()
    if not tid or not cid or tid_n != path_n:
        return None
    try:
        from psycopg2.extras import Json
    except ImportError:
        Json = None  # type: ignore
    bundle = {"tenant": dict(tenant), "scoped_tables": {}}
    try:
        cur = conn.cursor()
        for table in _CLIENT_SCOPED_TABLES:
            bundle["scoped_tables"][table] = _fetch_all_rows_for_client(cur, table, cid)
        payload = Json(bundle) if Json else json.dumps(bundle, default=str)
        cur.execute(
            """
            INSERT INTO tenant_removed_archive (former_tenant_id, client_id, actor_clerk_id, bundle)
            VALUES (%s, %s, %s, %s)
            RETURNING id
            """,
            (tid, cid, actor_clerk_id, payload),
        )
        archive_id = cur.fetchone()[0]
        for table in _CLIENT_SCOPED_TABLES:
            cur.execute(f"DELETE FROM {table} WHERE client_id = %s", (cid,))
        cur.execute("DELETE FROM tenants WHERE id = %s", (tenant_id,))
        if cur.rowcount < 1:
            conn.rollback()
            cur.close()
            print("[DB] db_archive_purge_and_delete_tenant: tenant row missing, rolled back")
            return None
        conn.commit()
        cur.close()
        print(f"[DB] Tenant removed archive_id={archive_id} client_id={cid!r}")
        return int(archive_id)
    except Exception as e:
        print(f"[DB] db_archive_purge_and_delete_tenant failed: {e}")
        try:
            conn.rollback()
        except Exception:
            pass
        return None


def db_legal_hold_set(
    client_id: str,
    *,
    reason: Optional[str] = None,
    hold_until: Optional[datetime] = None,
    created_by: Optional[str] = None,
) -> bool:
    """Create/update a legal hold for a tenant client_id."""
    cid = (client_id or "").strip()
    if not cid:
        return False
    conn = _get_conn()
    if not conn:
        return False
    try:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO legal_holds (client_id, reason, hold_until, created_by, updated_at)
            VALUES (%s, %s, %s, %s, NOW())
            ON CONFLICT (client_id) DO UPDATE SET
                reason = EXCLUDED.reason,
                hold_until = EXCLUDED.hold_until,
                created_by = EXCLUDED.created_by,
                updated_at = NOW()
            """,
            (cid, (reason or "").strip()[:2000] or None, hold_until, (created_by or "").strip()[:256] or None),
        )
        conn.commit()
        cur.close()
        return True
    except Exception as e:
        print(f"[DB] Failed to set legal hold: {e}")
        return False


def db_legal_hold_clear(client_id: str) -> bool:
    cid = (client_id or "").strip()
    if not cid:
        return False
    conn = _get_conn()
    if not conn:
        return False
    try:
        cur = conn.cursor()
        cur.execute("DELETE FROM legal_holds WHERE client_id = %s", (cid,))
        ok = cur.rowcount > 0
        conn.commit()
        cur.close()
        return ok
    except Exception as e:
        print(f"[DB] Failed to clear legal hold: {e}")
        return False


def db_legal_hold_list_active() -> List[dict]:
    conn = _get_conn()
    if not conn:
        return []
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT client_id, reason, hold_until, created_by, created_at, updated_at
            FROM legal_holds
            WHERE hold_until IS NULL OR hold_until > NOW()
            ORDER BY updated_at DESC
            """
        )
        rows = cur.fetchall()
        cur.close()
        return [
            {
                "client_id": r[0],
                "reason": r[1],
                "hold_until": r[2].isoformat() if r[2] else None,
                "created_by": r[3],
                "created_at": r[4].isoformat() if r[4] else None,
                "updated_at": r[5].isoformat() if r[5] else None,
            }
            for r in rows
        ]
    except Exception as e:
        print(f"[DB] Failed to list legal holds: {e}")
        return []


def db_retention_purge(days: int = 365 * 3) -> dict:
    """
    Purge expired rows while honoring legal holds.
    Returns deleted counts by table.
    """
    keep_days = max(1, int(days))
    conn = _get_conn()
    if not conn:
        return {"audit_events": 0, "call_log": 0, "sms_sessions": 0}
    out = {"audit_events": 0, "call_log": 0, "sms_sessions": 0}
    try:
        cur = conn.cursor()
        cur.execute(
            """
            DELETE FROM audit_events a
            WHERE a.occurred_at < NOW() - make_interval(days => %s::int)
              AND (
                a.client_id IS NULL OR a.client_id = '' OR NOT EXISTS (
                  SELECT 1 FROM legal_holds h
                  WHERE h.client_id = a.client_id
                    AND (h.hold_until IS NULL OR h.hold_until > NOW())
                )
              )
            """,
            (keep_days,),
        )
        out["audit_events"] = int(cur.rowcount or 0)
        cur.execute(
            """
            DELETE FROM call_log c
            WHERE c.created_at < NOW() - make_interval(days => %s::int)
              AND NOT EXISTS (
                SELECT 1 FROM legal_holds h
                WHERE h.client_id = c.client_id
                  AND (h.hold_until IS NULL OR h.hold_until > NOW())
              )
            """,
            (keep_days,),
        )
        out["call_log"] = int(cur.rowcount or 0)
        cur.execute(
            """
            DELETE FROM sms_sessions s
            WHERE s.updated_at < NOW() - make_interval(days => %s::int)
              AND NOT EXISTS (
                SELECT 1 FROM legal_holds h
                WHERE h.client_id = s.client_id
                  AND (h.hold_until IS NULL OR h.hold_until > NOW())
              )
            """,
            (keep_days,),
        )
        out["sms_sessions"] = int(cur.rowcount or 0)
        conn.commit()
        cur.close()
    except Exception as e:
        print(f"[DB] retention purge failed: {e}")
        try:
            conn.rollback()
        except Exception:
            pass
    return out


def db_export_tenant_snapshot(export_root: str, *, include_audit: bool = True) -> Optional[dict]:
    """
    Export tenant-scoped operational data to a local immutable snapshot file.
    Returns metadata with path + sha256.
    """
    conn = _get_conn()
    if not conn:
        return None
    root = Path(export_root).expanduser()
    root.mkdir(parents=True, exist_ok=True)
    ts = datetime.utcnow().strftime("%Y%m%dT%H%M%SZ")
    export_key = f"tenant-export-{ts}-{uuid.uuid4().hex[:10]}"
    out_path = root / f"{export_key}.json"
    try:
        cur = conn.cursor()
        cur.execute(f"SELECT {_tenant_select_cols()} FROM tenants ORDER BY created_at ASC")
        tenant_rows = cur.fetchall()
        payload = {
            "export_key": export_key,
            "created_at": datetime.utcnow().isoformat() + "Z",
            "tenants": [],
            "schema_tables": list(_CLIENT_SCOPED_TABLES),
        }
        scoped_tables = [t for t in _CLIENT_SCOPED_TABLES if include_audit or t != "audit_events"]
        for row in tenant_rows:
            tenant = _row_to_tenant(row)
            cid = (tenant.get("client_id") or "").strip()
            scoped_data = {}
            for table in scoped_tables:
                scoped_data[table] = _fetch_all_rows_for_client(cur, table, cid)
            payload["tenants"].append({"tenant": tenant, "tables": scoped_data})
        encoded = json.dumps(payload, default=str, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
        sha = hashlib.sha256(encoded).hexdigest()
        with out_path.open("wb") as f:
            f.write(encoded)
        cur.execute(
            """
            INSERT INTO backup_exports (export_key, destination_path, sha256)
            VALUES (%s, %s, %s)
            ON CONFLICT (export_key) DO NOTHING
            """,
            (export_key, str(out_path), sha),
        )
        conn.commit()
        cur.close()
        return {"export_key": export_key, "path": str(out_path), "sha256": sha, "tenants": len(payload["tenants"])}
    except Exception as e:
        print(f"[DB] tenant snapshot export failed: {e}")
        return None

def db_tenant_list_all() -> List[dict]:
    """List all tenants (admin only).

    Raises DatabaseUnavailable rather than returning [] — see that class.
    """
    conn = _get_conn()
    if not conn:
        raise DatabaseUnavailable("no database connection available for db_tenant_list_all")
    cur = conn.cursor()
    cur.execute(f"SELECT {_tenant_select_cols()} FROM tenants ORDER BY created_at DESC", ())
    rows = cur.fetchall()
    cur.close()
    return [_row_to_tenant(r) for r in rows]

def db_tenant_update_subscription(
    tenant_id: str,
    *,
    stripe_customer_id: Optional[str] = None,
    stripe_subscription_id: Optional[str] = None,
    subscription_status: Optional[str] = None,
    plan: Optional[str] = None,
    trial_ends_at: Optional[datetime] = None,
) -> bool:
    """Update tenant subscription fields (from Stripe webhook). Returns True on success.

    trial_ends_at is set when provided (e.g. a trialing subscription's trial_end),
    so the tenant reflects Stripe's real trial window and unlocks trial-tier access.
    """
    conn = _get_conn()
    if not conn:
        return False
    try:
        cur = conn.cursor()
        updates = []
        params = []
        if stripe_customer_id is not None:
            updates.append("stripe_customer_id = %s")
            params.append(stripe_customer_id)
        if stripe_subscription_id is not None:
            updates.append("stripe_subscription_id = %s")
            params.append(stripe_subscription_id)
        if subscription_status is not None:
            updates.append("subscription_status = %s")
            params.append(subscription_status)
        if trial_ends_at is not None:
            updates.append("trial_ends_at = %s")
            params.append(trial_ends_at)
        if plan is not None:
            updates.append("plan = %s")
            params.append(plan)
        if not updates:
            cur.close()
            return True
        params.append(tenant_id)
        cur.execute(f"UPDATE tenants SET {', '.join(updates)} WHERE id = %s", params)
        conn.commit()
        cur.close()
        return True
    except Exception as e:
        print(f"[DB] Failed to update tenant subscription: {e}")
        return False

def db_tenant_set_billing_exempt(tenant_id: str, exempt_until: Optional[datetime]) -> bool:
    """Set billing_exempt_until for a tenant (admin). None clears exemption. Returns True on success."""
    conn = _get_conn()
    if not conn:
        return False
    try:
        cur = conn.cursor()
        cur.execute("UPDATE tenants SET billing_exempt_until = %s WHERE id = %s", (exempt_until, tenant_id))
        conn.commit()
        cur.close()
        return True
    except Exception as e:
        print(f"[DB] Failed to set billing exempt: {e}")
        return False

def db_tenant_set_name(tenant_id: str, name: str) -> bool:
    """Rename a tenant (the business name callers hear in the greeting). Returns True
    on success; a blank name is rejected rather than wiping the column."""
    clean = (name or "").strip()
    if not clean:
        return False
    conn = _get_conn()
    if not conn:
        return False
    try:
        cur = conn.cursor()
        cur.execute("UPDATE tenants SET name = %s WHERE id = %s", (clean[:120], tenant_id))
        conn.commit()
        cur.close()
        return True
    except Exception as e:
        print(f"[DB] Failed to set tenant name: {e}")
        return False


def db_tenant_set_demo_mode(tenant_id: str, enabled: bool) -> bool:
    """Flag a tenant as a card-free demo account. Returns True on success."""
    conn = _get_conn()
    if not conn:
        return False
    try:
        cur = conn.cursor()
        cur.execute("UPDATE tenants SET demo_mode = %s WHERE id = %s", (bool(enabled), tenant_id))
        conn.commit()
        cur.close()
        return True
    except Exception as e:
        print(f"[DB] Failed to set demo mode: {e}")
        return False


# Tables holding the sample data a demo tenant is seeded with. Deliberately
# EXCLUDES audit_events (the record that the demo existed and was purged must
# survive), legal_holds, and sms_opt_out (an opt-out is a compliance record and
# is never deleted, even a sample one).
_DEMO_SEEDED_TABLES = (
    "booked_slots",
    "appointments",
    "messages",
    "call_log",
    "caller_memory",
    "leads",
    "sms_sessions",
    "sms_automations",
    "tenant_usage",
    "conversational_sms_period_usage",
    "conversational_sms_session_keys",
)


def db_tenant_deactivate_demo(
    tenant_id: str, replacement_config: Optional[dict] = None
) -> Optional[dict]:
    """Atomically convert a demo tenant to a real one: clear the demo flag and the
    comped exemption, replace the sample business config, and purge every row of
    seeded sample data.

    Returns {"client_id": ..., "deleted": {table: n}} if this call did the
    conversion, or None if the tenant was not in demo mode (already converted, or
    never a demo). Stripe redelivers webhooks, so the demo_mode flag is *claimed*
    by the same UPDATE that clears it — a redelivery finds demo_mode already false
    and purges nothing. That claim is what makes it impossible for this to delete a
    paying tenant's real data.

    replacement_config is written in the same statement that clears the flag, so
    the sample services can never outlive demo mode and end up read out to a real
    caller. The caller builds it (config defaults are a config_service concern).

    Everything runs in one transaction: either all of it happens, or none of it.
    """
    conn = _get_conn()
    if not conn:
        return None
    try:
        cur = conn.cursor()
        # Claim: only a row that is *currently* demo_mode yields a client_id here.
        if replacement_config is not None:
            cur.execute(
                "UPDATE tenants SET demo_mode = FALSE, billing_exempt_until = NULL, "
                "business_config = %s::jsonb "
                "WHERE id = %s AND demo_mode = TRUE RETURNING client_id",
                (json.dumps(replacement_config), tenant_id),
            )
        else:
            cur.execute(
                "UPDATE tenants SET demo_mode = FALSE, billing_exempt_until = NULL "
                "WHERE id = %s AND demo_mode = TRUE RETURNING client_id",
                (tenant_id,),
            )
        row = cur.fetchone()
        if not row:
            conn.rollback()
            cur.close()
            return None
        cid = row[0]
        deleted: dict = {}
        for table in _DEMO_SEEDED_TABLES:
            try:
                cur.execute(f"DELETE FROM {table} WHERE client_id = %s", (cid,))
                deleted[table] = cur.rowcount
            except Exception as e:
                # A missing optional table must not strand the tenant in demo mode.
                print(f"[DB] demo purge skipped {table}: {e}")
        conn.commit()
        cur.close()
        return {"client_id": cid, "deleted": deleted}
    except Exception as e:
        print(f"[DB] Failed to deactivate demo tenant: {e}")
        try:
            conn.rollback()
        except Exception:
            pass
        return None


def db_tenant_set_twilio_phone(tenant_id: str, twilio_phone_number: str) -> bool:
    """Update tenant inbound Twilio number (E.164). Used when the live Twilio number was not stored at create time."""
    conn = _get_conn()
    if not conn:
        return False
    phone = (twilio_phone_number or "").strip()
    phone_store = _normalize_e164(phone)
    if not phone_store or not any(c.isdigit() for c in phone_store):
        return False
    try:
        cur = conn.cursor()
        cur.execute(
            "UPDATE tenants SET twilio_phone_number = %s WHERE id = %s::uuid",
            (phone_store, tenant_id),
        )
        ok = cur.rowcount > 0
        conn.commit()
        cur.close()
        return ok
    except Exception as e:
        print(f"[DB] Failed to set tenant Twilio phone: {e}")
        try:
            conn.rollback()
        except Exception:
            pass
        return False

def db_tenant_set_account_paused(tenant_id: str, paused: bool) -> bool:
    """Set the admin manual-pause kill-switch for a tenant. Returns True on success."""
    conn = _get_conn()
    if not conn:
        return False
    try:
        cur = conn.cursor()
        cur.execute(
            "UPDATE tenants SET account_paused = %s WHERE id = %s",
            (bool(paused), tenant_id),
        )
        ok = cur.rowcount > 0
        conn.commit()
        cur.close()
        return ok
    except Exception as e:
        print(f"[DB] Failed to set account_paused: {e}")
        try:
            conn.rollback()
        except Exception:
            pass
        return False

def db_tenant_set_twilio_number_sid(tenant_id: str, number_sid: Optional[str]) -> bool:
    """Store the Twilio incoming-number SID (PNxxxx) so releases don't need a lookup."""
    conn = _get_conn()
    if not conn:
        return False
    sid = (number_sid or "").strip() or None
    try:
        cur = conn.cursor()
        cur.execute(
            "UPDATE tenants SET twilio_number_sid = %s WHERE id = %s",
            (sid, tenant_id),
        )
        ok = cur.rowcount > 0
        conn.commit()
        cur.close()
        return ok
    except Exception as e:
        print(f"[DB] Failed to set twilio_number_sid: {e}")
        try:
            conn.rollback()
        except Exception:
            pass
        return False

def db_tenant_clear_twilio(tenant_id: str) -> bool:
    """Clear both the Twilio number and its SID after the number is released."""
    conn = _get_conn()
    if not conn:
        return False
    try:
        cur = conn.cursor()
        cur.execute(
            "UPDATE tenants SET twilio_phone_number = NULL, twilio_number_sid = NULL WHERE id = %s",
            (tenant_id,),
        )
        ok = cur.rowcount > 0
        conn.commit()
        cur.close()
        return ok
    except Exception as e:
        print(f"[DB] Failed to clear tenant Twilio: {e}")
        try:
            conn.rollback()
        except Exception:
            pass
        return False

def db_tenant_extend_trial(tenant_id: str, trial_ends_at: datetime) -> bool:
    """Set trial_ends_at for a tenant (admin). Returns True on success."""
    conn = _get_conn()
    if not conn:
        return False
    try:
        cur = conn.cursor()
        cur.execute("UPDATE tenants SET trial_ends_at = %s WHERE id = %s", (trial_ends_at, tenant_id))
        conn.commit()
        cur.close()
        return True
    except Exception as e:
        print(f"[DB] Failed to extend trial: {e}")
        return False

def db_tenant_get_by_stripe_subscription_id(stripe_subscription_id: str) -> Optional[dict]:
    """Look up tenant by Stripe subscription ID (for webhook invoice.payment_failed)."""
    if not stripe_subscription_id:
        return None
    conn = _get_conn()
    if not conn:
        return None
    cur = conn.cursor()
    cur.execute(f"SELECT {_tenant_select_cols()} FROM tenants WHERE stripe_subscription_id = %s LIMIT 1", (stripe_subscription_id,))
    row = cur.fetchone()
    cur.close()
    if not row:
        return None
    return _row_to_tenant(row)

def db_tenant_get_by_client_id(client_id: str) -> Optional[dict]:
    """Look up tenant by client_id."""
    if not client_id:
        return None
    conn = _get_conn()
    if not conn:
        return None
    cur = conn.cursor()
    cur.execute(f"SELECT {_tenant_select_cols()} FROM tenants WHERE client_id = %s LIMIT 1", (client_id,))
    row = cur.fetchone()
    cur.close()
    if not row:
        return None
    return _row_to_tenant(row)


def _jsonb_to_dict(val: Any) -> Optional[dict]:
    if val is None:
        return None
    if isinstance(val, dict):
        return val
    if isinstance(val, str):
        try:
            parsed = json.loads(val)
            return parsed if isinstance(parsed, dict) else None
        except json.JSONDecodeError:
            return None
    return None


def db_tenant_get_business_config(client_id: str) -> Optional[dict]:
    """Load persisted business config (voice, greeting, services, etc.) for a tenant."""
    cid = (client_id or "").strip()
    if not cid:
        return None
    conn = _get_conn()
    if not conn:
        return None
    try:
        cur = conn.cursor()
        cur.execute("SELECT business_config FROM tenants WHERE client_id = %s LIMIT 1", (cid,))
        row = cur.fetchone()
        cur.close()
        if not row:
            return None
        return _jsonb_to_dict(row[0])
    except Exception as e:
        print(f"[DB] Failed to get business_config for {cid}: {e}")
        return None


def db_tenant_set_business_config(client_id: str, config: dict) -> bool:
    """Persist business config JSON for a tenant (survives Render redeploys)."""
    cid = (client_id or "").strip()
    if not cid or not isinstance(config, dict):
        return False
    conn = _get_conn()
    if not conn:
        return False
    try:
        cur = conn.cursor()
        cur.execute(
            "UPDATE tenants SET business_config = %s::jsonb WHERE client_id = %s",
            (json.dumps(config), cid),
        )
        ok = cur.rowcount > 0
        conn.commit()
        cur.close()
        return ok
    except Exception as e:
        print(f"[DB] Failed to set business_config for {cid}: {e}")
        return False


def db_audit_append(
    actor_type: str,
    action: str,
    *,
    actor_id: Optional[str] = None,
    resource_type: Optional[str] = None,
    resource_id: Optional[str] = None,
    client_id: Optional[str] = None,
    details: Optional[dict] = None,
    ip: Optional[str] = None,
    request_id: Optional[str] = None,
) -> None:
    """Append an audit event. Does not log full PII (e.g. no message bodies)."""
    conn = _get_conn()
    if not conn:
        return
    try:
        cur = conn.cursor()
        details_json = json.dumps(details) if details is not None else None
        cid = client_id if client_id is not None else _client_id()
        cur.execute("""
            INSERT INTO audit_events (actor_type, actor_id, action, resource_type, resource_id, client_id, details, ip, request_id)
            VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s)
        """, (actor_type, actor_id, action, resource_type, resource_id, cid or None, details_json, ip, request_id))
        conn.commit()
        cur.close()
    except Exception as e:
        print(f"[DB] Failed to append audit: {e}")

def db_audit_list(
    *,
    limit: int = 100,
    client_id: Optional[str] = None,
    action: Optional[str] = None,
) -> List[dict]:
    """Recent audit events for the admin viewer, newest first. Optional filters by
    client_id / action. Excludes the `details` blob (keeps the table clean and
    avoids surfacing hashes); core who/what/when/where is returned."""
    conn = _get_conn()
    if not conn:
        return []
    try:
        cur = conn.cursor()
        where = []
        params: list = []
        if client_id:
            where.append("client_id = %s")
            params.append(client_id)
        if action:
            where.append("action = %s")
            params.append(action)
        sql = (
            "SELECT id, occurred_at, actor_type, actor_id, action, resource_type, "
            "resource_id, client_id, ip FROM audit_events"
        )
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY occurred_at DESC LIMIT %s"
        params.append(max(1, min(int(limit), 500)))
        cur.execute(sql, tuple(params))
        rows = cur.fetchall()
        cur.close()
        return [
            {
                "id": r[0],
                "occurred_at": r[1].isoformat() if r[1] else None,
                "actor_type": r[2],
                "actor_id": r[3],
                "action": r[4],
                "resource_type": r[5],
                "resource_id": r[6],
                "client_id": r[7],
                "ip": r[8],
            }
            for r in rows
        ]
    except Exception as e:
        _log.warning("db_audit_list failed: %s", e)
        return []


def _normalize_phone(phone: str) -> str:
    return "".join(c for c in (phone or "") if c.isdigit())

# --- Appointments ---
def db_appointments_get_all(*, client_id: Optional[str] = None) -> List[dict]:
    conn = _get_conn()
    if not conn:
        return []
    cid = (client_id or "").strip() or _client_id()
    cur = conn.cursor()
    cur.execute(
        """SELECT id, name, email, phone, date, time, reason, status, source, created_at, staff_id, owner_decline_reason, confirmation_sms_failed
           FROM appointments WHERE client_id = %s ORDER BY date, time""",
        (cid,),
    )
    rows = cur.fetchall()
    cur.close()
    return [
        {
            "id": r[0],
            "name": r[1],
            "email": r[2] or "",
            "phone": r[3] or "",
            "date": r[4],
            "time": r[5] or "",
            "reason": r[6] or "",
            "status": r[7],
            "source": r[8] or "manual",
            "created_at": r[9].isoformat() if r[9] else "",
            "staff_id": r[10] if len(r) > 10 else None,
            "owner_decline_reason": r[11] if len(r) > 11 else None,
            "confirmation_sms_failed": bool(r[12]) if len(r) > 12 else False,
        }
        for r in rows
    ]


def db_appointments_diagnostics(dashboard_client_id: str) -> dict:
    """
    Compare appointment rows for the logged-in tenant vs other client_ids (counts only).
    Helps debug voice bookings stored under a different client_id than the dashboard.
    """
    conn = _get_conn()
    if not conn:
        return {"error": "no_db"}
    cid = (dashboard_client_id or "").strip()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT status, COUNT(*)::int
        FROM appointments
        WHERE client_id = %s
        GROUP BY status
        ORDER BY status
        """,
        (cid,),
    )
    by_status = {row[0]: row[1] for row in cur.fetchall()}
    cur.execute("SELECT COUNT(*)::int FROM appointments WHERE client_id = %s", (cid,))
    total = (cur.fetchone() or (0,))[0]
    cur.execute(
        """
        SELECT id, status, date, time, source, created_at
        FROM appointments
        WHERE client_id = %s
        ORDER BY created_at DESC
        LIMIT 8
        """,
        (cid,),
    )
    recent = [
        {
            "id": r[0],
            "status": r[1],
            "date": r[2],
            "time": r[3] or "",
            "source": r[4] or "",
            "created_at": r[5].isoformat() if r[5] else "",
        }
        for r in cur.fetchall()
    ]
    env_cid = (os.getenv("CLIENT_ID") or "").strip()
    env_count = None
    if env_cid and env_cid != cid:
        cur.execute("SELECT COUNT(*)::int FROM appointments WHERE client_id = %s", (env_cid,))
        env_count = (cur.fetchone() or (0,))[0]
    cur.close()
    # NOTE: we deliberately do NOT return a global counts_by_client breakdown.
    # That enumerated every tenant's client_id and relative volume to any
    # authenticated caller — a cross-tenant info leak. env_client_id below is
    # the deployment's OWN config value (not another customer's), kept because
    # ops logging and the single->multi migration banner rely on it.
    return {
        "dashboard_client_id": cid,
        "total": total,
        "by_status": by_status,
        "recent": recent,
        "env_client_id": env_cid or None,
        "env_client_id_appointment_count": env_count,
        "likely_mismatch": bool(env_cid and env_cid != cid and env_count and env_count > 0 and total == 0),
    }


def db_appointments_insert(data: dict) -> dict:
    conn = _get_conn()
    if not conn:
        raise RuntimeError("Database not available")
    cur = conn.cursor()
    cid = (data.get("client_id") or "").strip() or _client_id()
    _log.info(
        "[DB] db_appointments_insert client_id=%s date=%s time=%s",
        cid,
        data.get("date"),
        data.get("time"),
    )
    cur.execute("""
        INSERT INTO appointments (client_id, name, email, phone, date, time, reason, status, source, staff_id)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        RETURNING id, created_at
    """, (
        cid,
        data["name"],
        data.get("email", ""),
        data.get("phone", ""),
        data["date"],
        data.get("time", ""),
        data.get("reason", ""),
        data.get("status", "pending"),
        data.get("source", "manual"),
        data.get("staff_id"),
    ))
    row = cur.fetchone()
    conn.commit()
    cur.close()
    apt_id = row[0]
    _log.info("[DB] db_appointments_insert_ok id=%s client_id=%s", apt_id, cid)
    return {"id": apt_id, "created_at": row[1].isoformat() if row[1] else "", **data}

def db_appointments_update(
    appointment_id: int,
    *,
    client_id: Optional[str] = None,
    **kwargs,
) -> Optional[dict]:
    conn = _get_conn()
    if not conn:
        return None
    allowed = ("status", "date", "time", "reason", "name", "email", "phone", "staff_id", "owner_decline_reason", "confirmation_sms_failed")
    updates = []
    vals = []
    for k, v in kwargs.items():
        if k in allowed and v is not None:
            updates.append(f"{k} = %s")
            vals.append(v)
    if not updates:
        return None
    vals.append(appointment_id)
    cid = (client_id or "").strip() or _client_id()
    vals.append(cid)
    cur = conn.cursor()
    cur.execute(
        f"UPDATE appointments SET {', '.join(updates)} WHERE id = %s AND client_id = %s "
        "RETURNING id, name, email, phone, date, time, reason, status, source, created_at, staff_id, owner_decline_reason, confirmation_sms_failed",
        vals,
    )
    row = cur.fetchone()
    conn.commit()
    cur.close()
    if not row:
        return None
    return {
        "id": row[0],
        "name": row[1],
        "email": row[2] or "",
        "phone": row[3] or "",
        "date": row[4],
        "time": row[5] or "",
        "reason": row[6] or "",
        "status": row[7],
        "source": row[8] or "manual",
        "created_at": row[9].isoformat() if row[9] else "",
        "staff_id": row[10] if len(row) > 10 else None,
        "owner_decline_reason": row[11] if len(row) > 11 else None,
        "confirmation_sms_failed": bool(row[12]) if len(row) > 12 else False,
    }

def db_appointments_get_by_id(
    appointment_id: int,
    *,
    client_id: Optional[str] = None,
) -> Optional[dict]:
    conn = _get_conn()
    if not conn:
        return None
    cid = (client_id or "").strip() or _client_id()
    cur = conn.cursor()
    cur.execute(
        """SELECT id, name, email, phone, date, time, reason, status, source, created_at, staff_id, owner_decline_reason, confirmation_sms_failed
           FROM appointments WHERE id = %s AND client_id = %s""",
        (appointment_id, cid),
    )
    row = cur.fetchone()
    cur.close()
    if not row:
        return None
    return {
        "id": row[0],
        "name": row[1],
        "email": row[2] or "",
        "phone": row[3] or "",
        "date": row[4],
        "time": row[5] or "",
        "reason": row[6] or "",
        "status": row[7],
        "source": row[8] or "manual",
        "created_at": row[9].isoformat() if row[9] else "",
        "staff_id": row[10] if len(row) > 10 else None,
        "owner_decline_reason": row[11] if len(row) > 11 else None,
        "confirmation_sms_failed": bool(row[12]) if len(row) > 12 else False,
    }


def db_appointments_delete(appointment_id: int, *, client_id: Optional[str] = None) -> bool:
    """Permanently delete one appointment, tenant-scoped. Returns True if a row was removed."""
    conn = _get_conn()
    if not conn:
        return False
    cid = (client_id or "").strip() or _client_id()
    cur = conn.cursor()
    cur.execute(
        "DELETE FROM appointments WHERE id = %s AND client_id = %s",
        (appointment_id, cid),
    )
    deleted = cur.rowcount
    conn.commit()
    cur.close()
    return bool(deleted)


def db_appointments_delete_many(ids: List[int], *, client_id: Optional[str] = None) -> int:
    """Permanently delete several appointments, tenant-scoped. Returns the count removed."""
    clean = [int(i) for i in (ids or []) if str(i).strip()]
    if not clean:
        return 0
    conn = _get_conn()
    if not conn:
        return 0
    cid = (client_id or "").strip() or _client_id()
    cur = conn.cursor()
    cur.execute(
        "DELETE FROM appointments WHERE client_id = %s AND id = ANY(%s)",
        (cid, clean),
    )
    deleted = cur.rowcount
    conn.commit()
    cur.close()
    return int(deleted or 0)


def db_appointments_max_id() -> int:
    conn = _get_conn()
    if not conn:
        return 0
    cur = conn.cursor()
    cur.execute("SELECT COALESCE(MAX(id), 0) FROM appointments WHERE client_id = %s", (_client_id(),))
    row = cur.fetchone()
    cur.close()
    return row[0] if row else 0


def db_appointments_in_date_range(
    date_from: str,
    date_to: str,
    staff_id: Optional[str] = None,
    *,
    client_id: Optional[str] = None,
) -> List[dict]:
    """Active appointments for calendar grid (inclusive date range). Excludes cancelled/rejected."""
    conn = _get_conn()
    if not conn:
        return []
    cid = (client_id or "").strip() or _client_id()
    cur = conn.cursor()
    if staff_id:
        cur.execute(
            """
            SELECT id, name, email, phone, date, time, reason, status, source, created_at, staff_id, owner_decline_reason, confirmation_sms_failed
            FROM appointments
            WHERE client_id = %s AND date >= %s AND date <= %s AND staff_id = %s
              AND status NOT IN ('cancelled', 'rejected')
            ORDER BY date, time
            """,
            (cid, date_from, date_to, staff_id),
        )
    else:
        cur.execute(
            """
            SELECT id, name, email, phone, date, time, reason, status, source, created_at, staff_id, owner_decline_reason, confirmation_sms_failed
            FROM appointments
            WHERE client_id = %s AND date >= %s AND date <= %s
              AND status NOT IN ('cancelled', 'rejected')
            ORDER BY date, time
            """,
            (cid, date_from, date_to),
        )
    rows = cur.fetchall()
    cur.close()
    return [
        {
            "id": r[0],
            "name": r[1],
            "email": r[2] or "",
            "phone": r[3] or "",
            "date": r[4],
            "time": r[5] or "",
            "reason": r[6] or "",
            "status": r[7],
            "source": r[8] or "manual",
            "created_at": r[9].isoformat() if r[9] else "",
            "staff_id": r[10] if len(r) > 10 else None,
            "owner_decline_reason": r[11] if len(r) > 11 else None,
            "confirmation_sms_failed": bool(r[12]) if len(r) > 12 else False,
        }
        for r in rows
    ]


def db_appointments_get_accepted_for_date(client_id: str, date: str) -> List[dict]:
    """Get accepted appointments for client_id and date (YYYY-MM-DD) with reminder_sent_at IS NULL."""
    if not client_id or not date:
        return []
    conn = _get_conn()
    if not conn:
        return []
    cur = conn.cursor()
    cur.execute("""
        SELECT id, name, email, phone, date, time, reason, status
        FROM appointments
        WHERE client_id = %s AND status = 'accepted' AND date = %s AND reminder_sent_at IS NULL
        ORDER BY time
    """, (client_id, date))
    rows = cur.fetchall()
    cur.close()
    return [
        {"id": r[0], "name": r[1], "email": r[2] or "", "phone": r[3] or "", "date": r[4], "time": r[5] or "", "reason": r[6] or "", "status": r[7]}
        for r in rows
    ]

def db_appointments_mark_reminder_sent(appointment_id: int, client_id: str) -> bool:
    """Atomically set reminder_sent_at if not already set. Returns True if updated."""
    conn = _get_conn()
    if not conn:
        return False
    try:
        cur = conn.cursor()
        cur.execute("""
            UPDATE appointments SET reminder_sent_at = NOW()
            WHERE id = %s AND client_id = %s AND reminder_sent_at IS NULL
            RETURNING id
        """, (appointment_id, client_id))
        row = cur.fetchone()
        conn.commit()
        cur.close()
        return row is not None
    except Exception as e:
        print(f"[DB] Failed to mark reminder sent: {e}")
        return False

def db_appointments_get_pending_by_phone(phone: str) -> Optional[dict]:
    """Return most recent pending_review appointment for this phone, or None."""
    conn = _get_conn()
    if not conn:
        return None
    norm = _normalize_phone(phone or "")
    if not norm:
        return None
    cur = conn.cursor()
    cur.execute("""
        SELECT id, name, email, phone, date, time, reason, status, source, created_at
        FROM appointments
        WHERE client_id = %s AND status = 'pending_review'
          AND (regexp_replace(COALESCE(phone,''), '[^0-9]', '', 'g') = %s
               OR regexp_replace(COALESCE(phone,''), '[^0-9]', '', 'g') = right(%s, 10))
        ORDER BY created_at DESC
        LIMIT 1
    """, (_client_id(), norm, norm))
    row = cur.fetchone()
    cur.close()
    if not row:
        return None
    return {"id": row[0], "name": row[1], "email": row[2] or "", "phone": row[3] or "", "date": row[4], "time": row[5] or "", "reason": row[6] or "", "status": row[7], "source": row[8] or "manual", "created_at": row[9].isoformat() if row[9] else ""}

def db_appointments_get_by_phone_for_sms(
    phone: str,
    *,
    client_id: Optional[str] = None,
) -> Optional[dict]:
    """Return most recent appointment for this phone with status pending_customer or pending_review (for SMS reply context and confirm flow)."""
    conn = _get_conn()
    if not conn:
        return None
    norm = _normalize_phone(phone or "")
    if not norm:
        return None
    cid = (client_id or "").strip() or _client_id()
    cur = conn.cursor()
    cur.execute("""
        SELECT id, name, email, phone, date, time, reason, status, source, created_at
        FROM appointments
        WHERE client_id = %s AND status IN ('pending_customer', 'pending_review')
          AND (regexp_replace(COALESCE(phone,''), '[^0-9]', '', 'g') = %s
               OR regexp_replace(COALESCE(phone,''), '[^0-9]', '', 'g') = right(%s, 10))
        ORDER BY created_at DESC
        LIMIT 1
    """, (cid, norm, norm))
    row = cur.fetchone()
    cur.close()
    if not row:
        return None
    return {"id": row[0], "name": row[1], "email": row[2] or "", "phone": row[3] or "", "date": row[4], "time": row[5] or "", "reason": row[6] or "", "status": row[7], "source": row[8] or "manual", "created_at": row[9].isoformat() if row[9] else ""}


def db_appointments_get_active_for_sms_context(
    phone: str,
    *,
    client_id: Optional[str] = None,
    limit: int = 5,
) -> List[dict]:
    """
    Return recent non-cancelled appointments for this phone for conversational SMS context.
    Includes accepted and pending statuses so the AI can answer "how many appointments do I have?".
    """
    conn = _get_conn()
    if not conn:
        return []
    norm = _normalize_phone(phone or "")
    if not norm:
        return []
    cid = (client_id or "").strip() or _client_id()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT id, name, email, phone, date, time, reason, status, source, created_at
        FROM appointments
        WHERE client_id = %s
          AND status IN ('pending_customer', 'pending_review', 'accepted')
          AND (regexp_replace(COALESCE(phone,''), '[^0-9]', '', 'g') = %s
               OR regexp_replace(COALESCE(phone,''), '[^0-9]', '', 'g') = right(%s, 10))
        ORDER BY date ASC, time ASC, created_at DESC
        LIMIT %s
        """,
        (cid, norm, norm, max(1, int(limit))),
    )
    rows = cur.fetchall()
    cur.close()
    return [
        {
            "id": row[0],
            "name": row[1],
            "email": row[2] or "",
            "phone": row[3] or "",
            "date": row[4],
            "time": row[5] or "",
            "reason": row[6] or "",
            "status": row[7],
            "source": row[8] or "manual",
            "created_at": row[9].isoformat() if row[9] else "",
        }
        for row in rows
    ]


def db_appointments_update_active_name_by_phone(
    phone: str,
    *,
    client_id: str,
    name: str,
    exclude_appointment_id: Optional[int] = None,
) -> int:
    """Update name across active appointments for this phone (pending/accepted) and return row count."""
    conn = _get_conn()
    if not conn:
        return 0
    norm = _normalize_phone(phone or "")
    cid = (client_id or "").strip()
    nm = (name or "").strip()
    if not norm or not cid or not nm:
        return 0
    cur = conn.cursor()
    if exclude_appointment_id:
        cur.execute(
            """
            UPDATE appointments
            SET name = %s
            WHERE client_id = %s
              AND status IN ('pending_customer', 'pending_review', 'accepted')
              AND id <> %s
              AND (regexp_replace(COALESCE(phone,''), '[^0-9]', '', 'g') = %s
                   OR regexp_replace(COALESCE(phone,''), '[^0-9]', '', 'g') = right(%s, 10))
            """,
            (nm, cid, int(exclude_appointment_id), norm, norm),
        )
    else:
        cur.execute(
            """
            UPDATE appointments
            SET name = %s
            WHERE client_id = %s
              AND status IN ('pending_customer', 'pending_review', 'accepted')
              AND (regexp_replace(COALESCE(phone,''), '[^0-9]', '', 'g') = %s
                   OR regexp_replace(COALESCE(phone,''), '[^0-9]', '', 'g') = right(%s, 10))
            """,
            (nm, cid, norm, norm),
        )
    count = cur.rowcount or 0
    conn.commit()
    cur.close()
    return int(count)


def db_appointments_latest_identity_for_phone(
    phone: str,
    *,
    client_id: Optional[str] = None,
) -> Optional[dict]:
    """Most recent appointment for this phone with a name (excludes cancelled/rejected). Used to refresh caller memory."""
    conn = _get_conn()
    if not conn:
        return None
    norm = _normalize_phone(phone or "")
    if not norm:
        return None
    cid = (client_id or "").strip() or _client_id()
    cur = conn.cursor()
    cur.execute(
        """
        SELECT name, email, status, source, created_at
        FROM appointments
        WHERE client_id = %s
          AND COALESCE(NULLIF(TRIM(name), ''), '') <> ''
          AND status NOT IN ('cancelled', 'rejected')
          AND (regexp_replace(COALESCE(phone,''), '[^0-9]', '', 'g') = %s
               OR regexp_replace(COALESCE(phone,''), '[^0-9]', '', 'g') = right(%s, 10))
        ORDER BY created_at DESC
        LIMIT 1
        """,
        (cid, norm, norm),
    )
    row = cur.fetchone()
    cur.close()
    if not row:
        return None
    return {
        "name": (row[0] or "").strip(),
        "email": (row[1] or "").strip(),
        "status": row[2] or "",
        "source": row[3] or "manual",
    }


def db_appointments_resolve_for_sms(
    phone: str, client_id: str
) -> tuple[Optional[dict], str]:
    """
    Find the active voice-booking appointment for an inbound SMS: match caller phone, then fall back
    to the SMS session's linked appointment_id (set when the confirmation text is sent after a call).

    Returns (appointment_or_none, resolve_via) where resolve_via is phone|session|none.
    """
    cid = (client_id or "").strip()
    if not cid:
        return None, "none"
    apt = db_appointments_get_by_phone_for_sms(phone, client_id=cid)
    if apt:
        return apt, "phone"
    session = db_sms_session_get(phone, cid)
    if not session:
        return None, "none"
    aid = session.get("appointment_id")
    if not aid:
        return None, "none"
    row = db_appointments_get_by_id(int(aid), client_id=cid)
    if row and (row.get("status") or "") in ("pending_customer", "pending_review"):
        return row, "session"
    return None, "none"


# --- SMS Sessions ---
def db_sms_session_get(phone: str, client_id: str) -> Optional[dict]:
    """Get SMS session for phone+client. Returns {messages, appointment_id} or None."""
    conn = _get_conn()
    if not conn:
        return None
    norm = _normalize_phone(phone or "")
    if not norm:
        return None
    cur = conn.cursor()
    cur.execute(
        "SELECT messages, appointment_id, updated_at FROM sms_sessions WHERE phone = %s AND client_id = %s",
        (norm, client_id)
    )
    row = cur.fetchone()
    cur.close()
    if not row:
        return None
    msgs = row[0] if isinstance(row[0], list) else (json.loads(row[0]) if row[0] else [])
    return {"messages": msgs, "appointment_id": row[1], "updated_at": row[2]}

def db_sms_session_upsert(phone: str, client_id: str, messages: list, appointment_id: Optional[int] = None) -> None:
    """Insert or update SMS session."""
    conn = _get_conn()
    if not conn:
        return
    norm = _normalize_phone(phone or "")
    if not norm:
        return
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO sms_sessions (phone, client_id, messages, appointment_id, updated_at)
        VALUES (%s, %s, %s::jsonb, %s, NOW())
        ON CONFLICT (phone, client_id) DO UPDATE SET
            messages = EXCLUDED.messages,
            appointment_id = COALESCE(EXCLUDED.appointment_id, sms_sessions.appointment_id),
            updated_at = NOW()
    """, (norm, client_id, json.dumps(messages), appointment_id))
    conn.commit()
    cur.close()


def db_sms_threads_list(client_id: str, search: Optional[str] = None, limit: int = 200) -> List[dict]:
    """List SMS conversation threads for a tenant, newest first. Each entry carries the
    last message preview, total message count, and any linked appointment. Optional
    `search` filters by phone-number substring (digits only)."""
    conn = _get_conn()
    if not conn or not client_id:
        return []
    cur = conn.cursor()
    params: list = [client_id]
    where = "client_id = %s AND jsonb_array_length(messages) > 0"
    norm_search = "".join(ch for ch in (search or "") if ch.isdigit())
    if norm_search:
        where += " AND phone LIKE %s"
        params.append(f"%{norm_search}%")
    params.append(limit)
    cur.execute(
        f"""
        SELECT phone,
               jsonb_array_length(messages) AS msg_count,
               messages->-1 AS last_msg,
               appointment_id,
               updated_at
        FROM sms_sessions
        WHERE {where}
        ORDER BY updated_at DESC
        LIMIT %s
        """,
        tuple(params),
    )
    rows = cur.fetchall()
    cur.close()
    out = []
    for r in rows:
        last = r[2]
        if isinstance(last, str):
            try:
                last = json.loads(last)
            except Exception:
                last = {}
        if not isinstance(last, dict):
            last = {}
        out.append({
            "phone": r[0],
            "message_count": r[1] or 0,
            "last_message": (last.get("content") or "")[:300],
            "last_role": last.get("role") or "",
            "appointment_id": r[3],
            "updated_at": r[4].isoformat() if r[4] else "",
        })
    return out


def db_sms_messages_total(client_id: str) -> int:
    """Total text messages exchanged (inbound + outbound) across all SMS threads for a
    tenant. Powers the dashboard 'Total Messages' card."""
    conn = _get_conn()
    if not conn or not client_id:
        return 0
    cur = conn.cursor()
    cur.execute(
        "SELECT COALESCE(SUM(jsonb_array_length(messages)), 0)::int FROM sms_sessions WHERE client_id = %s",
        (client_id,),
    )
    row = cur.fetchone()
    cur.close()
    return (row[0] if row else 0) or 0


def db_sms_opt_out_is_blocked(phone: str, client_id: str) -> bool:
    """True if this customer has opted out of SMS for this tenant (digits-only phone key)."""
    conn = _get_conn()
    if not conn or not client_id:
        return False
    norm = _normalize_phone(phone or "")
    if not norm:
        return False
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT 1 FROM sms_opt_out WHERE phone = %s AND client_id = %s LIMIT 1",
            (norm, client_id),
        )
        row = cur.fetchone()
        cur.close()
        return row is not None
    except Exception as e:
        print(f"[DB] sms_opt_out check failed: {e}")
        return False


def db_sms_opt_out_set(phone: str, client_id: str) -> None:
    """Record SMS opt-out for phone + tenant."""
    conn = _get_conn()
    if not conn or not client_id:
        return
    norm = _normalize_phone(phone or "")
    if not norm:
        return
    try:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO sms_opt_out (phone, client_id) VALUES (%s, %s)
            ON CONFLICT (phone, client_id) DO NOTHING
            """,
            (norm, client_id),
        )
        conn.commit()
        cur.close()
    except Exception as e:
        print(f"[DB] sms_opt_out set failed: {e}")


def db_sms_opt_out_clear(phone: str, client_id: str) -> None:
    """Remove SMS opt-out (START / resubscribe)."""
    conn = _get_conn()
    if not conn or not client_id:
        return
    norm = _normalize_phone(phone or "")
    if not norm:
        return
    try:
        cur = conn.cursor()
        cur.execute("DELETE FROM sms_opt_out WHERE phone = %s AND client_id = %s", (norm, client_id))
        conn.commit()
        cur.close()
    except Exception as e:
        print(f"[DB] sms_opt_out clear failed: {e}")


def db_sms_consent_record(
    phone: str,
    client_id: str,
    source: str,
    *,
    consent_type: str = "service",
    detail: Optional[dict] = None,
    ip: Optional[str] = None,
) -> None:
    """Append a consent event to the opt-IN ledger.

    Append-only: one row per consent moment (inbound call, inbound SMS, START
    opt-in, voice booking). This is the provable trail that complements
    sms_opt_out. Graceful: never raises into a call/SMS flow — a missing table
    (un-migrated DB) or transient error just logs and returns.
    """
    conn = _get_conn()
    if not conn or not client_id:
        return
    norm = _normalize_phone(phone or "")
    if not norm:
        return
    try:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO sms_consent (phone, client_id, consent_type, source, detail, ip)
            VALUES (%s, %s, %s, %s, %s::jsonb, %s)
            """,
            (
                norm,
                client_id,
                consent_type,
                source,
                json.dumps(detail) if detail is not None else None,
                ip,
            ),
        )
        conn.commit()
        cur.close()
    except Exception as e:
        print(f"[DB] sms_consent record failed: {e}")


# --- Messages ---
def db_messages_get_all() -> List[dict]:
    conn = _get_conn()
    if not conn:
        return []
    cur = conn.cursor()
    cur.execute(
        "SELECT id, caller_name, caller_phone, message, urgency, status, created_at FROM messages WHERE client_id = %s ORDER BY created_at DESC",
        (_client_id(),)
    )
    rows = cur.fetchall()
    cur.close()
    return [
        {"id": r[0], "caller_name": r[1], "caller_phone": r[2], "message": r[3], "urgency": r[4], "status": r[5], "created_at": r[6].isoformat() if r[6] else ""}
        for r in rows
    ]

def db_messages_insert(data: dict, *, client_id: Optional[str] = None) -> dict:
    conn = _get_conn()
    if not conn:
        raise RuntimeError("Database not available")
    cid = (client_id or "").strip() or _client_id()
    cur = conn.cursor()
    cur.execute("""
        INSERT INTO messages (client_id, caller_name, caller_phone, message, urgency, status)
        VALUES (%s, %s, %s, %s, %s, %s)
        RETURNING id, created_at
    """, (cid, data.get("caller_name", ""), data.get("caller_phone", ""), data.get("message", ""), data.get("urgency", "normal"), data.get("status", "unread")))
    row = cur.fetchone()
    conn.commit()
    cur.close()
    return {"id": row[0], "created_at": row[1].isoformat() if row[1] else "", **data}


def db_messages_get_by_id(message_id: int, *, client_id: Optional[str] = None) -> Optional[dict]:
    conn = _get_conn()
    if not conn:
        return None
    cid = (client_id or "").strip() or _client_id()
    cur = conn.cursor()
    cur.execute(
        "SELECT id, caller_name, caller_phone, message, urgency, status, created_at "
        "FROM messages WHERE id = %s AND client_id = %s",
        (message_id, cid),
    )
    row = cur.fetchone()
    cur.close()
    if not row:
        return None
    return {
        "id": row[0],
        "caller_name": row[1],
        "caller_phone": row[2],
        "message": row[3],
        "urgency": row[4],
        "status": row[5],
        "created_at": row[6].isoformat() if row[6] else "",
    }


def db_messages_set_status(
    message_id: int, status: str, *, client_id: Optional[str] = None
) -> Optional[dict]:
    """Update a message's status (e.g. 'read' / 'unread'), tenant-scoped. Returns the
    updated row or None if not found."""
    conn = _get_conn()
    if not conn:
        return None
    cid = (client_id or "").strip() or _client_id()
    cur = conn.cursor()
    cur.execute(
        "UPDATE messages SET status = %s WHERE id = %s AND client_id = %s "
        "RETURNING id, caller_name, caller_phone, message, urgency, status, created_at",
        (status, message_id, cid),
    )
    row = cur.fetchone()
    conn.commit()
    cur.close()
    if not row:
        return None
    return {
        "id": row[0],
        "caller_name": row[1],
        "caller_phone": row[2],
        "message": row[3],
        "urgency": row[4],
        "status": row[5],
        "created_at": row[6].isoformat() if row[6] else "",
    }

def db_messages_max_id() -> int:
    conn = _get_conn()
    if not conn:
        return 0
    cur = conn.cursor()
    cur.execute("SELECT COALESCE(MAX(id), 0) FROM messages WHERE client_id = %s", (_client_id(),))
    row = cur.fetchone()
    cur.close()
    return row[0] if row else 0

# --- Usage tracking ---
def db_usage_get(client_id: str, month: str) -> Optional[dict]:
    """Get usage for client_id and month (YYYY-MM). Returns {voice_minutes, sms_count} or None."""
    if not client_id or not month:
        return None
    conn = _get_conn()
    if not conn:
        return None
    cur = conn.cursor()
    cur.execute(
        "SELECT voice_minutes, sms_count FROM tenant_usage WHERE client_id = %s AND month = %s",
        (client_id, month)
    )
    row = cur.fetchone()
    cur.close()
    if not row:
        return {"voice_minutes": 0, "sms_count": 0}
    return {"voice_minutes": row[0] or 0, "sms_count": row[1] or 0}

def db_usage_increment_voice(client_id: str, month: str, minutes: int) -> bool:
    """Atomically increment voice_minutes. Returns True on success."""
    if not client_id or not month or minutes < 0:
        return False
    conn = _get_conn()
    if not conn:
        return False
    try:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO tenant_usage (client_id, month, voice_minutes, sms_count)
            VALUES (%s, %s, %s, 0)
            ON CONFLICT (client_id, month) DO UPDATE SET
                voice_minutes = tenant_usage.voice_minutes + EXCLUDED.voice_minutes,
                updated_at = NOW()
        """, (client_id, month, minutes))
        conn.commit()
        cur.close()
        return True
    except Exception as e:
        print(f"[DB] Failed to increment voice usage: {e}")
        return False

def db_usage_increment_sms(client_id: str, month: str) -> bool:
    """Atomically increment sms_count. Returns True on success."""
    if not client_id or not month:
        return False
    conn = _get_conn()
    if not conn:
        return False
    try:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO tenant_usage (client_id, month, voice_minutes, sms_count)
            VALUES (%s, %s, 0, 1)
            ON CONFLICT (client_id, month) DO UPDATE SET
                sms_count = tenant_usage.sms_count + 1,
                updated_at = NOW()
        """, (client_id, month))
        conn.commit()
        cur.close()
        return True
    except Exception as e:
        print(f"[DB] Failed to increment SMS usage: {e}")
        return False

# --- Leads ---
def db_leads_insert(client_id: str, name: Optional[str], phone: str, reason: str, source: str) -> Optional[int]:
    """Insert a lead. Returns id or None on failure."""
    if not client_id or not phone or source not in ("call", "sms"):
        return None
    conn = _get_conn()
    if not conn:
        return None
    try:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO leads (client_id, name, phone, reason, source)
            VALUES (%s, %s, %s, %s, %s)
            RETURNING id
        """, (client_id, name or "", phone, reason or "", source))
        row = cur.fetchone()
        conn.commit()
        cur.close()
        return row[0] if row else None
    except Exception as e:
        print(f"[DB] Failed to insert lead: {e}")
        return None

def db_leads_get_all(client_id: str, limit: int = 100) -> List[dict]:
    """Get leads for client_id, newest first."""
    if not client_id:
        return []
    conn = _get_conn()
    if not conn:
        return []
    cur = conn.cursor()
    cur.execute("""
        SELECT id, name, phone, reason, source, created_at
        FROM leads WHERE client_id = %s ORDER BY created_at DESC LIMIT %s
    """, (client_id, limit))
    rows = cur.fetchall()
    cur.close()
    return [
        {"id": r[0], "name": r[1] or "", "phone": r[2], "reason": r[3] or "", "source": r[4], "created_at": r[5].isoformat() if r[5] else ""}
        for r in rows
    ]


def db_leads_get_by_id(lead_id: int, client_id: str) -> Optional[dict]:
    """Fetch a single lead, tenant-scoped. None if not found."""
    if not client_id:
        return None
    conn = _get_conn()
    if not conn:
        return None
    cur = conn.cursor()
    cur.execute(
        "SELECT id, name, phone, reason, source, created_at FROM leads WHERE id = %s AND client_id = %s",
        (lead_id, client_id),
    )
    row = cur.fetchone()
    cur.close()
    if not row:
        return None
    return {
        "id": row[0],
        "name": row[1] or "",
        "phone": row[2],
        "reason": row[3] or "",
        "source": row[4],
        "created_at": row[5].isoformat() if row[5] else "",
    }


# --- Feedback (bug reports / suggestions) ---
def db_feedback_insert(
    category: str,
    message: str,
    client_id: Optional[str] = None,
    user_email: Optional[str] = None,
    page_url: Optional[str] = None,
    user_agent: Optional[str] = None,
) -> Optional[int]:
    """Insert a feedback submission. Returns id or None on failure."""
    if category not in ("bug", "idea", "other") or not (message or "").strip():
        return None
    conn = _get_conn()
    if not conn:
        return None
    try:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO feedback (client_id, user_email, category, message, page_url, user_agent)
            VALUES (%s, %s, %s, %s, %s, %s)
            RETURNING id
            """,
            (
                client_id or None,
                (user_email or "").strip() or None,
                category,
                message.strip(),
                (page_url or "").strip()[:500] or None,
                (user_agent or "").strip()[:500] or None,
            ),
        )
        row = cur.fetchone()
        conn.commit()
        cur.close()
        return row[0] if row else None
    except Exception as e:
        print(f"[DB] Failed to insert feedback: {e}")
        return None


def db_feedback_get_all(limit: int = 200) -> List[dict]:
    """Get feedback submissions across all tenants, newest first. Admin-only use."""
    conn = _get_conn()
    if not conn:
        return []
    cur = conn.cursor()
    cur.execute(
        """
        SELECT id, client_id, user_email, category, message, page_url, user_agent, status, created_at
        FROM feedback ORDER BY created_at DESC LIMIT %s
        """,
        (limit,),
    )
    rows = cur.fetchall()
    cur.close()
    return [
        {
            "id": r[0],
            "client_id": r[1] or "",
            "user_email": r[2] or "",
            "category": r[3],
            "message": r[4] or "",
            "page_url": r[5] or "",
            "user_agent": r[6] or "",
            "status": r[7],
            "created_at": r[8].isoformat() if r[8] else "",
        }
        for r in rows
    ]


# --- SMS Automations ---
def db_sms_automations_get_all(client_id: str) -> List[dict]:
    """Get all sms_automations for client_id."""
    if not client_id:
        return []
    conn = _get_conn()
    if not conn:
        return []
    cur = conn.cursor()
    cur.execute("SELECT id, trigger, template, enabled, created_at FROM sms_automations WHERE client_id = %s ORDER BY id", (client_id,))
    rows = cur.fetchall()
    cur.close()
    return [{"id": r[0], "trigger": r[1], "template": r[2], "enabled": bool(r[3]), "created_at": r[4].isoformat() if r[4] else ""} for r in rows]

def db_sms_automations_count(client_id: str) -> int:
    """Count sms_automations for client_id."""
    if not client_id:
        return 0
    conn = _get_conn()
    if not conn:
        return 0
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM sms_automations WHERE client_id = %s", (client_id,))
    row = cur.fetchone()
    cur.close()
    return row[0] if row else 0

def db_sms_automations_get_by_trigger(client_id: str, trigger: str) -> List[dict]:
    """Get enabled automations for client_id and trigger."""
    if not client_id or trigger not in ("after_inquiry", "post_call"):
        return []
    conn = _get_conn()
    if not conn:
        return []
    cur = conn.cursor()
    cur.execute(
        "SELECT id, trigger, template, enabled FROM sms_automations WHERE client_id = %s AND trigger = %s AND enabled = true",
        (client_id, trigger)
    )
    rows = cur.fetchall()
    cur.close()
    return [{"id": r[0], "trigger": r[1], "template": r[2], "enabled": bool(r[3])} for r in rows]

def db_sms_automations_insert(client_id: str, trigger: str, template: str, enabled: bool = True) -> Optional[int]:
    """Insert sms_automation. Returns id or None."""
    if not client_id or trigger not in ("after_inquiry", "post_call"):
        return None
    conn = _get_conn()
    if not conn:
        return None
    try:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO sms_automations (client_id, trigger, template, enabled) VALUES (%s, %s, %s, %s) RETURNING id",
            (client_id, trigger, template, enabled)
        )
        row = cur.fetchone()
        conn.commit()
        cur.close()
        return row[0] if row else None
    except Exception as e:
        print(f"[DB] Failed to insert sms_automation: {e}")
        return None

def db_sms_automations_update(automation_id: int, client_id: str, template: Optional[str] = None, enabled: Optional[bool] = None) -> bool:
    """Update sms_automation. Returns True on success."""
    if not client_id:
        return False
    conn = _get_conn()
    if not conn:
        return False
    try:
        cur = conn.cursor()
        updates = []
        params = []
        if template is not None:
            updates.append("template = %s")
            params.append(template)
        if enabled is not None:
            updates.append("enabled = %s")
            params.append(enabled)
        if not updates:
            cur.close()
            return True
        params.extend([automation_id, client_id])
        cur.execute(
            f"UPDATE sms_automations SET {', '.join(updates)} WHERE id = %s AND client_id = %s",
            params
        )
        conn.commit()
        cur.close()
        return cur.rowcount > 0 if hasattr(cur, 'rowcount') else True
    except Exception as e:
        print(f"[DB] Failed to update sms_automation: {e}")
        return False

def db_sms_automations_delete(automation_id: int, client_id: str) -> bool:
    """Delete sms_automation. Returns True on success."""
    if not client_id:
        return False
    conn = _get_conn()
    if not conn:
        return False
    try:
        cur = conn.cursor()
        cur.execute("DELETE FROM sms_automations WHERE id = %s AND client_id = %s", (automation_id, client_id))
        conn.commit()
        deleted = cur.rowcount > 0
        cur.close()
        return deleted
    except Exception as e:
        print(f"[DB] Failed to delete sms_automation: {e}")
        return False

# --- Overage processed ---
def db_overage_processed_exists(client_id: str, month: str) -> bool:
    """Check if overage was already processed for this client/month."""
    if not client_id or not month:
        return False
    conn = _get_conn()
    if not conn:
        return False
    cur = conn.cursor()
    cur.execute("SELECT 1 FROM overage_processed WHERE client_id = %s AND month = %s LIMIT 1", (client_id, month))
    row = cur.fetchone()
    cur.close()
    return row is not None

def db_overage_processed_insert(client_id: str, month: str) -> bool:
    """Record that overage was processed for this client/month."""
    if not client_id or not month:
        return False
    conn = _get_conn()
    if not conn:
        return False
    try:
        cur = conn.cursor()
        cur.execute("INSERT INTO overage_processed (client_id, month) VALUES (%s, %s) ON CONFLICT DO NOTHING", (client_id, month))
        conn.commit()
        cur.close()
        return True
    except Exception as e:
        print(f"[DB] Failed to insert overage_processed: {e}")
        return False

def db_usage_alert_exists(client_id: str, month: str) -> bool:
    """True if a usage-cap alert has already been recorded for this client/month."""
    if not client_id or not month:
        return False
    conn = _get_conn()
    if not conn:
        return False
    cur = conn.cursor()
    cur.execute("SELECT 1 FROM usage_alert_sent WHERE client_id = %s AND month = %s LIMIT 1", (client_id, month))
    row = cur.fetchone()
    cur.close()
    return row is not None

def db_usage_alert_insert(client_id: str, month: str) -> bool:
    """Record that a usage-cap alert was sent for this client/month (idempotent)."""
    if not client_id or not month:
        return False
    conn = _get_conn()
    if not conn:
        return False
    try:
        cur = conn.cursor()
        cur.execute("INSERT INTO usage_alert_sent (client_id, month) VALUES (%s, %s) ON CONFLICT DO NOTHING", (client_id, month))
        conn.commit()
        cur.close()
        return True
    except Exception as e:
        print(f"[DB] Failed to insert usage_alert_sent: {e}")
        return False

# --- Referral program ---

def _norm_code(code: Optional[str]) -> str:
    return (code or "").strip().upper()


def _norm_email(email: Optional[str]) -> str:
    return (email or "").strip().lower()


def db_signup_payment_method_record(tenant_id: str, card_fingerprint: Optional[str], signup_email: Optional[str]) -> bool:
    """Record a completed signup's card fingerprint + email (global anti-abuse ledger)."""
    conn = _get_conn()
    if not conn:
        return False
    fp = (card_fingerprint or "").strip() or None
    email = _norm_email(signup_email) or None
    if not fp and not email:
        return False
    try:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO signup_payment_methods (tenant_id, card_fingerprint, signup_email) VALUES (%s, %s, %s)",
            (tenant_id, fp, email),
        )
        conn.commit()
        cur.close()
        return True
    except Exception as e:
        print(f"[DB] Failed to record signup payment method: {e}")
        return False


def db_signup_fingerprint_seen(card_fingerprint: Optional[str], exclude_tenant_id: Optional[str] = None) -> bool:
    """True if this card fingerprint was used by a different prior signup."""
    fp = (card_fingerprint or "").strip()
    if not fp:
        return False
    conn = _get_conn()
    if not conn:
        return False
    cur = conn.cursor()
    cur.execute(
        "SELECT 1 FROM signup_payment_methods WHERE card_fingerprint = %s AND (tenant_id IS DISTINCT FROM %s) LIMIT 1",
        (fp, exclude_tenant_id),
    )
    row = cur.fetchone()
    cur.close()
    return row is not None


def db_signup_email_seen(signup_email: Optional[str], exclude_tenant_id: Optional[str] = None) -> bool:
    """True if this email was used by a different prior signup."""
    email = _norm_email(signup_email)
    if not email:
        return False
    conn = _get_conn()
    if not conn:
        return False
    cur = conn.cursor()
    cur.execute(
        "SELECT 1 FROM signup_payment_methods WHERE signup_email = %s AND (tenant_id IS DISTINCT FROM %s) LIMIT 1",
        (email, exclude_tenant_id),
    )
    row = cur.fetchone()
    cur.close()
    return row is not None


def db_referral_code_create(code: str, referrer_name: str, referrer_contact: Optional[str], created_by: Optional[str]) -> Optional[int]:
    """Create a referral code (uppercased). Returns id, or None if it already exists/fails."""
    c = _norm_code(code)
    name = (referrer_name or "").strip()
    if not c or not name:
        return None
    conn = _get_conn()
    if not conn:
        return None
    try:
        cur = conn.cursor()
        cur.execute(
            """INSERT INTO referral_codes (code, referrer_name, referrer_contact, created_by)
               VALUES (%s, %s, %s, %s) ON CONFLICT (code) DO NOTHING RETURNING id""",
            (c, name, (referrer_contact or "").strip() or None, created_by),
        )
        row = cur.fetchone()
        conn.commit()
        cur.close()
        return row[0] if row else None
    except Exception as e:
        print(f"[DB] Failed to create referral code: {e}")
        return None


def db_referral_code_get_by_code(code: str, active_only: bool = True) -> Optional[dict]:
    """Look up a referral code (case-insensitive). active_only filters to active codes."""
    c = _norm_code(code)
    if not c:
        return None
    conn = _get_conn()
    if not conn:
        return None
    cur = conn.cursor()
    sql = "SELECT id, code, referrer_name, referrer_contact, active, created_at FROM referral_codes WHERE code = %s"
    if active_only:
        sql += " AND active = TRUE"
    cur.execute(sql + " LIMIT 1", (c,))
    row = cur.fetchone()
    cur.close()
    if not row:
        return None
    return {
        "id": row[0], "code": row[1], "referrer_name": row[2], "referrer_contact": row[3],
        "active": bool(row[4]), "created_at": row[5].isoformat() if row[5] else None,
    }


def db_referral_codes_list_with_counts() -> List[dict]:
    """List all codes with signup count and converted (paying) count."""
    conn = _get_conn()
    if not conn:
        return []
    cur = conn.cursor()
    cur.execute(
        """
        SELECT c.id, c.code, c.referrer_name, c.referrer_contact, c.active, c.created_at,
               COUNT(r.id) AS signups,
               COUNT(r.id) FILTER (WHERE r.status = 'converted') AS converted,
               COUNT(r.id) FILTER (WHERE r.status = 'flagged') AS flagged
        FROM referral_codes c
        LEFT JOIN referral_redemptions r ON r.referral_code_id = c.id
        GROUP BY c.id
        ORDER BY c.created_at DESC
        """
    )
    rows = cur.fetchall()
    cur.close()
    return [
        {
            "id": r[0], "code": r[1], "referrer_name": r[2], "referrer_contact": r[3],
            "active": bool(r[4]), "created_at": r[5].isoformat() if r[5] else None,
            "signups": int(r[6] or 0), "converted": int(r[7] or 0), "flagged": int(r[8] or 0),
        }
        for r in rows
    ]


def db_referral_code_set_active(code_id: int, active: bool) -> bool:
    conn = _get_conn()
    if not conn:
        return False
    try:
        cur = conn.cursor()
        cur.execute("UPDATE referral_codes SET active = %s WHERE id = %s", (bool(active), code_id))
        ok = cur.rowcount > 0
        conn.commit()
        cur.close()
        return ok
    except Exception as e:
        print(f"[DB] Failed to set referral code active: {e}")
        return False


def _row_to_redemption(row) -> dict:
    return {
        "id": row[0], "tenant_id": row[1], "referral_code_id": row[2],
        "code_snapshot": row[3], "referrer_name_snapshot": row[4], "plan_at_signup": row[5],
        "card_fingerprint": row[6], "signup_email": row[7], "status": row[8],
        "free_month_granted": bool(row[9]), "flagged_reason": row[10],
        "stripe_subscription_id": row[11],
        "first_paid_at": row[12].isoformat() if row[12] else None,
        "created_at": row[13].isoformat() if row[13] else None,
    }


_REDEMPTION_COLS = (
    "id, tenant_id, referral_code_id, code_snapshot, referrer_name_snapshot, plan_at_signup, "
    "card_fingerprint, signup_email, status, free_month_granted, flagged_reason, "
    "stripe_subscription_id, first_paid_at, created_at"
)


def db_referral_redemption_create(
    tenant_id: str, referral_code_id: int, code_snapshot: str, referrer_name_snapshot: str,
    plan_at_signup: Optional[str], stripe_subscription_id: Optional[str],
) -> Optional[int]:
    """Create a redemption (one per tenant). Returns id, or existing id on conflict."""
    if not tenant_id:
        return None
    conn = _get_conn()
    if not conn:
        return None
    try:
        cur = conn.cursor()
        cur.execute(
            """INSERT INTO referral_redemptions
               (tenant_id, referral_code_id, code_snapshot, referrer_name_snapshot, plan_at_signup, stripe_subscription_id)
               VALUES (%s, %s, %s, %s, %s, %s)
               ON CONFLICT (tenant_id) DO NOTHING RETURNING id""",
            (tenant_id, referral_code_id, _norm_code(code_snapshot), referrer_name_snapshot, plan_at_signup, stripe_subscription_id),
        )
        row = cur.fetchone()
        conn.commit()
        if row:
            cur.close()
            return row[0]
        # Already existed — return its id.
        cur.execute("SELECT id FROM referral_redemptions WHERE tenant_id = %s", (tenant_id,))
        existing = cur.fetchone()
        cur.close()
        return existing[0] if existing else None
    except Exception as e:
        print(f"[DB] Failed to create referral redemption: {e}")
        return None


def db_referral_redemption_get_by_tenant(tenant_id: str) -> Optional[dict]:
    if not tenant_id:
        return None
    conn = _get_conn()
    if not conn:
        return None
    cur = conn.cursor()
    cur.execute(f"SELECT {_REDEMPTION_COLS} FROM referral_redemptions WHERE tenant_id = %s", (tenant_id,))
    row = cur.fetchone()
    cur.close()
    return _row_to_redemption(row) if row else None


def db_referral_redemption_get_by_subscription(sub_id: str) -> Optional[dict]:
    if not sub_id:
        return None
    conn = _get_conn()
    if not conn:
        return None
    cur = conn.cursor()
    cur.execute(f"SELECT {_REDEMPTION_COLS} FROM referral_redemptions WHERE stripe_subscription_id = %s LIMIT 1", (sub_id,))
    row = cur.fetchone()
    cur.close()
    return _row_to_redemption(row) if row else None


def db_referral_redemption_update(redemption_id: int, **fields) -> bool:
    """Partial update of a redemption row. Allowed fields only."""
    allowed = {
        "status", "free_month_granted", "flagged_reason", "card_fingerprint",
        "signup_email", "first_paid_at", "stripe_subscription_id",
    }
    sets = []
    params: list = []
    for k, v in fields.items():
        if k not in allowed:
            continue
        if k == "signup_email":
            v = _norm_email(v) or None
        sets.append(f"{k} = %s")
        params.append(v)
    if not sets:
        return True
    conn = _get_conn()
    if not conn:
        return False
    try:
        cur = conn.cursor()
        params.append(redemption_id)
        cur.execute(f"UPDATE referral_redemptions SET {', '.join(sets)} WHERE id = %s", params)
        ok = cur.rowcount > 0
        conn.commit()
        cur.close()
        return ok
    except Exception as e:
        print(f"[DB] Failed to update referral redemption: {e}")
        return False


def db_referral_commission_insert(
    redemption_id: int, kind: str, period_key: str, amount_cents: int,
    plan_snapshot: Optional[str], code_snapshot: str, referrer_name_snapshot: str,
) -> Optional[int]:
    """Insert a commission line item. Idempotent on (redemption_id, kind, period_key)."""
    if kind not in ("signup_bounty", "mrr") or amount_cents < 0:
        return None
    conn = _get_conn()
    if not conn:
        return None
    try:
        cur = conn.cursor()
        cur.execute(
            """INSERT INTO referral_commissions
               (redemption_id, kind, period_key, amount_cents, plan_snapshot, code_snapshot, referrer_name_snapshot)
               VALUES (%s, %s, %s, %s, %s, %s, %s)
               ON CONFLICT (redemption_id, kind, period_key) DO NOTHING RETURNING id""",
            (redemption_id, kind, period_key, int(amount_cents), plan_snapshot, _norm_code(code_snapshot), referrer_name_snapshot),
        )
        row = cur.fetchone()
        conn.commit()
        cur.close()
        return row[0] if row else None
    except Exception as e:
        print(f"[DB] Failed to insert referral commission: {e}")
        return None


def db_referral_commission_count_mrr(redemption_id: int) -> int:
    conn = _get_conn()
    if not conn:
        return 0
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM referral_commissions WHERE redemption_id = %s AND kind = 'mrr'", (redemption_id,))
    row = cur.fetchone()
    cur.close()
    return int(row[0]) if row else 0


def db_referral_commissions_list_all(include_paid: bool = True) -> List[dict]:
    """All commission line items joined to the referred business name (best-effort)."""
    conn = _get_conn()
    if not conn:
        return []
    cur = conn.cursor()
    sql = """
        SELECT cm.id, cm.kind, cm.period_key, cm.amount_cents, cm.plan_snapshot,
               cm.code_snapshot, cm.referrer_name_snapshot, cm.paid, cm.paid_at, cm.created_at,
               t.name AS business_name
        FROM referral_commissions cm
        LEFT JOIN referral_redemptions r ON r.id = cm.redemption_id
        LEFT JOIN tenants t ON t.id::text = r.tenant_id
    """
    if not include_paid:
        sql += " WHERE cm.paid = FALSE"
    sql += " ORDER BY cm.created_at DESC"
    cur.execute(sql)
    rows = cur.fetchall()
    cur.close()
    return [
        {
            "id": r[0], "kind": r[1], "period_key": r[2], "amount_cents": int(r[3] or 0),
            "plan_snapshot": r[4], "code_snapshot": r[5], "referrer_name": r[6],
            "paid": bool(r[7]), "paid_at": r[8].isoformat() if r[8] else None,
            "created_at": r[9].isoformat() if r[9] else None, "business_name": r[10],
        }
        for r in rows
    ]


def db_referral_commission_mark_paid(commission_id: int) -> bool:
    conn = _get_conn()
    if not conn:
        return False
    try:
        cur = conn.cursor()
        cur.execute(
            "UPDATE referral_commissions SET paid = TRUE, paid_at = NOW() WHERE id = %s AND paid = FALSE",
            (commission_id,),
        )
        conn.commit()
        cur.close()
        return True
    except Exception as e:
        print(f"[DB] Failed to mark commission paid: {e}")
        return False

# --- Failed-event / incident log ---

def db_failed_event_insert(source: str, event_type: Optional[str], ref: Optional[str],
                           error: Optional[str], payload: Optional[dict] = None) -> Optional[int]:
    """Record a swallowed failure (webhook/cron/task) for visibility + manual retry."""
    conn = _get_conn()
    if not conn:
        return None
    try:
        cur = conn.cursor()
        payload_json = json.dumps(payload) if payload is not None else None
        cur.execute(
            """INSERT INTO failed_events (source, event_type, ref, error, payload)
               VALUES (%s, %s, %s, %s, %s) RETURNING id""",
            (source, event_type, ref, (error or "")[:2000], payload_json),
        )
        row = cur.fetchone()
        conn.commit()
        cur.close()
        return row[0] if row else None
    except Exception as e:
        print(f"[DB] Failed to insert failed_event: {e}")
        try:
            conn.rollback()
        except Exception:
            pass
        return None


def db_failed_events_list(include_resolved: bool = False, limit: int = 100) -> List[dict]:
    conn = _get_conn()
    if not conn:
        return []
    cur = conn.cursor()
    sql = "SELECT id, source, event_type, ref, error, resolved, resolved_at, created_at FROM failed_events"
    if not include_resolved:
        sql += " WHERE resolved = FALSE"
    sql += " ORDER BY created_at DESC LIMIT %s"
    cur.execute(sql, (limit,))
    rows = cur.fetchall()
    cur.close()
    return [
        {
            "id": r[0], "source": r[1], "event_type": r[2], "ref": r[3],
            "error": r[4], "resolved": bool(r[5]),
            "resolved_at": r[6].isoformat() if r[6] else None,
            "created_at": r[7].isoformat() if r[7] else None,
        }
        for r in rows
    ]


def db_failed_events_unresolved_count() -> int:
    conn = _get_conn()
    if not conn:
        return 0
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM failed_events WHERE resolved = FALSE")
    row = cur.fetchone()
    cur.close()
    return int(row[0]) if row else 0


def db_failed_event_resolve(event_id: int) -> bool:
    conn = _get_conn()
    if not conn:
        return False
    try:
        cur = conn.cursor()
        cur.execute(
            "UPDATE failed_events SET resolved = TRUE, resolved_at = NOW() WHERE id = %s AND resolved = FALSE",
            (event_id,),
        )
        conn.commit()
        cur.close()
        return True
    except Exception as e:
        print(f"[DB] Failed to resolve failed_event: {e}")
        return False

# --- Call log ---
def db_call_log_append(entry: dict) -> None:
    conn = _get_conn()
    if not conn:
        return
    try:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO call_log (client_id, call_sid, from_number, to_number, start_iso, end_iso, outcome, duration_sec, category,
                recording_sid, recording_url, recording_duration_sec, recording_status, call_summary)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            ON CONFLICT (call_sid) DO UPDATE SET
                end_iso = EXCLUDED.end_iso,
                outcome = COALESCE(EXCLUDED.outcome, call_log.outcome),
                duration_sec = EXCLUDED.duration_sec,
                recording_sid = COALESCE(EXCLUDED.recording_sid, call_log.recording_sid),
                recording_url = COALESCE(EXCLUDED.recording_url, call_log.recording_url),
                recording_duration_sec = COALESCE(EXCLUDED.recording_duration_sec, call_log.recording_duration_sec),
                recording_status = COALESCE(EXCLUDED.recording_status, call_log.recording_status),
                call_summary = COALESCE(EXCLUDED.call_summary, call_log.call_summary)
        """, (
            _client_id(), entry.get("call_sid"), entry.get("from_number"), entry.get("to_number"),
            entry.get("start_iso"), entry.get("end_iso"), entry.get("outcome"),
            entry.get("duration_sec"), entry.get("category"),
            entry.get("recording_sid"), entry.get("recording_url"), entry.get("recording_duration_sec"),
            entry.get("recording_status"), entry.get("call_summary"),
        ))
        conn.commit()
        cur.close()
    except Exception as e:
        print(f"[DB] Failed to append call log: {e}")

def db_call_log_load(limit: int = 5000, days: Optional[int] = None) -> List[dict]:
    """Load call log. If days is set, only include entries from last N days."""
    conn = _get_conn()
    if not conn:
        return []
    cur = conn.cursor()
    cid = _client_id()
    cols = """call_sid, from_number, to_number, start_iso, end_iso, outcome, duration_sec, category, created_at,
        recording_sid, recording_url, recording_duration_sec, recording_status, call_summary"""
    if days is not None and days > 0:
        cur.execute(f"""
            SELECT {cols}
            FROM call_log
            WHERE client_id = %s AND created_at >= NOW() - make_interval(days => %s::int)
            ORDER BY created_at DESC LIMIT %s
        """, (cid, days, limit))
    else:
        cur.execute(f"""
            SELECT {cols}
            FROM call_log WHERE client_id = %s ORDER BY created_at DESC LIMIT %s
        """, (cid, limit))
    rows = cur.fetchall()
    cur.close()
    def _row(r):
        return {
            "call_sid": r[0], "from_number": r[1], "to_number": r[2], "start_iso": r[3], "end_iso": r[4],
            "outcome": r[5], "duration_sec": r[6], "category": r[7],
            "created_at": r[8].isoformat() if len(r) > 8 and r[8] else None,
            "recording_sid": r[9] if len(r) > 9 else None,
            "recording_url": r[10] if len(r) > 10 else None,
            "recording_duration_sec": r[11] if len(r) > 11 else None,
            "recording_status": r[12] if len(r) > 12 else None,
            "call_summary": r[13] if len(r) > 13 else None,
        }
    return [_row(r) for r in rows]


def db_call_log_update_recording(
    call_sid: str,
    client_id: str,
    recording_sid: Optional[str] = None,
    recording_url: Optional[str] = None,
    recording_duration_sec: Optional[int] = None,
    recording_status: Optional[str] = None,
) -> bool:
    """Upsert recording metadata. Row may not exist yet if Twilio callbacks before call_log_end."""
    conn = _get_conn()
    if not conn:
        return False
    try:
        cur = conn.cursor()
        cur.execute("""
            INSERT INTO call_log (client_id, call_sid, recording_sid, recording_url, recording_duration_sec, recording_status)
            VALUES (%s, %s, %s, %s, %s, %s)
            ON CONFLICT (call_sid) DO UPDATE SET
                recording_sid = COALESCE(EXCLUDED.recording_sid, call_log.recording_sid),
                recording_url = COALESCE(EXCLUDED.recording_url, call_log.recording_url),
                recording_duration_sec = COALESCE(EXCLUDED.recording_duration_sec, call_log.recording_duration_sec),
                recording_status = COALESCE(EXCLUDED.recording_status, call_log.recording_status)
        """, (client_id, call_sid, recording_sid, recording_url, recording_duration_sec, recording_status))
        conn.commit()
        cur.close()
        return True
    except Exception as e:
        print(f"[DB] Failed to upsert call_log recording: {e}")
        return False


def db_call_log_update_summary(call_sid: str, client_id: str, call_summary: str) -> bool:
    conn = _get_conn()
    if not conn:
        return False
    try:
        cur = conn.cursor()
        cur.execute(
            "UPDATE call_log SET call_summary = %s WHERE call_sid = %s AND client_id = %s",
            (call_summary, call_sid, client_id),
        )
        ok = cur.rowcount > 0
        conn.commit()
        cur.close()
        return ok
    except Exception as e:
        print(f"[DB] Failed to update call_log summary: {e}")
        return False


def db_call_log_get_client_id_by_call_sid(call_sid: str) -> Optional[str]:
    """Resolve tenant client_id for a call (e.g. async recording webhook)."""
    conn = _get_conn()
    if not conn or not call_sid:
        return None
    try:
        cur = conn.cursor()
        cur.execute("SELECT client_id FROM call_log WHERE call_sid = %s LIMIT 1", (call_sid,))
        row = cur.fetchone()
        cur.close()
        return row[0] if row else None
    except Exception as e:
        print(f"[DB] Failed to lookup call_log client_id: {e}")
        return None


def db_call_log_get_by_call_sid(client_id: str, call_sid: str) -> Optional[dict]:
    """Single row for playback auth check."""
    conn = _get_conn()
    if not conn:
        return None
    try:
        cur = conn.cursor()
        cur.execute("""
            SELECT call_sid, from_number, to_number, start_iso, end_iso, outcome, duration_sec, category, created_at,
                recording_sid, recording_url, recording_duration_sec, recording_status, call_summary
            FROM call_log WHERE call_sid = %s AND client_id = %s LIMIT 1
        """, (call_sid, client_id))
        row = cur.fetchone()
        cur.close()
        if not row:
            return None
        return {
            "call_sid": row[0], "from_number": row[1], "to_number": row[2], "start_iso": row[3], "end_iso": row[4],
            "outcome": row[5], "duration_sec": row[6], "category": row[7],
            "created_at": row[8].isoformat() if row[8] else None,
            "recording_sid": row[9], "recording_url": row[10], "recording_duration_sec": row[11],
            "recording_status": row[12], "call_summary": row[13],
        }
    except Exception as e:
        print(f"[DB] Failed to get call_log row: {e}")
        return None

# --- Caller memory ---
def db_caller_memory_get(phone: str) -> Optional[dict]:
    conn = _get_conn()
    if not conn:
        return None
    key = _normalize_phone(phone)
    if not key:
        return None
    cur = conn.cursor()
    cur.execute("SELECT name, call_count, last_call_iso, last_reason, data FROM caller_memory WHERE phone = %s AND client_id = %s", (key, _client_id()))
    row = cur.fetchone()
    cur.close()
    if not row:
        return None
    data = row[4]
    if isinstance(data, str):
        try:
            data = json.loads(data) if data else {}
        except Exception:
            data = {}
    return {"name": row[0], "call_count": row[1], "last_call_iso": row[2], "last_reason": row[3], **(data or {})}

def db_caller_memory_upsert(
    phone: str,
    name: Optional[str] = None,
    last_reason: Optional[str] = None,
    increment_count: bool = True,
    data_patch: Optional[dict] = None,
) -> None:
    conn = _get_conn()
    if not conn:
        return
    key = _normalize_phone(phone)
    if not key:
        return
    try:
        from psycopg2.extras import Json
    except ImportError:
        Json = None  # type: ignore
    cur = conn.cursor()
    cur.execute(
        "SELECT call_count, data FROM caller_memory WHERE phone = %s AND client_id = %s",
        (key, _client_id()),
    )
    row = cur.fetchone()
    now = datetime.now().isoformat()

    def _parse_data(raw: Any) -> dict:
        if isinstance(raw, dict):
            return dict(raw)
        if isinstance(raw, str) and raw.strip():
            try:
                return dict(json.loads(raw))
            except Exception:
                return {}
        return {}

    name_clean = (name or "").strip() if name is not None else ""
    if row:
        base_count, raw_data = row[0], row[1]
        count = int(base_count or 0) + (1 if increment_count else 0)
        merged = _parse_data(raw_data)
        if data_patch:
            merged = {**merged, **data_patch}
        payload = Json(merged) if Json else json.dumps(merged, default=str)
        if name_clean:
            cur.execute(
                """
                UPDATE caller_memory SET name = %s, call_count = %s, last_call_iso = %s,
                    last_reason = COALESCE(%s, last_reason), data = %s::jsonb, updated_at = NOW()
                WHERE phone = %s AND client_id = %s
                """,
                (name_clean, count, now, last_reason, payload, key, _client_id()),
            )
        else:
            cur.execute(
                """
                UPDATE caller_memory SET call_count = %s, last_call_iso = %s,
                    last_reason = COALESCE(%s, last_reason), data = %s::jsonb, updated_at = NOW()
                WHERE phone = %s AND client_id = %s
                """,
                (count, now, last_reason, payload, key, _client_id()),
            )
    else:
        merged = dict(data_patch or {})
        payload = Json(merged) if Json else json.dumps(merged, default=str)
        start_count = 1 if increment_count else 0
        cur.execute(
            """
            INSERT INTO caller_memory (phone, client_id, name, call_count, last_call_iso, last_reason, data)
            VALUES (%s, %s, %s, %s, %s, %s, %s::jsonb)
            """,
            (key, _client_id(), name or "", start_count, now, last_reason or "", payload),
        )
    conn.commit()
    cur.close()

# --- Booked slots ---
def db_booked_slots_load() -> List[dict]:
    conn = _get_conn()
    if not conn:
        return []
    cur = conn.cursor()
    cur.execute(
        "SELECT date, time, appointment_id, duration_minutes, staff_id FROM booked_slots WHERE client_id = %s",
        (_client_id(),),
    )
    rows = cur.fetchall()
    cur.close()
    return [
        {
            "date": r[0],
            "time": r[1],
            "appointment_id": r[2],
            "duration_minutes": r[3] or 30,
            "staff_id": r[4] if len(r) > 4 else None,
        }
        for r in rows
    ]

def db_booked_slots_save(slots: List[dict]) -> None:
    """Bulk rewrite of a tenant's calendar holds (used by reconcile). ON CONFLICT DO
    NOTHING keeps it safe against the unique slot index. Prefer the incremental
    db_booked_slot_reserve / db_booked_slot_release for single-booking writes — they
    avoid the lost-update race of delete-all + re-insert under concurrency."""
    conn = _get_conn()
    if not conn:
        return
    cur = conn.cursor()
    cur.execute("DELETE FROM booked_slots WHERE client_id = %s", (_client_id(),))
    for s in slots:
        cur.execute(
            "INSERT INTO booked_slots (client_id, date, time, appointment_id, duration_minutes, staff_id) "
            "VALUES (%s, %s, %s, %s, %s, %s) "
            "ON CONFLICT (client_id, date, time, (COALESCE(staff_id, ''))) DO NOTHING",
            (
                _client_id(),
                s["date"],
                s["time"],
                s["appointment_id"],
                s.get("duration_minutes", 30),
                s.get("staff_id"),
            ),
        )
    conn.commit()
    cur.close()


def db_booked_slot_reserve(
    date: str, time: str, appointment_id: int, duration_minutes: int = 30, staff_id: Optional[str] = None
) -> bool:
    """Atomically claim one calendar slot. Returns True if reserved, False if the slot
    was already taken (the unique index rejected it) — closing the check-then-reserve
    race between two simultaneous bookings. A single INSERT, not read-modify-write."""
    conn = _get_conn()
    if not conn:
        return False
    cur = conn.cursor()
    cur.execute(
        "INSERT INTO booked_slots (client_id, date, time, appointment_id, duration_minutes, staff_id) "
        "VALUES (%s, %s, %s, %s, %s, %s) "
        "ON CONFLICT (client_id, date, time, (COALESCE(staff_id, ''))) DO NOTHING",
        (_client_id(), date, time, appointment_id, duration_minutes, staff_id),
    )
    reserved = cur.rowcount == 1
    conn.commit()
    cur.close()
    return reserved


def db_booked_slot_release(appointment_id: int) -> None:
    """Release a single calendar hold by appointment id (incremental delete)."""
    conn = _get_conn()
    if not conn:
        return
    cur = conn.cursor()
    cur.execute(
        "DELETE FROM booked_slots WHERE client_id = %s AND appointment_id = %s",
        (_client_id(), appointment_id),
    )
    conn.commit()
    cur.close()


# --- Conversational SMS session caps (billing-period scoped) ---
def db_conversational_sms_reserve_session(
    client_id: str,
    billing_period_key: str,
    phone_normalized: str,
    session_cap: int,
) -> dict:
    """
    Atomically allow an inbound conversational SMS session or deny when at cap.

    Returns dict: allowed (bool), is_new_session (bool), session_count (int), at_cap (bool).
    """
    if not client_id or not billing_period_key or not phone_normalized:
        return {"allowed": False, "is_new_session": False, "session_count": 0, "at_cap": True}
    cap = max(0, int(session_cap))
    conn = _get_conn()
    if not conn:
        return {"allowed": True, "is_new_session": True, "session_count": 0, "at_cap": False}
    try:
        cur = conn.cursor()
        cur.execute("BEGIN")
        cur.execute(
            """
            SELECT 1 FROM conversational_sms_session_keys
            WHERE client_id = %s AND billing_period_key = %s AND phone_normalized = %s
            """,
            (client_id, billing_period_key, phone_normalized),
        )
        if cur.fetchone():
            cur.execute(
                """
                SELECT session_count FROM conversational_sms_period_usage
                WHERE client_id = %s AND billing_period_key = %s
                """,
                (client_id, billing_period_key),
            )
            row = cur.fetchone()
            count = int(row[0]) if row else 0
            cur.execute("COMMIT")
            cur.close()
            return {
                "allowed": True,
                "is_new_session": False,
                "session_count": count,
                "at_cap": count >= cap if cap > 0 else False,
            }
        cur.execute(
            """
            INSERT INTO conversational_sms_period_usage (client_id, billing_period_key, session_count)
            VALUES (%s, %s, 0)
            ON CONFLICT (client_id, billing_period_key) DO NOTHING
            """,
            (client_id, billing_period_key),
        )
        cur.execute(
            """
            SELECT session_count FROM conversational_sms_period_usage
            WHERE client_id = %s AND billing_period_key = %s
            FOR UPDATE
            """,
            (client_id, billing_period_key),
        )
        row = cur.fetchone()
        current = int(row[0]) if row else 0
        if cap <= 0 or current >= cap:
            cur.execute("ROLLBACK")
            cur.close()
            return {
                "allowed": False,
                "is_new_session": True,
                "session_count": current,
                "at_cap": True,
            }
        cur.execute(
            """
            INSERT INTO conversational_sms_session_keys (client_id, billing_period_key, phone_normalized)
            VALUES (%s, %s, %s)
            """,
            (client_id, billing_period_key, phone_normalized),
        )
        cur.execute(
            """
            UPDATE conversational_sms_period_usage
            SET session_count = session_count + 1, updated_at = NOW()
            WHERE client_id = %s AND billing_period_key = %s
            RETURNING session_count
            """,
            (client_id, billing_period_key),
        )
        new_count = int(cur.fetchone()[0])
        cur.execute("COMMIT")
        cur.close()
        return {
            "allowed": True,
            "is_new_session": True,
            "session_count": new_count,
            "at_cap": new_count >= cap if cap > 0 else False,
        }
    except Exception as e:
        try:
            conn.rollback()
        except Exception:
            pass
        print(f"[DB] conversational_sms_reserve_session failed: {e}")
        return {"allowed": False, "is_new_session": False, "session_count": 0, "at_cap": True}


def db_conversational_sms_session_count(client_id: str, billing_period_key: str) -> int:
    """Current session count for client/period (for tests and diagnostics)."""
    conn = _get_conn()
    if not conn:
        return 0
    cur = conn.cursor()
    cur.execute(
        """
        SELECT session_count FROM conversational_sms_period_usage
        WHERE client_id = %s AND billing_period_key = %s
        """,
        (client_id, billing_period_key),
    )
    row = cur.fetchone()
    cur.close()
    return int(row[0]) if row else 0


def db_conversational_sms_clear_period(client_id: str, billing_period_key: str) -> None:
    """Test helper: reset conversational session counters for a period."""
    conn = _get_conn()
    if not conn:
        return
    cur = conn.cursor()
    cur.execute(
        "DELETE FROM conversational_sms_session_keys WHERE client_id = %s AND billing_period_key = %s",
        (client_id, billing_period_key),
    )
    cur.execute(
        "DELETE FROM conversational_sms_period_usage WHERE client_id = %s AND billing_period_key = %s",
        (client_id, billing_period_key),
    )
    conn.commit()
    cur.close()


# --- Cron run tracking (ops visibility) ---

CRON_JOB_NAMES = (
    "appointment-reminders",
    "process-overage",
    "retention-purge",
    "export-snapshot",
)

DAILY_CRON_JOBS = frozenset({"appointment-reminders", "retention-purge", "export-snapshot"})


def db_cron_run_start(job_name: str) -> Optional[int]:
    """Record cron job start. Returns run id or None."""
    conn = _get_conn()
    if not conn:
        return None
    try:
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO cron_runs (job_name, status)
            VALUES (%s, 'running')
            RETURNING id
            """,
            (job_name,),
        )
        row = cur.fetchone()
        conn.commit()
        cur.close()
        return int(row[0]) if row else None
    except Exception as e:
        _log.warning("db_cron_run_start failed: %s", e)
        try:
            conn.rollback()
        except Exception:
            pass
        return None


def db_cron_run_finish(run_id: Optional[int], status: str, summary: Optional[dict] = None) -> None:
    """Mark cron run complete."""
    if not run_id:
        return
    conn = _get_conn()
    if not conn:
        return
    try:
        cur = conn.cursor()
        cur.execute(
            """
            UPDATE cron_runs
            SET finished_at = NOW(), status = %s, summary = %s
            WHERE id = %s
            """,
            (status, json.dumps(summary or {}), run_id),
        )
        conn.commit()
        cur.close()
    except Exception as e:
        _log.warning("db_cron_run_finish failed: %s", e)
        try:
            conn.rollback()
        except Exception:
            pass


def db_cron_runs_last_success() -> dict:
    """Return last successful finish time per known cron job (ISO strings)."""
    conn = _get_conn()
    if not conn:
        return {}
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT DISTINCT ON (job_name) job_name, finished_at
            FROM cron_runs
            WHERE status = 'success' AND finished_at IS NOT NULL
            ORDER BY job_name, finished_at DESC
            """
        )
        rows = cur.fetchall()
        cur.close()
        out: dict = {}
        for job_name, finished_at in rows:
            if finished_at:
                out[job_name] = finished_at.isoformat()
        return out
    except Exception as e:
        _log.warning("db_cron_runs_last_success failed: %s", e)
        return {}


# --- Background provisioning (bulk onboarding) -------------------------------

import json as _json


def db_provisioning_job_create(job_id: str, created_by: Optional[str], total: int) -> bool:
    conn = _get_conn()
    if not conn:
        return False
    try:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO provisioning_jobs (id, created_by, total, status) "
            "VALUES (%s, %s, %s, 'pending')",
            (job_id, created_by, int(total)),
        )
        conn.commit()
        cur.close()
        return True
    except Exception as e:
        _log.warning("db_provisioning_job_create failed: %s", e)
        return False


def db_provisioning_task_create(
    job_id: str,
    client_id: str,
    *,
    name: Optional[str] = None,
    email: Optional[str] = None,
    area_code: Optional[str] = None,
    plan: str = "free",
) -> Optional[int]:
    conn = _get_conn()
    if not conn:
        return None
    try:
        cur = conn.cursor()
        cur.execute(
            "INSERT INTO provisioning_tasks (job_id, client_id, name, email, area_code, plan) "
            "VALUES (%s, %s, %s, %s, %s, %s) RETURNING id",
            (job_id, client_id, name, email, area_code, plan),
        )
        row = cur.fetchone()
        conn.commit()
        cur.close()
        return int(row[0]) if row else None
    except Exception as e:
        _log.warning("db_provisioning_task_create failed: %s", e)
        return None


def _row_to_provisioning_task(r) -> dict:
    steps = r[8]
    if isinstance(steps, str):
        try:
            steps = _json.loads(steps)
        except Exception:
            steps = []
    return {
        "id": r[0],
        "job_id": r[1],
        "client_id": r[2],
        "name": r[3],
        "email": r[4],
        "area_code": r[5],
        "plan": r[6],
        "status": r[7],
        "steps_done": steps or [],
        "phone_e164": r[9],
        "error": r[10],
        "attempts": r[11],
    }


_PROVISIONING_TASK_COLS = (
    "id, job_id, client_id, name, email, area_code, plan, status, steps_done, "
    "phone_e164, error, attempts"
)


def db_provisioning_tasks_for_job(job_id: str, *, only_unfinished: bool = False) -> List[dict]:
    conn = _get_conn()
    if not conn:
        return []
    try:
        cur = conn.cursor()
        sql = f"SELECT {_PROVISIONING_TASK_COLS} FROM provisioning_tasks WHERE job_id = %s"
        if only_unfinished:
            sql += " AND status <> 'done'"
        sql += " ORDER BY id"
        cur.execute(sql, (job_id,))
        rows = cur.fetchall()
        cur.close()
        return [_row_to_provisioning_task(r) for r in rows]
    except Exception as e:
        _log.warning("db_provisioning_tasks_for_job failed: %s", e)
        return []


def db_provisioning_task_save(
    task_id: int,
    *,
    status: str,
    steps_done: List[str],
    phone_e164: Optional[str] = None,
    error: Optional[str] = None,
    attempts: Optional[int] = None,
) -> bool:
    conn = _get_conn()
    if not conn:
        return False
    try:
        cur = conn.cursor()
        if attempts is None:
            cur.execute(
                "UPDATE provisioning_tasks SET status=%s, steps_done=%s::jsonb, "
                "phone_e164=COALESCE(%s, phone_e164), error=%s, "
                "attempts=attempts+1, updated_at=NOW() WHERE id=%s",
                (status, _json.dumps(steps_done or []), phone_e164, error, task_id),
            )
        else:
            cur.execute(
                "UPDATE provisioning_tasks SET status=%s, steps_done=%s::jsonb, "
                "phone_e164=COALESCE(%s, phone_e164), error=%s, "
                "attempts=%s, updated_at=NOW() WHERE id=%s",
                (status, _json.dumps(steps_done or []), phone_e164, error, int(attempts), task_id),
            )
        conn.commit()
        cur.close()
        return True
    except Exception as e:
        _log.warning("db_provisioning_task_save failed: %s", e)
        return False


def db_provisioning_job_set_status(job_id: str, status: str) -> bool:
    conn = _get_conn()
    if not conn:
        return False
    try:
        cur = conn.cursor()
        cur.execute(
            "UPDATE provisioning_jobs SET status=%s, updated_at=NOW() WHERE id=%s",
            (status, job_id),
        )
        conn.commit()
        cur.close()
        return True
    except Exception as e:
        _log.warning("db_provisioning_job_set_status failed: %s", e)
        return False


def db_provisioning_job_get(job_id: str) -> Optional[dict]:
    """Return the job with a per-status task count summary, or None if missing."""
    conn = _get_conn()
    if not conn:
        return None
    try:
        cur = conn.cursor()
        cur.execute(
            "SELECT id, created_by, total, status, created_at, updated_at "
            "FROM provisioning_jobs WHERE id = %s",
            (job_id,),
        )
        row = cur.fetchone()
        if not row:
            cur.close()
            return None
        cur.execute(
            "SELECT status, COUNT(*)::int FROM provisioning_tasks "
            "WHERE job_id = %s GROUP BY status",
            (job_id,),
        )
        counts = {s: n for s, n in cur.fetchall()}
        cur.close()
        return {
            "id": row[0],
            "created_by": row[1],
            "total": row[2],
            "status": row[3],
            "created_at": row[4].isoformat() if row[4] else None,
            "updated_at": row[5].isoformat() if row[5] else None,
            "counts": counts,
        }
    except Exception as e:
        _log.warning("db_provisioning_job_get failed: %s", e)
        return None


# ---------------------------------------------------------------------------
# Apply per-call connection scoping to every public db_* query function.
# Done once at import, after all functions are defined. Wrapping the module
# globals (not just exposing new names) means inter-function calls — e.g.
# db_tenant_member_add -> db_tenant_member_assign_owner — also go through the
# scope, and the reentrant depth counter keeps them on one shared connection.
# ---------------------------------------------------------------------------
for _name, _obj in list(globals().items()):
    if (
        _name.startswith("db_")
        and _name not in _SCOPE_EXCLUDE
        and callable(_obj)
        and getattr(_obj, "__module__", None) == __name__
        and not getattr(_obj, "_db_scoped", False)
    ):
        globals()[_name] = _scoped(_obj)
del _name, _obj
