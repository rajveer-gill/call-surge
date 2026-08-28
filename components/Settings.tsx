'use client'

import { useState, useEffect, useRef, useMemo, type ReactNode } from 'react'
import { AnimatePresence, motion, useReducedMotion } from 'framer-motion'
import * as Sentry from '@sentry/nextjs'
import { useAuth } from '@clerk/nextjs'
import {
  Volume2,
  Store,
  Save,
  Shuffle,
  User,
  Play,
  Square,
  CreditCard,
  CheckCircle2,
  Circle,
  AlertTriangle,
  Clock,
  Users,
  PhoneForwarded,
  MessageSquare,
  ChevronDown,
} from 'lucide-react'
import { useApiClient } from '@/lib/api'
import {
  RANDOM_NAMES,
  SPEECH_SPEED_MAX,
  SPEECH_SPEED_MIN,
  SPEECH_SPEED_STEP,
  VOICES,
  VOICE_SAMPLE_BASE,
  VOICE_SAMPLE_TEXT,
} from '@/components/settings/constants'
import { BookingModeSection } from '@/components/settings/BookingModeSection'
import { ImportServicesButton } from '@/components/settings/ImportServicesButton'
import { SmsAutomationsSection } from '@/components/settings/SmsAutomationsSection'
import { LockedFeature } from '@/components/ui/LockedFeature'
import { StaffMembersSection, normalizeStaffFromApi, WORKING_DAYS, type StaffRow } from '@/components/settings/StaffMembersSection'
import { TimeOffModal } from '@/components/settings/TimeOffModal'
import {
  TransferTargetsSection,
  normalizeTransferFromApi,
  type TransferRow,
} from '@/components/settings/TransferTargetsSection'
import {
  normalizeServices,
  normalizeSpecials,
  normalizeRules,
  ServicesEditor,
  SpecialsEditor,
  RulesEditor,
  type ServiceRow,
  type SpecialRow,
  type RuleRow,
} from '@/components/settings/StructuredListEditors'
import { BusinessHoursModal } from '@/components/settings/BusinessHoursModal'
import { CarrierForwardingInstructions } from '@/components/CarrierForwardingInstructions'
import { parseHoursToWeekly, summarizeSchedule } from '@/lib/businessHours'
/** Set NEXT_PUBLIC_DEBUG_SETTINGS=1 in .env.local (or Vercel) to log per-endpoint load outcomes — no tokens. */
const DEBUG_SETTINGS = process.env.NEXT_PUBLIC_DEBUG_SETTINGS === '1'

/**
 * SMS Automations is built (component + /api/sms-automations backend) but has NOT been tested
 * end-to-end, so it's hidden from Settings — we don't want to show or sell a feature we can't
 * stand behind yet. Hides both the live section and the locked upsell. Flip to true to restore;
 * nothing else was removed.
 */
const SHOW_SMS_AUTOMATIONS = false

/** Remembers which cards someone folded away, so Settings opens the way they left it. */
function useSectionOpen(storageKey: string | undefined, fallback: boolean) {
  const [open, setOpen] = useState(fallback)
  useEffect(() => {
    if (!storageKey) return
    try {
      const saved = window.localStorage.getItem(`cs.settings.${storageKey}`)
      if (saved === '0' || saved === '1') setOpen(saved === '1')
    } catch {
      /* private mode — the default is fine */
    }
  }, [storageKey])
  const toggle = () => {
    setOpen((v) => {
      const next = !v
      if (storageKey) {
        try {
          window.localStorage.setItem(`cs.settings.${storageKey}`, next ? '1' : '0')
        } catch {
          /* ignore */
        }
      }
      return next
    })
  }
  return { open, toggle }
}

function SettingsSection({
  children,
  className = '',
  delay = 0,
  title,
  icon,
  titleId,
  /** Omit to render a plain, always-open card (the setup checklist). */
  storageKey,
  defaultOpen = true,
  ...rest
}: {
  children: ReactNode
  className?: string
  delay?: number
  title?: ReactNode
  icon?: ReactNode
  titleId?: string
  storageKey?: string
  defaultOpen?: boolean
} & Omit<
  React.ComponentPropsWithoutRef<'section'>,
  // framer-motion declares its own versions of these with different
  // signatures, so spreading the native ones onto motion.section is a type
  // conflict. A settings panel passes none of them.
  'title' | 'onDrag' | 'onDragStart' | 'onDragEnd' | 'onDragEnter' | 'onDragLeave'
  | 'onDragOver' | 'onDrop' | 'onAnimationStart' | 'onAnimationEnd'
  | 'onAnimationIteration'
>) {
  const reduceMotion = useReducedMotion()
  const { open, toggle } = useSectionOpen(storageKey, defaultOpen)
  const collapsible = Boolean(title)
  return (
    <motion.section
      {...rest}
      className={`relative overflow-hidden rounded-2xl border border-slate-200/90 bg-white p-8 shadow-xl shadow-slate-900/10 ring-1 ring-slate-900/[0.04] ${className}`}
      initial={reduceMotion ? false : { opacity: 1, y: 14 }}
      animate={{ opacity: 1, y: 0 }}
      transition={
        reduceMotion
          ? { duration: 0 }
          : { delay: delay * 0.06, duration: 0.4, ease: [0.22, 1, 0.36, 1] }
      }
      whileHover={reduceMotion ? undefined : { y: -3, transition: { type: 'spring', stiffness: 420, damping: 26 } }}
    >
      <motion.div
        aria-hidden
        className="pointer-events-none absolute -right-16 -top-16 h-36 w-36 rounded-full bg-gradient-to-br from-primary-400/25 via-cyan-300/10 to-transparent blur-2xl"
        animate={reduceMotion ? undefined : { scale: [1, 1.12, 1], opacity: [0.35, 0.65, 0.35] }}
        transition={{ duration: 5.5, repeat: Infinity, ease: 'easeInOut' }}
      />
      <motion.div
        aria-hidden
        className="pointer-events-none absolute -bottom-12 -left-12 h-28 w-28 rounded-full bg-gradient-to-tr from-violet-400/15 to-transparent blur-2xl"
        animate={reduceMotion ? undefined : { x: [0, 8, 0], y: [0, -6, 0] }}
        transition={{ duration: 7, repeat: Infinity, ease: 'easeInOut' }}
      />
      <div className="relative z-10">
        {collapsible ? (
          <>
            <button
              type="button"
              onClick={toggle}
              aria-expanded={open}
              className="-m-2 mb-2 flex w-[calc(100%+1rem)] items-center gap-2 rounded-xl p-2 text-left transition hover:bg-gray-50"
            >
              {icon}
              <h2
                id={titleId}
                className="flex flex-1 items-center gap-2 text-xl font-bold text-gray-900"
              >
                {title}
              </h2>
              <ChevronDown
                className={`h-5 w-5 shrink-0 text-gray-400 transition-transform ${open ? 'rotate-180' : ''}`}
                aria-hidden
              />
            </button>
            {/* Unmounted rather than hidden: a collapsed card must not keep a focusable
                field in the tab order, and the page is long enough already. */}
            {open ? children : null}
          </>
        ) : (
          children
        )}
      </div>
    </motion.section>
  )
}

