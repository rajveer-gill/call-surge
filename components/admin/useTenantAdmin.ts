'use client'

/** Everything needed to list and manage stores, in one place.
 *
 * Extracted so the admin console and a single group's page can both do it. The
 * alternative was duplicating two dozen handlers across two routes, which is how two
 * copies quietly stop behaving the same.
 *
 * The page keeps its own error/success banner — this reports through callbacks rather
 * than growing a second set of messages beside the first.
 */

import { useCallback, useMemo, useState } from 'react'
import type { Variants } from 'framer-motion'
import { useRouter } from 'next/navigation'
import { useApiClient, sameOriginApiConfig, setSelectedStoreId } from '@/lib/api'
import type { Tenant, StripeStatus, TenantRowCtx } from '@/components/admin/tenantTypes'

type InviteLinkResult = {
  invite_sent?: boolean
  user_relinked?: boolean
  clerk_error?: string | null
  linked_clerk_user_id?: string | null
  linked_clerk_user_ids?: string[] | null
  clerk_users_matched_count?: number
}

function formatRelinkSuccessMessage(data: InviteLinkResult): string {
  const ids = data.linked_clerk_user_ids?.length
    ? data.linked_clerk_user_ids
    : data.linked_clerk_user_id
      ? [data.linked_clerk_user_id]
      : []
  const matched = data.clerk_users_matched_count ?? ids.length
  let msg = 'Account linked (no invite email — Clerk account already exists).'
  if (matched > 1) msg += ` Clerk had ${matched} accounts for this email; linked ${ids.length}.`
  if (ids.length) msg += ` Clerk user${ids.length > 1 ? 's' : ''}: ${ids.join(', ')}.`
  msg += ' Client should sign out and sign in again, then open Dashboard.'
  if (data.clerk_error) msg += ` (${data.clerk_error})`
  return msg
}

/** Console logging for invite/relink debugging, on when NEXT_PUBLIC_DEBUG_ADMIN=1. */
const DEBUG_ADMIN = process.env.NEXT_PUBLIC_DEBUG_ADMIN === '1'

function debugLogAdmin(label: string, payload: unknown) {
  if (!DEBUG_ADMIN) return
  console.info(`[admin-debug] ${label}`, payload)
}

