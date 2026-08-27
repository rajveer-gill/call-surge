'use client'

/** Admin: create multi-store groups (orgs), attach stores to them, and add the
 * people who oversee or manage them. This is the one-time setup that turns a set of
 * ordinary tenants into a franchise account — after this, an org manager provisions
 * and bills their own stores from /dashboard/stores.
 *
 * All calls hit /api/admin/* which proxies to the backend's require_admin routes. */

import { useCallback, useEffect, useMemo, useState } from 'react'
import { useApiClient, sameOriginApiConfig } from '@/lib/api'
import { CollapsibleSection } from '@/components/ui/CollapsibleSection'
import { Building2, Store, Trash2, UserPlus, X } from 'lucide-react'

interface OrgMember {
  clerk_user_id: string
  role: string
}

interface OrgStore {
  tenant_id: string
  client_id: string
  name: string
}

interface OrgInvite {
  email: string
  role: string
}

export interface Org {
  id: string
  name: string
  created_at?: string | null
  stores: OrgStore[]
  members: OrgMember[]
  pending_invites: OrgInvite[]
  /** Per-plan Stripe price IDs for a partner rate. Empty = standard pricing. */
  price_overrides?: Record<string, string> | null
}

interface AdminTenant {
  id: string
  client_id: string
  name: string
}

const detailOf = (e: unknown): string | null => {
  const d = (e as { response?: { data?: { detail?: unknown } } })?.response?.data?.detail
  return typeof d === 'string' ? d : null
}

export function OrgsPanel() {
  const api = useApiClient()
  const adminApi = useMemo(() => sameOriginApiConfig(), [])

  const [orgs, setOrgs] = useState<Org[] | null>(null)
  const [tenants, setTenants] = useState<AdminTenant[]>([])
  const [loading, setLoading] = useState(true)
  const [error, setError] = useState<string | null>(null)
  const [success, setSuccess] = useState<string | null>(null)

  const [newOrgName, setNewOrgName] = useState('')
  const [creating, setCreating] = useState(false)

  const load = useCallback(async () => {
    setLoading(true)
    setError(null)
    try {
      const [orgsRes, tenantsRes] = await Promise.all([
        api.get<{ orgs: Org[] }>('/api/admin/orgs', adminApi),
        api.get<{ tenants: AdminTenant[] }>('/api/admin/tenants', adminApi),
      ])
      setOrgs(orgsRes.data.orgs || [])
      setTenants(tenantsRes.data.tenants || [])
    } catch (e) {
      setError(detailOf(e) || 'Could not load groups.')
      // A failed REFRESH must not erase data already on screen — that reads as
      // deletion right after someone saved. Fall back to empty only if we never
      // had anything, so a failed first load still resolves out of "Loading…".
      setOrgs((prev) => prev ?? [])
    } finally {
      setLoading(false)
    }
  }, [api, adminApi])

  useEffect(() => {
    void load()
  }, [load])

  const flash = useCallback((msg: string) => {
    setSuccess(msg)
    setError(null)
    window.setTimeout(() => setSuccess(null), 4000)
  }, [])

  const createOrg = useCallback(
    async (e: React.FormEvent) => {
      e.preventDefault()
      const name = newOrgName.trim()
      if (!name) return
      setCreating(true)
      setError(null)
      try {
        await api.post('/api/admin/orgs', { name }, adminApi)
        setNewOrgName('')
        flash(`Created group “${name}”.`)
        await load()
      } catch (e) {
        setError(detailOf(e) || 'Could not create the group.')
      } finally {
        setCreating(false)
      }
    },
    [api, adminApi, newOrgName, flash, load]
  )

  // client_ids already in some org — hidden from the attach picker so a store can't
  // be double-attached (the backend would just move it, but the UI shouldn't imply that).
  const attachedClientIds = useMemo(() => {
    const set = new Set<string>()
    for (const o of orgs || []) for (const s of o.stores) set.add(s.client_id)
    return set
  }, [orgs])

  return (
    <CollapsibleSection
      title="Multi-store groups"
      description="Create a franchise/group account, attach its stores, and add the people who run it."
      className="mb-8"
    >
      {error && (
        <div className="mb-4 rounded-xl border border-red-500/30 bg-red-500/10 px-4 py-3 text-sm text-red-200">
          {error}
        </div>
      )}
      {success && (
        <div className="mb-4 rounded-xl border border-emerald-500/30 bg-emerald-500/10 px-4 py-3 text-sm text-emerald-100">
          {success}
        </div>
      )}

      <form onSubmit={createOrg} className="mb-6 flex flex-wrap items-end gap-3">
        <div className="min-w-[220px] flex-1">
          <label className="mb-1 block text-sm font-medium text-zinc-300">New group name</label>
          <input
            type="text"
            value={newOrgName}
            onChange={(e) => setNewOrgName(e.target.value)}
            placeholder="e.g. Supercuts — North Region"
            className="w-full rounded-lg border border-white/10 bg-zinc-950/60 px-3 py-2 text-sm text-white placeholder-zinc-600 focus:border-cyan-500 focus:outline-none"
          />
        </div>
        <button
          type="submit"
          disabled={creating || !newOrgName.trim()}
          className="inline-flex items-center gap-1.5 rounded-lg bg-gradient-to-r from-cyan-600 to-indigo-600 px-4 py-2 text-sm font-semibold text-white shadow-lg shadow-cyan-500/20 motion-safe-transition hover:brightness-110 disabled:opacity-50"
        >
          <Building2 className="h-4 w-4" aria-hidden />
          {creating ? 'Creating…' : 'Create group'}
        </button>
      </form>

      {loading && <p className="text-sm text-zinc-500">Loading groups…</p>}

      {!loading && orgs?.length === 0 && (
        <p className="text-sm text-zinc-500">
          No groups yet. Create one above, then attach its stores and add a manager.
        </p>
      )}

      <div className="space-y-4">
        {(orgs || []).map((org) => (
          <OrgCard
            key={org.id}
            org={org}
            tenants={tenants}
            attachedClientIds={attachedClientIds}
            api={api}
            adminApi={adminApi}
            onChanged={load}
            onError={setError}
            onSuccess={flash}
          />
        ))}
      </div>
    </CollapsibleSection>
  )
}

