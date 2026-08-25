'use client'

/** One group: its settings, then its stores.
 *
 * A franchise is one customer, and everything about it now lives on one page —
 * partner pricing, who can see it, and the locations themselves. Previously the
 * settings were in a panel near the top of the admin console and the stores in a
 * list further down, which meant scrolling between two halves of the same thing.
 */

import { useCallback, useEffect, useMemo, useState } from 'react'
import { useAuth } from '@clerk/nextjs'
import Link from 'next/link'
import { useParams } from 'next/navigation'
import { motion, useReducedMotion } from 'framer-motion'
import { ChevronLeft } from 'lucide-react'
import { AppChrome } from '@/components/layout/AppChrome'
import { useApiClient, sameOriginApiConfig } from '@/lib/api'
import { OrgCard, type Org } from '@/components/admin/OrgsPanel'
import { TenantRow } from '@/components/admin/TenantRow'
import { useTenantAdmin } from '@/components/admin/useTenantAdmin'

export default function AdminOrgPage() {
  const params = useParams<{ orgId: string }>()
  const orgId = String(params?.orgId || '')
  const { isLoaded, isSignedIn } = useAuth()
  const api = useApiClient()
  const adminApi = useMemo(() => sameOriginApiConfig(), [])
  const reduceMotion = useReducedMotion()

  const [error, setError] = useState<string | null>(null)
  const [success, setSuccess] = useState<string | null>(null)
  const [orgs, setOrgs] = useState<Org[] | null>(null)

  const listItem = useMemo(
    () => ({
      hidden: { opacity: 0, y: 12 },
      visible: {
        opacity: 1,
        y: 0,
        transition: { duration: reduceMotion ? 0 : 0.35, ease: [0.22, 1, 0.36, 1] as const },
      },
    }),
    [reduceMotion]
  )

  const { tenants, fetchTenants, rowCtx, loading } = useTenantAdmin({
    onError: setError,
    onSuccess: setSuccess,
    listItem,
  })

  const fetchOrgs = useCallback(async () => {
    try {
      const res = await api.get<{ orgs: Org[] }>('/api/admin/orgs', adminApi)
      setOrgs(res.data.orgs || [])
    } catch (e) {
      // A failed REFRESH must not erase data already on screen — that reads as
      // deletion right after someone saved. Fall back to empty only if we never
      // had anything, so a failed first load still resolves out of "Loading…".
      setOrgs((prev) => prev ?? [])
      const status = (e as { response?: { status?: number } })?.response?.status
      setError(
        status === 503
          ? 'Could not refresh groups from the database — showing the last loaded data. Nothing was changed.'
          : status === 504
            ? 'The API did not respond in time — it may be waking up. Showing the last loaded data; retry in a moment.'
            : 'Could not refresh this group. Showing the last loaded data.'
      )
    }
  }, [api, adminApi])

  useEffect(() => {
    // Gate on the session, as /admin does. Without this the page called both admin
    // endpoints on every mount while signed out, and each rejected call still cost a
    // full Netlify function invocation — 13s median, 42s worst — because the proxy
    // waits on the backend before the 401 comes back. A signed-out tab left open
    // billed all night for answers it was never entitled to.
    if (!isLoaded || !isSignedIn) return
    void fetchOrgs()
    void fetchTenants()
  }, [isLoaded, isSignedIn, fetchOrgs, fetchTenants])

  const org = (orgs || []).find((o) => o.id === orgId) || null
  const stores = tenants.filter((t) => (t.org_id || '') === orgId)

  return (
    <AppChrome>
      <main className="min-h-screen px-4 py-10 md:px-6">
        <div className="mx-auto max-w-5xl space-y-6">
          <Link
            href="/admin"
            className="inline-flex items-center gap-1 text-sm text-zinc-400 motion-safe-transition hover:text-zinc-200"
          >
            <ChevronLeft className="h-4 w-4" aria-hidden />
            Back to admin
          </Link>

          <div>
            <h1 className="font-display text-2xl font-semibold text-white">
              {!isLoaded
                ? 'Loading…'
                : !isSignedIn
                  ? 'Sign in to view this group'
                  : org?.name || (orgs === null ? 'Loading…' : 'Group not found')}
            </h1>
            <p className="mt-1 text-sm text-zinc-500">
              {/* "0 stores" is a claim. Only make it once the list has actually
                  loaded — before that it is indistinguishable from not knowing. */}
              {loading && !tenants.length
                ? 'Loading stores…'
                : `${stores.length} ${stores.length === 1 ? 'store' : 'stores'}`}
            </p>
          </div>

          {error && (
            <div className="rounded-xl border border-red-500/30 bg-red-500/10 px-4 py-3 text-sm text-red-200">
              {error}
            </div>
          )}
          {success && (
            <div className="rounded-xl border border-emerald-500/30 bg-emerald-500/10 px-4 py-3 text-sm text-emerald-200">
              {success}
            </div>
          )}

          {org && (
            <OrgCard
              org={org}
              tenants={tenants.map((t) => ({ id: t.id, client_id: t.client_id, name: t.name }))}
              attachedClientIds={new Set(tenants.filter((t) => t.org_id).map((t) => t.client_id))}
              api={api}
              adminApi={adminApi}
              onChanged={async () => {
                await fetchOrgs()
                await fetchTenants()
              }}
              onError={setError}
              onSuccess={setSuccess}
            />
          )}

          <section className="rounded-2xl border border-white/10 bg-zinc-900/70 p-6 shadow-xl backdrop-blur-md">
            <h2 className="mb-4 font-display text-lg font-semibold text-white">Stores</h2>
            {loading && !stores.length ? (
              <p className="text-sm text-zinc-500">Loading…</p>
            ) : stores.length === 0 ? (
              <p className="text-sm text-zinc-500">
                No stores in this group yet. Attach one above.
              </p>
            ) : (
              <motion.ul className="divide-y divide-white/10">
                {stores.map((t) => (
                  <TenantRow key={t.id} t={t} ctx={rowCtx} />
                ))}
              </motion.ul>
            )}
          </section>
        </div>
      </main>
    </AppChrome>
  )
}