export function useTenantAdmin({
  onError,
  onSuccess,
  listItem,
}: {
  onError: (m: string | null) => void
  onSuccess: (m: string | null) => void
  listItem: Variants
}) {
  const api = useApiClient()
  // MUST be memoized. Unmemoized, this returns a new object every render, which
  // gives every useCallback depending on it a new identity, which re-fires every
  // effect depending on THOSE — fetch, setState, re-render, fetch. That loop ran at
  // ~5-11 requests/second against a per-invocation-billed host and cost real money
  // twice. lib/api.ts keeps useApiClient stable for exactly this reason and says so.
  const adminApi = useMemo(() => sameOriginApiConfig(), [])
  const router = useRouter()
  const setError = onError
  const setSuccess = onSuccess

  const [tenants, setTenants] = useState<Tenant[]>([])
  const [loading, setLoading] = useState(true)
  const [deleting, setDeleting] = useState<string | null>(null)
  const [pausing, setPausing] = useState<string | null>(null)
  const [exempting, setExempting] = useState<string | null>(null)
  const [exemptAction, setExemptAction] = useState<Record<string, string>>({})
  const [exemptUntilDate, setExemptUntilDate] = useState<Record<string, string>>({})
  const [stripeStatus, setStripeStatus] = useState<Record<string, StripeStatus>>({})
  const [stripeChecking, setStripeChecking] = useState<string | null>(null)
  const [twilioDraft, setTwilioDraft] = useState<Record<string, string>>({})
  const [twilioSaving, setTwilioSaving] = useState<string | null>(null)
  const [inviteEmailByTenant, setInviteEmailByTenant] = useState<Record<string, string>>({})
  const [resendingInvite, setResendingInvite] = useState<string | null>(null)
  const [accessDebugOpen, setAccessDebugOpen] = useState<Record<string, boolean>>({})
  const [accessDebugData, setAccessDebugData] = useState<Record<string, unknown>>({})
  const [accessDebugLoading, setAccessDebugLoading] = useState<string | null>(null)

  const fetchTenants = useCallback(async () => {
    try {
      const res = await api.get<{ tenants: Tenant[]; db_enabled?: boolean }>(
        '/api/admin/tenants',
        adminApi
      )
      const list = res.data.tenants || []
      setTenants(list)
      setInviteEmailByTenant((prev) => {
        const next = { ...prev }
        for (const t of list) {
          next[t.id] = t.allocated_email || t.owner_email || t.pending_invite_email || ''
        }
        return next
      })
      if (res.data.db_enabled === false) {
        setError(
          'The backend started without a working database connection, so tenants cannot be listed. ' +
          'This is usually a restart while the database was still waking — restarting the backend ' +
          'service fixes it. Check DATABASE_URL only if a restart does not.'
        )
      } else if (list.length === 0) {
        setError(null)
      } else {
        setError(null)
      }
    } catch (e: unknown) {
      const err = e as {
        response?: { status?: number; data?: { detail?: string } }
        message?: string
      }
      // Deliberately NOT clearing the list. This runs on every refresh after a
      // save, so wiping it turned a transient hiccup into "0 stores" on a group
      // that has two — which reads as data loss at the exact moment someone just
      // changed that group's billing. Keep the last good data and show the error.
      if (err.response?.status === 403) {
        setError('Admin access required. Add your Clerk user ID to ADMIN_CLERK_USER_IDS on the backend.')
      } else if (err.response?.status === 401) {
        setError('Please sign in.')
      } else if (err.response?.status === 503) {
        setError(
          'Could not refresh from the database — showing the last loaded data. Nothing was changed.'
        )
      } else if (err.response?.status === 504) {
        setError(
          'The API did not respond in time — it may be waking up. Showing the last loaded data; retry in a moment.'
        )
      } else {
        const detail = err.response?.data?.detail
        setError(
          detail ||
            err.message ||
            'Failed to load tenants. Check the browser Network tab for /api/admin/tenants.'
        )
      }
    } finally {
      setLoading(false)
    }
  }, [api, adminApi])


  /** What actually happened at Stripe, in words. The grant is meaningless to a paying
   *  customer if Stripe kept billing them, so a failure here must never be swallowed
   *  into a cheerful "done". */
  const stripeOutcome = (r?: { applied?: boolean; reason?: string; error?: string | null }) => {
    if (!r) return ''
    if (r.applied) return ' Stripe billing deferred to the same date.'
    if (r.reason === 'no_subscription') return ' No Stripe subscription — nothing was billing them.'
    return ` WARNING: Stripe was NOT updated (${r.error || 'unknown error'}) — they will still be charged. Cancel or pause it in Stripe.`
  }

  const checkStripe = async (tenantId: string) => {
    setStripeChecking(tenantId)
    try {
      const { data } = await api.get<StripeStatus>(
        `/api/admin/tenants/${tenantId}/stripe-status`,
        adminApi
      )
      setStripeStatus((m) => ({ ...m, [tenantId]: data }))
    } catch {
      setStripeStatus((m) => ({
        ...m,
        [tenantId]: { has_subscription: false, message: 'Could not reach the server.' },
      }))
    } finally {
      setStripeChecking(null)
    }
  }

  const handleBillingExempt = async (tenantId: string) => {
    const action = exemptAction[tenantId]
    if (!action) return
    setExempting(tenantId)
    setError(null)
    setSuccess(null)
    try {
      if (action === 'extend_trial_1') {
        const r = await api.patch(`/api/admin/tenants/${tenantId}/billing-exempt`, { extend_trial_months: 1 }, adminApi)
        setSuccess('Trial extended by 1 month.' + stripeOutcome(r?.data?.stripe))
      } else if (action === 'free_1') {
        const r = await api.patch(`/api/admin/tenants/${tenantId}/billing-exempt`, { extend_months: 1 }, adminApi)
        setSuccess('1 month billing exemption set.' + stripeOutcome(r?.data?.stripe))
      } else if (action === 'free_3') {
        const r = await api.patch(`/api/admin/tenants/${tenantId}/billing-exempt`, { extend_months: 3 }, adminApi)
        setSuccess('3 months billing exemption set.' + stripeOutcome(r?.data?.stripe))
      } else if (action === 'exempt_until') {
        const date = exemptUntilDate[tenantId]
        if (!date) {
          setError('Pick a date for exempt until.')
          setExempting(null)
          return
        }
        const r = await api.patch(`/api/admin/tenants/${tenantId}/billing-exempt`, { exempt_until: date }, adminApi)
        setSuccess(`Exempt until ${date} set.` + stripeOutcome(r?.data?.stripe))
        setExemptUntilDate((d) => ({ ...d, [tenantId]: '' }))
      }
      setExemptAction((a) => ({ ...a, [tenantId]: '' }))
      fetchTenants()
    } catch (e: unknown) {
      const err = e as { response?: { data?: { detail?: string } } }
      setError(err.response?.data?.detail || 'Failed to update billing')
    } finally {
      setExempting(null)
    }
  }

  const handleDelete = async (tenant: Tenant) => {
    if (!confirm(`Remove "${tenant.name}" (${tenant.client_id})? This cannot be undone.`)) return
    setDeleting(tenant.id)
    setError(null)
    setSuccess(null)
    try {
      await api.delete(`/api/admin/tenants/${tenant.id}`, adminApi)
      setSuccess(`Tenant "${tenant.name}" removed.`)
      fetchTenants()
    } catch (e: unknown) {
      const err = e as { response?: { data?: { detail?: string } } }
      setError(err.response?.data?.detail || 'Failed to remove tenant')
    } finally {
      setDeleting(null)
    }
  }

  const handleTogglePause = async (tenant: Tenant) => {
    const next = !tenant.account_paused
    if (
      next &&
      !confirm(
        `Pause "${tenant.name}"? Their AI phone line and SMS will immediately stop answering until you resume.`,
      )
    )
      return
    setPausing(tenant.id)
    setError(null)
    setSuccess(null)
    try {
      await api.patch(`/api/admin/tenants/${tenant.id}/account-paused`, { paused: next }, adminApi)
      setSuccess(next ? `"${tenant.name}" paused.` : `"${tenant.name}" resumed.`)
      fetchTenants()
    } catch (e: unknown) {
      const err = e as { response?: { data?: { detail?: string } } }
      setError(err.response?.data?.detail || 'Failed to update pause state')
    } finally {
      setPausing(null)
    }
  }

  const loadTenantAccessDebug = async (tenantId: string) => {
    setAccessDebugLoading(tenantId)
    setError(null)
    try {
      const { data } = await api.get(`/api/admin/tenants/${tenantId}/access-debug`, adminApi)
      setAccessDebugData((d) => ({ ...d, [tenantId]: data }))
      setAccessDebugOpen((o) => ({ ...o, [tenantId]: true }))
      debugLogAdmin(`tenant ${tenantId}`, data)
    } catch (e: unknown) {
      const err = e as { response?: { data?: { detail?: string } } }
      setError(err.response?.data?.detail || 'Failed to load access debug')
    } finally {
      setAccessDebugLoading(null)
    }
  }

  const handleResendInvite = async (tenantId: string) => {
    const email = (inviteEmailByTenant[tenantId] || '').trim()
    if (!email || !email.includes('@')) {
      setError('Enter the client email address to resend or link the invite.')
      return
    }
    setResendingInvite(tenantId)
    setError(null)
    setSuccess(null)
    try {
      const { data } = await api.post<
        InviteLinkResult & { pending_invite_stored?: boolean; access_debug?: unknown }
      >(`/api/admin/tenants/${tenantId}/resend-invite`, { email }, adminApi)
      debugLogAdmin('resend-invite', data)
      if (data.access_debug) {
        setAccessDebugData((d) => ({ ...d, [tenantId]: data.access_debug }))
        setAccessDebugOpen((o) => ({ ...o, [tenantId]: true }))
      }
      if (data.user_relinked) {
        setSuccess(formatRelinkSuccessMessage(data))
        await fetchTenants()
        void loadTenantAccessDebug(tenantId)
      } else if (data.invite_sent) {
        setSuccess('Invitation email sent. Open that link from the inbox (same email you entered here).')
        await fetchTenants()
      } else {
        setError(
          data.clerk_error ||
            'Invite was not sent. Check Render CLERK_SECRET_KEY and Clerk Dashboard → Invitations.'
        )
      }
    } catch (e: unknown) {
      const err = e as { response?: { data?: { detail?: string } } }
      setError(err.response?.data?.detail || 'Failed to resend invite')
    } finally {
      setResendingInvite(null)
    }
  }

  const handleSaveTwilio = async (tenantId: string) => {
    const phone = (twilioDraft[tenantId] || '').trim()
    if (!/\d/.test(phone)) {
      setError('Enter a phone number with digits.')
      return
    }
    setTwilioSaving(tenantId)
    setError(null)
    setSuccess(null)
    try {
      const res = await api.patch<{
        success?: boolean
        webhook_config?: { voice_ok?: boolean; sms_ok?: boolean; errors?: string[] }
      }>(
        `/api/admin/tenants/${tenantId}/twilio-phone`,
        { twilio_phone_number: phone },
        adminApi
      )
      const wc = res.data.webhook_config
      if (wc?.voice_ok && wc?.sms_ok) {
        setSuccess('Twilio number saved and Voice + Messaging webhooks configured.')
      } else if (wc?.errors?.length) {
        setSuccess(`Number saved. Webhook config: ${wc.errors.join('; ')}`)
      } else {
        setSuccess('Twilio number saved. Inbound SMS/voice will match this number.')
      }
      await fetchTenants()
    } catch (e: unknown) {
      const err = e as { response?: { data?: { detail?: string } } }
      setError(err.response?.data?.detail || 'Failed to save Twilio number')
    } finally {
      setTwilioSaving(null)
    }
  }

  const rowCtx: TenantRowCtx = {
    accessDebugData, accessDebugLoading, accessDebugOpen, setAccessDebugOpen,
    loadTenantAccessDebug, checkStripe, stripeChecking, stripeStatus,
    deleting, handleDelete, exemptAction, setExemptAction, exemptUntilDate,
    setExemptUntilDate, exempting, handleBillingExempt, handleResendInvite,
    resendingInvite, inviteEmailByTenant, setInviteEmailByTenant, handleSaveTwilio,
    twilioDraft, setTwilioDraft, twilioSaving, handleTogglePause, pausing,
    listItem,
    openDashboard: (clientId: string) => {
      setSelectedStoreId(clientId)
      router.push('/dashboard')
    },
  }

  return { tenants, setTenants, fetchTenants, rowCtx, loading, twilioDraft, setTwilioDraft }
}
