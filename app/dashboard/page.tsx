'use client'

import dynamic from 'next/dynamic'
import { UserButton } from '@clerk/nextjs'
import { useState, useEffect, useCallback, useMemo } from 'react'
import Link from 'next/link'
import { useRouter } from 'next/navigation'
import { motion, useReducedMotion } from 'framer-motion'
import { useApiClient, sameOriginApiConfig, getSelectedStoreId, setSelectedStoreId } from '@/lib/api'
import { formatTrialEndDate } from '@/lib/formatTrialEnd'
import { hasSkippedOnboarding } from '@/lib/onboardingSkip'
import { PlanPicker } from '@/components/PlanPicker'
import { AppChrome } from '@/components/layout/AppChrome'
import { AlertTriangle, ChevronLeft, Eye, Store, Users } from 'lucide-react'

export const TEAM_ROSTER_SECTION_ID = 'team-roster-settings'
export const STORE_PHONE_SECTION_ID = 'store-phone-settings'

type SetupStatusSnapshot = {
  complete?: boolean
  missing?: string[]
  warnings?: string[]
  roster_ready?: boolean
  services_ready?: boolean
  forwarding_phone_ready?: boolean
  transfer_takes_message?: boolean
  voice_ready?: boolean
  roster_only_gap?: boolean
  twilio_number_set?: boolean
  webhooks_configured?: boolean
  onboarding_completed_at?: string | null
}

// Loading fallback so a tab never shows a blank panel while its chunk loads (which
// looked like "Settings needs a second click").
const TabLoading = () => (
  <div className="flex justify-center py-20" role="status" aria-label="Loading">
    <div className="h-8 w-8 animate-spin rounded-full border-2 border-cyan-400/30 border-t-cyan-400" />
  </div>
)
const Dashboard = dynamic(() => import('@/components/Dashboard'), { ssr: false, loading: TabLoading })
const Appointments = dynamic(() => import('@/components/Appointments'), { ssr: false, loading: TabLoading })
const Settings = dynamic(() => import('@/components/Settings'), { ssr: false, loading: TabLoading })
const Leads = dynamic(() => import('@/components/Leads'), { ssr: false, loading: TabLoading })
const Messages = dynamic(() => import('@/components/Messages'), { ssr: false, loading: TabLoading })

export type SubscriptionState = {
  can_use_app: boolean
  trial_ends_at: string | null
  subscription_status: string | null
  plan: string
  billing_exempt_until: string | null
  // Card-free demo account: everything on screen is sample data, and the account
  // has no phone line yet. Cleared when they activate.
  demo_mode?: boolean
  limits?: { has_lead_capture?: boolean; staff_max?: number; transfer_max?: number; minutes_cap?: number; sms_automations_max?: number; has_export?: boolean }
}

