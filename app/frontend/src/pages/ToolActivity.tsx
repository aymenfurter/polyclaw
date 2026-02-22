import { useState, useEffect, useCallback, useMemo, useRef } from 'react'
import { api, getToken } from '../api'
import type { ApiResponse } from '../types'

/* ------------------------------------------------------------------ */
/*  Types                                                              */
/* ------------------------------------------------------------------ */

interface ToolActivityEntry {
  id: string
  session_id: string
  tool: string
  call_id: string
  category: string
  arguments: string
  result: string
  status: string
  timestamp: number
  duration_ms: number | null
  flagged: boolean
  flag_reason: string
  risk_score: number
  risk_factors: string[]
  model: string
  interaction_type: string  // '' | hitl | aitl | pitl | filter | deny
}

interface ActivityListResponse extends ApiResponse {
  entries: ToolActivityEntry[]
  total: number
  offset: number
  limit: number
}

interface ActivitySummary extends ApiResponse {
  total: number
  flagged: number
  by_tool: Record<string, number>
  by_category: Record<string, number>
  by_status: Record<string, number>
  by_model: Record<string, number>
  sessions_with_activity: number
  avg_duration_ms: number
  max_duration_ms: number
  p95_duration_ms: number
  risk_high: number
  risk_medium: number
  risk_low: number
  by_interaction_type: Record<string, number>
}

interface TimelineBucket {
  timestamp: number
  total: number
  flagged: number
  sdk: number
  mcp: number
  custom: number
  skill: number
}

interface SessionBreakdown {
  session_id: string
  tool_count: number
  flagged_count: number
  max_risk: number
  categories: string[]
  unique_tools: number
  models: string[]
  first_activity: number
  last_activity: number
  total_duration_ms: number
}

type Tab = 'log' | 'sessions'
type GroupBy = 'none' | 'tool' | 'category' | 'session' | 'status' | 'model'
type SortField = 'timestamp' | 'tool' | 'risk_score' | 'duration_ms' | 'status' | 'model'
type SortDir = 'asc' | 'desc'

/* ------------------------------------------------------------------ */
/*  Main Component                                                     */
/* ------------------------------------------------------------------ */

