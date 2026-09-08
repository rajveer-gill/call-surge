'use client'

import { useState, useEffect, Fragment } from 'react'
import { Calendar, MessageSquare, Phone, TrendingUp, BarChart3, Check, Send, Loader2, RefreshCw, Lock, Search, X, ChevronRight } from 'lucide-react'
import { useApiClient } from '@/lib/api'
import { RevealStagger, RevealItem, AnimatedNumber } from '@/components/motion'
import { Skeleton } from '@/components/ui/Skeleton'
import { EmptyState } from '@/components/ui/EmptyState'
import { LockedFeature } from '@/components/ui/LockedFeature'
import { formatTimeHhmmToAmPm } from '@/lib/formatTime'
import { STATUS_CLASSES, STATUS_LABELS } from '@/components/appointments/appointmentStatus'
import { useNewRequestChime } from '@/lib/useNewRequestChime'

/** Call log outcome pills — dark text on tinted fills for white cards. */
function callOutcomeClass(outcome: string): string {
  if (outcome === 'answered_by_ai') return 'bg-emerald-200 text-emerald-950'
  if (outcome === 'forwarded') return 'bg-blue-200 text-blue-950'
  if (outcome === 'missed' || outcome === 'no_answer') return 'bg-red-200 text-red-950'
  return 'bg-gray-200 text-gray-900'
}

interface Appointment {
  id: number
  name: string
  email: string
  phone: string
  date: string
  time: string
  reason: string
  status: string
  created_at: string
}

interface Message {
  id: number
  caller_name: string
  caller_phone: string
  message: string
  urgency: string
  status: string
  created_at: string
}

interface Stats {
  total_appointments: number
  total_messages: number
  pending_appointments: number
}

interface SmsThread {
  phone: string
  message_count: number
  last_message: string
  last_role: string
  updated_at: string
  appointment_id: number | null
}

interface ThreadMessage {
  role: string
  content: string
}

interface AnalyticsSummary {
  total_calls: number
  by_outcome: Record<string, number>
  by_hour: Record<string, number>
  by_day_of_week: Record<string, number>
  client_id: string | null
  /** ISO date (Monday) — counts below are for this week only (UTC). */
  by_day_of_week_period_start?: string
  by_day_of_week_period_end?: string
  by_day_of_week_timezone?: string
}

interface AnalyticsHealth {
  period_days: number
  calls_total: number
  forward_rate: number
  error_rate: number
  missed_rate: number
  booking_completion_rate: number
  avg_duration_sec: number
  by_outcome: Record<string, number>
}

interface CallLogEntry {
  call_sid: string
  from_number: string
  to_number: string
  start_iso: string
  end_iso?: string
  outcome: string
  duration_sec?: number
  category?: string
  recording_sid?: string | null
  recording_url?: string | null
  recording_duration_sec?: number | null
  recording_status?: string | null
  call_summary?: string | null
  created_at?: string | null
}

const DAY_NAMES = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat']

function relativeAgo(ts: number | null): string {
  if (!ts) return ''
  const secs = Math.max(0, Math.round((Date.now() - ts) / 1000))
  if (secs < 5) return 'just now'
  if (secs < 60) return `${secs}s ago`
  const mins = Math.round(secs / 60)
  return `${mins}m ago`
}

/** Format a stored digits-only phone (e.g. "19259978995") as +1 (925) 997-8995. */
function formatPhone(raw: string): string {
  const d = (raw || '').replace(/\D/g, '')
  if (d.length === 11 && d.startsWith('1')) {
    return `+1 (${d.slice(1, 4)}) ${d.slice(4, 7)}-${d.slice(7)}`
  }
  if (d.length === 10) {
    return `(${d.slice(0, 3)}) ${d.slice(3, 6)}-${d.slice(6)}`
  }
  return raw || '—'
}

function relativeOrDate(iso: string): string {
  if (!iso) return ''
  const t = new Date(iso).getTime()
  if (Number.isNaN(t)) return ''
  const secs = Math.max(0, Math.round((Date.now() - t) / 1000))
  if (secs < 60) return 'just now'
  if (secs < 3600) return `${Math.round(secs / 60)}m ago`
  if (secs < 86400) return `${Math.round(secs / 3600)}h ago`
  return new Date(iso).toLocaleDateString()
}

