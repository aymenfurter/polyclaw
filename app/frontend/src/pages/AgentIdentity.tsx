import { useState, useEffect, useCallback } from 'react'
import { api } from '../api'
import type { ApiResponse } from '../types'

/* ------------------------------------------------------------------ */
/*  Types                                                              */
/* ------------------------------------------------------------------ */

interface IdentityInfo extends ApiResponse {
  configured: boolean
  strategy: string | null
  app_id: string
  mi_client_id: string
  tenant: string
  display_name: string
  principal_id: string
  principal_type: string
}

interface RoleAssignment {
  role: string
  scope: string
  condition: string
}

interface RoleCheck {
  feature: string
  role: string
  present: boolean
  data_action: string
}

interface RolesResponse extends ApiResponse {
  assignments: RoleAssignment[]
  checks: RoleCheck[]
  message?: string
}

interface FixStep {
  step: string
  status: string
  detail: string
}

interface FixResponse extends ApiResponse {
  steps: FixStep[]
}

/* ------------------------------------------------------------------ */
/*  Main Component                                                     */
/* ------------------------------------------------------------------ */

export default function AgentIdentity() {
  const [identity, setIdentity] = useState<IdentityInfo | null>(null)
  const [roles, setRoles] = useState<RolesResponse | null>(null)
  const [loadingId, setLoadingId] = useState(true)
  const [loadingRoles, setLoadingRoles] = useState(false)
  const [fixing, setFixing] = useState(false)
  const [fixResult, setFixResult] = useState<FixStep[] | null>(null)
  const [error, setError] = useState('')

  const fetchIdentity = useCallback(() => {
    setLoadingId(true)
    api<IdentityInfo>('identity/info')
      .then(setIdentity)
      .catch(() => setError('Failed to load identity'))
      .finally(() => setLoadingId(false))
  }, [])

  const fetchRoles = useCallback(() => {
    setLoadingRoles(true)
    setError('')
    api<RolesResponse>('identity/roles')
      .then(r => {
        setRoles(r)
        setFixResult(null)
      })
      .catch(() => setError('Failed to load roles'))
      .finally(() => setLoadingRoles(false))
  }, [])

  useEffect(() => { fetchIdentity() }, [fetchIdentity])

  const handleFixRoles = useCallback(() => {
    setFixing(true)
    setError('')
    api<FixResponse>('identity/fix-roles', { method: 'POST' })
      .then(r => {
        setFixResult(r.steps)
        fetchRoles()
      })
      .catch(() => setError('Fix request failed'))
      .finally(() => setFixing(false))
  }, [fetchRoles])

  const hasMissing = roles?.checks?.some(c => !c.present) ?? false

  return (
    <div className="page">
      <div className="page__header">
        <h1>Agent Identity</h1>
      </div>
      <p className="text-muted" style={{ marginBottom: 20, fontSize: 13, lineHeight: 1.5 }}>
        The runtime identity used by the agent for Azure API calls.
        Review RBAC assignments and ensure required roles are present.
      </p>

      {error && <div className="aid__error">{error}</div>}

      <div className="aid__grid">
        <IdentityCard identity={identity} loading={loadingId} onRefresh={fetchIdentity} />
        <RoleChecksCard
          roles={roles}
          loading={loadingRoles}
          hasMissing={hasMissing}
          fixing={fixing}
          fixResult={fixResult}
          onLoad={fetchRoles}
          onFix={handleFixRoles}
        />
      </div>

      {roles && roles.assignments.length > 0 && (
        <AssignmentsTable assignments={roles.assignments} />
      )}
    </div>
  )
}

/* ------------------------------------------------------------------ */
/*  Sub-components                                                     */
/* ------------------------------------------------------------------ */

function IdentityCard({
  identity,
  loading,
  onRefresh,
}: {
  identity: IdentityInfo | null
  loading: boolean
  onRefresh: () => void
}) {
  if (loading) {
    return (
      <div className="card">
        <h3>Runtime Identity</h3>
        <p className="text-muted">Loading...</p>
      </div>
    )
  }

  if (!identity || !identity.configured) {
    return (
      <div className="card">
        <div className="card__header">
          <h3>Runtime Identity</h3>
          <button className="btn btn--sm btn--outline" onClick={onRefresh}>Refresh</button>
        </div>
        <p className="text-muted">
          No identity configured. Set <code className="aid__code">RUNTIME_SP_APP_ID</code> or{' '}
          <code className="aid__code">ACA_MI_CLIENT_ID</code> to enable.
        </p>
      </div>
    )
  }

  const strategyLabel = identity.strategy === 'managed_identity'
    ? 'User-assigned Managed Identity'
    : identity.strategy === 'service_principal'
      ? 'Service Principal'
      : 'Unknown'

  return (
    <div className="card">
      <div className="card__header">
        <h3>Runtime Identity</h3>
        <button className="btn btn--sm btn--outline" onClick={onRefresh}>Refresh</button>
      </div>
      <div className="aid__fields">
        <Field label="Display Name" value={identity.display_name || '(not resolved)'} />
        <Field label="Strategy" value={strategyLabel} />
        {identity.app_id && <Field label="App ID" value={identity.app_id} mono />}
        {identity.mi_client_id && <Field label="MI Client ID" value={identity.mi_client_id} mono />}
        {identity.principal_id && <Field label="Principal Object ID" value={identity.principal_id} mono />}
        {identity.tenant && <Field label="Tenant" value={identity.tenant} mono />}
        {identity.principal_type && <Field label="Principal Type" value={identity.principal_type} />}
      </div>
    </div>
  )
}