export default function ToolActivity() {
  const [tab, setTab] = useState<Tab>('log')
  const [entries, setEntries] = useState<ToolActivityEntry[]>([])
  const [summary, setSummary] = useState<ActivitySummary | null>(null)
  const [timeline, setTimeline] = useState<TimelineBucket[]>([])
  const [sessions, setSessions] = useState<SessionBreakdown[]>([])
  const [total, setTotal] = useState(0)
  const [loading, setLoading] = useState(true)
  const [importing, setImporting] = useState(false)
  const [autoRefresh, setAutoRefresh] = useState(false)
  const autoRefreshRef = useRef<ReturnType<typeof setInterval> | null>(null)

  // Filters
  const [filterTool, setFilterTool] = useState('')
  const [filterCategory, setFilterCategory] = useState('')
  const [filterStatus, setFilterStatus] = useState('')
  const [filterFlagged, setFilterFlagged] = useState(false)
  const [filterSession, setFilterSession] = useState('')
  const [filterTimeRange, setFilterTimeRange] = useState('')
  const [filterModel, setFilterModel] = useState('')
  const [filterInteractionType, setFilterInteractionType] = useState('')

  // Sorting
  const [sortField, setSortField] = useState<SortField>('timestamp')
  const [sortDir, setSortDir] = useState<SortDir>('desc')

  // Grouping
  const [groupBy, setGroupBy] = useState<GroupBy>('none')

  // Inspection
  const [selected, setSelected] = useState<ToolActivityEntry | null>(null)

  // Pagination
  const [offset, setOffset] = useState(0)
  const limit = 100

  const sinceTimestamp = useMemo(() => {
    if (!filterTimeRange) return 0
    const now = Date.now() / 1000
    const map: Record<string, number> = {
      '1h': 3600, '6h': 21600, '24h': 86400, '7d': 604800, '30d': 2592000,
    }
    return now - (map[filterTimeRange] || 0)
  }, [filterTimeRange])

  const loadEntries = useCallback(async () => {
    setLoading(true)
    try {
      const params = new URLSearchParams()
      if (filterTool) params.set('tool', filterTool)
      if (filterCategory) params.set('category', filterCategory)
      if (filterStatus) params.set('status', filterStatus)
      if (filterFlagged) params.set('flagged', '1')
      if (filterSession) params.set('session_id', filterSession)
      if (filterModel) params.set('model', filterModel)
      if (filterInteractionType) params.set('interaction_type', filterInteractionType)
      if (sinceTimestamp > 0) params.set('since', String(sinceTimestamp))
      params.set('limit', String(limit))
      params.set('offset', String(offset))

      const qs = params.toString()
      const res = await api<ActivityListResponse>(`tool-activity${qs ? '?' + qs : ''}`)
      setEntries(res.entries || [])
      setTotal(res.total || 0)
    } catch {
      setEntries([])
    } finally {
      setLoading(false)
    }
  }, [filterTool, filterCategory, filterStatus, filterFlagged, filterSession, filterModel, filterInteractionType, sinceTimestamp, offset])

  const loadSummary = useCallback(async () => {
    try {
      const res = await api<ActivitySummary>('tool-activity/summary')
      setSummary(res)
    } catch { /* ignore */ }
  }, [])

  const loadTimeline = useCallback(async () => {
    try {
      const params = new URLSearchParams()
      if (sinceTimestamp > 0) params.set('since', String(sinceTimestamp))
      const qs = params.toString()
      const res = await api<{ buckets: TimelineBucket[] }>(`tool-activity/timeline${qs ? '?' + qs : ''}`)
      setTimeline(res.buckets || [])
    } catch { /* ignore */ }
  }, [sinceTimestamp])

  const loadSessions = useCallback(async () => {
    try {
      const res = await api<{ sessions: SessionBreakdown[] }>('tool-activity/sessions')
      setSessions(res.sessions || [])
    } catch { /* ignore */ }
  }, [])

  useEffect(() => {
    loadEntries()
    loadSummary()
    loadTimeline()
    loadSessions()
  }, [loadEntries, loadSummary, loadTimeline, loadSessions])

  // Auto-refresh
  useEffect(() => {
    if (autoRefresh) {
      autoRefreshRef.current = setInterval(() => {
        loadEntries()
        loadSummary()
        loadTimeline()
      }, 10000)
    }
    return () => { if (autoRefreshRef.current) clearInterval(autoRefreshRef.current) }
  }, [autoRefresh, loadEntries, loadSummary, loadTimeline])

  const handleImport = useCallback(async () => {
    setImporting(true)
    try {
      await api<ApiResponse>('tool-activity/import', { method: 'POST' })
      await Promise.all([loadEntries(), loadSummary(), loadTimeline(), loadSessions()])
    } catch { /* ignore */ }
    setImporting(false)
  }, [loadEntries, loadSummary, loadTimeline, loadSessions])

  const handleFlag = useCallback(async (entryId: string, reason: string) => {
    await api<ApiResponse>(`tool-activity/${entryId}/flag`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ reason }),
    })
    await Promise.all([loadEntries(), loadSummary()])
  }, [loadEntries, loadSummary])

  const handleUnflag = useCallback(async (entryId: string) => {
    await api<ApiResponse>(`tool-activity/${entryId}/unflag`, { method: 'POST' })
    await Promise.all([loadEntries(), loadSummary()])
  }, [loadEntries, loadSummary])

  const handleExport = useCallback(async () => {
    const params = new URLSearchParams()
    if (filterTool) params.set('tool', filterTool)
    if (filterCategory) params.set('category', filterCategory)
    if (filterStatus) params.set('status', filterStatus)
    if (filterModel) params.set('model', filterModel)
    if (filterInteractionType) params.set('interaction_type', filterInteractionType)
    if (filterFlagged) params.set('flagged', '1')
    const qs = params.toString()
    try {
      const headers: Record<string, string> = {}
      const token = getToken()
      if (token) headers['Authorization'] = `Bearer ${token}`
      const res = await fetch(`/api/tool-activity/export${qs ? '?' + qs : ''}`, { headers })
      if (!res.ok) throw new Error(`HTTP ${res.status}`)
      const blob = await res.blob()
      const url = URL.createObjectURL(blob)
      const a = document.createElement('a')
      a.href = url
      a.download = 'tool-activity.csv'
      a.click()
      URL.revokeObjectURL(url)
    } catch { /* ignore */ }
  }, [filterTool, filterCategory, filterStatus, filterModel, filterInteractionType, filterFlagged])

  const sortedEntries = useMemo(() => {
    const sorted = [...entries]
    sorted.sort((a, b) => {
      let cmp = 0
      switch (sortField) {
        case 'timestamp': cmp = a.timestamp - b.timestamp; break
        case 'tool': cmp = a.tool.localeCompare(b.tool); break
        case 'risk_score': cmp = a.risk_score - b.risk_score; break
        case 'duration_ms': cmp = (a.duration_ms ?? 0) - (b.duration_ms ?? 0); break
        case 'status': cmp = a.status.localeCompare(b.status); break
        case 'model': cmp = a.model.localeCompare(b.model); break
      }
      return sortDir === 'desc' ? -cmp : cmp
    })
    return sorted
  }, [entries, sortField, sortDir])

  const grouped = useMemo(() => {
    if (groupBy === 'none') return null
    const groups: Record<string, ToolActivityEntry[]> = {}
    for (const e of sortedEntries) {
      const key = groupBy === 'tool' ? e.tool
        : groupBy === 'category' ? e.category
        : groupBy === 'session' ? e.session_id.slice(0, 8)
        : groupBy === 'model' ? (e.model || '(unknown)')
        : e.status
      if (!groups[key]) groups[key] = []
      groups[key].push(e)
    }
    return Object.entries(groups).sort((a, b) => b[1].length - a[1].length)
  }, [sortedEntries, groupBy])

  const resetFilters = useCallback(() => {
    setFilterTool('')
    setFilterCategory('')
    setFilterStatus('')
    setFilterFlagged(false)
    setFilterSession('')
    setFilterModel('')
    setFilterInteractionType('')
    setFilterTimeRange('')
    setOffset(0)
  }, [])

  const handleSort = useCallback((field: SortField) => {
    if (sortField === field) {
      setSortDir(d => d === 'asc' ? 'desc' : 'asc')
    } else {
      setSortField(field)
      setSortDir('desc')
    }
  }, [sortField])

  const activeFilterCount = [filterTool, filterCategory, filterStatus, filterSession, filterModel, filterInteractionType, filterTimeRange]
    .filter(Boolean).length + (filterFlagged ? 1 : 0)

  return (
    <div className="page">
      <div className="page__header">
        <div className="ta__page-title-row">
          <div>
            <h1>Tool Activity</h1>
            <p className="page__subtitle">
              Audit and inspect tool calls, HITL/AITL/PITL interactions, MCP invocations, and model activity across sessions.
            </p>
          </div>
          <div className="ta__page-actions">
            <label className="ta__auto-refresh">
              <input type="checkbox" checked={autoRefresh} onChange={e => setAutoRefresh(e.target.checked)} />
              <span className={`ta__auto-refresh-dot ${autoRefresh ? 'ta__auto-refresh-dot--active' : ''}`} />
              <span>Live</span>
            </label>
            <button className="btn btn--sm btn--outline ta__btn-export" onClick={handleExport} title="Export filtered data as CSV">
              <svg width="14" height="14" viewBox="0 0 16 16" fill="currentColor"><path d="M8 12l-4-4h2.5V3h3v5H12L8 12zm-5 2h10v1H3v-1z"/></svg>
              Export CSV
            </button>
            <button className="btn btn--sm btn--outline" onClick={handleImport} disabled={importing}>
              {importing ? 'Importing...' : 'Import History'}
            </button>
          </div>
        </div>
      </div>

      {/* Risk + Summary Dashboard */}
      {summary && (
        <RiskDashboard
          summary={summary}
          onFilterCategory={c => { setFilterCategory(prev => prev === c ? '' : c); setOffset(0) }}
          onFilterFlagged={() => { setFilterFlagged(prev => !prev); setTab('log') }}
          onFilterDenied={() => { setFilterStatus(prev => prev === 'denied' ? '' : 'denied'); setOffset(0); setTab('log') }}
          onFilterModel={m => { setFilterModel(prev => prev === m ? '' : m); setOffset(0) }}
          activeCategory={filterCategory}
          activeModel={filterModel}
        />
      )}

      {/* Inline panels: Timeline + Categories & Models + Breakdown */}
      <div className="ta__panels-row ta__panels-row--3col">
        <TimelineCompact
          buckets={timeline}
          onFilterCategory={c => { setFilterCategory(prev => prev === c ? '' : c); setOffset(0); setTab('log') }}
          activeCategory={filterCategory}
        />
        {summary && (
          <CategoriesModelsPanel
            summary={summary}
            onFilterCategory={c => { setFilterCategory(prev => prev === c ? '' : c); setOffset(0); setTab('log') }}
            onFilterModel={m => { setFilterModel(prev => prev === m ? '' : m); setOffset(0); setTab('log') }}
            onFilterInteractionType={itl => { setFilterInteractionType(prev => prev === itl ? '' : itl); setOffset(0); setTab('log') }}
            activeCategory={filterCategory}
            activeModel={filterModel}
            activeInteractionType={filterInteractionType}
          />
        )}
        {summary && (
          <BreakdownCompact
            summary={summary}
            onFilterStatus={s => { setFilterStatus(prev => prev === s ? '' : s); setOffset(0); setTab('log') }}
            onFilterTool={t => { setFilterTool(prev => prev === t ? '' : t); setOffset(0); setTab('log') }}
            activeStatus={filterStatus}
            activeTool={filterTool}
          />
        )}
      </div>

      {/* Bottom tabs: Activity Log + Sessions */}
      <div className="ta__tabs">
        {(['log', 'sessions'] as Tab[]).map(t => (
          <button
            key={t}
            className={`ta__tab ${tab === t ? 'ta__tab--active' : ''}`}
            onClick={() => setTab(t)}
          >
            {t === 'log' && 'Activity Log'}
            {t === 'sessions' && 'Sessions'}
            {t === 'log' && total > 0 && <span className="ta__tab-badge">{total}</span>}
            {t === 'sessions' && sessions.length > 0 && <span className="ta__tab-badge">{sessions.length}</span>}
          </button>
        ))}
        {activeFilterCount > 0 && (
          <span className="ta__active-filters-badge">{activeFilterCount} filter{activeFilterCount > 1 ? 's' : ''} active</span>
        )}
      </div>

      {tab === 'log' && (
        <>
          <FilterBar
            filterTool={filterTool}
            filterCategory={filterCategory}
            filterStatus={filterStatus}
            filterFlagged={filterFlagged}
            filterSession={filterSession}
            filterModel={filterModel}
            filterInteractionType={filterInteractionType}
            filterTimeRange={filterTimeRange}
            groupBy={groupBy}
            onFilterTool={v => { setFilterTool(v); setOffset(0) }}
            onFilterCategory={v => { setFilterCategory(v); setOffset(0) }}
            onFilterStatus={v => { setFilterStatus(v); setOffset(0) }}
            onFilterFlagged={v => { setFilterFlagged(v); setOffset(0) }}
            onFilterSession={v => { setFilterSession(v); setOffset(0) }}
            onFilterModel={v => { setFilterModel(v); setOffset(0) }}
            onFilterInteractionType={v => { setFilterInteractionType(v); setOffset(0) }}
            onFilterTimeRange={v => { setFilterTimeRange(v); setOffset(0) }}
            onGroupBy={setGroupBy}
            onReset={resetFilters}
            availableModels={summary?.by_model ? Object.keys(summary.by_model) : []}
          />

          {loading ? (
            <LoadingSkeleton />
          ) : entries.length === 0 ? (
            <EmptyState onImport={handleImport} />
          ) : grouped ? (
            <GroupedView groups={grouped} onSelect={setSelected} onFlag={handleFlag} onUnflag={handleUnflag} groupBy={groupBy} />
          ) : (
            <ActivityTable
              entries={sortedEntries}
              onSelect={setSelected}
              onFlag={handleFlag}
              onUnflag={handleUnflag}
              sortField={sortField}
              sortDir={sortDir}
              onSort={handleSort}
            />
          )}

          {total > limit && (
            <div className="ta__pagination">
              <button className="btn btn--sm btn--outline" disabled={offset === 0} onClick={() => setOffset(Math.max(0, offset - limit))}>
                Previous
              </button>
              <span className="ta__pagination-info">
                Showing <strong>{offset + 1}</strong>--<strong>{Math.min(offset + limit, total)}</strong> of <strong>{total}</strong>
              </span>
              <button className="btn btn--sm btn--outline" disabled={offset + limit >= total} onClick={() => setOffset(offset + limit)}>
                Next
              </button>
            </div>
          )}
        </>
      )}

      {tab === 'sessions' && (
        <SessionsView
          sessions={sessions}
          onDrillDown={sid => { setFilterSession(sid); setTab('log') }}
        />
      )}

      {selected && (
        <DetailModal
          entry={selected}
          onClose={() => setSelected(null)}
          onFlag={handleFlag}
          onUnflag={handleUnflag}
        />
      )}
    </div>
  )
}

