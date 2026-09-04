/** Types shared by the admin page and the tenant row it renders. */

import type { Variants } from 'framer-motion'

export type TenantAccessStatus = 'active' | 'pending_invite' | 'none' | 'active_pending_mismatch'

export interface Tenant {
  id: string
  client_id: string
  name: string
  twilio_phone_number: string | null
  plan: string
  created_at: string | null
  trial_ends_at?: string | null
  subscription_status?: string | null
  billing_exempt_until?: string | null
  account_paused?: boolean
  business_vertical?: string | null
  owner_email?: string | null
  pending_invite_email?: string | null
  allocated_email?: string | null
  access_status?: TenantAccessStatus
  org_id?: string | null
  /** Group this store belongs to, so a franchise's locations sit together. */
  org_name?: string | null
}

export type StripeStatus = {
  has_subscription: boolean
  /** Whose subscription this is: the store's own, or the group's that pays for it. */
  scope?: 'tenant' | 'org' | null
  ours?: string | null
  stripe?: string | null
  in_sync?: boolean | null
  cancel_at_period_end?: boolean
  current_period_end?: string | null
  trial_end?: string | null
  message?: string
}

/** Everything a row needs from the page. Passed as one object rather than two dozen
 *  props — the row is a view onto the page's state, not an independent widget. */
export type TenantRowCtx = {
  accessDebugData: Record<string, unknown>
  accessDebugLoading: string | null
  accessDebugOpen: Record<string, boolean>
  setAccessDebugOpen: React.Dispatch<React.SetStateAction<Record<string, boolean>>>
  loadTenantAccessDebug: (id: string) => void | Promise<void>
  checkStripe: (id: string) => void | Promise<void>
  stripeChecking: string | null
  stripeStatus: Record<string, StripeStatus>
  deleting: string | null
  handleDelete: (t: Tenant) => void | Promise<void>
  exemptAction: Record<string, string>
  setExemptAction: React.Dispatch<React.SetStateAction<Record<string, string>>>
  exemptUntilDate: Record<string, string>
  setExemptUntilDate: React.Dispatch<React.SetStateAction<Record<string, string>>>
  exempting: string | null
  handleBillingExempt: (id: string) => void | Promise<void>
  handleResendInvite: (id: string) => void | Promise<void>
  resendingInvite: string | null
  inviteEmailByTenant: Record<string, string>
  setInviteEmailByTenant: React.Dispatch<React.SetStateAction<Record<string, string>>>
  handleSaveTwilio: (id: string) => void | Promise<void>
  twilioDraft: Record<string, string>
  setTwilioDraft: React.Dispatch<React.SetStateAction<Record<string, string>>>
  twilioSaving: string | null
  handleTogglePause: (t: Tenant) => void | Promise<void>
  pausing: string | null
  listItem: Variants
  openDashboard: (clientId: string) => void
}