export default function Dashboard() {
  const api = useApiClient()
  const [stats, setStats] = useState<Stats>({
    total_appointments: 0,
    total_messages: 0,
    pending_appointments: 0
  })
  const [appointments, setAppointments] = useState<Appointment[]>([])
  const [messages, setMessages] = useState<Message[]>([])
  const [smsThreads, setSmsThreads] = useState<SmsThread[]>([])
  const [threadSearch, setThreadSearch] = useState('')
  const [openThreadPhone, setOpenThreadPhone] = useState<string | null>(null)
  const [threadMessages, setThreadMessages] = useState<ThreadMessage[] | null>(null)
  const [threadLoading, setThreadLoading] = useState(false)
  const [analyticsSummary, setAnalyticsSummary] = useState<AnalyticsSummary | null>(null)
  const [callHealth, setCallHealth] = useState<AnalyticsHealth | null>(null)
  const [recentCalls, setRecentCalls] = useState<CallLogEntry[]>([])
  const [loading, setLoading] = useState(true)
  const [noTenant, setNoTenant] = useState(false)
  const [hasExport, setHasExport] = useState(false)
  const [hasMessages, setHasMessages] = useState(true)
  const [exporting, setExporting] = useState(false)
  const [usage, setUsage] = useState<{ voice_minutes: number; sms_count: number; month: string } | null>(null)
  const [minutesCap, setMinutesCap] = useState<number | null>(null)
  const [smsCap, setSmsCap] = useState<number | null>(null)
  const [busyMsgId, setBusyMsgId] = useState<number | null>(null)
  const [replyingId, setReplyingId] = useState<number | null>(null)
  const [replyText, setReplyText] = useState('')
  const [msgActionNote, setMsgActionNote] = useState<{ id: number; text: string; ok: boolean } | null>(null)
  const [lastUpdated, setLastUpdated] = useState<number | null>(null)
  const [refreshing, setRefreshing] = useState(false)
  const [, setTick] = useState(0)

  useEffect(() => {
    fetchData()
    const interval = setInterval(fetchData, 30000) // Refresh every 30 seconds
    return () => clearInterval(interval)
  }, [api])

  // Re-render the "updated Xs ago" label without refetching.
  useEffect(() => {
    const t = setInterval(() => setTick((n) => n + 1), 15000)
    return () => clearInterval(t)
  }, [])

  // The overview is what most shops leave open all day, so it chimes too. Same stored
  // preference as the Appointments page; the toggle lives there.
  useNewRequestChime(appointments, !loading)

  const manualRefresh = async () => {
    setRefreshing(true)
    try {
      await fetchData()
    } finally {
      setRefreshing(false)
    }
  }

  const fetchData = async () => {
    try {
      const [statsRes, appointmentsRes, messagesRes, summaryRes, callsRes, subRes, healthRes, threadsRes] = await Promise.all([
        api.get('/api/stats'),
        api.get('/api/appointments'),
        api.get('/api/messages'),
        api.get('/api/analytics/summary').catch(() => ({ data: null })),
        api.get('/api/analytics/calls?limit=20').catch(() => ({ data: { calls: [] } })),
        api.get('/api/subscription').catch(() => ({ data: null })),
        api.get('/api/analytics/health').catch(() => ({ data: null })),
        api.get('/api/sms/threads').catch(() => ({ data: { threads: [] } })),
      ])

      const sub = subRes?.data as { limits?: { has_export?: boolean; has_messages?: boolean; minutes_cap?: number; sms_cap?: number }; usage?: { voice_minutes?: number; sms_count?: number; month?: string } } | null
      const limits = sub?.limits
      setHasExport(!!limits?.has_export)
      setHasMessages(limits?.has_messages !== false)
      setMinutesCap(limits?.minutes_cap ?? null)
      setSmsCap(limits?.sms_cap ?? null)
      setUsage(
        sub?.usage
          ? {
              voice_minutes: sub.usage.voice_minutes ?? 0,
              sms_count: sub.usage.sms_count ?? 0,
              month: sub.usage.month ?? '',
            }
          : null,
      )
      setStats(statsRes.data)
      setAppointments(appointmentsRes.data.appointments || [])
      setMessages(messagesRes.data.messages || [])
      setAnalyticsSummary(summaryRes.data?.client_id != null ? summaryRes.data : null)
      setCallHealth(healthRes.data?.calls_total != null ? healthRes.data : null)
      setRecentCalls(callsRes.data?.calls || [])
      setSmsThreads(threadsRes.data?.threads || [])
      setLastUpdated(Date.now())
    } catch (error: unknown) {
      const status = (error as { response?: { status?: number } })?.response?.status
      if (status === 401 || status === 403) {
        setNoTenant(true)
      }
      console.error('Error fetching data:', error)
    } finally {
      setLoading(false)
    }
  }

  const updateMessageLocal = (m: Message) =>
    setMessages((prev) => prev.map((x) => (x.id === m.id ? { ...x, ...m } : x)))

  const markRead = async (id: number, read: boolean) => {
    setBusyMsgId(id)
    try {
      const res = await api.post(`/api/messages/${id}/read?read=${read}`)
      if (res?.data?.message) updateMessageLocal(res.data.message)
    } catch (error) {
      console.error('Failed to update message', error)
    } finally {
      setBusyMsgId(null)
    }
  }

  const openThread = async (phone: string) => {
    setOpenThreadPhone(phone)
    setThreadMessages(null)
    setThreadLoading(true)
    try {
      const res = await api.get(`/api/sms/thread?phone=${encodeURIComponent(phone)}`)
      setThreadMessages(res.data?.messages || [])
    } catch (error) {
      console.error('Failed to load conversation', error)
      setThreadMessages([])
    } finally {
      setThreadLoading(false)
    }
  }

  const closeThread = () => {
    setOpenThreadPhone(null)
    setThreadMessages(null)
  }

  const sendReply = async (id: number) => {
    const text = replyText.trim()
    if (!text) return
    setBusyMsgId(id)
    setMsgActionNote(null)
    try {
      const res = await api.post(`/api/messages/${id}/reply`, { text })
      if (res?.data?.message) updateMessageLocal(res.data.message)
      const ok = res?.data?.reply_sms_sent !== false
      setMsgActionNote({ id, ok, text: ok ? 'Reply sent.' : "Couldn't send the text — try calling the customer instead." })
      setReplyingId(null)
      setReplyText('')
      setTimeout(() => setMsgActionNote(null), ok ? 4000 : 8000)
    } catch (error) {
      console.error('Failed to send reply', error)
      setMsgActionNote({ id, ok: false, text: 'Failed to send reply.' })
      setTimeout(() => setMsgActionNote(null), 6000)
    } finally {
      setBusyMsgId(null)
    }
  }

  if (noTenant) {
    return (
      <div className="flex flex-col items-center justify-center h-64 space-y-4 text-center">
        <p className="max-w-md text-zinc-300">
          You don&rsquo;t have a business set up yet. Get your AI receptionist live in a couple of minutes.
        </p>
        <a
          href="/dashboard/create-business"
          className="rounded-full bg-gradient-to-r from-cyan-600 to-indigo-600 px-6 py-2.5 text-sm font-semibold text-white shadow-lg shadow-cyan-500/20 hover:brightness-110"
        >
          Set up your business
        </a>
        <p className="max-w-md text-xs text-zinc-500">
          Were you invited by our team? Use the link from your invite email instead.
        </p>
      </div>
    )
  }

  if (loading) {
    return (
      <div className="space-y-6">
        {/* Stat cards */}
        <div className="grid grid-cols-1 md:grid-cols-3 gap-6">
          {[0, 1, 2].map((i) => (
            <div key={i} className="bg-white rounded-lg shadow-md p-6">
              <div className="flex items-center justify-between">
                <div className="space-y-3">
                  <Skeleton className="h-3 w-28" />
                  <Skeleton className="h-8 w-16" />
                </div>
                <Skeleton className="h-12 w-12 rounded-full" />
              </div>
            </div>
          ))}
        </div>
        {/* Section blocks */}
        {[0, 1].map((i) => (
          <div key={i} className="bg-white rounded-lg shadow-md p-6 space-y-4">
            <Skeleton className="h-5 w-48" />
            <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
              {[0, 1, 2, 3].map((j) => (
                <Skeleton key={j} className="h-16 w-full" />
              ))}
            </div>
          </div>
        ))}
      </div>
    )
  }

  return (
    <div className="space-y-6 text-gray-900">
      <div className="flex items-center justify-end gap-3 text-sm text-gray-500">
        {lastUpdated && <span aria-live="polite">Updated {relativeAgo(lastUpdated)}</span>}
        <button
          type="button"
          onClick={manualRefresh}
          disabled={refreshing}
          className="inline-flex items-center gap-1.5 rounded-lg border border-gray-300 px-2.5 py-1.5 text-xs font-medium text-gray-700 hover:bg-gray-100 disabled:opacity-50"
        >
          <RefreshCw className={`h-3.5 w-3.5 ${refreshing ? 'animate-spin' : ''}`} />
          Refresh
        </button>
      </div>

      {/* Usage widget */}
      {usage != null && minutesCap != null && minutesCap > 0 && (
        <div className="bg-white rounded-lg shadow-md p-6">
          <h3 className="text-lg font-semibold text-gray-900 mb-3">Usage this month</h3>
          <div className="space-y-4">
            {/* Voice minutes */}
            <div>
              <div className="flex justify-between text-sm text-gray-600 mb-1">
                <span>Voice minutes: {usage.voice_minutes} / {minutesCap}</span>
                {usage.voice_minutes > minutesCap && (
                  <span className="text-amber-600">Overage: {usage.voice_minutes - minutesCap} extra min</span>
                )}
              </div>
              <div className="h-2 bg-gray-200 rounded-full overflow-hidden">
                <div
                  className={`h-full rounded-full ${usage.voice_minutes >= minutesCap ? 'bg-amber-500' : 'bg-primary-600'}`}
                  style={{ width: `${Math.min(100, (usage.voice_minutes / minutesCap) * 100)}%` }}
                />
              </div>
            </div>
            {/* Text messages */}
            <div>
              <div className="flex justify-between text-sm text-gray-600 mb-1">
                <span>
                  Text messages: {usage.sms_count}
                  {smsCap != null && smsCap > 0 ? ` / ${smsCap}` : ''}
                </span>
                {smsCap != null && smsCap > 0 && usage.sms_count > smsCap && (
                  <span className="text-amber-600">Overage: {usage.sms_count - smsCap} extra texts</span>
                )}
              </div>
              {smsCap != null && smsCap > 0 && (
                <div className="h-2 bg-gray-200 rounded-full overflow-hidden">
                  <div
                    className={`h-full rounded-full ${usage.sms_count >= smsCap ? 'bg-amber-500' : 'bg-primary-600'}`}
                    style={{ width: `${Math.min(100, (usage.sms_count / smsCap) * 100)}%` }}
                  />
                </div>
              )}
            </div>
          </div>
        </div>
      )}

      {/* Stats Cards */}
      <RevealStagger className="grid grid-cols-1 md:grid-cols-3 gap-6">
        <RevealItem className="bg-white rounded-lg shadow-md p-6 transition-transform duration-200 hover:-translate-y-1 hover:shadow-lg">
          <div className="flex items-center justify-between">
            <div>
              <p className="text-gray-600 text-sm font-medium">Total Appointments</p>
              <p className="text-3xl font-bold text-gray-900 mt-2">
                <AnimatedNumber value={stats.total_appointments} />
              </p>
            </div>
            <div className="bg-blue-100 p-3 rounded-full">
              <Calendar className="w-6 h-6 text-blue-600" />
            </div>
          </div>
        </RevealItem>

        <RevealItem className="bg-white rounded-lg shadow-md p-6 transition-transform duration-200 hover:-translate-y-1 hover:shadow-lg">
          <div className="flex items-center justify-between">
            <div>
              {/* Texts exchanged in SMS threads — NOT the count on the Messages tab,
                  which lists messages the receptionist took from callers. Naming both
                  "Messages" made the dashboard look broken when one was zero. */}
              <p className="text-gray-600 text-sm font-medium">Texts Exchanged</p>
              <p className="text-3xl font-bold text-gray-900 mt-2">
                <AnimatedNumber value={stats.total_messages} />
              </p>
            </div>
            <div className="bg-green-100 p-3 rounded-full">
              <MessageSquare className="w-6 h-6 text-green-600" />
            </div>
          </div>
        </RevealItem>

        <RevealItem className="bg-white rounded-lg shadow-md p-6 transition-transform duration-200 hover:-translate-y-1 hover:shadow-lg">
          <div className="flex items-center justify-between">
            <div>
              <p className="text-gray-600 text-sm font-medium">Pending Appointments</p>
              <p className="text-3xl font-bold text-gray-900 mt-2">
                <AnimatedNumber value={stats.pending_appointments} />
              </p>
            </div>
            <div className="bg-yellow-100 p-3 rounded-full">
              <TrendingUp className="w-6 h-6 text-yellow-600" />
            </div>
          </div>
        </RevealItem>
      </RevealStagger>

      {callHealth != null && (
        <div className="bg-white rounded-lg shadow-md p-6">
          <h2 className="text-xl font-bold text-gray-900 flex items-center mb-4">
            <Phone className="w-5 h-5 mr-2" />
            Call health (last {callHealth.period_days} days)
          </h2>
          <div className="grid grid-cols-2 md:grid-cols-4 gap-4">
            <div>
              <p className="text-gray-600 text-sm">Total calls</p>
              <p className="text-2xl font-bold text-gray-900">{callHealth.calls_total}</p>
            </div>
            <div>
              <p className="text-gray-600 text-sm">Forward rate</p>
              <p className="text-2xl font-bold text-gray-900">{Math.round(callHealth.forward_rate * 100)}%</p>
            </div>
            <div>
              <p className="text-gray-600 text-sm">Missed / no answer</p>
              <p className="text-2xl font-bold text-gray-900">{Math.round(callHealth.missed_rate * 100)}%</p>
            </div>
            <div>
              <p className="text-gray-600 text-sm">Avg duration</p>
              <p className="text-2xl font-bold text-gray-900">
                {callHealth.avg_duration_sec ? `${callHealth.avg_duration_sec}s` : '—'}
              </p>
            </div>
          </div>
        </div>
      )}

      {/* Call Analytics */}
      {analyticsSummary != null && (
        <div className="bg-white rounded-lg shadow-md p-6">
          <div className="flex justify-between items-center mb-4">
            <h2 className="text-xl font-bold text-gray-900 flex items-center">
              <BarChart3 className="w-5 h-5 mr-2" />
              Call Analytics
            </h2>
            {hasExport ? (
              <button
                type="button"
                disabled={exporting}
                onClick={async () => {
                  setExporting(true)
                  try {
                    const res = await api.get('/api/analytics/export', { responseType: 'blob' })
                    const blob = new Blob([res.data], { type: 'text/csv' })
                    const url = URL.createObjectURL(blob)
                    const a = document.createElement('a')
                    a.href = url
                    a.download = 'call_log.csv'
                    a.click()
                    setTimeout(() => URL.revokeObjectURL(url), 100)
                  } catch {
                    // 403 or error - ignore
                  } finally {
                    setExporting(false)
                  }
                }}
                className="inline-flex items-center gap-2 px-3 py-1.5 rounded-lg text-sm font-medium bg-primary-600 text-white hover:bg-primary-700 disabled:opacity-50"
              >
                {exporting ? 'Exporting…' : 'Export CSV'}
              </button>
            ) : (
              <button
                type="button"
                title="CSV export is available on Growth and Pro"
                onClick={async () => {
                  try {
                    const { data } = await api.post<{ url: string }>('/api/create-portal-session')
                    if (data?.url) window.location.href = data.url
                  } catch {
                    // ignore — keep the user on the page
                  }
                }}
                className="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-sm font-medium border border-gray-300 text-gray-500 hover:bg-gray-50"
              >
                <Lock className="w-3.5 h-3.5" /> Export CSV · Pro
              </button>
            )}
          </div>
          <div className="grid grid-cols-1 md:grid-cols-2 gap-6 mb-6">
            <div>
              <p className="text-gray-600 text-sm font-medium">Total calls</p>
              <p className="text-3xl font-bold text-gray-900 mt-1">{analyticsSummary.total_calls}</p>
            </div>
            <div>
              <p className="text-gray-600 text-sm font-medium mb-2">By outcome</p>
              <div className="flex flex-wrap gap-2">
                {Object.entries(analyticsSummary.by_outcome).map(([outcome, count]) => (
                  <span
                    key={outcome}
                    className={`px-2 py-1 rounded-full text-xs font-semibold ${callOutcomeClass(outcome)}`}
                  >
                    {outcome.replace(/_/g, ' ')}: {count}
                  </span>
                ))}
                {Object.keys(analyticsSummary.by_outcome).length === 0 && (
                  <span className="text-gray-500 text-sm">No data yet</span>
                )}
              </div>
            </div>
          </div>
          <div className="mb-4">
            <p className="text-gray-600 text-sm font-medium mb-2">Peak call times (by hour)</p>
            <div className="flex flex-wrap gap-1 items-end" style={{ minHeight: '24px' }}>
              {Array.from({ length: 24 }, (_, h) => {
                const count = analyticsSummary.by_hour[String(h)] ?? 0
                const max = Math.max(...Object.values(analyticsSummary.by_hour).map(Number), 1)
                const pct = max ? (count / max) * 100 : 0
                return (
                  <div key={h} className="flex flex-col items-center" title={`${h}:00 - ${count} calls`}>
                    <div
                      className="w-3 bg-primary-600 rounded-t min-h-[4px]"
                      style={{ height: `${Math.max(pct, 4)}px` }}
                    />
                    <span className="mt-1 text-[10px] font-medium text-gray-700">{h}</span>
                  </div>
                )
              })}
            </div>
          </div>
          <div>
            <p className="text-gray-600 text-sm font-medium mb-1">By day of week (this week)</p>
            {analyticsSummary.by_day_of_week_period_start && analyticsSummary.by_day_of_week_period_end && (
              <p className="text-gray-500 text-xs mb-2">
                {analyticsSummary.by_day_of_week_period_start} – {analyticsSummary.by_day_of_week_period_end}
                {analyticsSummary.by_day_of_week_timezone ? ` (${analyticsSummary.by_day_of_week_timezone})` : ''}. Full history stays in the database; use Export CSV for all calls in your plan window.
              </p>
            )}
            <div className="flex flex-wrap gap-2">
              {[0, 1, 2, 3, 4, 5, 6].map((d) => (
                <span key={d} className="rounded bg-gray-200 px-2 py-1 text-xs font-medium text-gray-900">
                  {DAY_NAMES[d]}: {analyticsSummary.by_day_of_week[String(d)] ?? 0}
                </span>
              ))}
            </div>
          </div>
          <div className="mt-6">
            <p className="text-gray-700 font-semibold mb-2">Recent calls</p>
            {recentCalls.length === 0 ? (
              <EmptyState
                icon={Phone}
                title="No calls logged yet"
                description="When your AI receptionist answers a call, a summary and recording will appear here."
              />
            ) : (
              <div className="overflow-x-auto">
                <table className="w-full text-sm text-gray-900">
                  <thead>
                    <tr className="border-b border-gray-200">
                      <th className="text-left py-2 px-2 font-semibold text-gray-800">From</th>
                      <th className="text-left py-2 px-2 font-semibold text-gray-800">Start</th>
                      <th className="text-left py-2 px-2 font-semibold text-gray-800">Duration</th>
                      <th className="text-left py-2 px-2 font-semibold text-gray-800">Outcome</th>
                      <th className="text-left py-2 px-2 font-semibold text-gray-800">Summary</th>
                      <th className="text-left py-2 px-2 font-semibold text-gray-800">Recording</th>
                    </tr>
                  </thead>
                  <tbody>
                    {recentCalls.slice(0, 10).map((call) => (
                      <tr key={call.call_sid} className="border-b border-gray-200 hover:bg-gray-50">
                        <td className="py-2 px-2 font-medium text-gray-900">{call.from_number}</td>
                        <td className="py-2 px-2 text-gray-900">
                          {call.start_iso ? new Date(call.start_iso).toLocaleString() : '—'}
                        </td>
                        <td className="py-2 px-2 text-gray-900">
                          {call.duration_sec != null ? `${call.duration_sec}s` : '—'}
                        </td>
                        <td className="py-2 px-2">
                          <span
                            className={`rounded px-2 py-0.5 text-xs font-semibold ${callOutcomeClass(call.outcome || '')}`}
                          >
                            {call.outcome || '—'}
                          </span>
                        </td>
                        <td className="max-w-[200px] py-2 px-2">
                          {call.call_summary ? (
                            <span className="line-clamp-2 text-gray-900" title={call.call_summary}>
                              {call.call_summary.length > 120 ? `${call.call_summary.slice(0, 120)}…` : call.call_summary}
                            </span>
                          ) : (
                            <span className="text-gray-600">—</span>
                          )}
                        </td>
                        <td className="py-2 px-2">
                          {call.recording_sid || call.recording_url ? (
                            <button
                              type="button"
                              className="text-primary-600 hover:text-primary-800 text-sm font-medium underline"
                              onClick={async () => {
                                try {
                                  const res = await api.get(
                                    `/api/analytics/calls/${encodeURIComponent(call.call_sid)}/recording`,
                                    { responseType: 'blob' }
                                  )
                                  const blob = new Blob([res.data], { type: 'audio/mpeg' })
                                  const url = URL.createObjectURL(blob)
                                  window.open(url, '_blank', 'noopener,noreferrer')
                                  setTimeout(() => URL.revokeObjectURL(url), 120_000)
                                } catch {
                                  // 404 / auth — ignore
                                }
                              }}
                            >
                              Play
                            </button>
                          ) : (
                            <span className="text-gray-600">—</span>
                          )}
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </div>
        </div>
      )}

      {/* Appointments Table */}
      <div className="bg-white rounded-lg shadow-md p-6">
        <h2 className="text-xl font-bold text-gray-900 mb-4 flex items-center">
          <Calendar className="w-5 h-5 mr-2" />
          Recent Appointments
        </h2>
        {appointments.length === 0 ? (
          <EmptyState
            icon={Calendar}
            title="No appointments yet"
            description="Bookings your AI receptionist takes will show up here for you to accept or decline."
          />
        ) : (
          <div className="overflow-x-auto">
            <table className="w-full text-gray-900">
              <thead>
                <tr className="border-b border-gray-200">
                  <th className="text-left py-3 px-4 font-semibold text-gray-800">Name</th>
                  <th className="text-left py-3 px-4 font-semibold text-gray-800">Date</th>
                  <th className="text-left py-3 px-4 font-semibold text-gray-800">Time</th>
                  <th className="text-left py-3 px-4 font-semibold text-gray-800">Reason</th>
                  <th className="text-left py-3 px-4 font-semibold text-gray-800">Status</th>
                </tr>
              </thead>
              <tbody>
                {appointments.slice(0, 10).map((appointment) => (
                  <tr key={appointment.id} className="border-b border-gray-200 hover:bg-gray-50">
                    <td className="py-3 px-4 font-medium text-gray-900">{appointment.name}</td>
                    <td className="py-3 px-4 text-gray-900">{appointment.date}</td>
                    <td className="py-3 px-4 text-gray-900">{formatTimeHhmmToAmPm(appointment.time)}</td>
                    <td className="py-3 px-4 text-gray-900">{appointment.reason}</td>
                    <td className="py-3 px-4">
                      <span
                        className={`inline-block rounded-full px-2 py-1 text-xs font-semibold ${
                          STATUS_CLASSES[appointment.status] || 'bg-gray-200 text-gray-900'
                        }`}
                      >
                        {STATUS_LABELS[appointment.status] || appointment.status}
                      </span>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </div>

      {/* SMS conversations (Growth+ perk) */}
      {hasMessages ? (
      <div className="bg-white rounded-lg shadow-md p-6">
        <div className="mb-4 flex flex-wrap items-center justify-between gap-3">
          <h2 className="text-xl font-bold text-gray-900 flex items-center">
            <MessageSquare className="w-5 h-5 mr-2" />
            Messages
          </h2>
          <div className="relative">
            <Search className="pointer-events-none absolute left-3 top-1/2 h-4 w-4 -translate-y-1/2 text-gray-400" />
            <input
              type="text"
              value={threadSearch}
              onChange={(e) => setThreadSearch(e.target.value)}
              placeholder="Search by phone number…"
              className="w-64 max-w-full rounded-lg border border-gray-300 py-2 pl-9 pr-3 text-sm text-gray-900 focus:border-cyan-500 focus:outline-none focus:ring-1 focus:ring-cyan-500"
            />
          </div>
        </div>
        {(() => {
          const q = threadSearch.replace(/\D/g, '')
          const filtered = q
            ? smsThreads.filter((t) => t.phone.replace(/\D/g, '').includes(q))
            : smsThreads
          if (smsThreads.length === 0) {
            return (
              <EmptyState
                icon={MessageSquare}
                title="No conversations yet"
                description="When a caller texts your number, the conversation appears here. Select one to read the whole thread."
              />
            )
          }
          if (filtered.length === 0) {
            return (
              <p className="py-8 text-center text-sm text-gray-500">
                No conversations match “{threadSearch}”.
              </p>
            )
          }
          return (
            <ul className="divide-y divide-gray-100">
              {filtered.map((t) => (
                <li key={t.phone}>
                  <button
                    type="button"
                    onClick={() => openThread(t.phone)}
                    className="-mx-2 flex w-full items-center gap-3 rounded-lg px-2 py-3 text-left hover:bg-gray-50"
                  >
                    <div className="flex h-10 w-10 shrink-0 items-center justify-center rounded-full bg-gradient-to-br from-cyan-500 to-indigo-600 text-white">
                      <MessageSquare className="h-5 w-5" />
                    </div>
                    <div className="min-w-0 flex-1">
                      <div className="flex items-center justify-between gap-2">
                        <span className="font-semibold text-gray-900">{formatPhone(t.phone)}</span>
                        <span className="shrink-0 text-xs text-gray-400">{relativeOrDate(t.updated_at)}</span>
                      </div>
                      <p className="truncate text-sm text-gray-600">
                        {t.last_role === 'assistant' && <span className="text-gray-400">You: </span>}
                        {t.last_message || '—'}
                      </p>
                    </div>
                    <span className="shrink-0 rounded-full bg-gray-100 px-2 py-0.5 text-xs font-medium text-gray-600">
                      {t.message_count}
                    </span>
                    <ChevronRight className="h-4 w-4 shrink-0 text-gray-300" />
                  </button>
                </li>
              ))}
            </ul>
          )
        })()}
      </div>
      ) : (
        <LockedFeature
          variant="light"
          title="See every text conversation"
          tagline="Your AI receptionist is already texting customers. Upgrade to read and search every conversation in one place—so you never miss a follow-up or lose track of what was said."
          bullets={[
            'Every caller’s full text thread, in one inbox',
            'Search by phone number to find any conversation',
            'See exactly what your receptionist replied',
          ]}
        />
      )}

      {/* Voicemail messages — only shown when callers have left any */}
      {messages.length > 0 && (
      <div className="bg-white rounded-lg shadow-md p-6">
        <h2 className="text-xl font-bold text-gray-900 mb-4 flex items-center">
          <MessageSquare className="w-5 h-5 mr-2" />
          Voicemail messages
        </h2>
          <div className="overflow-x-auto">
            <table className="w-full text-gray-900">
              <thead>
                <tr className="border-b border-gray-200">
                  <th className="text-left py-3 px-4 font-semibold text-gray-800">Caller</th>
                  <th className="text-left py-3 px-4 font-semibold text-gray-800">Phone</th>
                  <th className="text-left py-3 px-4 font-semibold text-gray-800">Message</th>
                  <th className="text-left py-3 px-4 font-semibold text-gray-800">Urgency</th>
                  <th className="text-left py-3 px-4 font-semibold text-gray-800">Status</th>
                  <th className="text-right py-3 px-4 font-semibold text-gray-800">Actions</th>
                </tr>
              </thead>
              <tbody>
                {messages.slice(0, 10).map((message) => {
                  const busy = busyMsgId === message.id
                  const isUnread = message.status === 'unread'
                  return (
                    <Fragment key={message.id}>
                      <tr className={`border-b border-gray-200 hover:bg-gray-50 ${isUnread ? 'bg-amber-50/40' : ''}`}>
                        <td className="py-3 px-4 font-medium text-gray-900">{message.caller_name || '—'}</td>
                        <td className="py-3 px-4 text-gray-900">{message.caller_phone || '—'}</td>
                        <td className="max-w-md truncate py-3 px-4 text-gray-900">{message.message}</td>
                        <td className="py-3 px-4">
                          <span
                            className={`inline-block rounded-full px-2 py-1 text-xs font-semibold ${
                              message.urgency === 'urgent'
                                ? 'bg-red-200 text-red-950'
                                : message.urgency === 'high'
                                  ? 'bg-orange-200 text-orange-950'
                                  : 'bg-sky-200 text-sky-950'
                            }`}
                          >
                            {message.urgency}
                          </span>
                        </td>
                        <td className="py-3 px-4">
                          <span
                            className={`inline-block rounded-full px-2 py-1 text-xs font-semibold ${
                              isUnread ? 'bg-amber-200 text-amber-950' : 'bg-emerald-200 text-emerald-950'
                            }`}
                          >
                            {message.status}
                          </span>
                        </td>
                        <td className="py-3 px-4">
                          <div className="flex items-center justify-end gap-2">
                            <button
                              type="button"
                              disabled={busy}
                              onClick={() => markRead(message.id, isUnread)}
                              className="inline-flex items-center gap-1 rounded-lg border border-gray-300 px-2.5 py-1.5 text-xs font-medium text-gray-700 hover:bg-gray-100 disabled:opacity-50"
                            >
                              <Check className="h-3.5 w-3.5" />
                              {isUnread ? 'Mark read' : 'Mark unread'}
                            </button>
                            <button
                              type="button"
                              disabled={busy || !message.caller_phone}
                              title={message.caller_phone ? 'Text the caller back' : 'No phone number to reply to'}
                              onClick={() => {
                                setReplyingId(replyingId === message.id ? null : message.id)
                                setReplyText('')
                                setMsgActionNote(null)
                              }}
                              className="inline-flex items-center gap-1 rounded-lg bg-gradient-to-r from-cyan-500 to-indigo-600 px-2.5 py-1.5 text-xs font-semibold text-white shadow disabled:opacity-50"
                            >
                              <Send className="h-3.5 w-3.5" />
                              Reply
                            </button>
                          </div>
                        </td>
                      </tr>
                      {(replyingId === message.id || msgActionNote?.id === message.id) && (
                        <tr className="border-b border-gray-200 bg-gray-50">
                          <td colSpan={6} className="px-4 py-3">
                            {replyingId === message.id && (
                              <div className="flex flex-col gap-2 sm:flex-row sm:items-end">
                                <label className="flex-1">
                                  <span className="mb-1 block text-xs font-medium text-gray-600">
                                    Reply by text to {message.caller_phone}
                                  </span>
                                  <textarea
                                    value={replyText}
                                    onChange={(e) => setReplyText(e.target.value)}
                                    rows={2}
                                    maxLength={1000}
                                    placeholder="Type your reply…"
                                    className="w-full rounded-lg border border-gray-300 px-3 py-2 text-sm text-gray-900 focus:border-cyan-500 focus:outline-none focus:ring-1 focus:ring-cyan-500"
                                  />
                                </label>
                                <button
                                  type="button"
                                  disabled={busy || !replyText.trim()}
                                  onClick={() => sendReply(message.id)}
                                  className="inline-flex items-center justify-center gap-1.5 rounded-lg bg-gradient-to-r from-cyan-500 to-indigo-600 px-4 py-2 text-sm font-semibold text-white shadow disabled:opacity-50"
                                >
                                  {busy ? <Loader2 className="h-4 w-4 animate-spin" /> : <Send className="h-4 w-4" />}
                                  Send text
                                </button>
                              </div>
                            )}
                            {msgActionNote?.id === message.id && (
                              <p className={`mt-1 text-xs font-medium ${msgActionNote.ok ? 'text-emerald-600' : 'text-red-600'}`}>
                                {msgActionNote.text}
                              </p>
                            )}
                          </td>
                        </tr>
                      )}
                    </Fragment>
                  )
                })}
              </tbody>
            </table>
          </div>
      </div>
      )}

      {openThreadPhone && (
        <div
          className="fixed inset-0 z-50 flex items-end justify-center bg-black/50 p-0 sm:items-center sm:p-4"
          onClick={closeThread}
        >
          <div
            className="flex h-[85vh] w-full max-w-lg flex-col overflow-hidden rounded-t-2xl bg-white shadow-2xl sm:h-[80vh] sm:rounded-2xl"
            onClick={(e) => e.stopPropagation()}
          >
            <div className="flex items-center justify-between border-b border-gray-200 px-5 py-4">
              <div className="flex items-center gap-3">
                <div className="flex h-10 w-10 items-center justify-center rounded-full bg-gradient-to-br from-cyan-500 to-indigo-600 text-white">
                  <MessageSquare className="h-5 w-5" />
                </div>
                <div>
                  <p className="font-semibold text-gray-900">{formatPhone(openThreadPhone)}</p>
                  <p className="text-xs text-gray-500">Conversation with your AI receptionist</p>
                </div>
              </div>
              <button
                type="button"
                onClick={closeThread}
                aria-label="Close conversation"
                className="rounded-full p-2 text-gray-400 hover:bg-gray-100 hover:text-gray-700"
              >
                <X className="h-5 w-5" />
              </button>
            </div>
            <div className="flex-1 space-y-3 overflow-y-auto bg-gray-50 px-4 py-5">
              {threadLoading ? (
                <div className="flex h-full items-center justify-center text-gray-400">
                  <Loader2 className="h-6 w-6 animate-spin" />
                </div>
              ) : !threadMessages || threadMessages.length === 0 ? (
                <p className="py-10 text-center text-sm text-gray-500">No messages in this conversation.</p>
              ) : (
                threadMessages.map((m, i) => {
                  const fromBiz = m.role === 'assistant'
                  return (
                    <div key={i} className={`flex ${fromBiz ? 'justify-end' : 'justify-start'}`}>
                      <div
                        className={`max-w-[78%] whitespace-pre-wrap rounded-2xl px-4 py-2 text-sm shadow-sm ${
                          fromBiz
                            ? 'rounded-br-sm bg-gradient-to-r from-cyan-500 to-indigo-600 text-white'
                            : 'rounded-bl-sm bg-white text-gray-900 ring-1 ring-gray-200'
                        }`}
                      >
                        {m.content}
                      </div>
                    </div>
                  )
                })
              )}
            </div>
            <div className="border-t border-gray-200 px-5 py-3 text-center text-xs text-gray-400">
              {formatPhone(openThreadPhone)} · {threadMessages?.length ?? 0} messages
            </div>
          </div>
        </div>
      )}
    </div>
  )
}












