'use client'

/** One store in the admin tenant list.
 *
 * Extracted from the admin page for a reason worth recording: the controls that
 * needed folding away were not siblings. The Twilio field sat inside the left
 * column, the exempt controls inside the right one, and the Stripe notice outside
 * both — so no wrapper could enclose them where they stood. Splitting the row apart
 * is what makes "Manage" possible at all.
 *
 * The summary is what you read; everything under Manage is what you do. A franchise
 * page is dozens of rows, and each was carrying nine controls whether you wanted
 * them or not.
 */

import { useState } from 'react'
import { motion } from 'framer-motion'
import { ChevronDown } from 'lucide-react'
import { formatTrialEndDate } from '@/lib/formatTrialEnd'
import {
  US_E164_PREFIX,
  inputClass,
  selectClass,
  isUsTenantTwilioDraft,
  nationalDigitsForUsTwilioInput,
  UsTwilioPhoneInput,
} from '@/components/admin/tenantFields'
import type { Tenant, TenantRowCtx } from '@/components/admin/tenantTypes'

export function accessStatusLabel(status: Tenant['access_status']): string {
  switch (status) {
    case 'active':
      return 'Active'
    case 'pending_invite':
      return 'Invite pending'
    case 'active_pending_mismatch':
      return 'Active · invite differs'
    default:
      return 'No email'
  }
}

export function accessStatusClass(status: Tenant['access_status']): string {
  switch (status) {
    case 'active':
      return 'bg-emerald-500/15 text-emerald-300'
    case 'pending_invite':
      return 'bg-amber-500/15 text-amber-200'
    case 'active_pending_mismatch':
      return 'bg-orange-500/15 text-orange-200'
    default:
      return 'bg-zinc-500/15 text-zinc-400'
  }
}