export default function Settings() {
  const { isLoaded, isSignedIn } = useAuth()
  const api = useApiClient()
  const [loading, setLoading] = useState(true)
  const [saving, setSaving] = useState(false)
  const [message, setMessage] = useState<{ type: 'success' | 'error'; text: string } | null>(null)
  const [voice, setVoice] = useState<string>('fable')
  const [speechSpeed, setSpeechSpeed] = useState<number>(1.0)
  const [previewing, setPreviewing] = useState<string | null>(null)
  const audioRef = useRef<HTMLAudioElement | null>(null)
  const [receptionistName, setReceptionistName] = useState('')
  const [aiPhone, setAiPhone] = useState('')
  const [numberMode, setNumberMode] = useState<'new' | 'existing'>('new')
  // The mode actually persisted on the server. numberMode is the *selected* mode in the
  // UI; switching to "existing" only persists once a valid number is entered and saved,
  // so we track the saved value to flag an unsaved selection.
  const [savedNumberMode, setSavedNumberMode] = useState<'new' | 'existing'>('new')
  const [existingNumber, setExistingNumber] = useState('')
  const [forwardingVerifiedAt, setForwardingVerifiedAt] = useState<string | null>(null)
  const [savingNumberMode, setSavingNumberMode] = useState(false)
  const [tenantClientId, setTenantClientId] = useState('')
  const [portalLoading, setPortalLoading] = useState(false)
  const [billingError, setBillingError] = useState<string | null>(null)
  const [form, setForm] = useState({
    name: '',
    business_type: '',
    hours: '',
    forwarding_phone: '',
    transfer_takes_message: false,
    quote_prices: true,
    public_name: '',
    email: '',
    address: '',
    menu_link: '',
    greeting: '',
  })
  const [serviceItems, setServiceItems] = useState<ServiceRow[]>([])
  const [closures, setClosures] = useState<string[]>([])
  const [timeOffOpen, setTimeOffOpen] = useState(false)
  const [specialItems, setSpecialItems] = useState<SpecialRow[]>([])
  const [ruleItems, setRuleItems] = useState<RuleRow[]>([])
  const [industryLocked, setIndustryLocked] = useState(false)
  const [verticalLabel, setVerticalLabel] = useState('')
  const [staff, setStaff] = useState<StaffRow[]>([])
  const [transferTargets, setTransferTargets] = useState<TransferRow[]>([])
  const [transferMax, setTransferMax] = useState<number | null>(null)
  const [greetingPreview, setGreetingPreview] = useState<{
    spoken_text: string
    main_greeting: string
    recording_disclosure: string | null
    config_source: string
    warnings?: string[]
    placeholders?: { business_name?: string; receptionist_name?: string }
  } | null>(null)
  const [greetingPreviewLoading, setGreetingPreviewLoading] = useState(false)
  const [automations, setAutomations] = useState<{ id: number; trigger: string; template: string; enabled: boolean }[]>([])
  const [smsAutomationsMax, setSmsAutomationsMax] = useState<number | null>(null)
  const [setupStatus, setSetupStatus] = useState<{ complete: boolean; missing: string[]; warnings: string[] } | null>(null)
  const [hoursModalOpen, setHoursModalOpen] = useState(false)
  const saveBarRef = useRef<HTMLDivElement>(null)
  const reduceMotion = useReducedMotion()

  const hoursSummaryPreview = useMemo(() => {
    const { schedule } = parseHoursToWeekly(form.hours || '')
    const line = summarizeSchedule(schedule, 96)
    return (form.hours || '').trim() ? line : ''
  }, [form.hours])

  // Shop open hours keyed by day code (mon..sun), derived from business hours. Undefined when hours
  // aren't set yet, so the staff working-day picker stays unrestricted. Schedule index 0=Mon..6=Sun
  // matches WORKING_DAYS order. Only open days get a key.
  const shopHours = useMemo(() => {
    if (!(form.hours || '').trim()) return undefined
    const { schedule } = parseHoursToWeekly(form.hours || '')
    const map: Record<string, { start: string; end: string }> = {}
    WORKING_DAYS.forEach((d, i) => {
      const slot = schedule[i]
      if (slot && !slot.closed) map[d.code] = { start: slot.open, end: slot.close }
    })
    return Object.keys(map).length ? map : undefined
  }, [form.hours])

  // Preload static voice samples so first play is instant
  useEffect(() => {
    VOICES.forEach((v) => {
      const a = new Audio()
      a.src = `${VOICE_SAMPLE_BASE}/${v}.mp3`
    })
  }, [])

  const refreshSetupStatus = () => {
    api.get('/api/setup-status').then((r) => {
      setSetupStatus(r.data)
      if (typeof window !== 'undefined') {
        window.dispatchEvent(new CustomEvent('call-surge-setup-status', { detail: r.data }))
      }
    }).catch(() => setSetupStatus(null))
  }

  useEffect(() => {
    if (!isLoaded) {
      return
    }
    if (!isSignedIn) {
      setLoading(false)
      setMessage(null)
      return
    }

    let cancelled = false
    setLoading(true)

    const swallow =
      (label: string, fallback: unknown) => (err: unknown) => {
        if (DEBUG_SETTINGS) {
          const ax = err as { response?: { status?: number; data?: unknown }; message?: string }
          console.warn(
            `[Settings] ${label} request failed`,
            ax.response?.status ?? 'network',
            ax.message,
            ax.response?.data !== undefined ? '(body present)' : ''
          )
        }
        return { data: fallback }
      }

    Promise.all([
      api.get('/api/business-info').catch(swallow('business-info', null as unknown)),
      api.get('/api/subscription').catch(swallow('subscription', null)),
      api.get('/api/sms-automations').catch(swallow('sms-automations', { automations: [] })),
      api.get('/api/setup-status').catch(swallow('setup-status', null)),
    ])
      .then(([infoRes, subRes, automationsRes, setupRes]) => {
        if (cancelled) return
        setMessage(null)
        try {
          const limits = (subRes?.data as { limits?: { transfer_max?: number; sms_automations_max?: number } } | null)?.limits
          if (limits?.transfer_max != null) setTransferMax(limits.transfer_max)
          if (limits?.sms_automations_max != null) setSmsAutomationsMax(limits.sms_automations_max)
          type AutomationRow = { id: number; trigger: string; template: string; enabled: boolean }
          setAutomations(
            (automationsRes?.data as { automations?: AutomationRow[] } | null)?.automations || []
          )
          const su = setupRes?.data as
            | { complete?: boolean; missing?: string[]; warnings?: string[] }
            | null
            | undefined
          setSetupStatus(
            su
              ? {
                  complete: Boolean(su.complete),
                  missing: su.missing ?? [],
                  warnings: su.warnings ?? [],
                }
              : null
          )

          const d = infoRes?.data as Record<string, unknown> | null | undefined
          if (!d) {
            if (DEBUG_SETTINGS) {
              console.warn(
                '[Settings] business-info body is empty — open Network → /api/business-info (status, JSON, CORS)'
              )
            }
            return
          }
          setVoice((d.voice as string) || 'fable')
          setStaff(normalizeStaffFromApi(d.staff ?? []))
          setTransferTargets(normalizeTransferFromApi(d.transfer_targets ?? []))
          const spd = typeof d.speed === 'number' ? d.speed : 1.0
          setSpeechSpeed(Math.max(SPEECH_SPEED_MIN, Math.min(SPEECH_SPEED_MAX, spd)))
          setReceptionistName((d.receptionist_name as string) || '')
          setAiPhone((d.phone as string) || '')
          setNumberMode((d.number_mode as 'new' | 'existing') === 'existing' ? 'existing' : 'new')
          setSavedNumberMode((d.number_mode as 'new' | 'existing') === 'existing' ? 'existing' : 'new')
          setExistingNumber((d.existing_business_number as string) || '')
          setForwardingVerifiedAt((d.forwarding_verified_at as string) || null)
          setTenantClientId((d.client_id as string) || '')
          setForm({
            name: (d.name as string) || '',
            business_type: (d.business_type as string) || '',
            hours: (d.hours as string) || '',
            forwarding_phone: (d.forwarding_phone as string) || '',
            transfer_takes_message: Boolean(d.transfer_takes_message),
            // Absent means yes — the shape every existing tenant is already in.
            quote_prices: d.quote_prices === undefined ? true : Boolean(d.quote_prices),
            public_name: (d.public_name as string) || '',
            email: (d.email as string) || '',
            address: (d.address as string) || '',
            menu_link: (d.menu_link as string) || '',
            greeting: (d.greeting as string) || '',
          })
          setServiceItems(normalizeServices(d.services))
          setSpecialItems(normalizeSpecials(d.specials))
          setRuleItems(normalizeRules(d.reservation_rules))
          setClosures(
            Array.isArray(d.closures)
              ? Array.from(
                  new Set((d.closures as unknown[]).map((x) => String(x).trim()).filter((s) => /^\d{4}-\d{2}-\d{2}$/.test(s))),
                ).sort()
              : [],
          )
          setIndustryLocked(!!d.business_type_admin_locked)
          setVerticalLabel(String(d.business_vertical_label || ''))
        } catch (e) {
          console.error('[Settings] failed to apply API response', e)
          Sentry.captureException(e instanceof Error ? e : new Error(String(e)), {
            tags: { area: 'settings_load' },
            extra: { phase: 'apply_response' },
          })
          setMessage({ type: 'error', text: 'Failed to load settings' })
        }
      })
      .catch((err) => {
        if (cancelled) return
        console.error('[Settings] settings fetch failed', err)
        Sentry.captureException(err instanceof Error ? err : new Error(String(err)), {
          tags: { area: 'settings_load' },
          extra: { phase: 'promise_all' },
        })
        setMessage({ type: 'error', text: 'Failed to load settings' })
      })
      .finally(() => {
        if (!cancelled) setLoading(false)
      })

    return () => {
      cancelled = true
    }
  }, [api, isLoaded, isSignedIn])

  useEffect(() => {
    if (!message || !saveBarRef.current) return
    saveBarRef.current.scrollIntoView({ behavior: reduceMotion ? 'auto' : 'smooth', block: 'nearest' })
  }, [message, reduceMotion])

  // Switch between a dedicated AI number and forwarding your own number. Persists on
  // its own (separate from the main Save) and resets verification when it changes.
  const saveNumberMode = async (mode: 'new' | 'existing', existing?: string) => {
    setSavingNumberMode(true)
    try {
      const r = await api.post('/api/business/number-mode', {
        number_mode: mode,
        existing_number: mode === 'existing' ? (existing ?? existingNumber) : undefined,
      })
      const persisted = r?.data?.number_mode === 'existing' ? 'existing' : 'new'
      setNumberMode(persisted)
      setSavedNumberMode(persisted)
      setExistingNumber(String(r?.data?.existing_business_number || ''))
      setForwardingVerifiedAt((r?.data?.forwarding_verified_at as string) || null)
      setMessage({ type: 'success', text: 'Phone setup updated' })
    } catch (e) {
      const detail = (e as { response?: { data?: { detail?: string } } })?.response?.data?.detail
      setMessage({ type: 'error', text: typeof detail === 'string' ? detail : 'Could not update phone setup' })
    } finally {
      setSavingNumberMode(false)
    }
  }

  // While forwarding isn't verified yet, poll for it — a forwarded test call stamps
  // it server-side, so the UI flips to "verified" without a manual refresh.
  useEffect(() => {
    if (numberMode !== 'existing' || forwardingVerifiedAt) return
    const id = setInterval(() => {
      api
        .get('/api/business-info')
        .then((r) => {
          const v = (r?.data?.forwarding_verified_at as string) || null
          if (v) setForwardingVerifiedAt(v)
        })
        .catch(() => {})
    }, 15000)
    return () => clearInterval(id)
  }, [api, numberMode, forwardingVerifiedAt])

  const randomizeName = () => {
    const current = receptionistName.trim().toLowerCase()
    let pick: string
    do {
      pick = RANDOM_NAMES[Math.floor(Math.random() * RANDOM_NAMES.length)]
    } while (pick.toLowerCase() === current && RANDOM_NAMES.length > 1)
    setReceptionistName(pick)
  }

  const previewVoice = async (v: string) => {
    if (previewing === v) {
      if (audioRef.current) {
        audioRef.current.pause()
        audioRef.current = null
      }
      setPreviewing(null)
      return
    }
    if (audioRef.current) {
      audioRef.current.pause()
      audioRef.current = null
    }
    setPreviewing(v)

    const cleanup = () => {
      setPreviewing(null)
      audioRef.current = null
    }

    const staticUrl = `${VOICE_SAMPLE_BASE}/${v}.mp3`
    const audio = new Audio(staticUrl)
    audio.playbackRate = speechSpeed
    audioRef.current = audio

    const fallbackToApi = async () => {
      audioRef.current = null
      try {
        const res = await api.post('/api/text-to-speech', { text: VOICE_SAMPLE_TEXT, voice: v, speed: speechSpeed }, { responseType: 'blob' })
        const url = URL.createObjectURL(res.data)
        const apiAudio = new Audio(url)
        audioRef.current = apiAudio
        apiAudio.onended = () => {
          setPreviewing(null)
          URL.revokeObjectURL(url)
          audioRef.current = null
        }
        apiAudio.onerror = () => {
          setPreviewing(null)
          URL.revokeObjectURL(url)
          audioRef.current = null
        }
        await apiAudio.play()
      } catch {
        setPreviewing(null)
      }
    }

    audio.onended = cleanup
    audio.onerror = () => fallbackToApi()
    try {
      await audio.play()
    } catch {
      fallbackToApi()
    }
  }

  const loadGreetingPreview = async () => {
    setGreetingPreviewLoading(true)
    try {
      const { data } = await api.get<{
        spoken_text: string
        main_greeting: string
        recording_disclosure: string | null
        config_source: string
        warnings?: string[]
        placeholders?: { business_name?: string; receptionist_name?: string }
      }>('/api/greeting-preview')
      setGreetingPreview(data)
    } catch {
      setGreetingPreview(null)
      setMessage({
        type: 'error',
        text: 'Could not load greeting preview. Save settings and try again.',
      })
    } finally {
      setGreetingPreviewLoading(false)
    }
  }

  const errorText = (e: unknown, fallback: string) => {
    const detail = (e as { response?: { data?: { detail?: unknown } } })?.response?.data?.detail
    if (typeof detail === 'string') return detail
    if (
      typeof detail === 'object' &&
      detail !== null &&
      'message' in detail &&
      typeof (detail as { message?: string }).message === 'string'
    ) {
      return (detail as { message: string }).message
    }
    return fallback
  }

  /** Write one part of Settings straight to the server.
   *
   * Sub-editors call this so a button labelled "Save" saves — and saves only what it
   * is attached to. Sending the whole form would quietly commit half-typed edits
   * elsewhere on the page, which is its own kind of surprise.
   *
   * Lists are sent even when empty, so deleting the last row persists; the PATCH
   * reads an absent field as "leave alone". Returns false on failure so the caller
   * can stay open and let the user retry.
   */
  const persistFields = async (
    patch: Record<string, unknown>,
    label: string
  ): Promise<boolean> => {
    setSaving(true)
    setMessage(null)
    try {
      await api.patch('/api/business-info', patch)
      setMessage({ type: 'success', text: `${label} saved.` })
      refreshSetupStatus()
      return true
    } catch (e: unknown) {
      setMessage({ type: 'error', text: errorText(e, `Failed to save ${label.toLowerCase()}`) })
      return false
    } finally {
      setSaving(false)
    }
  }

  const handleSave = async () => {
    setSaving(true)
    setMessage(null)
    try {
      const { data } = await api.patch('/api/business-info', {
        name: form.name || undefined,
        ...(!industryLocked ? { business_type: form.business_type || undefined } : {}),
        hours: form.hours || undefined,
        forwarding_phone: form.forwarding_phone || undefined,
        transfer_takes_message: form.transfer_takes_message,
        quote_prices: form.quote_prices,
        public_name: form.public_name ?? '',
        email: form.email || undefined,
        address: form.address || undefined,
        menu_link: form.menu_link || undefined,
        greeting: form.greeting || undefined,
        voice: voice || undefined,
        speed: speechSpeed,
        receptionist_name: receptionistName || undefined,
        staff: staff
          .filter((s) => s.name.trim() || s.phone.trim())
          .map((s) => ({
            id: s.id,
            name: s.name.trim(),
            phone: s.phone.trim(),
            email: s.email.trim() || undefined,
            notes: s.notes || undefined,
            service_ids: s.service_ids.length ? s.service_ids : undefined,
            // Preserve scheduling fields — omitting them here would wipe stylist
            // schedules / time off on a normal Settings save.
            working_days: s.working_days?.length ? s.working_days : undefined,
            working_hours: s.working_hours && Object.keys(s.working_hours).length ? s.working_hours : undefined,
            time_off: s.time_off?.length ? s.time_off : undefined,
          })),
        services: serviceItems.length ? serviceItems : undefined,
        specials: specialItems.length ? specialItems : undefined,
        reservation_rules: ruleItems.length ? ruleItems : undefined,
      })
      setStaff(normalizeStaffFromApi((data as { staff?: unknown }).staff ?? []))
      setMessage({ type: 'success', text: 'Settings saved. Your AI receptionist will use this info.' })
      setGreetingPreview(null)
      refreshSetupStatus()
    } catch (e: unknown) {
      setMessage({ type: 'error', text: errorText(e, 'Failed to save settings') })
    } finally {
      setSaving(false)
    }
  }

  const openBillingPortal = async () => {
    setPortalLoading(true)
    setBillingError(null)
    try {
      const { data } = await api.post<{ url: string }>('/api/create-portal-session')
      if (data?.url) {
        window.location.href = data.url
        return
      }
      setBillingError('Could not open billing portal')
    } catch (err: unknown) {
      const detail =
        (err as { response?: { data?: { detail?: string } } })?.response?.data?.detail
      setBillingError(detail || 'Could not open billing portal')
    } finally {
      setPortalLoading(false)
    }
  }

  if (loading) {
    return (
      <motion.div
        className="flex h-64 flex-col items-center justify-center gap-4"
        initial={{ opacity: 0 }}
        animate={{ opacity: 1 }}
      >
        <motion.div
          className="h-12 w-12 rounded-full border-2 border-primary-200 border-t-primary-600"
          animate={reduceMotion ? undefined : { rotate: 360 }}
          transition={{ duration: 0.9, repeat: Infinity, ease: 'linear' }}
        />
        <motion.p
          className="text-sm font-medium text-gray-500"
          animate={reduceMotion ? undefined : { opacity: [0.45, 1, 0.45] }}
          transition={{ duration: 1.6, repeat: Infinity }}
        >
          Loading settings...
        </motion.p>
      </motion.div>
    )
  }

  const setupComplete = setupStatus?.complete ?? true
  const missing = setupStatus?.missing ?? []
  const warnings = setupStatus?.warnings ?? []

  return (
    <div className="mx-auto max-w-4xl space-y-8 pb-44 text-gray-900">
      {/* Setup checklist: ensure AI has correct business info before taking calls */}
      <SettingsSection delay={0}>
        <h2 className="text-xl font-bold text-gray-900 flex items-center gap-2 mb-2">
          {setupComplete ? <CheckCircle2 className="w-6 h-6 text-green-600" /> : <AlertTriangle className="w-6 h-6 text-amber-500" />}
          Setup checklist
        </h2>
        <p className="text-gray-600 text-sm mb-4">
          Complete these so your AI receptionist can give callers accurate info and handle bookings. Works for any business: restaurants, salons, HVAC, real estate, and more.
        </p>
        <ul className="space-y-2">
          {(
            [
              { key: 'name', label: 'Business name' },
              { key: 'hours', label: 'Hours of operation' },
              { key: 'forwarding_phone', label: 'Store phone (real person)' },
              { key: 'address', label: 'Address' },
            ] as const
          ).map(({ key, label }) => {
            const done = !missing.includes(label)
            return (
            <motion.li
              key={key}
              layout
              initial={reduceMotion ? false : { opacity: 0, x: -8 }}
              animate={{ opacity: 1, x: 0 }}
              transition={{ delay: 0.05 * (['name', 'hours', 'forwarding_phone', 'address'].indexOf(key) + 1) }}
              className="flex items-center gap-2 text-sm"
            >
              {done ? <CheckCircle2 className="w-4 h-4 text-green-600 shrink-0" /> : <Circle className="w-4 h-4 text-gray-300 shrink-0" />}
              <span className={done ? 'text-gray-700' : 'text-gray-500'}>{label}</span>
              {key === 'forwarding_phone' && (
                <span className="text-gray-400 text-xs font-normal">(transfer number, or turn on &ldquo;take a message instead&rdquo;)</span>
              )}
            </motion.li>
            )
          })}
        </ul>
        {warnings.length > 0 && (
          <p className="mt-3 text-amber-700 text-sm flex items-start gap-2">
            <AlertTriangle className="w-4 h-4 shrink-0 mt-0.5" />
            {warnings[0]}
          </p>
        )}
        {!setupComplete && (
          <p className="mt-3 text-amber-700 text-sm font-medium">
            Fill in the required fields below and save. Your AI will work better with complete business info.
          </p>
        )}
      </SettingsSection>

      {/* AI Receptionist Identity */}
      <SettingsSection
        delay={1}
        storageKey="receptionist"
        icon={<User className="w-6 h-6 text-primary-600" />}
        title="AI Receptionist"
      >
        {(tenantClientId || aiPhone) && (
          <p className="text-sm text-gray-600 mb-6 rounded-lg border border-gray-200 bg-gray-50 px-3 py-2">
            {tenantClientId && (
              <>
                Settings apply to account <strong className="font-mono text-gray-800">{tenantClientId}</strong>
              </>
            )}
            {aiPhone && (
              <>
                {tenantClientId ? ' · ' : ''}
                Test calls must dial <strong className="text-gray-800">{aiPhone}</strong> (this line only).
              </>
            )}
          </p>
        )}

        {aiPhone && (
          <div className="mb-6 rounded-2xl border border-gray-200 bg-white p-5">
            <h3 className="flex items-center gap-2 text-base font-semibold text-gray-900">
              <PhoneForwarded className="h-5 w-5 text-teal-600" />
              Phone &amp; call forwarding
            </h3>
            <p className="mt-1 text-sm text-gray-500">
              Your AI line is <span className="font-mono text-gray-800">{aiPhone}</span>. Choose how customers reach it.
            </p>
            <div className="mt-4 grid gap-3 sm:grid-cols-2">
              <button
                type="button"
                onClick={() => saveNumberMode('new')}
                aria-pressed={numberMode === 'new'}
                disabled={savingNumberMode}
                className={`rounded-xl border p-3 text-left transition disabled:opacity-60 ${
                  numberMode === 'new'
                    ? 'border-cyan-500 bg-cyan-500/5 ring-1 ring-cyan-500/30'
                    : 'border-gray-200 hover:border-gray-300'
                }`}
              >
                <span className="flex items-center gap-2 text-sm font-semibold text-gray-900">
                  Use a dedicated number
                  {savedNumberMode === 'new' && (
                    <span className="inline-flex items-center gap-1 rounded-full bg-emerald-100 px-1.5 py-0.5 text-[10px] font-bold text-emerald-700">
                      <CheckCircle2 className="h-3 w-3" /> Active
                    </span>
                  )}
                </span>
                <span className="mt-0.5 block text-xs text-gray-500">Publish {aiPhone} as your business line.</span>
              </button>
              <button
                type="button"
                onClick={() => setNumberMode('existing')}
                aria-pressed={numberMode === 'existing'}
                className={`rounded-xl border p-3 text-left transition ${
                  numberMode === 'existing'
                    ? 'border-cyan-500 bg-cyan-500/5 ring-1 ring-cyan-500/30'
                    : 'border-gray-200 hover:border-gray-300'
                }`}
              >
                <span className="flex items-center gap-2 text-sm font-semibold text-gray-900">
                  Use my own number
                  {savedNumberMode === 'existing' && (
                    <span className="inline-flex items-center gap-1 rounded-full bg-emerald-100 px-1.5 py-0.5 text-[10px] font-bold text-emerald-700">
                      <CheckCircle2 className="h-3 w-3" /> Active
                    </span>
                  )}
                </span>
                <span className="mt-0.5 block text-xs text-gray-500">Keep your number; forward calls to the AI line.</span>
              </button>
            </div>

            {numberMode === 'existing' && (
              <div className="mt-4 space-y-4">
                {savedNumberMode !== 'existing' && (
                  <div className="flex items-start gap-2 rounded-lg border border-amber-300 bg-amber-50 px-3 py-2 text-sm text-amber-800">
                    <AlertTriangle className="mt-0.5 h-4 w-4 shrink-0" />
                    <span>
                      <strong className="font-semibold">Not saved yet.</strong> Selecting this doesn&apos;t switch your
                      account on its own — enter your existing business number below and click <strong>Save</strong> to
                      forward calls to your AI line.
                    </span>
                  </div>
                )}
                <div className="flex flex-wrap items-end gap-2">
                  <div>
                    <label className="mb-1 block text-sm font-medium text-gray-700">Your existing business number</label>
                    <input
                      type="tel"
                      value={existingNumber}
                      onChange={(e) => setExistingNumber(e.target.value.replace(/[^\d\s()+-]/g, '').slice(0, 20))}
                      placeholder="e.g. (415) 555-0199"
                      className="cs-field w-56"
                    />
                  </div>
                  <button
                    type="button"
                    onClick={() => saveNumberMode('existing', existingNumber)}
                    disabled={savingNumberMode || existingNumber.replace(/\D/g, '').replace(/^1/, '').length !== 10}
                    className="rounded-lg bg-cyan-600 px-4 py-2 text-sm font-semibold text-white hover:brightness-110 disabled:opacity-50"
                  >
                    {savingNumberMode ? 'Saving…' : 'Save'}
                  </button>
                </div>

                <CarrierForwardingInstructions aiLine={aiPhone} />

                {forwardingVerifiedAt ? (
                  <div className="flex items-center gap-2 rounded-lg border border-emerald-300 bg-emerald-50 px-3 py-2 text-sm font-medium text-emerald-800">
                    <CheckCircle2 className="h-4 w-4 shrink-0" />
                    Forwarding verified — your AI line is receiving forwarded calls.
                  </div>
                ) : (
                  <div className="rounded-lg border border-amber-300 bg-amber-50 px-3 py-2 text-sm text-amber-800">
                    Not verified yet. After setting up forwarding, call your business number from another phone — when the
                    AI answers, you&rsquo;re live. We&rsquo;ll auto-confirm here within ~30 seconds (some carriers don&rsquo;t
                    report forwarding, so the test call is the real proof either way).
                  </div>
                )}
              </div>
            )}
          </div>
        )}

        <div className="space-y-4">
          <div>
            <label className="block text-sm font-medium text-gray-700 mb-1">Receptionist name</label>
            <div className="flex gap-2">
              <input
                type="text"
                value={receptionistName}
                onChange={(e) => setReceptionistName(e.target.value)}
                className="cs-field flex-1 min-w-0"
                placeholder="Give your AI receptionist a name"
              />
              <motion.button
                type="button"
                onClick={randomizeName}
                whileHover={reduceMotion ? undefined : { scale: 1.03 }}
                whileTap={reduceMotion ? undefined : { scale: 0.97 }}
                className="inline-flex items-center gap-1.5 px-4 py-2 rounded-lg text-sm font-medium bg-gray-100 text-gray-700 hover:bg-gray-200 transition-colors"
                title="Random name"
              >
                <Shuffle className="w-4 h-4" />
                Random
              </motion.button>
            </div>
            <p className="text-xs text-gray-500 mt-1">This name is used when your AI introduces itself to callers.</p>
          </div>

          <div>
            <label className="block text-sm font-medium text-gray-700 mb-1">AI receptionist phone number</label>
            <input
              type="text"
              value={aiPhone}
              readOnly
              className="w-full cursor-not-allowed rounded-lg border border-gray-400 bg-gray-100 px-3 py-2 text-gray-800"
            />
            <p className="text-xs text-gray-500 mt-1">This is your AI receptionist&apos;s phone number. Contact your administrator to change it.</p>
            <p className="text-xs text-gray-500 mt-1">Calls and texts work when your number&apos;s Voice and Messaging webhooks are set in Twilio. If calls or texts aren&apos;t working, contact your administrator.</p>
          </div>

          {numberMode === 'existing' && aiPhone && (
            <div className="rounded-lg border border-primary-200 bg-primary-50 px-4 py-3 text-sm text-gray-700">
              <p className="font-medium text-gray-900">Forward your existing number to your AI line</p>
              <p className="mt-1">
                Customers keep calling{' '}
                <strong className="text-gray-900">{existingNumber || 'your existing number'}</strong>. Make sure
                that number forwards calls to your AI line{' '}
                <strong className="text-gray-900">{aiPhone}</strong>.
              </p>
              <p className="mt-2 text-xs text-gray-600">
                On most US carriers: dial <span className="font-mono">*72</span>, then {aiPhone}, then call
                (<span className="font-mono">*73</span> turns it off). On iPhone: Settings → Phone → Call
                Forwarding. Booking confirmation texts are sent from your AI line.
              </p>
            </div>
          )}
        </div>
      </SettingsSection>

      {/* Voice Settings */}
      <SettingsSection
        delay={2}
        storageKey="voice"
        defaultOpen={false}
        icon={<Volume2 className="w-6 h-6 text-primary-600" />}
        title="Voice settings"
      >
        <p className="text-gray-600 text-sm mb-4">
          Choose the voice and speaking speed for your AI receptionist (phone and SMS).
        </p>
        <div className="mb-6">
          <label className="block text-sm font-medium text-gray-700 mb-1">Speaking speed</label>
          <div className="flex items-center gap-4 flex-wrap">
            <input
              type="range"
              min={SPEECH_SPEED_MIN}
              max={SPEECH_SPEED_MAX}
              step={SPEECH_SPEED_STEP}
              value={speechSpeed}
              onChange={(e) => setSpeechSpeed(Number(e.target.value))}
              className="flex-1 min-w-[120px] h-2 rounded-lg appearance-none cursor-pointer bg-gray-200 accent-primary-600"
            />
            <div className="flex items-center gap-2">
              <input
                type="number"
                min={SPEECH_SPEED_MIN}
                max={SPEECH_SPEED_MAX}
                step={0.01}
                value={speechSpeed}
                onChange={(e) => {
                  const v = parseFloat(e.target.value)
                  if (!Number.isNaN(v)) {
                    setSpeechSpeed(Math.max(SPEECH_SPEED_MIN, Math.min(SPEECH_SPEED_MAX, v)))
                  }
                }}
                className="cs-field-compact w-20 text-right tabular-nums"
              />
            </div>
          </div>
          <p className="text-xs text-gray-500 mt-1">Drag the slider or type a value (0.25 = slowest, 4 = fastest).</p>
        </div>
        <div className="flex flex-wrap gap-3">
          {VOICES.map((v) => (
            <div key={v} className="flex items-center gap-1">
              <button
                type="button"
                onClick={() => setVoice(v)}
                className={`px-4 py-2 rounded-l-lg text-sm font-medium transition-all ${
                  voice === v
                    ? 'bg-primary-600 text-white shadow-md'
                    : 'bg-gray-100 text-gray-700 hover:bg-gray-200'
                }`}
              >
                {v.charAt(0).toUpperCase() + v.slice(1)}
              </button>
              <button
                type="button"
                onClick={() => previewVoice(v)}
                disabled={previewing !== null && previewing !== v}
                className={`px-2 py-2 rounded-r-lg text-sm transition-all ${
                  previewing === v
                    ? 'bg-red-500 text-white'
                    : voice === v
                      ? 'bg-primary-700 text-white hover:bg-primary-800'
                      : 'bg-gray-200 text-gray-600 hover:bg-gray-300'
                } disabled:opacity-40 disabled:cursor-not-allowed`}
                title={previewing === v ? 'Stop' : `Preview ${v}`}
              >
                {previewing === v ? <Square className="w-3.5 h-3.5" /> : <Play className="w-3.5 h-3.5" />}
              </button>
            </div>
          ))}
        </div>
        {previewing && (
          <motion.p
            initial={{ opacity: 0 }}
            animate={{ opacity: 1 }}
            className="mt-2 text-xs font-medium text-primary-600"
          >
            Playing {previewing} voice sample...
          </motion.p>
        )}
      </SettingsSection>

      {/* SMS Automations (Growth/Pro) — locked upsell on plans without it (e.g. Starter).
          Gated off entirely by SHOW_SMS_AUTOMATIONS until the feature is tested end-to-end. */}
      {!SHOW_SMS_AUTOMATIONS ? null : smsAutomationsMax != null && smsAutomationsMax > 0 ? (
        <SmsAutomationsSection
          automations={automations}
          smsAutomationsMax={smsAutomationsMax}
          onRefresh={() => api.get('/api/sms-automations').then((r) => setAutomations(r.data?.automations || [])).catch(() => {})}
          onAdd={(a) => setAutomations((prev) => [...prev, a])}
          api={api}
        />
      ) : smsAutomationsMax === 0 ? (
        <div className="mb-8">
          <LockedFeature
            variant="light"
            title="SMS automations"
            tagline="Automatically text customers at the right moment—after an inquiry or a missed call—so you stay top of mind without lifting a finger."
            bullets={[
              'Auto-text after an inquiry or post-call',
              'Custom templates for each trigger',
              'Set it once and it runs on every call',
            ]}
          />
        </div>
      ) : null}

      {/* Billing */}
      <SettingsSection
        delay={3}
        storageKey="billing"
        defaultOpen={false}
        icon={<CreditCard className="w-6 h-6 text-primary-600" />}
        title="Billing"
      >
        <p className="text-gray-600 text-sm mb-4">
          Change plan, update payment method, or manage your subscription.
        </p>
        <button
          type="button"
          onClick={openBillingPortal}
          disabled={portalLoading}
          className="inline-flex items-center gap-2 px-5 py-2.5 rounded-lg font-medium bg-primary-600 text-white hover:bg-primary-700 disabled:opacity-50"
        >
          <CreditCard className="w-4 h-4" />
          {portalLoading ? 'Opening...' : 'Manage subscription'}
        </button>
        {billingError && (
          <p className="mt-3 text-sm text-red-600">{billingError}</p>
        )}
        <p className="mt-4 text-xs text-gray-500">
          To cancel your subscription, use &quot;Manage subscription&quot; above; cancellation is available in the billing portal.
        </p>
        <button
          type="button"
          onClick={openBillingPortal}
          disabled={portalLoading}
          className="mt-3 text-xs text-gray-500 hover:text-gray-700 underline"
        >
          Cancel service
        </button>
      </SettingsSection>

      {/* Business info: restaurant, salon, HVAC, real estate, etc. */}
      <SettingsSection
        delay={4}
        storageKey="business"
        icon={<Store className="w-6 h-6 text-primary-600" />}
        title={<>Business info &amp; AI customizations</>}
      >
        <p className="text-gray-600 text-sm mb-6">
          Your AI receptionist uses this when answering calls and texts. Fill in hours, services, and booking rules
          so it can give accurate info and take bookings for any business type (restaurant, nail salon, HVAC, real
          estate, etc.).
        </p>

        <div className="grid grid-cols-1 md:grid-cols-2 gap-6">
          <div>
            <label className="block text-sm font-medium text-gray-700 mb-1">Business name</label>
            <input
              type="text"
              value={form.name}
              onChange={(e) => setForm((f) => ({ ...f, name: e.target.value }))}
              className="cs-field w-full"
              placeholder="Your Business Name"
            />
            <p className="mt-1 text-xs text-gray-500">
              How this location is filed in your records. Shown in your dashboard.
            </p>
          </div>
          <div>
            <label className="block text-sm font-medium text-gray-700 mb-1">
              Name callers hear <span className="text-gray-400 font-normal">(optional)</span>
            </label>
            <input
              type="text"
              value={form.public_name}
              onChange={(e) => setForm((f) => ({ ...f, public_name: e.target.value }))}
              className="cs-field w-full"
              placeholder={form.name || 'Same as business name'}
            />
            <p className="mt-1 text-xs text-gray-500">
              Set this when customers know you by a different name than the one you
              file under. Leave blank to use the business name.
            </p>
          </div>
          <div>
            <label className="block text-sm font-medium text-gray-700 mb-1">Industry</label>
            {industryLocked && verticalLabel ? (
              <>
                <div className="cs-field w-full bg-gray-50 text-gray-800">{verticalLabel}</div>
                <p className="text-xs text-gray-500 mt-1">Set by your administrator when the account was created.</p>
              </>
            ) : (
              <>
                <input
                  type="text"
                  value={form.business_type}
                  onChange={(e) => setForm((f) => ({ ...f, business_type: e.target.value }))}
                  className="cs-field w-full"
                  placeholder="e.g. nail salon, HVAC company, real estate brokerage, restaurant"
                />
                <p className="text-xs text-gray-500 mt-1">
                  This tells the AI what kind of business you run so it doesn&apos;t assume a generic or demo industry.
                </p>
              </>
            )}
          </div>
          <div className="md:col-span-2">
            <label className="block text-sm font-medium text-gray-700 mb-1">Hours of operation</label>
            <button
              type="button"
              onClick={() => setHoursModalOpen(true)}
              className="group flex w-full items-center justify-between gap-3 rounded-xl border border-gray-200 bg-gradient-to-br from-white via-white to-primary-50/40 px-4 py-3.5 text-left shadow-sm ring-1 ring-black/5 transition hover:border-primary-300 hover:shadow-md hover:ring-primary-200/40"
            >
              <div className="min-w-0 flex-1">
                <p className={`truncate text-sm ${hoursSummaryPreview ? 'font-medium text-gray-900' : 'text-gray-500'}`}>
                  {hoursSummaryPreview ||
                    'Set which days you\u0027re open and your hours (opens the visual editor)'}
                </p>
                <p className="mt-1 text-xs text-gray-500">
                  Schedule presets, copy weekdays, and live preview. Saved when you click Apply.
                </p>
              </div>
              <div className="flex h-11 w-11 shrink-0 items-center justify-center rounded-2xl bg-primary-100 text-primary-700 transition group-hover:scale-105 group-hover:bg-primary-200">
                <Clock className="h-5 w-5" aria-hidden />
              </div>
            </button>
            <BusinessHoursModal
              isOpen={hoursModalOpen}
              onClose={() => setHoursModalOpen(false)}
              hoursText={form.hours}
              // "Apply hours" should apply them — same reason the service, special and
              // rule modals save on Save rather than staging behind the bar below.
              onApply={(next) => {
                setForm((f) => ({ ...f, hours: next }))
                return persistFields({ hours: next }, 'Business hours')
              }}
            />
          </div>
          <div id="store-phone-settings" className="md:col-span-2">
            {(() => {
              const phoneSet = Boolean((form.forwarding_phone || '').trim())
              const takeMessage = form.transfer_takes_message
              const handoffSatisfied = phoneSet || takeMessage
              return (
                <div
                  className={`relative overflow-hidden rounded-2xl border p-5 transition-all duration-300 ${
                    handoffSatisfied
                      ? 'border-emerald-200 bg-emerald-50/40'
                      : 'border-amber-300 bg-amber-50/50 ring-1 ring-amber-200'
                  }`}
                >
                  <div
                    aria-hidden
                    className={`pointer-events-none absolute -right-12 -top-12 h-32 w-32 rounded-full blur-2xl transition-colors duration-500 ${
                      handoffSatisfied ? 'bg-emerald-300/20' : 'bg-amber-300/30'
                    }`}
                  />
                  <div className="relative">
                    {/* Header: title + required marker + live status pill */}
                    <div className="flex items-start justify-between gap-3">
                      <div className="flex items-center gap-3">
                        <div
                          className={`flex h-10 w-10 shrink-0 items-center justify-center rounded-xl transition-colors duration-300 ${
                            handoffSatisfied ? 'bg-emerald-100 text-emerald-600' : 'bg-amber-100 text-amber-600'
                          }`}
                        >
                          <PhoneForwarded className="h-5 w-5" aria-hidden />
                        </div>
                        <div>
                          <h3 className="flex items-center gap-1.5 text-sm font-semibold text-gray-900">
                            How callers reach a real person
                            <span className="text-rose-500" aria-label="required">
                              *
                            </span>
                          </h3>
                          <p className="text-xs text-gray-500">
                            Required — pick one so a caller is never stuck with the AI.
                          </p>
                        </div>
                      </div>
                      <span
                        className={`inline-flex shrink-0 items-center gap-1.5 rounded-full px-2.5 py-1 text-xs font-semibold transition-colors duration-300 ${
                          handoffSatisfied ? 'bg-emerald-100 text-emerald-700' : 'bg-amber-100 text-amber-700'
                        }`}
                      >
                        {handoffSatisfied ? (
                          <>
                            <CheckCircle2 className="h-3.5 w-3.5" aria-hidden />
                            Ready
                          </>
                        ) : (
                          <>
                            <span className="relative flex h-2 w-2" aria-hidden>
                              <span className="absolute inline-flex h-full w-full animate-ping rounded-full bg-amber-400 opacity-75" />
                              <span className="relative inline-flex h-2 w-2 rounded-full bg-amber-500" />
                            </span>
                            Action needed
                          </>
                        )}
                      </span>
                    </div>

                    {/* Option A — transfer to a number */}
                    <div
                      className={`mt-4 rounded-xl border bg-white p-3 transition-all duration-200 ${
                        takeMessage
                          ? 'opacity-50'
                          : phoneSet
                            ? 'border-emerald-300 ring-1 ring-emerald-100'
                            : 'border-gray-200'
                      }`}
                    >
                      <label htmlFor="forwarding-phone-input" className="mb-1 block text-sm font-medium text-gray-700">
                        Transfer to a number
                      </label>
                      <input
                        id="forwarding-phone-input"
                        type="text"
                        value={form.forwarding_phone}
                        onChange={(e) => setForm((f) => ({ ...f, forwarding_phone: e.target.value }))}
                        disabled={takeMessage}
                        className={`cs-field w-full ${takeMessage ? 'cursor-not-allowed' : ''}`}
                        placeholder="e.g. (555) 123-4567"
                      />
                      <p className="mt-1 text-xs text-gray-500">
                        When a caller asks for a person, we ring this number (not the AI line). If they name someone on your
                        transfer list, we use that number instead — see Call transfers below.
                      </p>
                    </div>

                    {/* OR divider */}
                    <div className="my-3 flex items-center gap-3">
                      <span className="h-px flex-1 bg-gray-200" />
                      <span className="text-[11px] font-semibold uppercase tracking-wider text-gray-400">or</span>
                      <span className="h-px flex-1 bg-gray-200" />
                    </div>

                    {/* Option B — take a message instead */}
                    <button
                      type="button"
                      onClick={() => setForm((f) => ({ ...f, transfer_takes_message: !f.transfer_takes_message }))}
                      aria-pressed={takeMessage}
                      className={`flex w-full items-start gap-3 rounded-xl border p-3 text-left transition-all duration-200 ${
                        takeMessage
                          ? 'border-emerald-300 bg-emerald-50/60 ring-1 ring-emerald-100'
                          : 'border-gray-200 bg-white hover:border-gray-300 hover:bg-gray-50'
                      }`}
                    >
                      <span
                        aria-hidden
                        className={`relative mt-0.5 inline-flex h-6 w-11 shrink-0 items-center rounded-full transition-colors duration-200 ${
                          takeMessage ? 'bg-emerald-500' : 'bg-gray-300'
                        }`}
                      >
                        <span
                          className={`inline-block h-4 w-4 transform rounded-full bg-white shadow transition-transform duration-200 ${
                            takeMessage ? 'translate-x-6' : 'translate-x-1'
                          }`}
                        />
                      </span>
                      <span>
                        <span className="flex items-center gap-1.5 text-sm font-medium text-gray-800">
                          <MessageSquare className="h-4 w-4 text-gray-400" aria-hidden />
                          Take a message instead
                        </span>
                        <span className="mt-0.5 block text-xs text-gray-500">
                          No separate line to send callers to? (For example, your published number forwards to the AI.) The
                          AI takes a message so you can call them back.
                        </span>
                      </span>
                    </button>
                  </div>
                </div>
              )
            })()}
          </div>
          <div className="md:col-span-2">
            <label className="block text-sm font-medium text-gray-700 mb-1">Email</label>
            <input
              type="email"
              value={form.email}
              onChange={(e) => setForm((f) => ({ ...f, email: e.target.value }))}
              className="cs-field w-full"
              placeholder="info@yourbusiness.com"
            />
          </div>
          <div className="md:col-span-2">
            <label className="block text-sm font-medium text-gray-700 mb-1">Address</label>
            <input
              type="text"
              value={form.address}
              onChange={(e) => setForm((f) => ({ ...f, address: e.target.value }))}
              className="cs-field w-full"
              placeholder="123 Main St, City, State"
            />
          </div>
          <div className="md:col-span-2">
            <label className="block text-sm font-medium text-gray-700 mb-1">Website or menu link (optional)</label>
            <input
              type="text"
              value={form.menu_link}
              onChange={(e) => setForm((f) => ({ ...f, menu_link: e.target.value }))}
              className="cs-field w-full"
              placeholder="https://... (menu, services, or main site)"
            />
          </div>
          <div className="md:col-span-2">
            <label className="block text-sm font-medium text-gray-700 mb-1">Custom greeting (optional)</label>
            <input
              type="text"
              value={form.greeting}
              onChange={(e) => setForm((f) => ({ ...f, greeting: e.target.value }))}
              className="cs-field w-full"
              placeholder="Thank you for calling {business_name}. How can I help?"
            />
            <p className="text-xs text-gray-500 mt-1">
              Use {'{business_name}'} for your business name and {'{receptionist_name}'} for the AI name above. If you
              leave the name out of this text, we prepend &quot;Hi, I&apos;m [name].&quot; on the phone greeting automatically.
              When call recording is enabled for your plan, the recording notice is always spoken after this greeting.
            </p>
            <motion.div className="mt-2 flex flex-wrap items-center gap-2">
              <button
                type="button"
                onClick={loadGreetingPreview}
                disabled={greetingPreviewLoading}
                className="text-sm font-medium text-teal-700 hover:text-teal-900 disabled:opacity-50"
              >
                {greetingPreviewLoading ? 'Loading preview…' : 'Preview phone greeting'}
              </button>
              <span className="text-xs text-gray-500">Shows saved settings (save first if you just edited).</span>
            </motion.div>
            {greetingPreview && (
              <motion.div
                initial={{ opacity: 0, y: 4 }}
                animate={{ opacity: 1, y: 0 }}
                className="mt-3 rounded-lg border border-teal-200 bg-teal-50/80 p-3 text-sm text-gray-800"
              >
                <p className="font-medium text-gray-900 mb-1">What callers will hear</p>
                <p className="whitespace-pre-wrap">{greetingPreview.spoken_text}</p>
                {greetingPreview.recording_disclosure && (
                  <p className="text-xs text-gray-600 mt-2">
                    Recording line (always last): {greetingPreview.recording_disclosure}
                  </p>
                )}
                <p className="text-xs text-gray-500 mt-2">
                  Config: {greetingPreview.config_source}
                  {greetingPreview.placeholders?.business_name != null && (
                    <> · Business name: &quot;{greetingPreview.placeholders.business_name}&quot;</>
                  )}
                  {greetingPreview.placeholders?.receptionist_name != null && (
                    <> · AI name: &quot;{greetingPreview.placeholders.receptionist_name || '(empty)'}&quot;</>
                  )}
                </p>
                {(greetingPreview.warnings?.length ?? 0) > 0 && (
                  <ul className="mt-2 text-xs text-amber-800 list-disc pl-4">
                    {greetingPreview.warnings!.map((w) => (
                      <li key={w}>{w}</li>
                    ))}
                  </ul>
                )}
              </motion.div>
            )}
          </div>
          <ServicesEditor
            items={serviceItems}
            onChange={setServiceItems}
            onPersist={(next) => persistFields({ services: next }, 'Services')}
            required
            importSlot={
              <ImportServicesButton
                api={api}
                existing={serviceItems}
                // "Add 51 services" should add them, not stage them behind another
                // button — same reason the modal's Save now saves.
                onImport={(next) => {
                  setServiceItems(next)
                  void persistFields({ services: next }, 'Services')
                }}
              />
            }
          />
          {/* Sits under Services, because it is a fact about the menu above rather
              than a call-handling preference. Off means the prices never reach the
              AI at all — see prompts/receptionist.py; it cannot say what it was
              never given. */}
          <label className="mt-3 flex cursor-pointer items-start gap-3 rounded-xl border border-gray-200 bg-white p-3 transition-colors hover:border-gray-300">
            <input
              type="checkbox"
              className="mt-1 h-4 w-4 shrink-0 rounded border-gray-300 text-blue-600 focus:ring-blue-500"
              checked={!form.quote_prices}
              onChange={(e) => {
                const next = !e.target.checked
                setForm((f) => ({ ...f, quote_prices: next }))
                void persistFields({ quote_prices: next }, 'Pricing')
              }}
            />
            <span className="text-sm">
              <span className="font-medium text-gray-900">
                Don&rsquo;t give prices over the phone
              </span>
              <span className="mt-0.5 block text-gray-500">
                The receptionist never states or estimates a cost. If a caller asks, it
                says pricing depends on the stylist and is confirmed in person, then
                offers to book them in. Your prices stay in the menu above for your own
                reference &mdash; they are simply never sent to the AI.
              </span>
            </span>
          </label>

          <SpecialsEditor
            items={specialItems}
            onChange={setSpecialItems}
            onPersist={(next) => persistFields({ specials: next }, 'Specials')}
          />
          <RulesEditor
            items={ruleItems}
            onChange={setRuleItems}
            onPersist={(next) => persistFields({ reservation_rules: next }, 'Booking rules')}
          />
        </div>

      </SettingsSection>

      <SettingsSection
        delay={5}
        id="team-roster-settings"
        aria-labelledby="team-roster-settings-heading"
        storageKey="team"
        titleId="team-roster-settings-heading"
        icon={<Users className="w-6 h-6 text-teal-600" />}
        title={
          <>
            Team roster
            <span className="text-rose-500 text-base" aria-label="required">*</span>
          </>
        }
      >
        <p className="text-gray-600 text-sm mb-4 max-w-3xl">
          Staff your callers can book with (stylists, artists, providers, chairs). Add as many as you need. This list is only for
          scheduling and AI context, not live call transfers.
        </p>
        {staff.some((s) => s.name.trim()) && (
          <button
            type="button"
            onClick={() => setTimeOffOpen(true)}
            className="mb-4 inline-flex items-center gap-1.5 rounded-xl border border-gray-200 px-4 py-2 text-sm font-medium text-gray-700 hover:bg-gray-50"
          >
            <Clock className="h-4 w-4 text-teal-600" />
            Time off
          </button>
        )}
        <div className="mb-8">
          <BookingModeSection />
        </div>
        <StaffMembersSection
          staff={staff}
          availableServices={serviceItems}
          shopHours={shopHours}
          onStaffChange={setStaff}
          api={api}
          onNotify={setMessage}
          onAfterSave={refreshSetupStatus}
        />
        <TimeOffModal
          open={timeOffOpen}
          onClose={() => setTimeOffOpen(false)}
          staff={staff}
          closures={closures}
          api={api}
          onSaved={(nextStaff, nextClosures) => {
            setStaff(nextStaff)
            setClosures(nextClosures)
            refreshSetupStatus()
          }}
          onNotify={setMessage}
        />
      </SettingsSection>

      <SettingsSection
        delay={6}
        aria-labelledby="call-transfers-settings-heading"
        storageKey="transfers"
        defaultOpen={false}
        titleId="call-transfers-settings-heading"
        icon={<PhoneForwarded className="w-6 h-6 text-violet-600" />}
        title="Call transfers"
      >
        <p className="text-gray-600 text-sm mb-6 max-w-3xl">
          When a caller asks to speak with someone by name, the AI can transfer only to numbers you list here. Your plan limits
          how many destinations you can add, not how many people are on your booking roster above.
        </p>
        <TransferTargetsSection
          transfers={transferTargets}
          staff={staff}
          transferMax={transferMax}
          onTransfersChange={setTransferTargets}
          api={api}
          onNotify={setMessage}
          onAfterSave={refreshSetupStatus}
        />
      </SettingsSection>

      <motion.div
        ref={saveBarRef}
        initial={reduceMotion ? false : { y: 28, opacity: 0 }}
        animate={{ y: 0, opacity: 1 }}
        transition={{ type: 'spring', stiffness: 380, damping: 32, delay: 0.15 }}
        className="fixed bottom-0 left-0 right-0 z-50 border-t border-white/10 bg-gradient-to-t from-zinc-950 via-zinc-950/95 to-zinc-950/85 px-4 pt-3 pb-[max(1rem,env(safe-area-inset-bottom))] backdrop-blur-xl"
      >
        <motion.div
          aria-hidden
          className="pointer-events-none absolute inset-x-0 top-0 h-px bg-gradient-to-r from-transparent via-cyan-400/70 to-transparent"
          animate={reduceMotion ? undefined : { opacity: [0.35, 1, 0.35] }}
          transition={{ duration: 2.8, repeat: Infinity, ease: 'easeInOut' }}
        />
        <motion.div className="mx-auto flex w-full max-w-4xl flex-col gap-3">
          <AnimatePresence mode="wait">
            {message && (
              <motion.div
                key={`${message.type}-${message.text}`}
                role="alert"
                initial={reduceMotion ? false : { opacity: 0, y: 12, scale: 0.98 }}
                animate={{ opacity: 1, y: 0, scale: 1 }}
                exit={reduceMotion ? undefined : { opacity: 0, y: 8, scale: 0.98 }}
                transition={{ type: 'spring', stiffness: 500, damping: 28 }}
                className={`flex items-start gap-2.5 rounded-xl border px-4 py-3 text-sm shadow-lg ${
                  message.type === 'success'
                    ? 'border-emerald-500/35 bg-emerald-500/15 text-emerald-50'
                    : 'border-red-500/35 bg-red-500/15 text-red-50'
                }`}
              >
                {message.type === 'success' ? (
                  <CheckCircle2 className="mt-0.5 h-5 w-5 shrink-0 text-emerald-400" />
                ) : (
                  <AlertTriangle className="mt-0.5 h-5 w-5 shrink-0 text-red-400" />
                )}
                <span>{message.text}</span>
              </motion.div>
            )}
          </AnimatePresence>
          <motion.button
            type="button"
            onClick={handleSave}
            disabled={saving}
            whileHover={reduceMotion ? undefined : { scale: 1.01 }}
            whileTap={reduceMotion ? undefined : { scale: 0.98 }}
            className="relative flex w-full items-center justify-center gap-2 overflow-hidden rounded-xl bg-gradient-to-r from-cyan-600 via-primary-600 to-indigo-600 px-6 py-4 text-base font-semibold text-white shadow-lg shadow-cyan-900/35 disabled:opacity-55"
          >
            {!reduceMotion && !saving && (
              <motion.span
                aria-hidden
                className="absolute inset-0 bg-gradient-to-r from-white/0 via-white/20 to-white/0"
                animate={{ x: ['-120%', '120%'] }}
                transition={{ duration: 2.6, repeat: Infinity, ease: 'easeInOut', repeatDelay: 0.8 }}
              />
            )}
            <Save className="relative h-5 w-5" />
            <span className="relative">{saving ? 'Saving...' : 'Save changes'}</span>
          </motion.button>
        </motion.div>
      </motion.div>
    </div>
  )
}