/* ------------------------------------------------------------------ */
/*  Risk Dashboard                                                     */
/* ------------------------------------------------------------------ */

function RiskDashboard({ summary, onFilterCategory, onFilterFlagged, onFilterDenied, onFilterModel, activeCategory, activeModel }: {
  summary: ActivitySummary
  onFilterCategory: (c: string) => void
  onFilterFlagged: () => void
  onFilterDenied: () => void
  onFilterModel: (m: string) => void
  activeCategory?: string
  activeModel?: string
}) {
  const deniedCount = summary.by_status?.denied ?? 0
  const riskTotal = summary.risk_high + summary.risk_medium + summary.risk_low
  const riskPct = (n: number) => riskTotal > 0 ? Math.round((n / riskTotal) * 100) : 0

  return (
    <div className="ta__dashboard">
      {/* Primary stats row + risk distribution */}
      <div className="ta__stats-row">
        <div className="card ta__stat-card">
          <div className="ta__stat-icon ta__stat-icon--total">
            <svg width="20" height="20" viewBox="0 0 16 16" fill="currentColor"><path d="M6 2a2 2 0 00-2 2v8a2 2 0 002 2h4a2 2 0 002-2V4a2 2 0 00-2-2H6zm0 1h4a1 1 0 011 1v8a1 1 0 01-1 1H6a1 1 0 01-1-1V4a1 1 0 011-1z"/></svg>
          </div>
          <div className="ta__stat-value">{summary.total.toLocaleString()}</div>
          <div className="ta__stat-label">Total Calls</div>
        </div>

        <button className={`card ta__stat-card ta__stat-card--clickable ${summary.flagged > 0 ? 'ta__stat-card--danger' : ''}`}
          onClick={onFilterFlagged}>
          <div className="ta__stat-icon ta__stat-icon--flagged">
            <svg width="20" height="20" viewBox="0 0 16 16" fill="currentColor"><path d="M3.5 1v14h1V9h8l-2-4 2-4h-9z"/></svg>
          </div>
          <div className="ta__stat-value">{summary.flagged}</div>
          <div className="ta__stat-label">Flagged</div>
        </button>

        <button className={`card ta__stat-card ta__stat-card--clickable ${deniedCount > 0 ? 'ta__stat-card--warn' : ''}`}
          onClick={onFilterDenied}>
          <div className="ta__stat-icon ta__stat-icon--denied">
            <svg width="20" height="20" viewBox="0 0 16 16" fill="currentColor"><path d="M8 1a7 7 0 100 14A7 7 0 008 1zm0 1.2a5.8 5.8 0 014.1 9.9L3.9 3.9A5.77 5.77 0 018 2.2zm0 11.6a5.8 5.8 0 01-4.1-9.9l8.2 8.2A5.77 5.77 0 018 13.8z"/></svg>
          </div>
          <div className="ta__stat-value">{deniedCount}</div>
          <div className="ta__stat-label">Denied</div>
        </button>

        <div className="card ta__stat-card">
          <div className="ta__stat-icon ta__stat-icon--sessions">
            <svg width="20" height="20" viewBox="0 0 16 16" fill="currentColor"><path d="M8 1a7 7 0 100 14A7 7 0 008 1zm0 1a6 6 0 110 12A6 6 0 018 2zm-.5 2v5h4V8h-3V4h-1z"/></svg>
          </div>
          <div className="ta__stat-value">{summary.sessions_with_activity}</div>
          <div className="ta__stat-label">Sessions</div>
        </div>

        <div className="card ta__stat-card">
          <div className="ta__stat-icon ta__stat-icon--speed">
            <svg width="20" height="20" viewBox="0 0 16 16" fill="currentColor"><path d="M8 2a6 6 0 100 12A6 6 0 008 2zM1 8a7 7 0 1114 0A7 7 0 011 8zm7-3v3.5l2.5 1.5-.5.87L7 9V5h1z"/></svg>
          </div>
          <div className="ta__stat-value">{summary.avg_duration_ms > 0 ? formatDuration(summary.avg_duration_ms) : '--'}</div>
          <div className="ta__stat-label">Avg Duration</div>
        </div>

        <div className="card ta__stat-card">
          <div className="ta__stat-icon ta__stat-icon--p95">
            <svg width="20" height="20" viewBox="0 0 16 16" fill="currentColor"><path d="M8 1a7 7 0 100 14A7 7 0 008 1zM2 8a6 6 0 1112 0A6 6 0 012 8zm5.5-4v4.7l3.1 1.8-.5.9L6.5 9V4h1z"/></svg>
          </div>
          <div className="ta__stat-value">{summary.p95_duration_ms > 0 ? formatDuration(summary.p95_duration_ms) : '--'}</div>
          <div className="ta__stat-label">P95 Duration</div>
        </div>

        {/* Risk distribution inline */}
        <div className="card ta__stat-card ta__stat-card--risk">
          <div className="ta__risk-inline-header">
            <span className="ta__risk-inline-title">Risk</span>
            <span className="ta__risk-inline-total">{riskTotal}</span>
          </div>
          <div className="ta__risk-bar ta__risk-bar--inline">
            {summary.risk_high > 0 && (
              <div className="ta__risk-bar-seg ta__risk-bar-seg--high"
                style={{ width: `${riskPct(summary.risk_high)}%` }}
                title={`High: ${summary.risk_high}`} />
            )}
            {summary.risk_medium > 0 && (
              <div className="ta__risk-bar-seg ta__risk-bar-seg--medium"
                style={{ width: `${riskPct(summary.risk_medium)}%` }}
                title={`Medium: ${summary.risk_medium}`} />
            )}
            {summary.risk_low > 0 && (
              <div className="ta__risk-bar-seg ta__risk-bar-seg--low"
                style={{ width: `${riskPct(summary.risk_low)}%` }}
                title={`Low: ${summary.risk_low}`} />
            )}
            {riskTotal === 0 && <div className="ta__risk-bar-seg ta__risk-bar-seg--empty" style={{ width: '100%' }} />}
          </div>
          <div className="ta__risk-legend ta__risk-legend--inline">
            <span className="ta__risk-legend-item"><span className="ta__risk-dot ta__risk-dot--high" /> {summary.risk_high}</span>
            <span className="ta__risk-legend-item"><span className="ta__risk-dot ta__risk-dot--medium" /> {summary.risk_medium}</span>
            <span className="ta__risk-legend-item"><span className="ta__risk-dot ta__risk-dot--low" /> {summary.risk_low}</span>
          </div>
        </div>
      </div>
    </div>
  )
}

