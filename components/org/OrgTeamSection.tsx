'use client'

import { useCallback, useEffect, useState } from 'react'
import type { AxiosInstance } from 'axios'
import { Users, UserPlus, ShieldCheck, Trash2, Loader2 } from 'lucide-react'
import {
  ORG_ROLE_BLURB,
  ORG_ROLE_LABEL,
  ORG_ROLE_PICKER_HINT,
  orgRoleAtLeast,
} from '@/lib/orgRoles'

type Member = {
  clerk_user_id: string
  role: string
  created_at?: string | null
  email?: string | null
  is_you?: boolean
}
type PendingInvite = { email: string; role?: string | null }

type MembersResponse = {
  org_id: string
  your_role: string | null
  members: Member[]
  pending_invites: PendingInvite[]
  can_manage: boolean
  can_manage_owners: boolean
}

function detailOf(e: unknown): string | null {
  const d = (e as { response?: { data?: { detail?: unknown } } })?.response?.data?.detail
  return typeof d === 'string' ? d : null
}

function RoleBadge({ role }: { role: string }) {
  const tone =
    role === 'owner'
      ? 'bg-amber-500/15 text-amber-200 ring-amber-500/30'
      : role === 'manager'
        ? 'bg-cyan-500/15 text-cyan-200 ring-cyan-500/30'
        : 'bg-white/5 text-zinc-400 ring-white/10'
  return (
    <span className={`rounded-full px-2 py-0.5 text-[11px] font-medium ring-1 ${tone}`}>
      {ORG_ROLE_LABEL[role] || role}
    </span>
  )
}

type OrgChoice = { org_id: string; name: string }

