'use client'

/**
 * Bulk onboarding panel — drives the background provisioning pipeline
 * (POST /api/admin/provisioning/jobs, poll status, resume failed).
 *
 * Motion is purposeful and reduced-motion-aware: a spring progress bar, layout
 * animations on the live task list, and a count-up on the tallies. Polling backs
 * off and stops at a terminal job state, and is cleaned up on unmount.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import {
  AnimatePresence,
  motion,
  useReducedMotion,
  useMotionValue,
  useSpring,
  useTransform,
} from 'framer-motion'
import { useApiClient, sameOriginApiConfig } from '@/lib/api'

type TaskStatus = 'pending' | 'running' | 'done' | 'failed'

interface ProvisioningTask {
  id: number
  client_id: string
  name?: string | null
  email?: string | null
  status: TaskStatus
  steps_done: string[]
  phone_e164?: string | null
  error?: string | null
  attempts?: number
}

interface ProvisioningJob {
  id: string
  status: 'pending' | 'running' | 'done' | 'failed'
  total: number
  counts: Partial<Record<TaskStatus, number>>
  tasks: ProvisioningTask[]
}

interface ParsedRow {
  client_id: string
  name?: string
  email?: string
  area_code?: string
}

const STEP_LABELS: Record<string, string> = {
  tenant_created: 'Tenant',
  number_purchased: 'Number',
  config_seeded: 'Config',
  clerk_invited: 'Invite',
}
const ALL_STEPS = ['tenant_created', 'number_purchased', 'config_seeded', 'clerk_invited']
const TERMINAL = new Set(['done', 'failed'])

/** Parse "client-id, Name, email, area_code" lines. Only client-id is required. */
function parseRows(text: string): { rows: ParsedRow[]; errors: string[] } {
  const rows: ParsedRow[] = []
  const errors: string[] = []
  const seen = new Set<string>()
  text
    .split('\n')
    .map((l) => l.trim())
    .filter(Boolean)
    .forEach((line, i) => {
      const [rawId, name, email, area] = line.split(',').map((s) => (s || '').trim())
      const client_id = (rawId || '').replace(/\s+/g, '-').toLowerCase()
      if (!client_id) {
        errors.push(`Line ${i + 1}: missing client id`)
        return
      }
      if (seen.has(client_id)) {
        errors.push(`Line ${i + 1}: duplicate client id "${client_id}"`)
        return
      }
      if (email && !/^[^@\s]+@[^@\s]+\.[^@\s]+$/.test(email)) {
        errors.push(`Line ${i + 1}: "${email}" is not a valid email`)
        return
      }
      seen.add(client_id)
      rows.push({
        client_id,
        name: name || undefined,
        email: email || undefined,
        area_code: area ? area.replace(/\D/g, '').slice(0, 3) || undefined : undefined,
      })
    })
  return { rows, errors }
}

const STATUS_STYLE: Record<TaskStatus, string> = {
  pending: 'border-zinc-500/30 bg-zinc-500/10 text-zinc-300',
  running: 'border-primary-400/40 bg-primary-400/10 text-primary-200',
  done: 'border-emerald-500/30 bg-emerald-500/10 text-emerald-200',
  failed: 'border-red-500/40 bg-red-500/10 text-red-200',
}

function CountUp({ value, reduce }: { value: number; reduce: boolean }) {
  const mv = useMotionValue(0)
  const spring = useSpring(mv, { stiffness: 90, damping: 18 })
  const rounded = useTransform(spring, (v) => Math.round(v).toString())
  useEffect(() => {
    if (reduce) mv.set(value)
    else mv.set(value)
  }, [value, mv, reduce])
  if (reduce) return <span>{value}</span>
  return <motion.span>{rounded}</motion.span>
}