/* ------------------------------------------------------------------ */
/*  Filter Bar                                                         */
/* ------------------------------------------------------------------ */

interface FilterBarProps {
  filterTool: string
  filterCategory: string
  filterStatus: string
  filterFlagged: boolean
  filterSession: string
  filterModel: string
  filterInteractionType: string
  filterTimeRange: string
  groupBy: GroupBy
  onFilterTool: (v: string) => void
  onFilterCategory: (v: string) => void
  onFilterStatus: (v: string) => void
  onFilterFlagged: (v: boolean) => void
  onFilterSession: (v: string) => void
  onFilterModel: (v: string) => void
  onFilterInteractionType: (v: string) => void
  onFilterTimeRange: (v: string) => void
  onGroupBy: (v: GroupBy) => void
  onReset: () => void
  availableModels: string[]
}

function FilterBar(p: FilterBarProps) {
  const hasFilters = p.filterTool || p.filterCategory || p.filterStatus || p.filterFlagged || p.filterSession || p.filterModel || p.filterInteractionType || p.filterTimeRange

  return (
    <div className="ta__filters">
      <div className="ta__filter-row">
        <div className="ta__search-wrap">
          <svg className="ta__search-icon" width="14" height="14" viewBox="0 0 16 16" fill="currentColor">
            <path d="M11.5 7a4.5 4.5 0 11-9 0 4.5 4.5 0 019 0zm-.82 4.74a6 6 0 111.06-1.06l3.04 3.04-1.06 1.06-3.04-3.04z"/>
          </svg>
          <input
            className="input ta__filter-input ta__filter-input--search"
            type="text"
            placeholder="Search tool name..."
            value={p.filterTool}
            onChange={e => p.onFilterTool(e.target.value)}
          />
        </div>
        <select className="input ta__filter-select" value={p.filterCategory} onChange={e => p.onFilterCategory(e.target.value)}>
          <option value="">All Categories</option>
          <option value="sdk">SDK</option>
          <option value="custom">Custom</option>
          <option value="mcp">MCP</option>
          <option value="skill">Skill</option>
        </select>
        <select className="input ta__filter-select" value={p.filterStatus} onChange={e => p.onFilterStatus(e.target.value)}>
          <option value="">All Statuses</option>
          <option value="started">Started</option>
          <option value="completed">Completed</option>
          <option value="denied">Denied</option>
          <option value="error">Error</option>
        </select>
        <select className="input ta__filter-select" value={p.filterTimeRange} onChange={e => p.onFilterTimeRange(e.target.value)}>
          <option value="">All Time</option>
          <option value="1h">Last Hour</option>
          <option value="6h">Last 6 Hours</option>
          <option value="24h">Last 24 Hours</option>
          <option value="7d">Last 7 Days</option>
          <option value="30d">Last 30 Days</option>
        </select>
        <select className="input ta__filter-select" value={p.filterModel} onChange={e => p.onFilterModel(e.target.value)}>
          <option value="">All Models</option>
          {p.availableModels.map(m => (
            <option key={m} value={m}>{m}</option>
          ))}
        </select>
        <select className="input ta__filter-select" value={p.filterInteractionType} onChange={e => p.onFilterInteractionType(e.target.value)}>
          <option value="">All Interactions</option>
          <option value="allow">Allow (auto)</option>
          <option value="hitl">HITL</option>
          <option value="aitl">AITL</option>
          <option value="pitl">PITL (Experimental)</option>
          <option value="filter">Prompt Shields</option>
          <option value="deny">Denied</option>
        </select>
      </div>
      <div className="ta__filter-row">
        <input
          className="input ta__filter-input"
          type="text"
          placeholder="Filter by session ID..."
          value={p.filterSession}
          onChange={e => p.onFilterSession(e.target.value)}
        />
        <select className="input ta__filter-select" value={p.groupBy} onChange={e => p.onGroupBy(e.target.value as GroupBy)}>
          <option value="none">No Grouping</option>
          <option value="tool">Group by Tool</option>
          <option value="category">Group by Category</option>
          <option value="session">Group by Session</option>
          <option value="model">Group by Model</option>
          <option value="status">Group by Status</option>
        </select>
        <label className="ta__flag-toggle">
          <input type="checkbox" checked={p.filterFlagged} onChange={e => p.onFilterFlagged(e.target.checked)} />
          <span className="ta__flag-toggle-slider" />
          <span>Flagged only</span>
        </label>
        {hasFilters && (
          <button className="btn btn--sm btn--ghost ta__btn-clear" onClick={p.onReset}>
            <svg width="12" height="12" viewBox="0 0 16 16" fill="currentColor"><path d="M8 1a7 7 0 100 14A7 7 0 008 1zm2.646 4.354L8.707 7.293l1.94 1.94-1.061 1.06-1.94-1.94-1.94 1.94-1.06-1.06 1.94-1.94-1.94-1.94 1.06-1.06 1.94 1.94 1.94-1.94 1.06 1.06z"/></svg>
            Clear Filters
          </button>
        )}
      </div>
    </div>
  )
}

/* ------------------------------------------------------------------ */
/*  Activity Table                                                     */
/* ------------------------------------------------------------------ */

