'use client'

import { useState, useEffect, useCallback, useMemo } from 'react'
import { useAuth } from '@clerk/nextjs'
import Link from 'next/link'
import { motion, useReducedMotion } from 'framer-motion'
import { useApiClient, sameOriginApiConfig } from '@/lib/api'
import { formatTrialEndDate } from '@/lib/formatTrialEnd'
import { AppChrome } from '@/components/layout/AppChrome'
import { ProvisioningPanel } from '@/components/admin/ProvisioningPanel'
import { type Org } from '@/components/admin/OrgsPanel'
import { ReferralsPanel } from '@/components/admin/ReferralsPanel'
import { SystemHealthPanel } from '@/components/admin/SystemHealthPanel'
import { AuditLog } from '@/components/admin/AuditLog'
import { FleetHealthSummary } from '@/components/admin/FleetHealthSummary'
import { CollapsibleSection } from '@/components/ui/CollapsibleSection'
import { ChevronDown, ChevronRight } from 'lucide-react'
import { TenantRow, accessStatusLabel, accessStatusClass } from '@/components/admin/TenantRow'
import { useTenantAdmin } from '@/components/admin/useTenantAdmin'
import {
  US_E164_PREFIX,
  inputClass,
  selectClass,
  digitsOnly,
  fullUsE164FromNationalInput,
  nationalDigitsForUsTwilioInput,
  isUsTenantTwilioDraft,
  UsTwilioPhoneInput,
} from '@/components/admin/tenantFields'
import type { Tenant } from '@/components/admin/tenantTypes'

/** Logs invite/relink debug JSON in browser console when set on Vercel. */
const DEBUG_ADMIN = process.env.NEXT_PUBLIC_DEBUG_ADMIN === '1'

function debugLogAdmin(label: string, payload: unknown) {
  if (!DEBUG_ADMIN) return
  console.info(`[admin-debug] ${label}`, payload)
}

type InviteLinkResult = {
  invite_sent?: boolean
  user_relinked?: boolean
  clerk_error?: string | null
  linked_clerk_user_id?: string | null
  linked_clerk_user_ids?: string[] | null
  clerk_users_matched_count?: number
}

type OpsSelfCheck = {
  frontend_url?: string | null
  frontend_url_ok?: boolean
  public_base_url_set?: boolean
  twilio_signature_validation_enabled?: boolean
  cron_secret_set?: boolean
  multi_tenant_client_id_env_ok?: boolean
  database_enabled?: boolean
  last_cron_runs?: Record<string, string>
  stale_cron_jobs?: string[]
  cron_jobs_healthy?: boolean
  redis_url_set?: boolean
  redis_url_scheme_ok?: boolean
  redis_ping_ok?: boolean
  voice_state_backend?: 'redis' | 'memory'
  redis_config_consistent?: boolean
  redis_host_looks_external?: boolean
  redis_production_ready?: boolean
  clerk_issuer_set?: boolean
  clerk_audience_set?: boolean
  deepgram_ready?: boolean
}

function opsCheckRow(label: string, ok: boolean | undefined, detail?: string) {
  const pass = ok === true
  return (
    <div className="flex items-start justify-between gap-3 rounded-lg border border-white/10 bg-zinc-950/60 px-3 py-2">
      <div>
        <p className="text-sm text-zinc-200">{label}</p>
        {detail ? <p className="mt-0.5 text-xs text-zinc-500">{detail}</p> : null}
      </div>
      <span
        className={`shrink-0 rounded-full px-2 py-0.5 text-xs font-medium ${
          pass ? 'bg-emerald-500/15 text-emerald-300' : 'bg-red-500/15 text-red-300'
        }`}
      >
        {pass ? 'OK' : 'Check'}
      </span>
    </div>
  )
}