export default function DashboardPage() {
  const router = useRouter()
  const api = useApiClient()
  const reduceMotion = useReducedMotion()
  const [activeTab, setActiveTab] = useState<'dashboard' | 'appointments' | 'leads' | 'messages' | 'settings'>('appointments')
  const [access, setAccess] = useState<'loading' | 'granted' | 'denied' | 'subscription_required'>('loading')
  const [deniedKind, setDeniedKind] = useState<'no_membership' | 'verification_failed'>('no_membership')
  const [deniedDetail, setDeniedDetail] = useState<string | null>(null)
  const [accessDebug, setAccessDebug] = useState<{
    user_id?: string
    clerk_emails?: string[]
    db_tenant_client_id?: string | null
    db_tenant_name?: string | null
    db_tenant_id?: string | null
    jwt_metadata_tenant_id?: string | null
    clerk_api_tenant_id?: string | null
    has_tenant_membership?: boolean
    is_admin?: boolean
    db_memberships?: Array<{ tenant_id: string; client_id: string; name: string }>
    pending_invite_for_primary_email?: string | null
    diagnosis?: { issues?: string[]; recommended_action?: string }
  } | null>(null)
  const [subscription, setSubscription] = useState<SubscriptionState | null>(null)
  const [setupStatus, setSetupStatus] = useState<SetupStatusSnapshot | null>(null)
  // Multi-store oversight: set only for a franchise/regional account. A normal store
  // owner gets is_org_member=false and sees none of this.
  const [org, setOrg] = useState<{ is_org_member: boolean; can_edit_any?: boolean } | null>(null)
  const [orgChecked, setOrgChecked] = useState(false)
  // Set when an admin is looking at a customer's store through the admin console.
  // Without it they'd have no visible way back — the bar below is org-member only.
  const [viewingAsAdmin, setViewingAsAdmin] = useState(false)
  const [viewingStore, setViewingStore] = useState<string | null>(null)

  const tabs = useMemo(() => {
    // Leads is always shown; when the plan doesn't include it (e.g. Starter after the
    // trial) the tab renders a locked upsell instead of being hidden.
    return [
      { id: 'appointments', label: 'Appointments' },
      { id: 'dashboard', label: 'Dashboard' },
      { id: 'leads', label: 'Leads' },
      { id: 'messages', label: 'Messages' },
      { id: 'settings', label: 'Settings' },
    ] as { id: typeof activeTab; label: string }[]
  }, [])

  const applySubscriptionError = useCallback((err: { response?: { status?: number; data?: { detail?: string } } }) => {
    const status = err.response?.status
    const detail = typeof err.response?.data?.detail === 'string' ? err.response.data.detail : null
    if (detail) setDeniedDetail(detail)
    if (status === 401 || status === 403) {
      setDeniedKind('no_membership')
    } else {
      setDeniedKind('verification_failed')
    }
    setAccess('denied')
  }, [])

  const fetchSubscription = useCallback(() => {
    api
      .get<SubscriptionState>('/api/subscription')
      .then((res) => {
        if (res.data.can_use_app) {
          setAccess('granted')
        } else {
          setAccess('subscription_required')
        }
        setSubscription(res.data)
      })
      .catch(applySubscriptionError)
  }, [api, applySubscriptionError])

  const fetchSetupStatus = useCallback(() => {
    api
      .get<SetupStatusSnapshot>('/api/setup-status')
      .then((res) => setSetupStatus(res.data))
      .catch(() => setSetupStatus(null))
  }, [api])

  // Is this an overseer, and which store are they looking at? Cheap and independent
  // of the access gate, so it can run alongside the subscription check.
  useEffect(() => {
    setViewingStore(getSelectedStoreId())
    api
      .get<{ is_org_member: boolean; can_edit_any?: boolean }>('/api/org/me')
      .then((r) => setOrg(r.data))
      .catch((e) => {
        console.info('[route-decision] org lookup failed', {
          status: (e as { response?: { status?: number } })?.response?.status ?? null,
        })
        setOrg(null)
      })
      // Separate from `org` because null means BOTH "still loading" and "lookup
      // failed". Redirects wait on this instead, so a failed lookup falls through
      // to the normal path rather than leaving someone on a page that never moves.
      .finally(() => setOrgChecked(true))
  }, [api])

  const backToAllStores = useCallback(() => {
    setSelectedStoreId(null)
    setViewingStore(null)
    router.push('/dashboard/stores')
  }, [router])

  // An overseer owns no store of their own, so with no store picked require_tenant
  // has nothing to resolve and denies them. That's correct but useless as a landing
  // page — send them to the store list instead of a "no tenant" dead end.
  useEffect(() => {
    if (access === 'denied' && deniedKind === 'no_membership' && org?.is_org_member && !viewingStore) {
      console.info('[route-decision] dashboard -> stores (group member)', {
        access,
        deniedKind,
        can_edit_any: org?.can_edit_any ?? null,
      })
      router.replace('/dashboard/stores')
    }
  }, [access, deniedKind, org, viewingStore, router])

  const goToTeamRoster = useCallback(() => {
    setActiveTab('settings')
    window.setTimeout(() => {
      document.getElementById(TEAM_ROSTER_SECTION_ID)?.scrollIntoView({ behavior: 'smooth', block: 'start' })
    }, 400)
  }, [])

  const goToStorePhone = useCallback(() => {
    setActiveTab('settings')
    window.setTimeout(() => {
      document.getElementById(STORE_PHONE_SECTION_ID)?.scrollIntoView({ behavior: 'smooth', block: 'start' })
    }, 400)
  }, [])

  useEffect(() => {
    let cancelled = false
    const POLL_DELAY_MS = 1500
    const MAX_POLL_ATTEMPTS = 5

    const loadSubscription = () => {
      api.get<SubscriptionState>('/api/subscription')
        .then((res) => {
          if (cancelled) return
          if (res.data.can_use_app) {
            setAccess('granted')
            setSubscription(res.data)
            return
          }
          setSubscription(res.data)
          const hasSessionId = typeof window !== 'undefined' && window.location.search.includes('session_id')
          if (hasSessionId) {
            let attempts = 0
            const poll = () => {
              if (cancelled || attempts >= MAX_POLL_ATTEMPTS) {
                if (!cancelled) setAccess('subscription_required')
                return
              }
              attempts += 1
              api.get<SubscriptionState>('/api/subscription')
                .then((r) => {
                  if (cancelled) return
                  if (r.data.can_use_app) {
                    setAccess('granted')
                    setSubscription(r.data)
                    return
                  }
                  if (attempts < MAX_POLL_ATTEMPTS) setTimeout(poll, POLL_DELAY_MS)
                  else setAccess('subscription_required')
                })
                .catch((err: { response?: { status?: number } }) => {
                  if (cancelled) return
                  const st = err.response?.status
                  if (st === 401 || st === 403) {
                    applySubscriptionError(err)
                    return
                  }
                  if (attempts < MAX_POLL_ATTEMPTS) setTimeout(poll, POLL_DELAY_MS)
                  else setAccess('subscription_required')
                })
            }
            setTimeout(poll, POLL_DELAY_MS)
          } else {
            setAccess('subscription_required')
          }
        })
        .catch((err: { response?: { status?: number } }) => {
          if (!cancelled) applySubscriptionError(err)
        })
    }

    api.get<{ is_admin: boolean }>('/api/admin/session', sameOriginApiConfig())
      .then((sessionRes) => {
        if (cancelled) return
        // An admin has no store of their own, so /dashboard would be a "no tenant"
        // dead end — hence the bounce to the console. But an admin who picked a store
        // in the admin panel has asked for that store specifically, and bouncing them
        // makes "Open dashboard" do nothing at all.
        // An admin has no store of their own, so /dashboard would be a dead end —
        // hence the bounce to the console. The layout enforces this server-side too;
        // this is the client-side half for a selection made after load.
        if (sessionRes.data.is_admin && !getSelectedStoreId()) {
          router.replace('/admin')
          return
        }
        if (sessionRes.data.is_admin) setViewingAsAdmin(true)
        loadSubscription()
      })
      .catch(() => {
        if (cancelled) return
        setDeniedKind('verification_failed')
        setAccess('denied')
      })

    return () => { cancelled = true }
  }, [api, router, applySubscriptionError])

  const fetchAccessDebug = useCallback(() => {
    return api.get('/api/me/access', sameOriginApiConfig()).then((res) => {
      setAccessDebug(res.data)
      if (process.env.NEXT_PUBLIC_DEBUG_SETTINGS === '1') {
        console.info('[dashboard-access-debug]', res.data)
      }
      return res.data
    })
  }, [api])

  useEffect(() => {
    if (access !== 'denied' || deniedKind !== 'no_membership') return
    // WAIT for org membership to load before deciding. This raced: `access` resolves
    // before /api/org/me does, so an invited group member — who has no business of
    // their own and never will — was thrown to "create a business" before anyone
    // knew they belonged to a group. The effect below sends them to their stores,
    // but only if it gets to run first, which it usually did not.
    //
    if (!orgChecked) return
    if (org?.is_org_member) return
    // Self-serve is the primary path: a signed-in non-admin without a business goes to
    // setup, not the invite-only "No Access" wall. (Admins were redirected to /admin
    // earlier; the create-business page handles users who do have a business.)
    console.info('[route-decision] dashboard -> create-business', {
      access,
      deniedKind,
      is_org_member: org?.is_org_member ?? null,
    })
    router.replace('/dashboard/create-business')
  }, [access, deniedKind, org, orgChecked, router])

  useEffect(() => {
    if (access !== 'granted' && access !== 'subscription_required') return
    if (process.env.NEXT_PUBLIC_DEBUG_SETTINGS !== '1') return
    void fetchAccessDebug()
  }, [access, fetchAccessDebug])

  useEffect(() => {
    if (access !== 'granted' && access !== 'subscription_required') return
    const refresh = () => {
      if (document.visibilityState === 'visible') fetchSubscription()
    }
    document.addEventListener('visibilitychange', refresh)
    window.addEventListener('focus', refresh)
    return () => {
      document.removeEventListener('visibilitychange', refresh)
      window.removeEventListener('focus', refresh)
    }
  }, [access, fetchSubscription])

  useEffect(() => {
    if (access !== 'granted') return
    fetchSetupStatus()
  }, [access, fetchSetupStatus])

  useEffect(() => {
    if (access !== 'granted') return
    const onRefresh = (e: Event) => {
      const detail = (e as CustomEvent<SetupStatusSnapshot>).detail
      if (
        detail &&
        (typeof detail.roster_ready === 'boolean' ||
          typeof detail.forwarding_phone_ready === 'boolean' ||
          typeof detail.voice_ready === 'boolean')
      ) {
        setSetupStatus((prev) => ({ ...prev, ...detail }))
      } else {
        fetchSetupStatus()
      }
    }
    window.addEventListener('call-surge-setup-status', onRefresh)
    return () => window.removeEventListener('call-surge-setup-status', onRefresh)
  }, [access, fetchSetupStatus])

  useEffect(() => {
    if (access !== 'granted' || !setupStatus) return
    // A demo has no phone line by design, so webhooks_configured is always false for
    // one — without this guard the demo bounces straight to the setup checklist and
    // the prospect never sees the dashboard they came to look at.
    const needsOnboarding =
      !subscription?.demo_mode &&
      // "Skip to dashboard" on the wizard would otherwise bounce straight back here
      // and be sent to the wizard again — an unescapable loop.
      !hasSkippedOnboarding() &&
      !setupStatus.onboarding_completed_at &&
      (setupStatus.voice_ready === false || setupStatus.webhooks_configured === false)
    if (needsOnboarding && typeof window !== 'undefined') {
      const path = window.location.pathname
      if (path === '/dashboard' && !window.location.search.includes('tab=settings')) {
        router.replace('/dashboard/onboarding')
      }
    }
  }, [access, setupStatus, subscription, router])

  const showVoiceSetupWarning = access === 'granted' && setupStatus?.voice_ready === false
  const rosterOnlyGap =
    showVoiceSetupWarning &&
    setupStatus?.forwarding_phone_ready === true &&
    setupStatus?.roster_ready === false
  // Handoff is satisfied by a real transfer number OR the "take a message instead" toggle.
  const handoffReady =
    setupStatus?.forwarding_phone_ready === true || setupStatus?.transfer_takes_message === true
  const needsHandoff = showVoiceSetupWarning && !handoffReady

  const panelTransition = reduceMotion ? { duration: 0 } : { duration: 0.22, ease: [0.22, 1, 0.36, 1] as const }

  if (access === 'loading') {
    return (
      <AppChrome>
        <div className="flex min-h-screen items-center justify-center px-4">
          <div className="text-center">
            <div className="mx-auto mb-4 h-10 w-10 animate-spin rounded-full border-2 border-cyan-400/30 border-t-cyan-400" />
            <p className="text-sm text-zinc-400">Loading your workspace…</p>
          </div>
        </div>
      </AppChrome>
    )
  }

  if (access === 'denied') {
    if (deniedKind === 'no_membership') {
      // Redirecting to /dashboard/create-business (see effect above).
      return (
        <AppChrome>
          <main className="flex min-h-screen items-center justify-center">
            <div className="text-center">
              <div className="mx-auto mb-4 h-10 w-10 animate-spin rounded-full border-2 border-cyan-400/30 border-t-cyan-400" />
              <p className="text-sm text-zinc-400">Taking you to setup…</p>
            </div>
          </main>
        </AppChrome>
      )
    }
    return (
      <AppChrome>
        <main className="flex min-h-screen items-center justify-center px-4 py-12">
          <div className="w-full max-w-md">
            <motion.div
              initial={reduceMotion ? false : { opacity: 0, y: 12 }}
              animate={{ opacity: 1, y: 0 }}
              transition={{ duration: reduceMotion ? 0 : 0.35 }}
              className="rounded-2xl border border-white/10 bg-zinc-900/90 p-8 shadow-2xl shadow-black/40 backdrop-blur-sm"
            >
              <div className="mx-auto mb-4 flex h-12 w-12 items-center justify-center rounded-full bg-red-500/15">
                <svg className="h-6 w-6 text-red-400" fill="none" stroke="currentColor" viewBox="0 0 24 24">
                  <path strokeLinecap="round" strokeLinejoin="round" strokeWidth={2} d="M12 9v2m0 4h.01M21 12a9 9 0 11-18 0 9 9 0 0118 0z" />
                </svg>
              </div>
              <h2 className="font-display text-center text-xl font-semibold text-white">
                {deniedKind === 'verification_failed' ? 'Could not verify access' : 'No Access'}
              </h2>
              <p className="mt-3 text-center text-sm leading-relaxed text-zinc-400">
                {deniedKind === 'verification_failed'
                  ? 'We could not confirm your account with the server. Check your connection and try again.'
                  : deniedDetail ||
                    'Your sign-in is not linked to a business yet (this is not a trial or billing issue). Ask your administrator to resend the invite, then sign out and open the link from that email. If you already signed up, use the same email address that was invited.'}
              </p>
              <div className="mt-8 flex flex-wrap items-center justify-center gap-4">
                {deniedKind === 'verification_failed' && (
                  <button
                    type="button"
                    onClick={() => window.location.reload()}
                    className="rounded-full bg-gradient-to-r from-cyan-600 to-indigo-600 px-5 py-2.5 text-sm font-semibold text-white shadow-lg shadow-cyan-500/20 motion-safe-transition hover:brightness-110"
                  >
                    Try again
                  </button>
                )}
                <Link href="/" className="text-sm text-cyan-400 motion-safe-transition hover:text-cyan-300">
                  ← Back to home
                </Link>
                <UserButton afterSignOutUrl="/" />
              </div>
            </motion.div>
          </div>
        </main>
      </AppChrome>
    )
  }

  if (access === 'subscription_required') {
    return (
      <AppChrome>
        <main className="min-h-screen px-4 py-8">
          <header className="mx-auto mb-8 flex max-w-3xl items-center justify-between">
            <Link href="/" className="text-sm text-zinc-400 motion-safe-transition hover:text-white">
              ← Call Surge
            </Link>
            <UserButton afterSignOutUrl="/" />
          </header>
          <div className="mx-auto max-w-3xl rounded-2xl border border-white/10 bg-white p-6 shadow-2xl shadow-black/50 md:p-10">
            <PlanPicker subscription={subscription} onSubscribed={fetchSubscription} api={api} />
          </div>
        </main>
      </AppChrome>
    )
  }

  return (
    <AppChrome>
      <main className="min-h-screen px-4 py-8 md:px-6">
        <div className="mx-auto max-w-6xl">
          <header className="mb-8 flex flex-col items-start justify-between gap-4 sm:flex-row sm:items-center">
            <div className="flex flex-col gap-1 sm:flex-row sm:items-center sm:gap-6">
              <Link href="/" className="text-sm text-zinc-400 motion-safe-transition hover:text-white">
                ← Call Surge
              </Link>
              <div>
                <h1 className="font-display text-2xl font-semibold tracking-tight text-white sm:text-4xl">Call Surge</h1>
                <p className="mt-1 text-sm text-zinc-500">AI-Powered Voice Receptionist</p>
              </div>
            </div>
            <div className="flex items-center gap-3">
              {/* The only way into the stores page used to be the "All stores" button
                  in the store-context bar — which itself only renders once a store is
                  selected, which only happens by coming FROM the stores page. Circular,
                  so a one-location owner could never reach it and never discover they
                  could add a second. Shown to anyone who manages their account.

                  Label matches the page's own title. Not "Add a location" (promises an
                  action, delivers a dashboard) and not "Org" (our word, not theirs). */}
              {org?.is_org_member && org.can_edit_any && (
                <Link
                  href="/dashboard/stores"
                  className="inline-flex items-center gap-1.5 rounded-full border border-white/15 px-3 py-1.5 text-xs font-medium text-zinc-300 motion-safe-transition hover:border-white/30 hover:text-white"
                >
                  <Store className="h-3.5 w-3.5" aria-hidden />
                  Your stores
                </Link>
              )}
              <UserButton afterSignOutUrl="/" />
            </div>
          </header>

          {(org?.is_org_member || viewingAsAdmin) && viewingStore && (
            <div className="mb-6 flex flex-wrap items-center justify-between gap-3 rounded-xl border border-white/10 bg-zinc-900/60 px-4 py-3">
              <div className="flex items-center gap-2 text-sm text-zinc-300">
                <Eye className="h-4 w-4 shrink-0 text-zinc-500" aria-hidden />
                <span>
                  Viewing <span className="font-semibold text-white">{viewingStore}</span>
                  {viewingAsAdmin ? (
                    <span className="text-amber-300/90"> · as admin</span>
                  ) : (
                    !org?.can_edit_any && <span className="text-zinc-500"> · view only</span>
                  )}
                </span>
              </div>
              <button
                type="button"
                onClick={() => {
                  if (viewingAsAdmin) {
                    // Drop the selection on the way out, or the next visit to
                    // /dashboard silently lands back in this customer's store.
                    setSelectedStoreId(null)
                    router.push('/admin')
                    return
                  }
                  backToAllStores()
                }}
                className="inline-flex items-center gap-1 rounded-full border border-white/15 px-3 py-1.5 text-xs font-medium text-zinc-300 motion-safe-transition hover:border-white/30 hover:text-white"
              >
                <ChevronLeft className="h-3.5 w-3.5" aria-hidden />
                {viewingAsAdmin ? 'Back to admin' : 'All stores'}
              </button>
            </div>
          )}

          {subscription?.demo_mode && (
            <div className="mb-6 rounded-2xl border border-cyan-400/40 bg-gradient-to-br from-cyan-500/15 to-indigo-600/10 p-5">
              <div className="flex flex-col gap-4 md:flex-row md:items-center md:justify-between">
                <div>
                  <h2 className="font-display text-lg font-semibold text-cyan-50">
                    You&rsquo;re exploring a demo
                  </h2>
                  <p className="mt-1 max-w-2xl text-sm text-cyan-100/80">
                    Every call, appointment and message below is sample data for a made-up
                    shop, so you can click around freely — nothing here is real and no
                    phone line is connected yet. Activate to get your number and start
                    taking real calls; the sample data is cleared and you&rsquo;ll set up
                    your own services and team.
                  </p>
                </div>
                <Link
                  href="/dashboard/create-business"
                  className="shrink-0 rounded-full bg-gradient-to-r from-cyan-600 to-indigo-600 px-5 py-2.5 text-center text-sm font-semibold text-white shadow-lg shadow-cyan-500/20 motion-safe-transition hover:brightness-110"
                >
                  Activate my line
                </Link>
              </div>
            </div>
          )}

          {subscription?.subscription_status === 'trialing' && subscription?.trial_ends_at && (
            <div className="mb-6 rounded-xl border border-amber-500/25 bg-amber-500/10 px-4 py-3 text-center text-sm text-amber-100">
              Your free trial ends on{' '}
              {formatTrialEndDate(subscription.trial_ends_at)}
              .
              {Math.ceil((new Date(subscription.trial_ends_at).getTime() - Date.now()) / (1000 * 60 * 60 * 24)) > 0 && (
                <>
                  {' '}
                  {Math.ceil((new Date(subscription.trial_ends_at).getTime() - Date.now()) / (1000 * 60 * 60 * 24))} days
                  left.
                </>
              )}
            </div>
          )}

          {showVoiceSetupWarning && (
            <div
              role="alert"
              className="sticky top-4 z-20 mb-6 rounded-2xl border-2 border-amber-400/80 bg-gradient-to-br from-amber-500/25 via-amber-600/15 to-orange-600/20 p-5 shadow-lg shadow-amber-900/30 md:p-6"
            >
              <div className="flex flex-col gap-4 md:flex-row md:items-start md:justify-between">
                <div className="flex gap-4">
                  <AlertTriangle className="h-10 w-10 shrink-0 text-amber-300" aria-hidden />
                  <div>
                    <h2 className="text-lg font-bold text-amber-50 md:text-xl">
                      {rosterOnlyGap
                        ? 'Team roster required for AI receptionist'
                        : 'Setup required before calls work'}
                    </h2>
                    <p className="mt-1 max-w-2xl text-sm leading-relaxed text-amber-100/95">
                      {rosterOnlyGap ? (
                        <>
                          Your store phone is set, so callers hear a short message and are{' '}
                          <strong className="font-semibold text-white">transferred to your store</strong> until you add at
                          least one team member with a name. The AI receptionist cannot book or answer normally until the
                          roster is updated.
                        </>
                      ) : needsHandoff ? (
                        <>
                          Your AI receptionist cannot take calls until setup is finished in Settings. Callers hear a message
                          and the call ends — add your team roster, plus a way to reach a real person: a transfer number, or
                          turn on <strong className="font-semibold text-white">take a message instead</strong>.
                        </>
                      ) : (
                        <>
                          Your AI receptionist cannot take calls normally until setup is complete in Settings.
                        </>
                      )}
                    </p>
                    <ul className="mt-3 space-y-1 text-sm text-amber-50/95 list-disc pl-5">
                      {!setupStatus?.roster_ready && (
                        <li>
                          Add at least one team member with a <strong className="font-semibold text-white">name</strong> on your
                          roster
                        </li>
                      )}
                      {!handoffReady && (
                        <li>
                          Add a <strong className="font-semibold text-white">transfer number</strong>, or turn on{' '}
                          <strong className="font-semibold text-white">take a message instead</strong>, in Settings
                        </li>
                      )}
                      {setupStatus?.services_ready === false && (
                        <li>
                          Add at least one <strong className="font-semibold text-white">service</strong> in Settings so the AI
                          knows what you offer
                        </li>
                      )}
                    </ul>
                  </div>
                </div>
                <div className="flex shrink-0 flex-wrap gap-2">
                  {!setupStatus?.roster_ready && (
                    <button
                      type="button"
                      onClick={goToTeamRoster}
                      className="inline-flex items-center justify-center gap-2 rounded-xl bg-amber-400 px-5 py-3 text-sm font-bold text-amber-950 shadow-md motion-safe-transition hover:bg-amber-300 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-amber-200"
                    >
                      <Users className="h-5 w-5" aria-hidden />
                      Add team members
                    </button>
                  )}
                  {!handoffReady && (
                    <button
                      type="button"
                      onClick={goToStorePhone}
                      className="inline-flex items-center justify-center gap-2 rounded-xl bg-rose-300 px-5 py-3 text-sm font-bold text-rose-950 shadow-md motion-safe-transition hover:bg-rose-200 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-rose-200"
                    >
                      Set up call handling
                    </button>
                  )}
                </div>
              </div>
            </div>
          )}

          <div
            className="relative mb-8 inline-flex w-full flex-wrap justify-center gap-1 rounded-full border border-white/10 bg-zinc-900/70 p-1.5 backdrop-blur-md"
            role="tablist"
            aria-label="Dashboard sections"
          >
            {tabs.map((tab) => (
              <button
                key={tab.id}
                type="button"
                role="tab"
                aria-selected={activeTab === tab.id}
                onClick={() => setActiveTab(tab.id)}
                className={`relative rounded-full px-5 py-2.5 text-sm font-medium motion-safe-transition focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-cyan-400 ${
                  activeTab === tab.id ? 'text-white' : 'text-zinc-400 hover:text-zinc-200'
                }`}
              >
                {activeTab === tab.id &&
                  (reduceMotion ? (
                    <span
                      className="absolute inset-0 rounded-full bg-gradient-to-r from-cyan-600 to-indigo-600 shadow-lg shadow-cyan-500/15"
                      aria-hidden
                    />
                  ) : (
                    <motion.span
                      layoutId="dashboard-tab-pill"
                      className="absolute inset-0 rounded-full bg-gradient-to-r from-cyan-600 to-indigo-600 shadow-lg shadow-cyan-500/15"
                      transition={{ type: 'spring', stiffness: 400, damping: 34 }}
                      aria-hidden
                    />
                  ))}
                <span className="relative z-10">{tab.label}</span>
              </button>
            ))}
          </div>

          {/* No AnimatePresence here on purpose. It was wrapping this panel with
              mode="wait", which holds the incoming child until the outgoing one has
              finished exiting — and every panel below is a lazy dynamic() chunk that
              suspends while it loads. The result was a permanently blank panel for
              every tab except the default one (which mounts without a wait cycle).
              Keying the div on activeTab still remounts + fades each panel in. */}
          <motion.div
            key={activeTab}
            role="tabpanel"
            initial={reduceMotion ? false : { opacity: 0, y: 10 }}
            animate={{ opacity: 1, y: 0 }}
            transition={panelTransition}
          >
            {activeTab === 'appointments' && <Appointments />}
            {activeTab === 'leads' && <Leads locked={!subscription?.limits?.has_lead_capture} />}
            {activeTab === 'messages' && <Messages />}
            {activeTab === 'dashboard' && <Dashboard />}
            {activeTab === 'settings' && <Settings />}
          </motion.div>

          <footer className="mt-12 border-t border-white/10 pt-6 text-center text-sm text-zinc-500">
            <Link href="/terms" className="motion-safe-transition hover:text-zinc-300">
              Terms of Service
            </Link>
            <span className="mx-2">·</span>
            <Link href="/privacy" className="motion-safe-transition hover:text-zinc-300">
              Privacy Policy
            </Link>
          </footer>
        </div>
      </main>
    </AppChrome>
  )
}