export function OrgCard({
  org,
  tenants,
  attachedClientIds,
  api,
  adminApi,
  onChanged,
  onError,
  onSuccess,
}: {
  org: Org
  tenants: AdminTenant[]
  attachedClientIds: Set<string>
  api: ReturnType<typeof useApiClient>
  adminApi: { baseURL: string }
  onChanged: () => Promise<void>
  onError: (m: string) => void
  onSuccess: (m: string) => void
}) {
  const [attachId, setAttachId] = useState('')
  const [busy, setBusy] = useState(false)
  const [memberEmail, setMemberEmail] = useState('')
  // Partner pricing. Kept as a discounted PRICE rather than a coupon because an org
  // is one subscription with quantity = store count: a fixed coupon would come off
  // the invoice once, not off each store, and a percentage would change value with
  // the plan.
  const [prices, setPrices] = useState<Record<string, string>>(() => ({
    starter: org.price_overrides?.starter || '',
    growth: org.price_overrides?.growth || '',
    pro: org.price_overrides?.pro || '',
  }))
  const [savingPrices, setSavingPrices] = useState(false)
  const activePlans = Object.entries(prices)
    .filter(([, v]) => (v || '').startsWith('price_'))
    .map(([k]) => k)
  const hasPartnerRate = activePlans.length > 0
  const [discount, setDiscount] = useState('')
  const [applyingDiscount, setApplyingDiscount] = useState(false)
  const [priceNote, setPriceNote] = useState<string | null>(null)
  const [memberRole, setMemberRole] = useState<'manager' | 'viewer'>('manager')

  // Stores not yet in any group — the candidates to attach here.
  const available = useMemo(
    () => tenants.filter((t) => !attachedClientIds.has(t.client_id)),
    [tenants, attachedClientIds]
  )

  const run = useCallback(
    async (fn: () => Promise<void>) => {
      setBusy(true)
      try {
        await fn()
        await onChanged()
      } catch (e) {
        onError(detailOf(e) || 'Something went wrong.')
      } finally {
        setBusy(false)
      }
    },
    [onChanged, onError]
  )

  const attachStore = () =>
    run(async () => {
      if (!attachId) return
      await api.post(`/api/admin/orgs/${org.id}/stores`, { tenant_ids: [attachId] }, adminApi)
      setAttachId('')
      onSuccess('Store attached.')
    })

  const detachStore = (tenantId: string) =>
    run(async () => {
      await api.delete(`/api/admin/orgs/${org.id}/stores/${tenantId}`, adminApi)
      onSuccess('Store removed from group.')
    })

  /** The everyday path: type a dollar amount, let the server work out the prices.
   *  Pasting three Stripe price IDs by hand is three chances to paste a product id
   *  where a price belongs. */
  const applyDiscount = async (clear = false) => {
    setApplyingDiscount(true)
    setPriceNote(null)
    try {
      const amount = clear ? 0 : parseFloat(discount)
      if (!clear && (!Number.isFinite(amount) || amount <= 0)) {
        setPriceNote('Enter a dollar amount, e.g. 50')
        return
      }
      const { data } = await api.patch<{
        price_overrides?: Record<string, string>
        errors?: string[]
        cleared?: boolean
        subscription?: { repriced?: boolean; reason?: string | null }
      }>(`/api/admin/orgs/${org.id}/partner-discount`, { amount_off_per_store: amount }, adminApi)
      const next = data.price_overrides || {}
      setPrices({ starter: next.starter || '', growth: next.growth || '', pro: next.pro || '' })
      // Say what happened to a subscription they already had. "Applied" used to be
      // reported for groups that went on paying list price, because nothing moved
      // the existing subscription onto the new price.
      const sub = data.subscription || {}
      const subNote = sub.repriced
        ? ' Their current subscription now bills at this rate from the next invoice.'
        : sub.reason === 'no_subscription'
          ? ' They have not checked out yet, so it applies when they do.'
          : sub.reason === 'already_on_price'
            ? ' Their subscription was already on this rate.'
            : ` Their existing subscription was NOT repriced (${sub.reason || 'unknown'}).`
      setPriceNote(
        data.cleared
          ? 'Cleared — back to standard pricing.' + subNote
          : `$${amount} off each store on ${Object.keys(next).length} plan(s).` +
            (data.errors?.length ? ` Not applied to: ${data.errors.join('; ')}` : '') +
            subNote
      )
      if (clear) setDiscount('')
      // Awaited, not fired and forgotten. An un-awaited refresh here overlapped the
      // one the caller already runs, so two hit the API at once and doubled the
      // chance of catching the pool mid-burst.
      await onChanged()
    } catch (e) {
      setPriceNote(detailOf(e) || 'Could not apply the discount.')
    } finally {
      setApplyingDiscount(false)
    }
  }

  const savePrices = async () => {
    setSavingPrices(true)
    setPriceNote(null)
    try {
      await api.patch(`/api/admin/orgs/${org.id}/price-overrides`, prices, adminApi)
      const set = Object.values(prices).filter((v) => v.trim()).length
      setPriceNote(
        set
          ? `Saved. Use "Apply to all plans" above to also move an existing subscription onto these prices.`
          : 'Cleared — back to standard pricing.'
      )
      await onChanged()
    } catch (e) {
      setPriceNote(detailOf(e) || 'Could not save the prices.')
    } finally {
      setSavingPrices(false)
    }
  }

  const addMember = () =>
    run(async () => {
      const email = memberEmail.trim()
      if (!email) return
      const { data } = await api.post<{ added?: boolean; invite_sent?: boolean; pending?: boolean }>(
        `/api/admin/orgs/${org.id}/members`,
        { email, role: memberRole },
        adminApi
      )
      setMemberEmail('')
      if (data?.added) onSuccess(`Added ${email} as ${memberRole}.`)
      else if (data?.invite_sent) onSuccess(`Invite emailed to ${email}.`)
      else onSuccess(`Invite queued for ${email} — they'll join when they sign up.`)
    })

  const removeMember = (uid: string) =>
    run(async () => {
      await api.delete(`/api/admin/orgs/${org.id}/members/${encodeURIComponent(uid)}`, adminApi)
      onSuccess('Member removed.')
    })

  const deleteOrg = () =>
    run(async () => {
      if (!window.confirm(`Delete the group "${org.name}"? Its members and pending invites go with it.`)) {
        return
      }
      try {
        await api.delete(`/api/admin/orgs/${org.id}`, adminApi)
        onSuccess(`Deleted group "${org.name}".`)
        return
      } catch (e) {
        const status = (e as { response?: { status?: number } })?.response?.status
        if (status !== 409) throw e
        // The backend refuses while a live subscription or stores remain. Forcing was
        // API-only, so the console told the admin to "retry with force=true" and gave
        // them no way to do it — an error naming an action the product did not offer.
        // It is offered here, but behind typed confirmation rather than a click,
        // because the damage it describes is billing that nothing can find again.
        const detail = detailOf(e) || 'The group could not be deleted.'
        // Offer the safe resolution first. Cancelling then deleting leaves nothing
        // billing and nothing orphaned; forcing leaves a live subscription that
        // nothing in the app can find again, so it is the fallback, not the default.
        const typed = window.prompt(
          `${detail}

Type CANCEL to cancel the subscription and delete the group.
` +
            `Type EVERYTHING to also DELETE its stores — call history, appointments and ` +
            `phone numbers go too, and this cannot be undone.
` +
            `Type FORCE to delete only the group and leave the subscription billing.`,
          ''
        )
        const choice = (typed || '').trim().toUpperCase()
        if (choice === 'EVERYTHING') {
          // Detaching leaves live stores with a phone number, no owner and no
          // billing. "Delete the group" reasonably means its locations go too — but
          // that destroys real call history, so it is its own word, not a default.
          await api.delete(
            `/api/admin/orgs/${org.id}?force=true&cancel_subscription=true&delete_stores=true`,
            adminApi
          )
          onSuccess(`Deleted group "${org.name}", its stores, and cancelled its subscription.`)
          return
        }
        if (choice === 'CANCEL') {
          await api.delete(
            `/api/admin/orgs/${org.id}?force=true&cancel_subscription=true`,
            adminApi
          )
          onSuccess(`Cancelled the subscription and deleted group "${org.name}". Its stores are now independent.`)
          return
        }
        if (choice === 'FORCE') {
          await api.delete(`/api/admin/orgs/${org.id}?force=true`, adminApi)
          onSuccess(
            `Force-deleted group "${org.name}". Its Stripe subscription is still live and nothing points at it — cancel it in Stripe.`
          )
          return
        }
        onError('Left the group alone.')
      }
    })

  const revokeInvite = (email: string) =>
    run(async () => {
      await api.delete(`/api/admin/orgs/${org.id}/invites`, { ...adminApi, data: { email } })
      onSuccess('Invite cancelled.')
    })

  return (
    <div className="rounded-2xl border border-white/10 bg-zinc-950/40 p-5">
      <div className="mb-4 flex items-center gap-2">
        <Building2 className="h-5 w-5 text-cyan-400" aria-hidden />
        <h3 className="font-semibold text-white">{org.name}</h3>
        <span className="text-xs text-zinc-500">
          {org.stores.length} {org.stores.length === 1 ? 'store' : 'stores'} ·{' '}
          {org.members.length} {org.members.length === 1 ? 'member' : 'members'}
          {org.pending_invites.length > 0 && ` · ${org.pending_invites.length} invited`}
        </span>
        <button
          type="button"
          onClick={deleteOrg}
          disabled={busy}
          title={
            org.stores.length
              ? 'Detach its stores first'
              : 'Delete this group'
          }
          className="ml-auto rounded p-1 text-zinc-600 motion-safe-transition hover:bg-white/5 hover:text-red-300 disabled:opacity-50"
        >
          <Trash2 className="h-4 w-4" aria-hidden />
        </button>
      </div>

      <div className="grid gap-5 md:grid-cols-2">
        {/* Stores */}
        <div>
          <div className="mb-2 flex items-center gap-1.5 text-xs font-medium uppercase tracking-wide text-zinc-500">
            <Store className="h-3.5 w-3.5" aria-hidden />
            Stores
          </div>
          <div className="space-y-1.5">
            {org.stores.length === 0 && (
              <p className="text-xs text-zinc-600">No stores attached yet.</p>
            )}
            {org.stores.map((s) => (
              <div
                key={s.tenant_id}
                className="flex items-center justify-between rounded-lg border border-white/10 bg-zinc-900/60 px-3 py-1.5"
              >
                <span className="truncate text-sm text-zinc-200">{s.name}</span>
                <button
                  type="button"
                  onClick={() => detachStore(s.tenant_id)}
                  disabled={busy}
                  title="Remove from group"
                  className="rounded p-1 text-zinc-500 hover:text-red-300 disabled:opacity-50"
                >
                  <X className="h-3.5 w-3.5" aria-hidden />
                </button>
              </div>
            ))}
          </div>
          <div className="mt-2 flex gap-2">
            <select
              value={attachId}
              onChange={(e) => setAttachId(e.target.value)}
              className="min-w-0 flex-1 rounded-lg border border-white/10 bg-zinc-950/60 px-2 py-1.5 text-sm text-white focus:border-cyan-500 focus:outline-none"
            >
              <option value="">Attach an existing store…</option>
              {available.map((t) => (
                <option key={t.id} value={t.id}>
                  {t.name} ({t.client_id})
                </option>
              ))}
            </select>
            <button
              type="button"
              onClick={attachStore}
              disabled={busy || !attachId}
              className="shrink-0 rounded-lg border border-white/15 px-3 py-1.5 text-xs font-medium text-zinc-200 hover:bg-white/5 disabled:opacity-50"
            >
              Attach
            </button>
          </div>
          {available.length === 0 && (
            <p className="mt-1 text-xs text-zinc-600">
              Every store is already in a group. A manager can add new ones from their dashboard.
            </p>
          )}
        </div>

        {/* Members */}
        <div>
          <div className="mb-2 flex items-center gap-1.5 text-xs font-medium uppercase tracking-wide text-zinc-500">
            <UserPlus className="h-3.5 w-3.5" aria-hidden />
            People
          </div>
          <div className="space-y-1.5">
            {org.members.length === 0 && (
              <p className="text-xs text-zinc-600">No one can see this group yet.</p>
            )}
            {org.members.map((m) => (
              <div
                key={m.clerk_user_id}
                className="flex items-center justify-between rounded-lg border border-white/10 bg-zinc-900/60 px-3 py-1.5"
              >
                <span className="min-w-0">
                  <span className="block truncate font-mono text-xs text-zinc-300">
                    {m.clerk_user_id}
                  </span>
                  <span className="text-[11px] text-zinc-500">{m.role}</span>
                </span>
                <button
                  type="button"
                  onClick={() => removeMember(m.clerk_user_id)}
                  disabled={busy}
                  title="Remove access"
                  className="rounded p-1 text-zinc-500 hover:text-red-300 disabled:opacity-50"
                >
                  <X className="h-3.5 w-3.5" aria-hidden />
                </button>
              </div>
            ))}
            {org.pending_invites.map((inv) => (
              <div
                key={inv.email}
                className="flex items-center justify-between rounded-lg border border-dashed border-white/15 bg-zinc-900/30 px-3 py-1.5"
              >
                <span className="min-w-0">
                  <span className="block truncate text-xs text-zinc-300">{inv.email}</span>
                  <span className="text-[11px] text-amber-400/80">Invited · {inv.role} · not signed up yet</span>
                </span>
                <button
                  type="button"
                  onClick={() => revokeInvite(inv.email)}
                  disabled={busy}
                  title="Cancel invite"
                  className="rounded p-1 text-zinc-500 hover:text-red-300 disabled:opacity-50"
                >
                  <X className="h-3.5 w-3.5" aria-hidden />
                </button>
              </div>
            ))}
          </div>
          <div className="mt-2 space-y-2">
            <input
              type="email"
              value={memberEmail}
              onChange={(e) => setMemberEmail(e.target.value)}
              placeholder="manager@company.com"
              className="w-full rounded-lg border border-white/10 bg-zinc-950/60 px-2 py-1.5 text-sm text-white placeholder-zinc-600 focus:border-cyan-500 focus:outline-none"
            />
            <div className="flex gap-2">
              <select
                value={memberRole}
                onChange={(e) => setMemberRole(e.target.value as 'manager' | 'viewer')}
                className="min-w-0 flex-1 rounded-lg border border-white/10 bg-zinc-950/60 px-2 py-1.5 text-sm text-white focus:border-cyan-500 focus:outline-none"
              >
                <option value="manager">Manager — provisions &amp; edits stores</option>
                <option value="viewer">Viewer — read-only oversight</option>
              </select>
              <button
                type="button"
                onClick={addMember}
                disabled={busy || !memberEmail.trim()}
                className="shrink-0 rounded-lg border border-white/15 px-3 py-1.5 text-xs font-medium text-zinc-200 hover:bg-white/5 disabled:opacity-50"
              >
                Invite
              </button>
            </div>
            <p className="text-[11px] text-zinc-600">
              If they already have an account they&rsquo;re added right away; otherwise we email an
              invite and they join the first time they sign in with this address.
            </p>
          </div>

          <div className="space-y-2 rounded-xl border border-white/10 bg-zinc-950/40 p-3">
            <p className="text-xs font-medium text-zinc-300">Partner pricing</p>
            <p className="text-[11px] text-zinc-600">
              A flat amount off every store, on every plan. This has to be a price
              rather than a coupon: a group is one subscription with quantity = store
              count, so a coupon would come off the invoice once instead of off each
              store, and a percentage would change value when they change plan.
              A group that has already checked out is moved onto the new price too,
              effective from their next invoice.
            </p>
            <p className="mb-2 text-[11px] text-zinc-400">
              {hasPartnerRate ? (
                <span className="text-cyan-300">
                  Partner rate active on: {activePlans.join(', ')}. The box below is for
                  changing it — it starts empty, it is not a readout.
                </span>
              ) : (
                <span>No partner rate — this group pays list price.</span>
              )}
            </p>
            <div className="flex flex-wrap items-end gap-2">
              <label className="block">
                <span className="mb-1 block text-[11px] uppercase tracking-wide text-zinc-500">
                  $ off each store / month
                </span>
                <input
                  value={discount}
                  onChange={(e) => setDiscount(e.target.value)}
                  inputMode="decimal"
                  placeholder="e.g. 50"
                  className="w-28 rounded-lg border border-white/15 bg-zinc-950 px-2 py-1.5 text-sm text-zinc-100 placeholder:text-zinc-700 focus:border-cyan-500/50 focus:outline-none"
                />
              </label>
              <button
                type="button"
                onClick={() => void applyDiscount()}
                disabled={applyingDiscount}
                className="rounded-lg bg-cyan-500/15 px-3 py-1.5 text-xs font-medium text-cyan-200 hover:bg-cyan-500/25 disabled:opacity-50"
              >
                {applyingDiscount ? 'Applying…' : 'Apply to all plans'}
              </button>
              <button
                type="button"
                onClick={() => void applyDiscount(true)}
                disabled={applyingDiscount}
                className="rounded-lg px-2.5 py-1.5 text-xs text-zinc-400 hover:bg-white/5 disabled:opacity-50"
              >
                Clear
              </button>
            </div>

            <details className="group/prices">
              <summary className="cursor-pointer list-none text-[11px] text-zinc-500 hover:text-zinc-300 [&::-webkit-details-marker]:hidden">
                Price IDs it resolved to (advanced)
              </summary>
            <div className="mt-2 grid gap-2 sm:grid-cols-3">
              {(['starter', 'growth', 'pro'] as const).map((plan) => (
                <label key={plan} className="block">
                  <span className="mb-1 block text-[11px] uppercase tracking-wide text-zinc-500">
                    {plan}
                  </span>
                  <input
                    value={prices[plan] || ''}
                    onChange={(e) => setPrices((p) => ({ ...p, [plan]: e.target.value }))}
                    placeholder="price_…"
                    className="w-full rounded-lg border border-white/15 bg-zinc-950 px-2 py-1.5 text-xs text-zinc-100 placeholder:text-zinc-700 focus:border-cyan-500/50 focus:outline-none"
                  />
                </label>
              ))}
            </div>
            </details>
            <div className="flex flex-wrap items-center gap-2">
              <button
                type="button"
                onClick={() => void savePrices()}
                disabled={savingPrices}
                className="rounded-lg bg-white/10 px-3 py-1.5 text-xs text-zinc-200 hover:bg-white/15 disabled:opacity-50"
              >
                {savingPrices ? 'Saving…' : 'Save pricing'}
              </button>
              {priceNote && <span className="text-[11px] text-zinc-400">{priceNote}</span>}
            </div>
          </div>
        </div>
      </div>
    </div>
  )
}