function ActivityTable({ entries, onSelect, onFlag, onUnflag, sortField, sortDir, onSort }: {
  entries: ToolActivityEntry[]
  onSelect: (e: ToolActivityEntry) => void
  onFlag: (id: string, reason: string) => void
  onUnflag: (id: string) => void
  sortField: SortField
  sortDir: SortDir
  onSort: (f: SortField) => void
}) {
  function SortHeader({ field, children }: { field: SortField; children: React.ReactNode }) {
    const active = sortField === field
    return (
      <th className={`ta__th-sort ${active ? 'ta__th-sort--active' : ''}`} onClick={() => onSort(field)}>
        {children}
        <span className="ta__sort-arrow">{active ? (sortDir === 'asc' ? '\u25B2' : '\u25BC') : '\u25BD'}</span>
      </th>
    )
  }

  return (
    <div className="ta__table-wrap">
      <table className="ta__table">
        <thead>
          <tr>
            <SortHeader field="timestamp">Time</SortHeader>
            <SortHeader field="tool">Tool</SortHeader>
            <th>Category</th>
            <SortHeader field="model">Model</SortHeader>
            <SortHeader field="status">Status</SortHeader>
            <th>ITL</th>
            <SortHeader field="risk_score">Risk</SortHeader>
            <SortHeader field="duration_ms">Duration</SortHeader>
            <th>Session</th>
            <th className="ta__col-actions">Actions</th>
          </tr>
        </thead>
        <tbody>
          {entries.map(e => (
            <tr
              key={e.id}
              className={`ta__row ${e.flagged ? 'ta__row--flagged' : ''} ${riskRowClass(e.risk_score)}`}
              onClick={() => onSelect(e)}
            >
              <td className="ta__cell-time">{formatTime(e.timestamp)}</td>
              <td className="ta__cell-tool"><code>{e.tool}</code></td>
              <td><span className={`ta__badge ta__badge--${e.category}`}>{categoryLabel(e.category)}</span></td>
              <td className="ta__cell-model">{e.model ? <code>{e.model}</code> : <span className="ta__muted">--</span>}</td>
              <td><span className={`ta__badge ta__badge--${statusVariant(e.status)}`}>{e.status}</span></td>
              <td>{e.interaction_type ? <span className={`ta__badge ta__badge--itl ta__badge--itl-${e.interaction_type}`}>{interactionLabel(e.interaction_type)}</span> : <span className="ta__muted">--</span>}</td>
              <td className="ta__cell-risk"><RiskIndicator score={e.risk_score} /></td>
              <td className="ta__cell-duration">{e.duration_ms != null ? formatDuration(e.duration_ms) : '--'}</td>
              <td className="ta__cell-session" title={e.session_id}>{e.session_id.slice(0, 8)}</td>
              <td className="ta__col-actions" onClick={ev => ev.stopPropagation()}>
                {e.flagged ? (
                  <button className="ta__action-btn ta__action-btn--unflag" title="Remove flag" onClick={() => onUnflag(e.id)}>
                    <svg width="14" height="14" viewBox="0 0 16 16" fill="currentColor"><path d="M3.5 1v14h1V9h8l-2-4 2-4h-9z"/></svg>
                  </button>
                ) : (
                  <button className="ta__action-btn" title="Flag as suspicious" onClick={() => onFlag(e.id, 'Manually flagged')}>
                    <svg width="14" height="14" viewBox="0 0 16 16" fill="currentColor"><path d="M3.5 1v14h1V9h8l-2-4 2-4h-9zM5 2v6h6.28l-1.5-3 1.5-3H5z"/></svg>
                  </button>
                )}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  )
}

/* ------------------------------------------------------------------ */
/*  Risk Indicator                                                     */
/* ------------------------------------------------------------------ */

function RiskIndicator({ score }: { score: number }) {
  const level = riskLevel(score)
  return (
    <div className={`ta__risk-indicator ta__risk-indicator--${level}`} title={`Risk score: ${score}`}>
      <div className="ta__risk-ring">
        <svg viewBox="0 0 36 36" className="ta__risk-ring-svg">
          <circle cx="18" cy="18" r="15" fill="none" stroke="currentColor" strokeWidth="3" opacity="0.15" />
          <circle
            cx="18" cy="18" r="15" fill="none" stroke="currentColor" strokeWidth="3"
            strokeDasharray={`${score * 0.9425} 94.25`}
            strokeLinecap="round"
            transform="rotate(-90 18 18)"
          />
        </svg>
        <span className="ta__risk-ring-text">{score}</span>
      </div>
    </div>
  )
}

/* ------------------------------------------------------------------ */
/*  Timeline View                                                      */
/* ------------------------------------------------------------------ */

function TimelineView({ buckets }: { buckets: TimelineBucket[] }) {
  if (buckets.length === 0) {
    return (
      <div className="ta__empty">
        <div className="ta__empty-icon">
          <svg width="48" height="48" viewBox="0 0 16 16" fill="currentColor" opacity="0.3"><path d="M1 1v14h14V1H1zm1 1h12v12H2V2zm1 9h1v1H3v-1zm2-2h1v3H5V9zm2-3h1v6H7V6zm2 1h1v5H9V7zm2-4h1v9h-1V3z"/></svg>
        </div>
        <p>No timeline data available yet.</p>
        <p className="ta__empty-hint">Activity will appear here as tool calls are recorded.</p>
      </div>
    )
  }

  const maxTotal = Math.max(1, ...buckets.map(b => b.total))

  return (
    <div className="ta__timeline">
      <div className="card ta__timeline-card">
        <div className="ta__timeline-header">
          <h3>Activity Over Time</h3>
          <div className="ta__timeline-legend">
            <span className="ta__timeline-legend-item"><span className="ta__tl-dot ta__tl-dot--sdk" /> SDK</span>
            <span className="ta__timeline-legend-item"><span className="ta__tl-dot ta__tl-dot--mcp" /> MCP</span>
            <span className="ta__timeline-legend-item"><span className="ta__tl-dot ta__tl-dot--custom" /> Custom</span>
            <span className="ta__timeline-legend-item"><span className="ta__tl-dot ta__tl-dot--skill" /> Skill</span>
            <span className="ta__timeline-legend-item"><span className="ta__tl-dot ta__tl-dot--flagged" /> Flagged</span>
          </div>
        </div>
        <div className="ta__timeline-chart">
          <div className="ta__timeline-y-axis">
            <span>{maxTotal}</span>
            <span>{Math.round(maxTotal / 2)}</span>
            <span>0</span>
          </div>
          <div className="ta__timeline-bars">
            {buckets.map((b, i) => (
              <div key={i} className="ta__timeline-col" title={`${new Date(b.timestamp * 1000).toLocaleString()}\nTotal: ${b.total}\nFlagged: ${b.flagged}`}>
                <div className="ta__timeline-stack" style={{ height: `${(b.total / maxTotal) * 100}%` }}>
                  {b.sdk > 0 && <div className="ta__tl-seg ta__tl-seg--sdk" style={{ flex: b.sdk }} />}
                  {b.mcp > 0 && <div className="ta__tl-seg ta__tl-seg--mcp" style={{ flex: b.mcp }} />}
                  {b.custom > 0 && <div className="ta__tl-seg ta__tl-seg--custom" style={{ flex: b.custom }} />}
                  {b.skill > 0 && <div className="ta__tl-seg ta__tl-seg--skill" style={{ flex: b.skill }} />}
                </div>
                {b.flagged > 0 && <div className="ta__tl-flag-dot" />}
                <span className="ta__timeline-label">{formatBucketLabel(b.timestamp)}</span>
              </div>
            ))}
          </div>
        </div>
      </div>
    </div>
  )
}

/* ------------------------------------------------------------------ */
/*  Sessions View                                                      */
/* ------------------------------------------------------------------ */

function SessionsView({ sessions, onDrillDown }: {
  sessions: SessionBreakdown[]
  onDrillDown: (sessionId: string) => void
}) {
  if (sessions.length === 0) {
    return (
      <div className="ta__empty">
        <p>No session data available.</p>
        <p className="ta__empty-hint">Sessions will appear here as tool activity is recorded.</p>
      </div>
    )
  }

  return (
    <div className="ta__sessions">
      <div className="ta__table-wrap">
        <table className="ta__table ta__sessions-table">
          <thead>
            <tr>
              <th>Session</th>
              <th>Tools Used</th>
              <th>Total Calls</th>
              <th>Flagged</th>
              <th>Max Risk</th>
              <th>Categories</th>
              <th>Models</th>
              <th>Duration</th>
              <th>Last Activity</th>
              <th></th>
            </tr>
          </thead>
          <tbody>
            {sessions.map(s => (
              <tr key={s.session_id} className={`ta__row ${s.flagged_count > 0 ? 'ta__row--flagged' : ''}`}>
                <td className="ta__cell-session" title={s.session_id}>
                  <code>{s.session_id.slice(0, 12)}</code>
                </td>
                <td className="ta__cell-tools-count">{s.unique_tools}</td>
                <td>{s.tool_count}</td>
                <td>{s.flagged_count > 0 ? <span className="ta__flagged-count">{s.flagged_count}</span> : '0'}</td>
                <td><RiskIndicator score={s.max_risk} /></td>
                <td>
                  <div className="ta__cat-chips">
                    {s.categories.map(c => (
                      <span key={c} className={`ta__badge ta__badge--${c} ta__badge--xs`}>{categoryLabel(c)}</span>
                    ))}
                  </div>
                </td>
                <td>
                  <div className="ta__cat-chips">
                    {s.models.map(m => (
                      <span key={m} className="ta__badge ta__badge--model ta__badge--xs">{m}</span>
                    ))}
                  </div>
                </td>
                <td className="ta__cell-duration">{formatDuration(s.total_duration_ms)}</td>
                <td className="ta__cell-time">{formatTime(s.last_activity)}</td>
                <td>
                  <button className="ta__action-btn" onClick={() => onDrillDown(s.session_id)} title="View session activity">
                    <svg width="14" height="14" viewBox="0 0 16 16" fill="currentColor"><path d="M6 3l5 5-5 5V3z"/></svg>
                  </button>
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </div>
  )
}

/* ------------------------------------------------------------------ */
/*  Grouped View                                                       */
/* ------------------------------------------------------------------ */

function GroupedView({ groups, onSelect, onFlag, onUnflag, groupBy }: {
  groups: [string, ToolActivityEntry[]][]
  onSelect: (e: ToolActivityEntry) => void
  onFlag: (id: string, reason: string) => void
  onUnflag: (id: string) => void
  groupBy: GroupBy
}) {
  const [expanded, setExpanded] = useState<Set<string>>(new Set())

  const toggle = (key: string) => {
    setExpanded(prev => {
      const next = new Set(prev)
      if (next.has(key)) next.delete(key)
      else next.add(key)
      return next
    })
  }

  return (
    <div className="ta__groups">
      {groups.map(([key, items]) => {
        const isOpen = expanded.has(key)
        const flaggedCount = items.filter(e => e.flagged).length
        const maxRisk = Math.max(0, ...items.map(e => e.risk_score))
        return (
          <div key={key} className={`ta__group ${flaggedCount > 0 ? 'ta__group--has-flagged' : ''}`}>
            <button className="ta__group-header" onClick={() => toggle(key)}>
              <span className={`ta__group-chevron ${isOpen ? 'ta__group-chevron--open' : ''}`}>
                <svg width="10" height="10" viewBox="0 0 16 16" fill="currentColor"><path d="M6 3l5 5-5 5V3z"/></svg>
              </span>
              <span className="ta__group-title">{groupBy === 'category' ? categoryLabel(key) : key || '(empty)'}</span>
              <span className="ta__group-count">{items.length} call{items.length !== 1 ? 's' : ''}</span>
              {maxRisk > 0 && (
                <span className={`ta__group-risk ta__group-risk--${riskLevel(maxRisk)}`}>
                  Risk {maxRisk}
                </span>
              )}
              {flaggedCount > 0 && <span className="ta__group-flagged">{flaggedCount} flagged</span>}
            </button>
            {isOpen && (
              <div className="ta__group-body">
                <ActivityTable
                  entries={items}
                  onSelect={onSelect}
                  onFlag={onFlag}
                  onUnflag={onUnflag}
                  sortField="timestamp"
                  sortDir="desc"
                  onSort={() => {}}
                />
              </div>
            )}
          </div>
        )
      })}
    </div>
  )
}

/* ------------------------------------------------------------------ */
/*  Breakdown View                                                     */
/* ------------------------------------------------------------------ */

function BreakdownView({ summary }: { summary: ActivitySummary }) {
  const toolEntries = Object.entries(summary.by_tool).sort((a, b) => b[1] - a[1])
  const maxCount = toolEntries.length > 0 ? toolEntries[0][1] : 1

  return (
    <div className="ta__breakdown">
      <div className="card ta__perf-card">
        <h3>Performance</h3>
        <div className="ta__perf-grid">
          <div className="ta__perf-item">
            <span className="ta__perf-val">{formatDuration(summary.avg_duration_ms)}</span>
            <span className="ta__perf-label">Average</span>
          </div>
          <div className="ta__perf-item">
            <span className="ta__perf-val">{formatDuration(summary.p95_duration_ms)}</span>
            <span className="ta__perf-label">P95</span>
          </div>
          <div className="ta__perf-item">
            <span className="ta__perf-val">{formatDuration(summary.max_duration_ms)}</span>
            <span className="ta__perf-label">Max</span>
          </div>
        </div>
      </div>

      <div className="card">
        <h3>Tool Usage Distribution</h3>
        {toolEntries.length === 0 ? (
          <p className="ta__empty-hint">No tool usage recorded yet.</p>
        ) : (
          <div className="ta__bar-chart">
            {toolEntries.map(([name, count]) => (
              <div key={name} className="ta__bar-row">
                <span className="ta__bar-label"><code>{name}</code></span>
                <div className="ta__bar-track">
                  <div className="ta__bar-fill" style={{ width: `${(count / maxCount) * 100}%` }} />
                </div>
                <span className="ta__bar-count">{count}</span>
              </div>
            ))}
          </div>
        )}
      </div>

      <div className="ta__breakdown-grid">
        <div className="card">
          <h3>By Category</h3>
          <dl className="ta__dl">
            {Object.entries(summary.by_category).map(([k, v]) => (
              <div key={k} className="ta__dl-row">
                <dt><span className={`ta__badge ta__badge--${k}`}>{categoryLabel(k)}</span></dt>
                <dd>{v}</dd>
              </div>
            ))}
          </dl>
        </div>
        <div className="card">
          <h3>By Status</h3>
          <dl className="ta__dl">
            {Object.entries(summary.by_status).map(([k, v]) => (
              <div key={k} className="ta__dl-row">
                <dt><span className={`ta__badge ta__badge--${statusVariant(k)}`}>{k}</span></dt>
                <dd>{v}</dd>
              </div>
            ))}
          </dl>
        </div>
        <div className="card">
          <h3>By Model</h3>
          {Object.keys(summary.by_model).length === 0 ? (
            <p className="ta__empty-hint">No model data recorded yet.</p>
          ) : (
            <dl className="ta__dl">
              {Object.entries(summary.by_model)
                .sort((a, b) => b[1] - a[1])
                .map(([k, v]) => (
                  <div key={k} className="ta__dl-row">
                    <dt><span className="ta__badge ta__badge--model">{k}</span></dt>
                    <dd>{v}</dd>
                  </div>
                ))}
            </dl>
          )}
        </div>
      </div>
    </div>
  )
}

/* ------------------------------------------------------------------ */
/*  Categories & Models Panel (inline)                                 */
/* ------------------------------------------------------------------ */

function CategoriesModelsPanel({ summary, onFilterCategory, onFilterModel, onFilterInteractionType, activeCategory, activeModel, activeInteractionType }: {
  summary: ActivitySummary
  onFilterCategory: (c: string) => void
  onFilterModel: (m: string) => void
  onFilterInteractionType: (itl: string) => void
  activeCategory: string
  activeModel: string
  activeInteractionType: string
}) {
  const hasItl = Object.keys(summary.by_interaction_type || {}).length > 0
  return (
    <div className="card ta__panel-card">
      <h3 className="ta__panel-title" style={{ marginBottom: '10px' }}>Categories & Models</h3>
      <div className="ta__chips-wrap ta__chips-wrap--compact">
        {Object.entries(summary.by_category).map(([cat, count]) => (
          <button
            key={cat}
            className={`ta__chip ta__chip--sm ta__chip--${cat} ${activeCategory === cat ? 'ta__chip--active' : ''}`}
            onClick={() => onFilterCategory(cat)}
          >
            <span className="ta__chip-label">{categoryLabel(cat)}</span>
            <span className="ta__chip-count">{count}</span>
          </button>
        ))}
        {Object.entries(summary.by_model)
          .sort((a, b) => b[1] - a[1])
          .map(([model, count]) => (
            <button
              key={model}
              className={`ta__chip ta__chip--sm ta__chip--model ${activeModel === model ? 'ta__chip--active' : ''}`}
              onClick={() => onFilterModel(model)}
            >
              <span className="ta__chip-label">{model}</span>
              <span className="ta__chip-count">{count}</span>
            </button>
          ))}
      </div>
      {hasItl && (
        <>
          <h3 className="ta__panel-title" style={{ marginTop: '12px', marginBottom: '8px' }}>Interaction Types</h3>
          <div className="ta__chips-wrap ta__chips-wrap--compact">
            {Object.entries(summary.by_interaction_type)
              .sort((a, b) => b[1] - a[1])
              .map(([itl, count]) => (
                <button
                  key={itl}
                  className={`ta__chip ta__chip--sm ta__chip--itl-${itl} ${activeInteractionType === itl ? 'ta__chip--active' : ''}`}
                  onClick={() => onFilterInteractionType(itl)}
                >
                  <span className="ta__chip-label">{interactionLabel(itl)}</span>
                  <span className="ta__chip-count">{count}</span>
                </button>
              ))}
          </div>
        </>
      )}
    </div>
  )
}

/* ------------------------------------------------------------------ */
/*  Timeline Compact (inline panel)                                    */
/* ------------------------------------------------------------------ */

function TimelineCompact({ buckets, onFilterCategory, activeCategory }: {
  buckets: TimelineBucket[]
  onFilterCategory: (c: string) => void
  activeCategory: string
}) {
  const cats: { key: string; label: string; cls: string }[] = [
    { key: 'sdk', label: 'SDK', cls: 'ta__tl-dot--sdk' },
    { key: 'mcp', label: 'MCP', cls: 'ta__tl-dot--mcp' },
    { key: 'custom', label: 'Custom', cls: 'ta__tl-dot--custom' },
    { key: 'skill', label: 'Skill', cls: 'ta__tl-dot--skill' },
  ]

  if (buckets.length === 0) {
    return (
      <div className="card ta__panel-card">
        <h3 className="ta__panel-title">Activity Over Time</h3>
        <p className="text-muted" style={{ fontSize: '12px', marginTop: '8px' }}>No timeline data yet.</p>
      </div>
    )
  }

  const maxTotal = Math.max(1, ...buckets.map(b => b.total))

  return (
    <div className="card ta__panel-card">
      <div className="ta__panel-header">
        <h3 className="ta__panel-title">Activity Over Time</h3>
        <div className="ta__timeline-legend ta__timeline-legend--compact">
          {cats.map(c => (
            <button
              key={c.key}
              className={`ta__timeline-legend-btn ${activeCategory === c.key ? 'ta__timeline-legend-btn--active' : ''}`}
              onClick={() => onFilterCategory(c.key)}
            >
              <span className={`ta__tl-dot ${c.cls}`} />
              <span>{c.label}</span>
            </button>
          ))}
        </div>
      </div>
      <div className="ta__timeline-chart ta__timeline-chart--compact">
        <div className="ta__timeline-y-axis">
          <span>{maxTotal}</span>
          <span>{Math.round(maxTotal / 2)}</span>
          <span>0</span>
        </div>
        <div className="ta__timeline-bars">
          {buckets.map((b, i) => (
            <div key={i} className="ta__timeline-col" title={`${new Date(b.timestamp * 1000).toLocaleString()}\nTotal: ${b.total}`}>
              {b.flagged > 0 && <div className="ta__tl-flag-dot" />}
              <div className="ta__timeline-stack" style={{ height: `${(b.total / maxTotal) * 100}%` }}>
                {b.sdk > 0 && <div className={`ta__tl-seg ta__tl-seg--sdk${activeCategory && activeCategory !== 'sdk' ? ' ta__tl-seg--dim' : ''}`} style={{ flex: b.sdk }} />}
                {b.mcp > 0 && <div className={`ta__tl-seg ta__tl-seg--mcp${activeCategory && activeCategory !== 'mcp' ? ' ta__tl-seg--dim' : ''}`} style={{ flex: b.mcp }} />}
                {b.custom > 0 && <div className={`ta__tl-seg ta__tl-seg--custom${activeCategory && activeCategory !== 'custom' ? ' ta__tl-seg--dim' : ''}`} style={{ flex: b.custom }} />}
                {b.skill > 0 && <div className={`ta__tl-seg ta__tl-seg--skill${activeCategory && activeCategory !== 'skill' ? ' ta__tl-seg--dim' : ''}`} style={{ flex: b.skill }} />}
              </div>
              <span className="ta__timeline-label">{formatBucketLabel(b.timestamp)}</span>
            </div>
          ))}
        </div>
      </div>
    </div>
  )
}

/* ------------------------------------------------------------------ */
/*  Breakdown Compact (inline panel)                                   */
/* ------------------------------------------------------------------ */

function BreakdownCompact({ summary, onFilterStatus, onFilterTool, activeStatus, activeTool }: {
  summary: ActivitySummary
  onFilterStatus: (s: string) => void
  onFilterTool: (t: string) => void
  activeStatus: string
  activeTool: string
}) {
  const toolEntries = Object.entries(summary.by_tool).sort((a, b) => b[1] - a[1]).slice(0, 5)
  const maxCount = toolEntries.length > 0 ? toolEntries[0][1] : 1

  return (
    <div className="card ta__panel-card">
      <h3 className="ta__panel-title" style={{ marginBottom: '12px' }}>Breakdown</h3>

      <div className="ta__bd-section">
        <div className="ta__bd-items">
          {Object.entries(summary.by_status).map(([k, v]) => (
            <button
              key={k}
              className={`ta__bd-item ta__bd-item--${statusVariant(k)} ${activeStatus === k ? 'ta__bd-item--active' : ''}`}
              onClick={() => onFilterStatus(k)}
            >
              <span>{k}</span>
              <span className="ta__bd-count">{v}</span>
            </button>
          ))}
        </div>
      </div>

      {toolEntries.length > 0 && (
        <div className="ta__bd-section">
          <span className="ta__bd-label">Top Tools</span>
          <div className="ta__bd-tools">
            {toolEntries.map(([name, count]) => (
              <button
                key={name}
                className={`ta__bd-tool ${activeTool === name ? 'ta__bd-tool--active' : ''}`}
                onClick={() => onFilterTool(name)}
              >
                <span className="ta__bd-tool-name"><code>{name}</code></span>
                <div className="ta__bd-tool-bar">
                  <div className="ta__bd-tool-fill" style={{ width: `${(count / maxCount) * 100}%` }} />
                </div>
                <span className="ta__bd-count">{count}</span>
              </button>
            ))}
          </div>
        </div>
      )}
    </div>
  )
}

/* ------------------------------------------------------------------ */
/*  Detail Modal                                                       */
/* ------------------------------------------------------------------ */

function DetailModal({ entry, onClose, onFlag, onUnflag }: {
  entry: ToolActivityEntry
  onClose: () => void
  onFlag: (id: string, reason: string) => void
  onUnflag: (id: string) => void
}) {
  const [flagReason, setFlagReason] = useState('')

  return (
    <div className="modal-overlay" onClick={onClose}>
      <div className="modal ta__detail" onClick={e => e.stopPropagation()}>
        <div className="modal__header">
          <div className="ta__detail-title-row">
            <h2>Tool Call Detail</h2>
            {entry.risk_score > 0 && (
              <span className={`ta__detail-risk-badge ta__detail-risk-badge--${riskLevel(entry.risk_score)}`}>
                Risk {entry.risk_score}
              </span>
            )}
          </div>
          <button className="btn btn--ghost" onClick={onClose}>&times;</button>
        </div>

        <div className="ta__detail-body">
          {/* Risk Assessment Section */}
          {(entry.risk_score > 0 || entry.risk_factors.length > 0) && (
            <div className={`ta__risk-assessment ta__risk-assessment--${riskLevel(entry.risk_score)}`}>
              <div className="ta__risk-assessment-header">
                <RiskIndicator score={entry.risk_score} />
                <div>
                  <div className="ta__risk-assessment-title">Risk Assessment</div>
                  <div className="ta__risk-assessment-level">{riskLabel(entry.risk_score)}</div>
                </div>
              </div>
              {entry.risk_factors.length > 0 && (
                <ul className="ta__risk-factors">
                  {entry.risk_factors.map((f, i) => (
                    <li key={i} className="ta__risk-factor">{f}</li>
                  ))}
                </ul>
              )}
            </div>
          )}

          <div className="ta__detail-meta">
            <MetaRow label="Tool" value={entry.tool} />
            <MetaRow label="Model" value={entry.model || '--'} mono />
            <MetaRow label="Category" value={categoryLabel(entry.category)} badgeClass={`ta__badge--${entry.category}`} />
            <MetaRow label="Status" value={entry.status} badgeClass={`ta__badge--${statusVariant(entry.status)}`} />
            {entry.interaction_type && <MetaRow label="Interaction" value={interactionLabel(entry.interaction_type)} badgeClass={`ta__badge--itl ta__badge--itl-${entry.interaction_type}`} />}
            <MetaRow label="Call ID" value={entry.call_id || '--'} mono />
            <MetaRow label="Session" value={entry.session_id} mono />
            <MetaRow label="Timestamp" value={new Date(entry.timestamp * 1000).toLocaleString()} />
            <MetaRow label="Duration" value={entry.duration_ms != null ? formatDuration(entry.duration_ms) : '--'} />
            {entry.flagged && <MetaRow label="Flag Reason" value={entry.flag_reason} flagged />}
          </div>

          <div className="ta__detail-section">
            <h3>Arguments</h3>
            <pre className="ta__code-block">{formatJson(entry.arguments)}</pre>
          </div>

          <div className="ta__detail-section">
            <h3>Result</h3>
            <pre className="ta__code-block">{formatJson(entry.result)}</pre>
          </div>

          <div className="ta__detail-actions">
            {entry.flagged ? (
              <button className="btn btn--sm btn--outline" onClick={() => { onUnflag(entry.id); onClose() }}>
                Remove Flag
              </button>
            ) : (
              <div className="ta__flag-form-row">
                <input
                  className="input"
                  type="text"
                  placeholder="Reason for flagging..."
                  value={flagReason}
                  onChange={e => setFlagReason(e.target.value)}
                />
                <button
                  className="btn btn--danger btn--sm"
                  onClick={() => { onFlag(entry.id, flagReason || 'Manually flagged'); onClose() }}
                >
                  Flag Suspicious
                </button>
              </div>
            )}
          </div>
        </div>
      </div>
    </div>
  )
}

/* ------------------------------------------------------------------ */
/*  Shared sub-components                                              */
/* ------------------------------------------------------------------ */

function MetaRow({ label, value, flagged, mono, badgeClass }: {
  label: string; value: string; flagged?: boolean; mono?: boolean; badgeClass?: string
}) {
  return (
    <div className={`ta__meta-row ${flagged ? 'ta__meta-row--flagged' : ''}`}>
      <span className="ta__meta-label">{label}</span>
      {badgeClass ? (
        <span className={`ta__badge ${badgeClass}`}>{value}</span>
      ) : (
        <span className={`ta__meta-value ${mono ? 'ta__meta-value--mono' : ''}`}>{value}</span>
      )}
    </div>
  )
}

function LoadingSkeleton() {
  return (
    <div className="ta__skeleton">
      {Array.from({ length: 8 }).map((_, i) => (
        <div key={i} className="ta__skeleton-row">
          <div className="ta__skeleton-cell ta__skeleton-cell--sm" />
          <div className="ta__skeleton-cell ta__skeleton-cell--md" />
          <div className="ta__skeleton-cell ta__skeleton-cell--sm" />
          <div className="ta__skeleton-cell ta__skeleton-cell--sm" />
          <div className="ta__skeleton-cell ta__skeleton-cell--xs" />
        </div>
      ))}
    </div>
  )
}

function EmptyState({ onImport }: { onImport: () => void }) {
  return (
    <div className="ta__empty">
      <div className="ta__empty-icon">
        <svg width="64" height="64" viewBox="0 0 16 16" fill="currentColor" opacity="0.15">
          <path d="M6 2a2 2 0 00-2 2v8a2 2 0 002 2h4a2 2 0 002-2V4a2 2 0 00-2-2H6zm0 1h4a1 1 0 011 1v8a1 1 0 01-1 1H6a1 1 0 01-1-1V4a1 1 0 011-1zm.5 2a.5.5 0 000 1h3a.5.5 0 000-1h-3zm0 2a.5.5 0 000 1h3a.5.5 0 000-1h-3zm0 2a.5.5 0 000 1h2a.5.5 0 000-1h-2z"/>
        </svg>
      </div>
      <p className="ta__empty-title">No tool activity found</p>
      <p className="ta__empty-hint">Tool calls will appear here as the agent executes actions.</p>
      <button className="btn btn--sm btn--outline ta__empty-btn" onClick={onImport}>Import from existing sessions</button>
    </div>
  )
}

/* ------------------------------------------------------------------ */
/*  Helpers                                                            */
/* ------------------------------------------------------------------ */

function formatTime(ts: number): string {
  if (!ts) return '--'
  const d = new Date(ts * 1000)
  const now = new Date()
  if (d.toDateString() === now.toDateString()) {
    return d.toLocaleTimeString()
  }
  return d.toLocaleDateString() + ' ' + d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
}

function formatDuration(ms: number): string {
  if (!ms || ms <= 0) return '--'
  if (ms < 1000) return `${Math.round(ms)}ms`
  if (ms < 60000) return `${(ms / 1000).toFixed(1)}s`
  return `${(ms / 60000).toFixed(1)}m`
}

function formatBucketLabel(ts: number): string {
  const d = new Date(ts * 1000)
  return d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' })
}

function formatJson(raw: string): string {
  if (!raw) return '(none)'
  try {
    return JSON.stringify(JSON.parse(raw), null, 2)
  } catch {
    return raw
  }
}

function categoryLabel(cat: string): string {
  const labels: Record<string, string> = { sdk: 'SDK', custom: 'Custom', mcp: 'MCP', skill: 'Skill' }
  return labels[cat] || cat
}

function interactionLabel(itl: string): string {
  const labels: Record<string, string> = {
    hitl: 'HITL', aitl: 'AITL', pitl: 'PITL (Experimental)',
    filter: 'Prompt Shields', deny: 'Denied',
  }
  return labels[itl] || itl.toUpperCase()
}

function statusVariant(status: string): string {
  const map: Record<string, string> = { started: 'warn', completed: 'ok', denied: 'err', error: 'err' }
  return map[status] || 'default'
}

function riskLevel(score: number): 'high' | 'medium' | 'low' | 'none' {
  if (score >= 70) return 'high'
  if (score >= 30) return 'medium'
  if (score > 0) return 'low'
  return 'none'
}

function riskLabel(score: number): string {
  if (score >= 70) return 'High Risk'
  if (score >= 30) return 'Medium Risk'
  if (score > 0) return 'Low Risk'
  return 'No Risk Detected'
}

function riskRowClass(score: number): string {
  if (score >= 70) return 'ta__row--risk-high'
  if (score >= 30) return 'ta__row--risk-medium'
  return ''
}