export function TenantRow({ t, ctx }: { t: Tenant; ctx: TenantRowCtx }) {
  const [manageOpen, setManageOpen] = useState(false)
  const {
    accessDebugData, accessDebugLoading, accessDebugOpen, setAccessDebugOpen,
    loadTenantAccessDebug, checkStripe, stripeChecking, stripeStatus,
    deleting, handleDelete, exemptAction, setExemptAction, exemptUntilDate,
    setExemptUntilDate, exempting, handleBillingExempt, handleResendInvite,
    resendingInvite, inviteEmailByTenant, setInviteEmailByTenant, handleSaveTwilio,
    twilioDraft, setTwilioDraft, twilioSaving, handleTogglePause, pausing,
    listItem, openDashboard,
  } = ctx
  const twilioDraftVal = twilioDraft[t.id] ?? ''
  const canSaveTwilioNumber = isUsTenantTwilioDraft(twilioDraftVal)
    ? nationalDigitsForUsTwilioInput(twilioDraftVal).length > 0
    : twilioDraftVal.trim().length > 0

  return (
    <motion.li variants={listItem} className="py-5 first:pt-0 last:pb-0">
      <div className="flex flex-wrap items-start justify-between gap-4">
        <div className="min-w-0">
          <span className="font-medium text-zinc-100">{t.name}</span>
          <span className="ml-2 text-sm text-zinc-500">({t.client_id})</span>
          <div className="mt-2 flex flex-wrap items-center gap-2 text-sm">
            <span className="text-zinc-500">Dashboard email:</span>
            {t.allocated_email ? (
              <span className="font-medium text-zinc-100">{t.allocated_email}</span>
            ) : (
              <span className="italic text-zinc-600">None assigned</span>
            )}
            <span
              className={`rounded-full px-2 py-0.5 text-xs font-medium ${accessStatusClass(t.access_status)}`}
            >
              {accessStatusLabel(t.access_status)}
            </span>
          </div>
          {t.access_status === 'active_pending_mismatch' &&
            t.owner_email &&
            t.pending_invite_email && (
              <p className="mt-1 text-xs text-orange-200/90">
                Signed in as {t.owner_email}; pending invite for {t.pending_invite_email}. Resend
                invite to replace owner.
              </p>
            )}
          <div className="mt-1 flex flex-wrap items-center gap-3">
            <span className="text-sm text-zinc-400">{t.twilio_phone_number || 'No number yet'}</span>
            <span className="rounded-full bg-cyan-500/15 px-2 py-0.5 text-xs font-medium text-cyan-300">{t.plan}</span>
            {t.business_vertical && (
              <span className="text-xs text-zinc-500">vertical: {t.business_vertical}</span>
            )}
            {t.subscription_status && (
              <span className="text-xs text-zinc-500">status: {t.subscription_status}</span>
            )}
            {t.account_paused && (
              <span className="rounded-full bg-amber-500/15 px-2 py-0.5 text-[10px] font-semibold uppercase tracking-wide text-amber-300">
                Paused
              </span>
            )}
          </div>
        </div>
        <div className="flex shrink-0 flex-wrap items-center gap-2">
          <button
            type="button"
            onClick={() => openDashboard(t.client_id)}
            className="rounded-lg border border-cyan-400/30 px-2.5 py-1.5 text-sm text-cyan-200 motion-safe-transition hover:bg-cyan-500/10"
          >
            Open dashboard
          </button>
          <button
            type="button"
            onClick={() => setManageOpen((v) => !v)}
            aria-expanded={manageOpen}
            className="inline-flex items-center gap-1 rounded-lg border border-white/10 px-2.5 py-1.5 text-sm text-zinc-300 motion-safe-transition hover:bg-white/5"
          >
            <ChevronDown
              className={`h-4 w-4 transition-transform ${manageOpen ? 'rotate-180' : ''}`}
              aria-hidden
            />
            Manage
          </button>
        </div>
      </div>

      {manageOpen && (
        <div className="mt-4 rounded-xl border border-white/10 bg-zinc-950/40 p-4">
          <div className="mt-3 flex max-w-xl flex-wrap items-end gap-2">
            <div className="min-w-[200px] flex-1">
              <label className="mb-1 block text-xs font-medium text-zinc-500">
                Inbound Twilio US number (E.164)
              </label>
              {isUsTenantTwilioDraft(twilioDraft[t.id]) ? (
                <UsTwilioPhoneInput
                  autoComplete="tel-national"
                  value={twilioDraft[t.id] ?? US_E164_PREFIX}
                  onChange={(full) => setTwilioDraft((d) => ({ ...d, [t.id]: full }))}
                  placeholderNational="5551234567"
                />
              ) : (
                <input
                  type="tel"
                  autoComplete="tel"
                  value={twilioDraft[t.id] ?? ''}
                  onChange={(e) => setTwilioDraft((d) => ({ ...d, [t.id]: e.target.value }))}
                  placeholder="+15551234567"
                  className={inputClass}
                />
              )}
            </div>
            <button
              type="button"
              onClick={() => handleSaveTwilio(t.id)}
              disabled={twilioSaving === t.id || !canSaveTwilioNumber}
              className="rounded-lg bg-cyan-600/80 px-3 py-2 text-sm font-medium text-white motion-safe-transition hover:bg-cyan-600 disabled:cursor-not-allowed disabled:opacity-50"
            >
              {twilioSaving === t.id ? 'Saving…' : 'Save number'}
            </button>
          </div>
          {(t.trial_ends_at || t.billing_exempt_until) && (
            <div className="mt-1 text-xs text-zinc-500">
              {t.trial_ends_at && <>Trial ends: {formatTrialEndDate(t.trial_ends_at)}</>}
              {t.trial_ends_at && t.billing_exempt_until && ' · '}
              {t.billing_exempt_until && <>Exempt until: {formatTrialEndDate(t.billing_exempt_until)}</>}
            </div>
          )}
          <div className="mt-3 flex max-w-xl flex-wrap items-end gap-2">
            <div className="min-w-[200px] flex-1">
              <label className="mb-1 block text-xs font-medium text-zinc-500">
                Client email (one per tenant — replaces prior owner)
              </label>
              <input
                type="email"
                value={inviteEmailByTenant[t.id] ?? ''}
                onChange={(e) =>
                  setInviteEmailByTenant((d) => ({ ...d, [t.id]: e.target.value }))
                }
                placeholder="you@yourdomain.com"
                className={inputClass}
              />
            </div>
            <button
              type="button"
              onClick={() => handleResendInvite(t.id)}
              disabled={resendingInvite === t.id}
              className="rounded-lg border border-cyan-500/40 bg-cyan-500/10 px-3 py-2 text-sm font-medium text-cyan-200 motion-safe-transition hover:bg-cyan-500/20 disabled:cursor-not-allowed disabled:opacity-50"
            >
              {resendingInvite === t.id ? 'Sending...' : 'Resend invite'}
            </button>
            <button
              type="button"
              onClick={() => void loadTenantAccessDebug(t.id)}
              disabled={accessDebugLoading === t.id}
              className="rounded-lg border border-white/15 bg-white/5 px-3 py-2 text-sm text-zinc-300 hover:bg-white/10 disabled:opacity-50"
            >
              {accessDebugLoading === t.id ? 'Loading…' : 'Access debug'}
            </button>
          </div>
          <div className="flex flex-wrap items-center gap-2">

            <div className="flex flex-wrap items-center gap-2">
              <select
                value={exemptAction[t.id] || ''}
                onChange={(e) => setExemptAction((a) => ({ ...a, [t.id]: e.target.value }))}
                className={selectClass}
              >
                <option value="">Exempt from payment…</option>
                <option value="extend_trial_1">Extend trial 1 month</option>
                <option value="free_1">Give 1 month free</option>
                <option value="free_3">Give 3 months free</option>
                <option value="exempt_until">Exempt until date</option>
              </select>
              {exemptAction[t.id] === 'exempt_until' && (
                <input
                  type="date"
                  value={exemptUntilDate[t.id] || ''}
                  onChange={(e) => setExemptUntilDate((d) => ({ ...d, [t.id]: e.target.value }))}
                  className={`${selectClass} date-input-dark`}
                />
              )}
              <button
                type="button"
                onClick={() => handleBillingExempt(t.id)}
                disabled={
                  exempting === t.id ||
                  !exemptAction[t.id] ||
                  (exemptAction[t.id] === 'exempt_until' && !exemptUntilDate[t.id])
                }
                className="rounded-lg bg-white/10 px-2 py-1 text-sm text-zinc-200 motion-safe-transition hover:bg-white/15 disabled:opacity-50"
              >
                {exempting === t.id ? 'Applying…' : 'Apply'}
              </button>
              {/* These controls grant access in Call Surge AND push the Stripe
                  trial — on the group's subscription when the group pays, since
                  the store's own column is empty then. Both times that was
                  missed, a customer with an "extended" trial was charged. */}
              <button
                type="button"
                onClick={() => void checkStripe(t.id)}
                disabled={stripeChecking === t.id}
                className="rounded-lg border border-white/15 px-2 py-1 text-sm text-zinc-300 motion-safe-transition hover:bg-white/10 disabled:opacity-50"
              >
                {stripeChecking === t.id ? 'Checking…' : 'Check Stripe'}
              </button>
            </div>
            <button
              type="button"
              onClick={() => handleTogglePause(t)}
              disabled={pausing === t.id}
              className={`rounded-lg border px-3 py-1.5 text-sm motion-safe-transition disabled:cursor-not-allowed disabled:opacity-50 ${
                t.account_paused
                  ? 'border-emerald-500/40 text-emerald-300 hover:bg-emerald-500/10'
                  : 'border-amber-500/40 text-amber-300 hover:bg-amber-500/10'
              }`}
            >
              {pausing === t.id
                ? 'Saving…'
                : t.account_paused
                  ? 'Resume'
                  : 'Pause'}
            </button>
            <button
              type="button"
              onClick={() => handleDelete(t)}
              disabled={deleting === t.id}
              className="rounded-lg border border-red-500/40 px-3 py-1.5 text-sm text-red-300 motion-safe-transition hover:bg-red-500/10 disabled:cursor-not-allowed disabled:opacity-50"
            >
              {deleting === t.id ? 'Removing…' : 'Remove'}
            </button>
          </div>
          <p className="mt-2 text-xs text-zinc-500">
            Exemptions and trial extensions also push the Stripe trial to the
            same date, so a customer with a live subscription stops being
            billed until then. If Stripe can&rsquo;t be updated the message
            above will say so — read it, because the grant alone does not
            stop a charge.
          </p>
          {stripeStatus[t.id] && (
            <div
              className={`mt-2 rounded-lg border px-3 py-2 text-xs ${
                stripeStatus[t.id].in_sync === false
                  ? 'border-amber-500/40 bg-amber-500/10 text-amber-200'
                  : 'border-white/10 bg-white/5 text-zinc-300'
              }`}
            >
              {!stripeStatus[t.id].has_subscription || stripeStatus[t.id].stripe == null ? (
                <span>{stripeStatus[t.id].message || 'No Stripe subscription.'}</span>
              ) : (
                <>
                  <span>
                    Stripe says <strong>{stripeStatus[t.id].stripe}</strong>; we have{' '}
                    <strong>{stripeStatus[t.id].ours || 'nothing'}</strong>
                    {stripeStatus[t.id].scope === 'org' && " (the group's subscription)"}.
                  </span>
                  {stripeStatus[t.id].in_sync === false && (
                    <span className="ml-1 font-medium">
                      These disagree — a webhook probably didn&rsquo;t land.
                    </span>
                  )}
                  {stripeStatus[t.id].current_period_end && (
                    <span className="ml-1">
                      {stripeStatus[t.id].cancel_at_period_end
                        ? 'Ends'
                        : 'Renews and charges'}{' '}
                      {formatTrialEndDate(stripeStatus[t.id].current_period_end as string)}.
                    </span>
                  )}
                </>
              )}
            </div>
          )}
        </div>
      )}
    </motion.li>
  )
}