function formatRelinkSuccessMessage(data: InviteLinkResult): string {
  const ids =
    data.linked_clerk_user_ids?.length ?
      data.linked_clerk_user_ids
    : data.linked_clerk_user_id ? [data.linked_clerk_user_id]
    : []
  const matched = data.clerk_users_matched_count ?? ids.length
  let msg =
    'Account linked (no invite email — Clerk account already exists).'
  if (matched > 1) {
    msg += ` Clerk had ${matched} accounts for this email; linked ${ids.length}.`
  }
  if (ids.length) {
    msg += ` Clerk user${ids.length > 1 ? 's' : ''}: ${ids.join(', ')}.`
  }
  msg += ' Client should sign out and sign in again, then open Dashboard.'
  if (data.clerk_error) {
    msg += ` (${data.clerk_error})`
  }
  return msg
}


export default function AdminPage() {
  const { isLoaded, isSignedIn } = useAuth()
  const api = useApiClient()
  const adminApi = useMemo(() => sameOriginApiConfig(), [])
  const reduceMotion = useReducedMotion()
  const [adminAllowed, setAdminAllowed] = useState<boolean | null>(null)
  const [tenantQuery, setTenantQuery] = useState('')
  const [error, setError] = useState<string | null>(null)
  const [submitting, setSubmitting] = useState(false)
  const [success, setSuccess] = useState<string | null>(null)
  const [form, setForm] = useState({
    client_id: '',
    name: '',
    twilio_phone_number: US_E164_PREFIX,
    email: '',
    business_vertical: 'salon_chair',
  })
  // Live Stripe state per tenant. Our own subscription_status is a webhook-maintained
  // copy, and it is wrong precisely when an admin is investigating a billing problem —
  // so the screen asks Stripe rather than showing our copy back.
  const [sessionError, setSessionError] = useState<string | null>(null)
  const [emailLookup, setEmailLookup] = useState('')
  const [emailLookupResult, setEmailLookupResult] = useState<unknown>(null)
  const [emailLookupLoading, setEmailLookupLoading] = useState(false)
  const [opsCheck, setOpsCheck] = useState<OpsSelfCheck | null>(null)
  const [opsCheckLoading, setOpsCheckLoading] = useState(false)
  // Groups own the billing and the stores, so the tenant list is organised around
  // them: pick a group, then work inside it. Previously group settings lived in one
  // panel and its stores in another, which meant two places for one customer.
  const [orgs, setOrgs] = useState<Org[]>([])
  const [newOrgName, setNewOrgName] = useState('')
  const [creatingOrg, setCreatingOrg] = useState(false)

  const listContainer = {
    hidden: {},
    visible: {
      transition: { staggerChildren: reduceMotion ? 0 : 0.06, delayChildren: reduceMotion ? 0 : 0.02 },
    },
  }

  const listItem = {
    hidden: { opacity: 0, y: 12 },
    visible: {
      opacity: 1,
      y: 0,
      transition: { duration: reduceMotion ? 0 : 0.35, ease: [0.22, 1, 0.36, 1] },
    },
  }

  // One source of tenant state and handlers for both admin routes. Keeping a second
  // copy here is how the two drifted: the "0 stores" wipe had to be fixed twice, in
  // two files, because both had their own fetchTenants with the same bug.
  const { tenants, setTenants, fetchTenants, rowCtx, loading } = useTenantAdmin({
    onError: setError,
    onSuccess: setSuccess,
    listItem,
  })

  const fetchOrgs = useCallback(async () => {
    try {
      const res = await api.get<{ orgs: Org[] }>('/api/admin/orgs', adminApi)
      setOrgs(res.data.orgs || [])
    } catch {
      // A failed REFRESH must not erase data already on screen — that reads as
      // deletion right after someone saved. Fall back to empty only if we never
      // had anything, so a failed first load still resolves out of "Loading…".
      setOrgs((prev) => (prev.length ? prev : []))
    }
  }, [api, adminApi])

  const createOrg = async () => {
    const name = newOrgName.trim()
    if (!name) return
    setCreatingOrg(true)
    try {
      await api.post('/api/admin/orgs', { name }, adminApi)
      setNewOrgName('')
      await fetchOrgs()
      setSuccess(`Group "${name}" created.`)
    } catch (e) {
      const d = (e as { response?: { data?: { detail?: string } } })?.response?.data?.detail
      setError(d || 'Could not create the group.')
    } finally {
      setCreatingOrg(false)
    }
  }

  const fetchOpsCheck = useCallback(async () => {
    setOpsCheckLoading(true)
    try {
      const res = await api.get<OpsSelfCheck>('/api/admin/ops/self-check', adminApi)
      setOpsCheck(res.data)
    } catch {
      setOpsCheck(null)
    } finally {
      setOpsCheckLoading(false)
    }
  }, [api, adminApi])

  const verifyAdminSession = useCallback(async () => {
    setSessionError(null)
    setAdminAllowed(null)
    try {
      const res = await api.get<{ is_admin: boolean }>('/api/admin/session', adminApi)
      // Do NOT redirect non-admins away: the server layout may have sent an admin
      // here based on the frontend ADMIN_CLERK_USER_IDS env. If the backend (the real
      // source of truth) disagrees, redirecting to /dashboard would ping-pong with the
      // layout forever. Render an explicit "not admin" state instead (see below).
      setAdminAllowed(Boolean(res.data.is_admin))
    } catch {
      setSessionError('Could not verify admin access. Check your connection and try again.')
      setAdminAllowed(false)
    }
  }, [api, adminApi])

  useEffect(() => {
    if (!isLoaded || !isSignedIn) return
    void verifyAdminSession()
  }, [isLoaded, isSignedIn, verifyAdminSession])

  useEffect(() => {
    if (adminAllowed !== true) return
    fetchTenants()
    void fetchOrgs()
    void fetchOpsCheck()
  }, [adminAllowed, fetchTenants, fetchOrgs, fetchOpsCheck])

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault()
    setSubmitting(true)
    setSuccess(null)
    setError(null)
    try {
      const { data } = await api.post<InviteLinkResult>(
        '/api/admin/tenants',
        { ...form, plan: 'free' },
        adminApi
      )
      if (data.user_relinked) {
        setSuccess(`Tenant "${form.name}" created. ${formatRelinkSuccessMessage(data)}`)
      } else if (data.invite_sent) {
        setSuccess(`Tenant "${form.name}" created. Invitation email sent to ${form.email}.`)
      } else {
        setError(
          data.clerk_error ||
            `Tenant "${form.name}" was created but no invite email was sent. Fix the issue below, then use Resend invite on the tenant.`
        )
        if (data.clerk_error) {
          setSuccess(`Tenant "${form.name}" created (pending invite).`)
        }
      }
      setForm({
        client_id: '',
        name: '',
        twilio_phone_number: US_E164_PREFIX,
        email: '',
        business_vertical: 'salon_chair',
      })
      fetchTenants()
    } catch (e: unknown) {
      const err = e as { response?: { status?: number; data?: { detail?: string } } }
      setError(err.response?.data?.detail || 'Failed to create tenant')
    } finally {
      setSubmitting(false)
    }
  }

  /** What actually happened at Stripe, in words. The grant is meaningless to a paying
   *  customer if Stripe kept billing them, so a failure here must never be swallowed
   *  into a cheerful "done". */
  const stripeOutcome = (r?: { applied?: boolean; reason?: string; error?: string | null }) => {
    if (!r) return ''
    if (r.applied) return ' Stripe billing deferred to the same date.'
    if (r.reason === 'no_subscription') return ' No Stripe subscription — nothing was billing them.'
    return ` WARNING: Stripe was NOT updated (${r.error || 'unknown error'}) — they will still be charged. Cancel or pause it in Stripe.`
  }

  const resolveEmailLookup = async () => {
    const email = emailLookup.trim()
    if (!email.includes('@')) {
      setError('Enter a valid email to look up.')
      return
    }
    setEmailLookupLoading(true)
    setError(null)
    try {
      const { data } = await api.get('/api/admin/debug/resolve-email', {
        ...adminApi,
        params: { email },
      })
      setEmailLookupResult(data)
      debugLogAdmin('resolve-email', data)
    } catch (e: unknown) {
      const err = e as { response?: { data?: { detail?: string } } }
      setError(err.response?.data?.detail || 'Email lookup failed')
      setEmailLookupResult(null)
    } finally {
      setEmailLookupLoading(false)
    }
  }

  const groupTenants = (list: Tenant[]) => {
    const groups = new Map<string, { key: string; name: string; rows: Tenant[] }>()
    const loose: Tenant[] = []
    for (const t of list) {
      const key = (t.org_id || '').trim()
      if (!key) {
        loose.push(t)
        continue
      }
      const g = groups.get(key)
      if (g) g.rows.push(t)
      else groups.set(key, { key, name: t.org_name || 'Group', rows: [t] })
    }
    const out = Array.from(groups.values()).sort((a, b) => a.name.localeCompare(b.name))
    if (loose.length) out.push({ key: '__independent__', name: 'Independent stores', rows: loose })
    return out
  }

  /** The row is a view onto this page's state, so it takes one object rather than
   *  two dozen props. Rebuilt whenever any of it changes — cheaper than the alternative
   *  of threading each value through by hand and forgetting one. */
  const tenantQ = tenantQuery.trim().toLowerCase()
  const filteredTenants = tenantQ
    ? tenants.filter((t) =>
        [
          t.name,
          t.client_id,
          t.twilio_phone_number,
          t.allocated_email,
          t.owner_email,
          t.pending_invite_email,
          t.plan,
          t.business_vertical,
        ].some((v) => (v || '').toLowerCase().includes(tenantQ))
      )
    : tenants

  return (
    <AppChrome>
      <main className="min-h-screen px-4 py-10 md:px-6">
        <div className="mx-auto max-w-4xl">
          <div className="mb-8 flex items-start justify-between gap-4">
            <div>
              <h1 className="font-display text-2xl font-semibold tracking-tight text-white md:text-3xl">Admin console</h1>
              <p className="mt-1 text-sm text-zinc-400">Manage tenants, provisioning, and production health.</p>
            </div>
            <Link href="/" className="shrink-0 text-sm text-zinc-400 motion-safe-transition hover:text-white">
              ← Home
            </Link>
          </div>

          {error && (
            <div className="mb-6 rounded-xl border border-red-500/30 bg-red-500/10 px-4 py-3 text-sm text-red-200">{error}</div>
          )}
          {success && (
            <div className="mb-6 rounded-xl border border-emerald-500/30 bg-emerald-500/10 px-4 py-3 text-sm text-emerald-100">{success}</div>
          )}

          <CollapsibleSection
            title="Production ops"
            description="Environment & infrastructure self-check."
            className="mb-8"
          >
            <div className="mb-4 flex justify-end">
              <button
                type="button"
                onClick={() => void fetchOpsCheck()}
                disabled={opsCheckLoading}
                className="rounded-lg border border-white/15 px-3 py-1.5 text-xs font-medium text-zinc-300 hover:bg-white/5 disabled:opacity-50"
              >
                {opsCheckLoading ? 'Refreshing…' : 'Refresh'}
              </button>
            </div>
            {opsCheck ? (
              <div className="grid gap-2">
                {/* The VALUE, not a tick. Every invitation link is built from it,
                    so set-but-wrong sends invitees to the other environment and the
                    invite cannot be redeemed — a boolean cannot show that. */}
                {opsCheckRow(
                  'FRONTEND_URL (invite links)',
                  opsCheck.frontend_url_ok,
                  opsCheck.frontend_url || 'not set'
                )}
                {opsCheckRow('PUBLIC_BASE_URL set', opsCheck.public_base_url_set)}
                {opsCheckRow('Twilio signature validation', opsCheck.twilio_signature_validation_enabled)}
                {opsCheckRow('CRON_SECRET set', opsCheck.cron_secret_set)}
                {opsCheckRow('CLIENT_ID unset (multi-tenant)', opsCheck.multi_tenant_client_id_env_ok)}
                {opsCheckRow('Database enabled', opsCheck.database_enabled)}
                {opsCheckRow('REDIS_URL set', opsCheck.redis_url_set)}
                {opsCheckRow('Redis reachable (PING)', opsCheck.redis_ping_ok)}
                {opsCheckRow('Voice state on Redis', opsCheck.voice_state_backend === 'redis')}
                {opsCheckRow(
                  'Redis config consistent',
                  opsCheck.redis_config_consistent,
                  opsCheck.redis_config_consistent === false && opsCheck.redis_url_set
                    ? 'REDIS_URL is set but app is using in-memory voice state — critical for multi-worker / Deepgram'
                    : undefined
                )}
                {opsCheckRow('Redis production ready', opsCheck.redis_production_ready)}
                {opsCheck.redis_host_looks_external ? (
                  <p className="text-xs text-amber-400/90">
                    Redis host looks externally reachable — confirm private networking per docs/REDIS-SECURITY.md
                  </p>
                ) : null}
                {opsCheckRow('CLERK_ISSUER set', opsCheck.clerk_issuer_set)}
                {opsCheckRow('CLERK_AUDIENCE set', opsCheck.clerk_audience_set)}
                {opsCheckRow('Deepgram ready', opsCheck.deepgram_ready)}
                {opsCheckRow(
                  'Daily cron jobs fresh',
                  opsCheck.cron_jobs_healthy,
                  opsCheck.stale_cron_jobs?.length
                    ? `Stale: ${opsCheck.stale_cron_jobs.join(', ')}`
                    : opsCheck.last_cron_runs
                      ? `Last runs tracked for ${Object.keys(opsCheck.last_cron_runs).length} job(s)`
                      : 'No cron runs recorded yet'
                )}
              </div>
            ) : (
              <p className="text-sm text-zinc-500">Ops self-check unavailable.</p>
            )}
            <p className="mt-3 text-xs text-zinc-500">
              Redis security checklist: <code className="text-zinc-400">docs/REDIS-SECURITY.md</code> in the repo.
            </p>
          </CollapsibleSection>

          <SystemHealthPanel />

          <ProvisioningPanel />


          <ReferralsPanel />

          <AuditLog />

          <motion.form
            onSubmit={handleSubmit}
            className="mb-8 rounded-2xl border border-white/10 bg-zinc-900/70 p-6 shadow-xl backdrop-blur-md md:p-8"
            initial={reduceMotion ? false : { opacity: 0, y: 10 }}
            animate={{ opacity: 1, y: 0 }}
            transition={{ duration: reduceMotion ? 0 : 0.35 }}
          >
            <h2 className="mb-4 font-display text-lg font-semibold text-white">Add new client</h2>
            <div className="grid gap-4">
              <div>
                <label className="mb-1 block text-sm font-medium text-zinc-400">Client ID (slug)</label>
                <input
                  type="text"
                  required
                  inputMode="text"
                  autoCapitalize="none"
                  spellCheck={false}
                  placeholder="e.g. acme-salon"
                  value={form.client_id}
                  onChange={(e) => setForm({ ...form, client_id: e.target.value.toLowerCase().replace(/[^a-z0-9-]+/g, '-') })}
                  className={inputClass}
                />
                <p className="mt-1 text-xs text-zinc-500">
                  Lowercase letters, numbers, and hyphens only (typed automatically). This is the permanent
                  internal ID — pick something stable; it can&apos;t be changed later.
                </p>
              </div>
              <div>
                <label className="mb-1 block text-sm font-medium text-zinc-400">Business name</label>
                <input
                  type="text"
                  required
                  placeholder="e.g. Acme Salon"
                  value={form.name}
                  onChange={(e) => setForm({ ...form, name: e.target.value })}
                  className={inputClass}
                />
                <p className="mt-1 text-xs text-zinc-500">Shown to callers in the greeting and across the dashboard.</p>
              </div>
              <div>
                <label className="mb-1 block text-sm font-medium text-zinc-400">Industry (vertical)</label>
                <select
                  value={form.business_vertical}
                  onChange={(e) => setForm({ ...form, business_vertical: e.target.value })}
                  className={`${inputClass} w-full`}
                >
                  <option value="salon_chair">Salon, barbershop, nails & similar (chair services)</option>
                </select>
                <p className="mt-1 text-xs text-zinc-500">More industries later; this sets AI defaults for booking-style businesses.</p>
              </div>
              <div>
                <label className="mb-1 block text-sm font-medium text-zinc-400">Twilio US number (E.164)</label>
                <UsTwilioPhoneInput
                  required
                  minNationalLength={10}
                  value={form.twilio_phone_number}
                  onChange={(full) => setForm({ ...form, twilio_phone_number: full })}
                  placeholderNational="5551234567"
                />
                <p className="mt-1 text-xs text-zinc-500">
                  US A2P–approved numbers only: country code +1 is fixed. Enter the 10-digit number after +1. Buy the
                  number in Twilio Console, then enter it here (we store full E.164 to match webhooks). In Twilio, set
                  Voice webhook to your-backend/api/phone/incoming and Messaging webhook to your-backend/api/sms/incoming.
                </p>
              </div>
              <div>
                <label className="mb-1 block text-sm font-medium text-zinc-400">Client email (for invite)</label>
                <input
                  type="email"
                  required
                  placeholder="you@yourdomain.com"
                  value={form.email}
                  onChange={(e) => setForm({ ...form, email: e.target.value })}
                  className={inputClass}
                />
                <p className="mt-1 text-xs text-zinc-500">
                  One email per business — must match sign-in (including Google). Assigning a new email removes the
                  previous owner&apos;s dashboard access.
                </p>
              </div>
              <button
                type="submit"
                disabled={submitting}
                className="rounded-full bg-gradient-to-r from-cyan-600 to-indigo-600 px-6 py-2.5 font-semibold text-white shadow-lg shadow-cyan-500/15 motion-safe-transition hover:brightness-110 disabled:cursor-not-allowed disabled:opacity-50"
              >
                {submitting ? 'Creating…' : 'Create tenant and send invite'}
              </button>
            </div>
          </motion.form>

          <FleetHealthSummary tenants={tenants} />

          <section className="rounded-2xl border border-white/10 bg-zinc-900/70 p-6 shadow-xl backdrop-blur-md md:p-8">
            <div className="mb-4 flex flex-wrap items-center justify-between gap-3">
              <h2 className="font-display text-lg font-semibold text-white">Existing tenants</h2>
              {!loading && tenants.length > 0 && (
                <span className="text-xs text-zinc-500">
                  {tenantQ ? `${filteredTenants.length} of ${tenants.length}` : `${tenants.length} total`}
                </span>
              )}
            </div>
            {tenants.length > 0 && (
              <div className="mb-4">
                <input
                  type="text"
                  value={tenantQuery}
                  onChange={(e) => setTenantQuery(e.target.value)}
                  placeholder="Search by name, client ID, email, phone, or plan…"
                  className={inputClass}
                  aria-label="Search tenants"
                />
              </div>
            )}
            <details className="group mb-6 rounded-xl border border-white/10 bg-zinc-950/60">
              <summary className="flex cursor-pointer list-none items-center justify-between gap-2 p-4 text-xs font-medium text-zinc-400 [&::-webkit-details-marker]:hidden">
                Access debug — look up an email
                <ChevronDown className="h-4 w-4 shrink-0 transition-transform group-open:rotate-180" aria-hidden />
              </summary>
              <div className="px-4 pb-4">
              <p className="mt-1 text-xs text-zinc-500">
                Shows which tenant(s) and Clerk user(s) match an address. Set{' '}
                <code className="text-zinc-400">ADMIN_ACCESS_DEBUG=1</code> on Render for extra server logs.
                {DEBUG_ADMIN ? (
                  <span className="text-cyan-400"> Browser console logging is on.</span>
                ) : (
                  <span>
                    {' '}
                    Set <code className="text-zinc-400">NEXT_PUBLIC_DEBUG_ADMIN=1</code> on Vercel for console
                    output.
                  </span>
                )}
              </p>
              <div className="mt-3 flex flex-wrap items-end gap-2">
                <div className="min-w-[200px] flex-1">
                  <input
                    type="email"
                    value={emailLookup}
                    onChange={(e) => setEmailLookup(e.target.value)}
                    placeholder="coworker@company.com"
                    className={inputClass}
                  />
                </div>
                <button
                  type="button"
                  onClick={() => void resolveEmailLookup()}
                  disabled={emailLookupLoading}
                  className="rounded-lg border border-white/15 bg-white/5 px-3 py-2 text-sm text-zinc-200 hover:bg-white/10 disabled:opacity-50"
                >
                  {emailLookupLoading ? 'Looking up…' : 'Look up email'}
                </button>
              </div>
              {emailLookupResult != null && (
                <pre className="mt-3 max-h-48 overflow-auto rounded-lg border border-white/10 bg-black/40 p-3 text-left text-xs text-zinc-300">
                  {JSON.stringify(emailLookupResult, null, 2)}
                </pre>
              )}
              </div>
            </details>
            {loading ? (
              <div className="flex justify-center py-12">
                <div className="h-8 w-8 animate-spin rounded-full border-2 border-cyan-400/30 border-t-cyan-400" />
              </div>
            ) : tenants.length === 0 ? (
              <p className="text-zinc-500">No tenants yet.</p>
            ) : filteredTenants.length === 0 ? (
              <p className="text-zinc-500">No tenants match “{tenantQuery.trim()}”.</p>
            ) : (
              <motion.ul
                className="divide-y divide-white/10"
                variants={listContainer}
                initial={reduceMotion ? false : 'hidden'}
                animate="visible"
              >
                {groupTenants(filteredTenants).map((group) => {
                  const independent = group.key === '__independent__'
                  const body = (
                    <>
                      <span className="text-sm font-semibold text-zinc-200">{group.name}</span>
                      <span className="text-xs text-zinc-500">
                        {group.rows.length} {group.rows.length === 1 ? 'store' : 'stores'}
                      </span>
                    </>
                  )
                  return (
                    <li key={group.key} className="py-1">
                      {independent ? (
                        // Stores with no group have nowhere to drill into; list them
                        // here rather than inventing a page for the absence of a group.
                        <details className="group/loose">
                          <summary className="flex cursor-pointer list-none items-center gap-2 rounded-lg px-1 py-2 hover:bg-white/5 [&::-webkit-details-marker]:hidden">
                            <ChevronDown className="h-4 w-4 shrink-0 text-zinc-500 transition-transform group-open/loose:rotate-180 -rotate-90" aria-hidden />
                            {body}
                          </summary>
                          <ul className="divide-y divide-white/10 pl-1">
                            {group.rows.map((t) => (
                              <TenantRow key={t.id} t={t} ctx={rowCtx} />
                            ))}
                          </ul>
                        </details>
                      ) : (
                        <Link
                          href={`/admin/orgs/${group.key}`}
                          className="flex items-center gap-2 rounded-lg px-1 py-2 motion-safe-transition hover:bg-white/5"
                        >
                          {body}
                          <ChevronRight className="ml-auto h-4 w-4 shrink-0 text-zinc-600" aria-hidden />
                        </Link>
                      )}
                    </li>
                  )
                })}
                <li className="flex flex-wrap items-center gap-2 pt-4">
                  <input
                    value={newOrgName}
                    onChange={(e) => setNewOrgName(e.target.value)}
                    placeholder="New group name"
                    className="min-w-[12rem] flex-1 rounded-lg border border-white/15 bg-zinc-950 px-3 py-2 text-sm text-zinc-100 placeholder:text-zinc-600 focus:border-cyan-500/50 focus:outline-none"
                  />
                  <button
                    type="button"
                    onClick={() => void createOrg()}
                    disabled={creatingOrg || !newOrgName.trim()}
                    className="rounded-lg bg-white/10 px-3 py-2 text-sm text-zinc-200 hover:bg-white/15 disabled:opacity-50"
                  >
                    {creatingOrg ? 'Creating…' : 'New group'}
                  </button>
                </li>
              </motion.ul>
            )}
          </section>

        </div>
      </main>
    </AppChrome>
  )
}
