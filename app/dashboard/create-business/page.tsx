'use client'

import { useEffect, useRef, useState } from 'react'
import { useRouter } from 'next/navigation'
import { Check, Play, Pause } from 'lucide-react'
import { AppChrome } from '@/components/layout/AppChrome'
import { useApiClient } from '@/lib/api'

// Voices a caller might hear, previewable before paying (static samples in public/).
const PREVIEW_VOICES = [
  { id: 'fable', label: 'Fable' },
  { id: 'nova', label: 'Nova' },
  { id: 'shimmer', label: 'Shimmer' },
  { id: 'onyx', label: 'Onyx' },
] as const

// Prices are display-only — the real charge comes from the Stripe price IDs on the
// backend. Keep these in sync with your Stripe products. Features mirror backend/plans.py.
const PLANS = [
  {
    id: 'starter',
    name: 'Starter',
    price: '$150',
    tagline: 'Get your AI receptionist answering & booking.',
    features: [
      '500 call minutes / month',
      'Answers calls & books appointments, 24/7',
      'Texts appointment confirmations',
      '100 two-way text conversations / month',
      '30-day call history',
    ],
  },
  {
    id: 'growth',
    name: 'Growth',
    price: '$250',
    popular: true,
    tagline: 'Fill more of the calendar and chase every lead.',
    features: [
      'Everything in Starter, plus:',
      '1,500 call minutes / month',
      'Appointment reminders (cut no-shows)',
      'Lead capture & follow-up texts',
      'Messages inbox — read & search every text',
      'Call recording & AI summaries',
      'SMS automations + CSV export',
      '90-day call history',
    ],
  },
  {
    id: 'pro',
    name: 'Pro',
    price: '$399',
    tagline: 'Full power for high-volume, multi-chair shops.',
    features: [
      'Everything in Growth, plus:',
      '3,500 call minutes / month',
      'Unlimited SMS automations & transfers',
      '1,000 text conversations / month',
      'Unlimited call history',
    ],
  },
] as const

type PlanId = (typeof PLANS)[number]['id']