function Field({ label, value, mono }: { label: string; value: string; mono?: boolean }) {
  return (
    <div className="aid__field">
      <span className="aid__field-label">{label}</span>
      <span className={mono ? 'aid__field-value aid__field-value--mono' : 'aid__field-value'}>
        {value}
      </span>
    </div>
  )
}

function RoleChecksCard({
  roles,
  loading,
  hasMissing,
  fixing,
  fixResult,
  onLoad,
  onFix,
}: {
  roles: RolesResponse | null
  loading: boolean
  hasMissing: boolean
  fixing: boolean
  fixResult: FixStep[] | null
  onLoad: () => void
  onFix: () => void
}) {
  return (
    <div className="card">
      <div className="card__header">
        <h3>Required Roles</h3>
        <div style={{ display: 'flex', gap: 8 }}>
          {hasMissing && (
            <button
              className="btn btn--sm btn--primary"
              onClick={onFix}
              disabled={fixing}
            >
              {fixing ? 'Fixing...' : 'Fix Missing Roles'}
            </button>
          )}
          <button
            className="btn btn--sm btn--outline"
            onClick={onLoad}
            disabled={loading}
          >
            {loading ? 'Checking...' : roles ? 'Re-check' : 'Check Roles'}
          </button>
        </div>
      </div>

      {!roles && !loading && (
        <p className="text-muted" style={{ fontSize: 13 }}>
          Click "Check Roles" to audit the agent's RBAC assignments against
          required permissions.
        </p>
      )}

      {loading && <p className="text-muted">Loading role assignments...</p>}

      {roles?.message && !roles.checks?.length && (
        <p className="text-muted">{roles.message}</p>
      )}

      {roles?.checks && roles.checks.length > 0 && (
        <div className="aid__checks">
          {roles.checks.map(c => (
            <div key={c.role} className="aid__check-row">
              <span className={`aid__check-dot ${c.present ? 'aid__check-dot--ok' : 'aid__check-dot--err'}`} />
              <div className="aid__check-info">
                <span className="aid__check-role">{c.role}</span>
                <span className="aid__check-feature">{c.feature}</span>
                {c.data_action && (
                  <span className="aid__check-action">{c.data_action}</span>
                )}
              </div>
              <span className={`badge ${c.present ? 'badge--ok' : 'badge--err'}`}>
                {c.present ? 'Assigned' : 'Missing'}
              </span>
            </div>
          ))}
        </div>
      )}

      {fixResult && fixResult.length > 0 && (
        <div className="aid__fix-results">
          <h4 style={{ marginTop: 16, marginBottom: 8, fontSize: 13 }}>Fix Results</h4>
          {fixResult.map((s, i) => (
            <div key={i} className="aid__fix-step">
              <span className={`badge badge--sm ${s.status === 'ok' ? 'badge--ok' : s.status === 'failed' ? 'badge--err' : 'badge--warn'}`}>
                {s.status}
              </span>
              <span className="aid__fix-detail">{s.detail}</span>
            </div>
          ))}
        </div>
      )}
    </div>
  )
}

function AssignmentsTable({ assignments }: { assignments: RoleAssignment[] }) {
  return (
    <div className="card" style={{ marginTop: 20 }}>
      <h3>All Role Assignments</h3>
      <p className="text-muted" style={{ fontSize: 12, marginBottom: 12 }}>
        {assignments.length} assignment{assignments.length !== 1 ? 's' : ''} found
      </p>
      <div className="table-wrap">
        <table className="table" style={{ width: '100%' }}>
          <thead>
            <tr>
              <th>Role</th>
              <th>Scope</th>
            </tr>
          </thead>
          <tbody>
            {assignments.map((a, i) => (
              <tr key={i}>
                <td style={{ whiteSpace: 'nowrap' }}>{a.role}</td>
                <td style={{ fontSize: 12, fontFamily: 'var(--font-mono)', wordBreak: 'break-all' }}>
                  {formatScope(a.scope)}
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
/*  Helpers                                                            */
/* ------------------------------------------------------------------ */

function formatScope(scope: string): string {
  // Show the last two segments to keep it readable
  const parts = scope.split('/')
  if (parts.length <= 4) return scope
  return '.../' + parts.slice(-4).join('/')
}