export function ProvisioningPanel() {
  const reduce = useReducedMotion()
  const api = useApiClient()
  const adminApi = useMemo(() => sameOriginApiConfig(), [])

  const [text, setText] = useState('')
  const [defaultAreaCode, setDefaultAreaCode] = useState('')
  const [job, setJob] = useState<ProvisioningJob | null>(null)
  const [submitting, setSubmitting] = useState(false)
  const [error, setError] = useState<string | null>(null)
  const pollRef = useRef<ReturnType<typeof setTimeout> | null>(null)
  // Consecutive failures tolerated before giving up rather than polling forever.
  const MAX_POLL_ATTEMPTS = 5

  const parsed = useMemo(() => parseRows(text), [text])
  const isRunning = job?.status === 'running' || job?.status === 'pending'

  const stopPolling = useCallback(() => {
    if (pollRef.current) {
      clearTimeout(pollRef.current)
      pollRef.current = null
    }
  }, [])

  const poll = useCallback(
    async (jobId: string, delay = 1500, attempt = 0) => {
      try {
        const { data } = await api.get<ProvisioningJob>(
          `/api/admin/provisioning/jobs/${jobId}`,
          adminApi,
        )
        setJob(data)
        if (!TERMINAL.has(data.status)) {
          pollRef.current = setTimeout(
            () => void poll(jobId, Math.min(delay * 1.25, 5000)),
            delay,
          )
        }
      } catch {
        // "retry once" is what the old comment claimed; there was no counter, so this
        // retried forever. A job endpoint that keeps failing (signed out, backend
        // down) then polls for as long as the tab is open, and each attempt is a
        // billed serverless invocation. Bound it and surface the give-up.
        if (attempt >= MAX_POLL_ATTEMPTS) {
          setError('Lost contact with the provisioning job. Reload to check its status.')
          return
        }
        pollRef.current = setTimeout(
          () => void poll(jobId, Math.min(delay * 2, 15000), attempt + 1),
          delay,
        )
      }
    },
    [api, adminApi],
  )

  useEffect(() => stopPolling, [stopPolling])

  const submit = useCallback(async () => {
    setError(null)
    if (parsed.errors.length) {
      setError(parsed.errors[0])
      return
    }
    if (!parsed.rows.length) {
      setError('Add at least one store (one per line).')
      return
    }
    setSubmitting(true)
    stopPolling()
    try {
      const { data } = await api.post<{ job_id: string; total: number }>(
        '/api/admin/provisioning/jobs',
        {
          tenants: parsed.rows,
          default_area_code: defaultAreaCode.replace(/\D/g, '').slice(0, 3) || undefined,
        },
        adminApi,
      )
      setJob({ id: data.job_id, status: 'running', total: data.total, counts: {}, tasks: [] })
      void poll(data.job_id)
    } catch (e: unknown) {
      const detail =
        (e as { response?: { data?: { detail?: string } } })?.response?.data?.detail
      setError(detail || 'Failed to start provisioning job.')
    } finally {
      setSubmitting(false)
    }
  }, [api, adminApi, parsed, defaultAreaCode, poll, stopPolling])

  const resume = useCallback(async () => {
    if (!job) return
    setError(null)
    stopPolling()
    try {
      await api.post(`/api/admin/provisioning/jobs/${job.id}/resume`, {}, adminApi)
      setJob((j) => (j ? { ...j, status: 'running' } : j))
      void poll(job.id)
    } catch {
      setError('Failed to resume job.')
    }
  }, [api, adminApi, job, poll, stopPolling])

  const done = job?.counts.done ?? 0
  const failed = job?.counts.failed ?? 0
  const total = job?.total ?? parsed.rows.length
  const pct = total ? Math.round(((done + failed) / total) * 100) : 0

  return (
    <section className="mb-8 rounded-2xl border border-white/10 bg-zinc-900/70 p-6 shadow-xl backdrop-blur-md md:p-8">
      <div className="mb-1 flex items-center gap-2">
        <h2 className="font-display text-lg font-semibold text-white">Bulk onboarding</h2>
        <span className="rounded-full border border-primary-400/30 bg-primary-400/10 px-2 py-0.5 text-[10px] font-medium uppercase tracking-wider text-primary-200">
          auto-provision
        </span>
      </div>
      <p className="mb-4 text-sm text-zinc-400">
        One store per line: <code className="text-zinc-300">client-id, Business Name, owner@email.com, area-code</code>.
        Only the client-id is required. Each store gets a Twilio number purchased and webhooks wired automatically.
      </p>

      <textarea
        value={text}
        onChange={(e) => setText(e.target.value)}
        rows={6}
        spellCheck={false}
        placeholder={'acme-salon, Acme Salon, owner@acme.com, 415\nbeta-barbers, Beta Barbers, ops@beta.com'}
        className="w-full resize-y rounded-xl border border-white/10 bg-black/30 px-4 py-3 font-mono text-sm text-zinc-100 outline-none transition-colors focus:border-primary-400/60 focus:ring-1 focus:ring-primary-400/40"
      />

      <div className="mt-3 flex flex-wrap items-center gap-3">
        <input
          type="text"
          value={defaultAreaCode}
          onChange={(e) => setDefaultAreaCode(e.target.value.replace(/\D/g, '').slice(0, 3))}
          placeholder="Default area code"
          className="w-40 rounded-lg border border-white/10 bg-black/30 px-3 py-2 text-sm text-zinc-100 outline-none focus:border-primary-400/60"
        />
        <motion.button
          type="button"
          onClick={() => void submit()}
          disabled={submitting || isRunning || !parsed.rows.length}
          whileTap={reduce ? undefined : { scale: 0.97 }}
          className="rounded-lg bg-primary-500 px-4 py-2 text-sm font-semibold text-white shadow-lg shadow-primary-500/20 transition-colors hover:bg-primary-400 disabled:cursor-not-allowed disabled:opacity-50"
        >
          {submitting ? 'Starting…' : isRunning ? 'Provisioning…' : `Provision ${parsed.rows.length || ''} store${parsed.rows.length === 1 ? '' : 's'}`}
        </motion.button>
        <AnimatePresence>
          {parsed.errors.length > 0 && (
            <motion.span
              initial={reduce ? false : { opacity: 0, x: -6 }}
              animate={{ opacity: 1, x: 0 }}
              exit={{ opacity: 0 }}
              className="text-xs text-amber-400"
            >
              {parsed.errors.length} input issue{parsed.errors.length === 1 ? '' : 's'} — {parsed.errors[0]}
            </motion.span>
          )}
        </AnimatePresence>
      </div>

      <AnimatePresence>
        {error && (
          <motion.div
            initial={reduce ? false : { opacity: 0, height: 0 }}
            animate={{ opacity: 1, height: 'auto' }}
            exit={{ opacity: 0, height: 0 }}
            className="mt-4 overflow-hidden rounded-xl border border-red-500/30 bg-red-500/10 px-4 py-3 text-sm text-red-200"
          >
            {error}
          </motion.div>
        )}
      </AnimatePresence>

      {/* Live job progress */}
      <AnimatePresence>
        {job && (
          <motion.div
            initial={reduce ? false : { opacity: 0, y: 12 }}
            animate={{ opacity: 1, y: 0 }}
            exit={{ opacity: 0 }}
            className="mt-6"
          >
            <div className="mb-2 flex items-center justify-between text-sm">
              <div className="flex items-center gap-4 text-zinc-300">
                <span>
                  <CountUp value={done} reduce={!!reduce} />
                  <span className="text-zinc-500"> done</span>
                </span>
                {failed > 0 && (
                  <span className="text-red-300">
                    <CountUp value={failed} reduce={!!reduce} /> failed
                  </span>
                )}
                <span className="text-zinc-500">of {total}</span>
              </div>
              <div className="flex items-center gap-3">
                {isRunning && (
                  <span className="flex items-center gap-1.5 text-xs text-primary-300">
                    <motion.span
                      className="h-1.5 w-1.5 rounded-full bg-primary-400"
                      animate={reduce ? undefined : { opacity: [1, 0.3, 1] }}
                      transition={{ duration: 1.1, repeat: Infinity }}
                    />
                    live
                  </span>
                )}
                {failed > 0 && !isRunning && (
                  <button
                    type="button"
                    onClick={() => void resume()}
                    className="rounded-lg border border-amber-500/40 bg-amber-500/10 px-3 py-1 text-xs font-medium text-amber-200 hover:bg-amber-500/20"
                  >
                    Retry {failed} failed
                  </button>
                )}
              </div>
            </div>

            {/* spring progress bar */}
            <div className="h-2 w-full overflow-hidden rounded-full bg-white/5">
              <motion.div
                className={`h-full rounded-full ${failed ? 'bg-gradient-to-r from-primary-500 to-amber-400' : 'bg-gradient-to-r from-primary-500 to-emerald-400'}`}
                initial={false}
                animate={{ width: `${pct}%` }}
                transition={reduce ? { duration: 0 } : { type: 'spring', stiffness: 120, damping: 24 }}
              />
            </div>

            {/* per-tenant rows */}
            <motion.ul layout className="mt-4 grid gap-2">
              <AnimatePresence initial={false}>
                {job.tasks.map((t) => (
                  <motion.li
                    key={t.id ?? t.client_id}
                    layout
                    initial={reduce ? false : { opacity: 0, y: 8 }}
                    animate={{ opacity: 1, y: 0 }}
                    exit={{ opacity: 0 }}
                    transition={{ duration: 0.25, ease: [0.22, 1, 0.36, 1] }}
                    className="flex items-center justify-between gap-3 rounded-xl border border-white/5 bg-black/20 px-4 py-2.5"
                  >
                    <div className="min-w-0">
                      <p className="truncate text-sm font-medium text-zinc-100">
                        {t.name || t.client_id}
                        {t.phone_e164 && (
                          <span className="ml-2 font-mono text-xs text-emerald-300/90">{t.phone_e164}</span>
                        )}
                      </p>
                      <div className="mt-1 flex flex-wrap gap-1">
                        {ALL_STEPS.map((s) => {
                          const ok = t.steps_done?.includes(s)
                          return (
                            <span
                              key={s}
                              className={`rounded px-1.5 py-0.5 text-[10px] font-medium transition-colors ${ok ? 'bg-emerald-500/15 text-emerald-300' : 'bg-white/5 text-zinc-500'}`}
                            >
                              {STEP_LABELS[s]}
                            </span>
                          )
                        })}
                        {t.error && (
                          <span className="truncate text-[10px] text-red-300/80" title={t.error}>
                            · {t.error}
                          </span>
                        )}
                      </div>
                    </div>
                    <span
                      className={`shrink-0 rounded-full border px-2.5 py-0.5 text-xs font-medium capitalize ${STATUS_STYLE[t.status] ?? STATUS_STYLE.pending}`}
                    >
                      {t.status}
                    </span>
                  </motion.li>
                ))}
              </AnimatePresence>
            </motion.ul>
          </motion.div>
        )}
      </AnimatePresence>
    </section>
  )
}