export default function CreateBusinessPage() {
  const api = useApiClient()
  const router = useRouter()
  const [name, setName] = useState('')
  const [plan, setPlan] = useState<PlanId>('starter')
  const [numberMode, setNumberMode] = useState<'new' | 'existing'>('new')
  const [existingNumber, setExistingNumber] = useState('')
  const [areaCode, setAreaCode] = useState('')
  const [submitting, setSubmitting] = useState(false)
  const [demoSubmitting, setDemoSubmitting] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const [checking, setChecking] = useState(true)
  // They already have a seeded demo tenant and are here to turn it into a real one.
  const [isDemo, setIsDemo] = useState(false)
  const [playingVoice, setPlayingVoice] = useState<string | null>(null)
  const audioRef = useRef<HTMLAudioElement | null>(null)
  const [referralCode, setReferralCode] = useState('')
  const [referralState, setReferralState] = useState<'idle' | 'checking' | 'valid' | 'invalid'>('idle')
  const [referrerName, setReferrerName] = useState('')

  const playSample = (v: string) => {
    try {
      audioRef.current?.pause()
      if (playingVoice === v) {
        setPlayingVoice(null)
        return
      }
      const a = new Audio(`/voice-samples/${v}.mp3`)
      audioRef.current = a
      a.onended = () => setPlayingVoice(null)
      setPlayingVoice(v)
      void a.play().catch(() => setPlayingVoice(null))
    } catch {
      setPlayingVoice(null)
    }
  }

  useEffect(() => () => audioRef.current?.pause(), [])

  // Live-validate the referral code (debounced) so the user sees the free month apply.
  useEffect(() => {
    const code = referralCode.trim()
    if (!code) {
      setReferralState('idle')
      setReferrerName('')
      return
    }
    setReferralState('checking')
    const handle = setTimeout(() => {
      api
        .get(`/api/referral/validate?code=${encodeURIComponent(code)}`)
        .then((r) => {
          if (r?.data?.valid) {
            setReferralState('valid')
            setReferrerName(r.data.referrer_first_name || '')
          } else {
            setReferralState('invalid')
            setReferrerName('')
          }
        })
        .catch(() => setReferralState('invalid'))
    }, 450)
    return () => clearTimeout(handle)
  }, [referralCode, api])

  // If they already have a live business, skip this and go to the dashboard. A demo
  // tenant also has can_use_app (its access comes from a billing exemption), but it
  // must still reach this form — this is the only way to activate.
  useEffect(() => {
    Promise.all([
      api.get('/api/subscription'),
      // Someone invited to oversee a group owns no store of their own, so
      // can_use_app is false and this form kept them — asking them to build the
      // business they were just invited to. The sign-up link carries a redirect,
      // but that only covers the first arrival; signing in later landed here again.
      api.get('/api/org/me').catch(() => null),
    ])
      .then(([r, orgRes]) => {
        if (r?.data?.can_use_app && !r?.data?.demo_mode) {
          router.replace('/dashboard')
          return
        }
        if (orgRes?.data?.is_org_member) {
          router.replace('/dashboard/stores')
          return
        }
        setIsDemo(Boolean(r?.data?.demo_mode))
        setChecking(false) // no tenant yet, pending payment, or activating a demo
      })
      .catch(() => setChecking(false))
  }, [api, router])

  // Prefill the business name for a demo tenant activating, so they aren't retyping
  // it — and so an untouched field can't silently rename their shop.
  useEffect(() => {
    if (!isDemo) return
    api
      .get('/api/business-info')
      .then((r) => {
        const existing = (r?.data?.name || '').trim()
        if (existing) setName((cur) => cur || existing)
      })
      .catch(() => {})
  }, [isDemo, api])

  // 10 US digits (ignoring a leading country 1) before we let them forward an existing line.
  const existingDigits = existingNumber.replace(/\D/g, '').replace(/^1/, '')
  const existingValid = numberMode === 'new' || existingDigits.length === 10

  // Card-free path: build a seeded demo tenant and drop them straight into it.
  const startDemo = async () => {
    if (!name.trim()) {
      setError('Enter your business name first — the demo is set up under it.')
      return
    }
    setDemoSubmitting(true)
    setError(null)
    try {
      await api.post('/api/onboarding/start-demo', { name: name.trim() })
      router.push('/dashboard')
    } catch (err) {
      const detail = (err as { response?: { data?: { detail?: string } } })?.response?.data?.detail
      setError(typeof detail === 'string' ? detail : 'Could not start the demo. Please try again.')
      setDemoSubmitting(false)
    }
  }

  const submit = async (e: React.FormEvent) => {
    e.preventDefault()
    if (!name.trim()) return
    if (numberMode === 'existing' && !existingValid) {
      setError('Enter the existing business number you want to forward calls from.')
      return
    }
    setSubmitting(true)
    setError(null)
    try {
      await api.post('/api/onboarding/create-business', {
        name: name.trim(),
        plan,
        number_mode: numberMode,
        existing_number: numberMode === 'existing' ? existingNumber.trim() : undefined,
      })
      const res = await api.post('/api/create-checkout-session', {
        plan,
        area_code: areaCode.trim() || undefined,
        referral_code: referralCode.trim() || undefined,
      })
      const url = res?.data?.url
      if (url) {
        window.location.href = url
        return
      }
      setError('Could not start checkout. Please try again.')
      setSubmitting(false)
    } catch (err) {
      const detail = (err as { response?: { data?: { detail?: string } } })?.response?.data?.detail
      setError(typeof detail === 'string' ? detail : 'Something went wrong. Please try again.')
      setSubmitting(false)
    }
  }

  if (checking) {
    return (
      <AppChrome>
        <main className="flex min-h-screen items-center justify-center">
          <div className="h-10 w-10 animate-spin rounded-full border-2 border-cyan-400/30 border-t-cyan-400" />
        </main>
      </AppChrome>
    )
  }

  return (
    <AppChrome>
      <main className="min-h-screen px-4 py-10 md:px-6">
        <div className="mx-auto max-w-lg">
          <h1 className="font-display text-2xl font-semibold text-white">
            {isDemo ? 'Activate your line' : 'Set up your business'}
          </h1>
          <p className="mt-1 text-sm text-zinc-400">
            {isDemo ? (
              <>
                Pick your plan and number, then add a card to start your 7-day free trial. Your
                demo&rsquo;s sample calls, appointments and services are cleared when you activate,
                so you&rsquo;ll set up your own services and team on a clean slate.
              </>
            ) : (
              <>
                Tell us about your business and pick a plan. You&rsquo;ll add a card to start a 7-day
                free trial — then we set up your AI phone line automatically.
              </>
            )}
          </p>

          {error && (
            <div className="mt-4 rounded-xl border border-red-500/30 bg-red-500/10 px-4 py-3 text-sm text-red-200">
              {error}
            </div>
          )}

          <form
            onSubmit={submit}
            className="mt-6 space-y-5 rounded-2xl border border-white/10 bg-zinc-900/70 p-6 shadow-xl backdrop-blur-md"
          >
            <div>
              <label className="mb-1 block text-sm font-medium text-zinc-300">Business name</label>
              <input
                type="text"
                required
                value={name}
                onChange={(e) => setName(e.target.value)}
                placeholder="e.g. Acme Salon"
                className="w-full rounded-lg border border-white/10 bg-zinc-950/60 px-3 py-2 text-sm text-white placeholder-zinc-600 focus:border-cyan-500 focus:outline-none"
              />
              <p className="mt-1 text-xs text-zinc-500">Callers hear this in your greeting.</p>
            </div>

            <div>
              <label className="mb-2 block text-sm font-medium text-zinc-300">
                Hear your AI receptionist
              </label>
              <p className="mb-2 text-xs text-zinc-500">
                Tap to preview a voice — you can pick and fine-tune yours in Settings after signup.
              </p>
              <div className="flex flex-wrap gap-2">
                {PREVIEW_VOICES.map((v) => {
                  const playing = playingVoice === v.id
                  return (
                    <button
                      type="button"
                      key={v.id}
                      onClick={() => playSample(v.id)}
                      aria-pressed={playing}
                      className={`inline-flex items-center gap-1.5 rounded-full border px-3 py-1.5 text-xs font-medium transition ${
                        playing
                          ? 'border-cyan-500 bg-cyan-500/10 text-cyan-200'
                          : 'border-white/10 bg-zinc-950/40 text-zinc-300 hover:border-white/25'
                      }`}
                    >
                      {playing ? <Pause className="h-3.5 w-3.5" /> : <Play className="h-3.5 w-3.5" />}
                      {v.label}
                    </button>
                  )
                })}
              </div>
            </div>

            <div>
              <label className="mb-2 block text-sm font-medium text-zinc-300">Choose a plan</label>
              <div className="grid gap-3">
                {PLANS.map((p) => {
                  const selected = plan === p.id
                  return (
                    <button
                      type="button"
                      key={p.id}
                      onClick={() => setPlan(p.id)}
                      className={`relative rounded-2xl border p-4 text-left transition ${
                        selected
                          ? 'border-cyan-500 bg-cyan-500/10 ring-1 ring-cyan-500/40'
                          : 'border-white/10 bg-zinc-950/40 hover:border-white/25'
                      }`}
                    >
                      {'popular' in p && (
                        <span className="absolute -top-2 right-4 rounded-full bg-gradient-to-r from-cyan-500 to-indigo-600 px-2 py-0.5 text-[10px] font-semibold uppercase tracking-wide text-white">
                          Most popular
                        </span>
                      )}
                      <div className="flex items-start justify-between gap-3">
                        <div className="flex items-center gap-2">
                          <span
                            className={`h-4 w-4 shrink-0 rounded-full border ${
                              selected ? 'border-cyan-400 bg-cyan-400' : 'border-zinc-600'
                            }`}
                          />
                          <div>
                            <span className="text-base font-semibold text-white">{p.name}</span>
                            <p className="text-xs text-zinc-400">{p.tagline}</p>
                          </div>
                        </div>
                        <div className="shrink-0 text-right">
                          <span className="text-lg font-bold text-white">{p.price}</span>
                          <span className="block text-[11px] text-zinc-500">/month</span>
                        </div>
                      </div>
                      <ul className="mt-3 space-y-1.5 pl-6">
                        {p.features.map((f) => (
                          <li key={f} className="flex items-start gap-2 text-xs text-zinc-300">
                            <Check className="mt-0.5 h-3.5 w-3.5 shrink-0 text-cyan-400" />
                            <span>{f}</span>
                          </li>
                        ))}
                      </ul>
                    </button>
                  )
                })}
              </div>
            </div>

            <div>
              <label className="mb-2 block text-sm font-medium text-zinc-300">Phone number</label>
              <div className="grid gap-3 sm:grid-cols-2">
                {([
                  {
                    id: 'new' as const,
                    title: 'Get a new number',
                    blurb: "We give you a dedicated number for your AI receptionist.",
                  },
                  {
                    id: 'existing' as const,
                    title: 'Use my existing number',
                    blurb: 'Keep your current number — forward calls to your AI line.',
                  },
                ]).map((opt) => {
                  const selected = numberMode === opt.id
                  return (
                    <button
                      type="button"
                      key={opt.id}
                      onClick={() => setNumberMode(opt.id)}
                      className={`rounded-2xl border p-4 text-left transition ${
                        selected
                          ? 'border-cyan-500 bg-cyan-500/10 ring-1 ring-cyan-500/40'
                          : 'border-white/10 bg-zinc-950/40 hover:border-white/25'
                      }`}
                    >
                      <div className="flex items-center gap-2">
                        <span
                          className={`h-4 w-4 shrink-0 rounded-full border ${
                            selected ? 'border-cyan-400 bg-cyan-400' : 'border-zinc-600'
                          }`}
                        />
                        <span className="text-sm font-semibold text-white">{opt.title}</span>
                      </div>
                      <p className="mt-1 pl-6 text-xs text-zinc-400">{opt.blurb}</p>
                    </button>
                  )
                })}
              </div>

              {numberMode === 'existing' && (
                <div className="mt-3 rounded-xl border border-white/10 bg-zinc-950/40 p-4">
                  <label className="mb-1 block text-sm font-medium text-zinc-300">
                    Your existing business number
                  </label>
                  <input
                    type="tel"
                    inputMode="tel"
                    value={existingNumber}
                    onChange={(e) => setExistingNumber(e.target.value.replace(/[^\d\s()+-]/g, '').slice(0, 20))}
                    placeholder="e.g. (415) 555-0199"
                    className="w-56 rounded-lg border border-white/10 bg-zinc-950/60 px-3 py-2 text-sm text-white placeholder-zinc-600 focus:border-cyan-500 focus:outline-none"
                  />
                  {existingNumber.trim() && !existingValid && (
                    <p className="mt-1 text-xs text-amber-400">Enter a 10-digit US phone number.</p>
                  )}
                  <p className="mt-2 text-xs text-zinc-500">
                    Customers keep calling this number. After signup we&rsquo;ll show you how to forward it to
                    your AI line (takes ~2 minutes). Booking confirmation texts come from the AI line.
                  </p>
                </div>
              )}
            </div>

            <div>
              <label className="mb-1 block text-sm font-medium text-zinc-300">
                Preferred area code <span className="text-zinc-500">(optional)</span>
              </label>
              <input
                type="text"
                inputMode="numeric"
                maxLength={3}
                value={areaCode}
                onChange={(e) => setAreaCode(e.target.value.replace(/\D/g, '').slice(0, 3))}
                placeholder="e.g. 415"
                className="w-32 rounded-lg border border-white/10 bg-zinc-950/60 px-3 py-2 text-sm text-white placeholder-zinc-600 focus:border-cyan-500 focus:outline-none"
              />
              <p className="mt-1 text-xs text-zinc-500">
                {numberMode === 'existing'
                  ? 'Your AI line is behind the scenes, but you can still pick its area code.'
                  : 'We’ll try to get you a number in this area code.'}
              </p>
            </div>

            <div>
              <label className="mb-1 block text-sm font-medium text-zinc-300">
                Have a referral code? <span className="text-zinc-500">(optional)</span>
              </label>
              <input
                type="text"
                value={referralCode}
                onChange={(e) => setReferralCode(e.target.value.toUpperCase().replace(/[^A-Z0-9-]/g, '').slice(0, 40))}
                placeholder="e.g. JANE"
                className="w-48 rounded-lg border border-white/10 bg-zinc-950/60 px-3 py-2 text-sm text-white placeholder-zinc-600 focus:border-cyan-500 focus:outline-none"
              />
              {referralState === 'valid' && (
                <p className="mt-1 text-xs font-medium text-emerald-400">
                  🎉 First month free{referrerName ? ` — referred by ${referrerName}` : ''}!
                </p>
              )}
              {referralState === 'invalid' && referralCode.trim() && (
                <p className="mt-1 text-xs text-zinc-500">
                  We don&rsquo;t recognize that code — you&rsquo;ll still get the standard 7-day trial.
                </p>
              )}
              {referralState === 'checking' && (
                <p className="mt-1 text-xs text-zinc-500">Checking code…</p>
              )}
            </div>

            <button
              type="submit"
              disabled={submitting || demoSubmitting || !name.trim() || !existingValid}
              className="w-full rounded-full bg-gradient-to-r from-cyan-600 to-indigo-600 px-6 py-3 text-sm font-semibold text-white shadow-lg shadow-cyan-500/20 hover:brightness-110 disabled:opacity-50"
            >
              {submitting ? 'Starting checkout…' : 'Continue to payment'}
            </button>
            <p className="text-center text-xs text-zinc-500">
              {referralState === 'valid' ? 'First month free · cancel anytime' : 'Free for 7 days · cancel anytime'}
            </p>

            {!isDemo && (
              <div className="border-t border-white/10 pt-5">
                <button
                  type="button"
                  onClick={startDemo}
                  disabled={submitting || demoSubmitting || !name.trim()}
                  className="w-full rounded-full border border-white/15 bg-zinc-950/40 px-6 py-3 text-sm font-semibold text-zinc-200 motion-safe-transition hover:border-white/30 hover:text-white disabled:opacity-50"
                >
                  {demoSubmitting ? 'Building your demo…' : 'Explore a demo dashboard first'}
                </button>
                <p className="mt-2 text-center text-xs text-zinc-500">
                  No card needed. We&rsquo;ll fill a dashboard with sample calls and bookings so you
                  can see how it works. Your trial doesn&rsquo;t start until you activate.
                </p>
              </div>
            )}
          </form>
        </div>
      </main>
    </AppChrome>
  )
}