export function OrgTeamSection({ api, orgs }: { api: AxiosInstance; orgs: OrgChoice[] }) {
  const [orgId, setOrgId] = useState(orgs[0]?.org_id)
  const [data, setData] = useState<MembersResponse | null>(null)
  const [error, setError] = useState<string | null>(null)
  const [notice, setNotice] = useState<string | null>(null)
  const [busy, setBusy] = useState<string | null>(null)
  const [inviteEmail, setInviteEmail] = useState('')
  const [inviteRole, setInviteRole] = useState('manager')
  // Typed confirmation, not a window.confirm: handing over the account is the one
  // action here the person cannot undo themselves afterwards.
  const [transferTo, setTransferTo] = useState<string | null>(null)
  const [transferConfirm, setTransferConfirm] = useState('')

  const qs = orgId ? `?org_id=${encodeURIComponent(orgId)}` : ''

  const load = useCallback(async () => {
    try {
      const res = await api.get<MembersResponse>(`/api/org/members${qs}`)
      setData(res.data)
      setError(null)
    } catch (e) {
      // Keep whatever is on screen; a failed refresh is not evidence the team is empty.
      setError(detailOf(e) || 'Could not load your team.')
    }
  }, [api, qs])

  useEffect(() => {
    void load()
  }, [load])

  const run = async (key: string, fn: () => Promise<unknown>, ok: string) => {
    setBusy(key)
    setError(null)
    setNotice(null)
    try {
      await fn()
      setNotice(ok)
      await load()
    } catch (e) {
      setError(detailOf(e) || 'That did not work.')
    } finally {
      setBusy(null)
    }
  }

  if (!data) {
    return (
      <section className="rounded-2xl border border-white/10 bg-zinc-900/70 p-6">
        <p className="text-sm text-zinc-500">{error || 'Loading your team…'}</p>
      </section>
    )
  }

  const canManage = data.can_manage
  const isOwner = data.can_manage_owners
  const owner = data.members.find((m) => m.role === 'owner')

  return (
    <section className="rounded-2xl border border-white/10 bg-zinc-900/70 p-6 shadow-xl">
      <div className="mb-1 flex items-center gap-2">
        <Users className="h-5 w-5 text-cyan-300" aria-hidden />
        <h2 className="font-display text-lg font-semibold text-white">Team</h2>
      </div>
      <p className="mb-4 text-sm text-zinc-500">
        Who can see and run every store in this group. To give someone access to a
        single store only, invite them from that store instead.
      </p>

      {orgs.length > 1 && (
        <label className="mb-5 block">
          <span className="mb-1 block text-[11px] uppercase tracking-wide text-zinc-500">Group</span>
          <select
            value={orgId}
            onChange={(e) => setOrgId(e.target.value)}
            className="w-full max-w-sm rounded-lg border border-white/15 bg-zinc-950 px-3 py-2 text-sm text-zinc-100"
          >
            {orgs.map((o) => (
              <option key={o.org_id} value={o.org_id}>
                {o.name}
              </option>
            ))}
          </select>
        </label>
      )}

      {error && (
        <p className="mb-4 rounded-lg border border-red-500/30 bg-red-500/10 px-3 py-2 text-sm text-red-200">
          {error}
        </p>
      )}
      {notice && (
        <p className="mb-4 rounded-lg border border-emerald-500/30 bg-emerald-500/10 px-3 py-2 text-sm text-emerald-200">
          {notice}
        </p>
      )}

      <ul className="mb-6 divide-y divide-white/5">
        {data.members.map((m) => {
          const targetIsOwner = m.role === 'owner'
          // A manager may not touch an owner; nobody may remove the only owner.
          const mayEdit = canManage && (!targetIsOwner || isOwner)
          return (
            <li key={m.clerk_user_id} className="flex flex-wrap items-center gap-3 py-3">
              <div className="min-w-0 flex-1">
                <p className="truncate text-sm text-zinc-200">
                  {m.email || m.clerk_user_id}
                  {m.is_you && <span className="ml-2 text-xs text-zinc-500">(you)</span>}
                </p>
                <p className="text-xs text-zinc-500">{ORG_ROLE_BLURB[m.role] || ''}</p>
              </div>
              <RoleBadge role={m.role} />

              {mayEdit && !targetIsOwner && (
                <select
                  value={m.role}
                  disabled={busy === m.clerk_user_id}
                  onChange={(e) =>
                    void run(
                      m.clerk_user_id,
                      () =>
                        api.patch(`/api/org/members/${m.clerk_user_id}`, {
                          role: e.target.value,
                          org_id: orgId,
                        }),
                      'Role updated.'
                    )
                  }
                  className="rounded-lg border border-white/15 bg-zinc-950 px-2 py-1 text-xs text-zinc-200"
                >
                  <option value="manager">Group admin</option>
                  <option value="viewer">Viewer</option>
                </select>
              )}

              {mayEdit && !targetIsOwner && (
                <button
                  type="button"
                  disabled={busy === m.clerk_user_id}
                  onClick={() =>
                    void run(
                      m.clerk_user_id,
                      () =>
                        api.delete(`/api/org/members/${m.clerk_user_id}${qs}`),
                      'Removed from the group.'
                    )
                  }
                  className="rounded-lg px-2 py-1 text-xs text-red-300 hover:bg-red-500/10 disabled:opacity-50"
                >
                  <Trash2 className="h-3.5 w-3.5" aria-hidden />
                  <span className="sr-only">Remove {m.email || m.clerk_user_id}</span>
                </button>
              )}

              {isOwner && !targetIsOwner && (
                <button
                  type="button"
                  onClick={() => {
                    setTransferTo(m.clerk_user_id)
                    setTransferConfirm('')
                  }}
                  className="rounded-lg px-2 py-1 text-xs text-amber-200 hover:bg-amber-500/10"
                >
                  Make owner
                </button>
              )}
            </li>
          )
        })}
      </ul>

      {data.pending_invites.length > 0 && (
        <div className="mb-6">
          <p className="mb-1 text-[11px] uppercase tracking-wide text-zinc-500">Invited, not yet signed in</p>
          <p className="mb-2 text-xs text-zinc-600">
            They get access the first time they sign in with that email. Until then
            they have none.
          </p>
          <ul className="space-y-1">
            {data.pending_invites.map((p) => (
              <li key={p.email} className="flex items-center gap-2 text-sm text-zinc-400">
                <span className="truncate">{p.email}</span>
                {p.role && <RoleBadge role={p.role} />}
                {canManage && (
                  <button
                    type="button"
                    disabled={busy === p.email}
                    onClick={() => {
                      // Say whether the emailed link is actually dead. If Clerk
                      // could not be reached it may still work, and "cancelled" on
                      // its own would imply otherwise.
                      setBusy(p.email)
                      setError(null)
                      setNotice(null)
                      void (async () => {
                        try {
                          const { data } = await api.delete<{
                            link_revoked?: boolean
                            clerk_error?: string | null
                          }>(
                            `/api/org/invites?email=${encodeURIComponent(p.email)}${
                              orgId ? `&org_id=${encodeURIComponent(orgId)}` : ''
                            }`
                          )
                          setNotice(
                            data.link_revoked
                              ? `Invitation to ${p.email} cancelled, and the emailed link no longer works.`
                              : `Invitation to ${p.email} cancelled here, but the emailed link may still work${
                                  data.clerk_error ? ` (${data.clerk_error})` : ''
                                }. Re-invite to replace it.`
                          )
                          await load()
                        } catch (e) {
                          setError(detailOf(e) || 'That did not work.')
                        } finally {
                          setBusy(null)
                        }
                      })()
                    }}
                    className="rounded-lg px-2 py-0.5 text-xs text-red-300 hover:bg-red-500/10 disabled:opacity-50"
                  >
                    Cancel
                  </button>
                )}
              </li>
            ))}
          </ul>
        </div>
      )}

      {canManage && (
        <form
          className="flex flex-wrap items-end gap-2 border-t border-white/5 pt-5"
          onSubmit={(e) => {
            e.preventDefault()
            const email = inviteEmail.trim()
            if (!email) return
            // "Invited" covered three different outcomes, one of which sends no
            // email at all. The API distinguishes them; say which happened rather
            // than leaving someone waiting on a message that was never sent.
            setBusy('invite')
            setError(null)
            setNotice(null)
            void (async () => {
              try {
                const { data } = await api.post<{
                  added?: boolean
                  invite_sent?: boolean
                  pending?: boolean
                  clerk_error?: string | null
                }>('/api/org/members', { email, role: inviteRole, org_id: orgId })
                setInviteEmail('')
                if (data.added) {
                  setNotice(`${email} already had an account and now has access. No email was sent.`)
                } else if (data.invite_sent) {
                  setNotice(`Invite email sent to ${email}. They get access when they sign in with that address.`)
                } else if (data.pending) {
                  setNotice(
                    `${email} is on the list and will get access when they sign up with that address — but no email was sent${
                      data.clerk_error ? `: ${data.clerk_error}` : '.'
                    } Send them the link yourself.`
                  )
                } else {
                  setNotice(`Invited ${email}.`)
                }
                await load()
              } catch (e) {
                setError(detailOf(e) || 'That did not work.')
              } finally {
                setBusy(null)
              }
            })()
          }}
        >
          <label className="block">
            <span className="mb-1 block text-[11px] uppercase tracking-wide text-zinc-500">
              Invite by email
            </span>
            <input
              type="email"
              value={inviteEmail}
              onChange={(e) => setInviteEmail(e.target.value)}
              placeholder="name@company.com"
              className="w-64 rounded-lg border border-white/15 bg-zinc-950 px-3 py-2 text-sm text-zinc-100 placeholder:text-zinc-600"
            />
          </label>
          <label className="block">
            <span className="mb-1 block text-[11px] uppercase tracking-wide text-zinc-500">Role</span>
            <select
              value={inviteRole}
              onChange={(e) => setInviteRole(e.target.value)}
              className="rounded-lg border border-white/15 bg-zinc-950 px-3 py-2 text-sm text-zinc-100"
            >
              <option value="manager">Group admin</option>
              <option value="viewer">Viewer</option>
            </select>
          </label>
          <button
            type="submit"
            disabled={busy === 'invite' || !inviteEmail.trim()}
            className="inline-flex items-center gap-1.5 rounded-lg bg-cyan-500/15 px-3 py-2 text-sm font-medium text-cyan-200 hover:bg-cyan-500/25 disabled:opacity-50"
          >
            {busy === 'invite' ? (
              <Loader2 className="h-4 w-4 animate-spin" aria-hidden />
            ) : (
              <UserPlus className="h-4 w-4" aria-hidden />
            )}
            Send invite
          </button>
          {/* Owner is absent from both selects on purpose: ownership moves by
              transfer, never by promotion, so there is only one owner at a time. */}
          <p className="w-full text-xs text-zinc-600">
            {ORG_ROLE_PICKER_HINT} There is only one Owner — to hand the group over,
            invite them as a Group admin first, then use “Make owner”.
          </p>
        </form>
      )}

      {transferTo && (
        <div className="mt-5 rounded-xl border border-amber-500/30 bg-amber-500/5 p-4">
          <div className="mb-2 flex items-center gap-2">
            <ShieldCheck className="h-4 w-4 text-amber-300" aria-hidden />
            <p className="text-sm font-semibold text-amber-100">Hand over the group</p>
          </div>
          <p className="mb-3 text-sm text-amber-100/80">
            <strong>{data.members.find((m) => m.clerk_user_id === transferTo)?.email || transferTo}</strong>{' '}
            becomes the owner and{' '}
            <strong>you become a manager</strong>. You will not be able to undo this
            yourself — only the new owner can hand it back.
            {owner ? '' : ' This group currently has no owner.'}
          </p>
          <label className="mb-3 block">
            <span className="mb-1 block text-[11px] uppercase tracking-wide text-amber-200/70">
              Type TRANSFER to confirm
            </span>
            <input
              value={transferConfirm}
              onChange={(e) => setTransferConfirm(e.target.value)}
              className="w-48 rounded-lg border border-amber-500/30 bg-zinc-950 px-3 py-2 text-sm text-amber-50"
            />
          </label>
          <div className="flex gap-2">
            <button
              type="button"
              disabled={transferConfirm.trim().toUpperCase() !== 'TRANSFER' || busy === 'transfer'}
              onClick={() =>
                void run(
                  'transfer',
                  async () => {
                    await api.post(
                      `/api/org/members/${transferTo}/transfer-ownership`,
                      { org_id: orgId }
                    )
                    setTransferTo(null)
                    setTransferConfirm('')
                  },
                  'Ownership transferred. You are now a manager.'
                )
              }
              className="rounded-lg bg-amber-500/20 px-3 py-2 text-sm font-medium text-amber-100 hover:bg-amber-500/30 disabled:opacity-40"
            >
              Transfer ownership
            </button>
            <button
              type="button"
              onClick={() => {
                setTransferTo(null)
                setTransferConfirm('')
              }}
              className="rounded-lg px-3 py-2 text-sm text-zinc-400 hover:bg-white/5"
            >
              Cancel
            </button>
          </div>
        </div>
      )}

      {!canManage && (
        <p className="border-t border-white/5 pt-5 text-sm text-zinc-500">
          You can see this group but not change who is in it.{' '}
          {orgRoleAtLeast(data.your_role, 'manager') ? '' : 'Ask an owner or manager for access.'}
        </p>
      )}
    </section>
  )
}
