import { useState, useEffect } from 'react'
import { api } from '../api'
import { showToast } from '../components/Toast'
import type { Deployment } from '../types'

export function EnvironmentsContent() {
  const [deployments, setDeployments] = useState<Deployment[]>([])
  const [loading, setLoading] = useState(true)
  const [selectedId, setSelectedId] = useState<string | null>(null)
  const [detail, setDetail] = useState<Deployment | null>(null)
  const [auditResults, setAuditResults] = useState<any>(null)
  const [auditing, setAuditing] = useState(false)

  const load = async () => {
    setLoading(true)
    try {
      const r = await api<Deployment[]>('environments')
      setDeployments(Array.isArray(r) ? r : (r as any).deployments || [])
    } catch { /* ignore */ }
    setLoading(false)
  }

  useEffect(() => { load() }, [])

  const loadDetail = async (id: string) => {
    setSelectedId(id)
    try {
      const d = await api<Deployment>(`environments/${id}`)
      setDetail(d)
    } catch (e: any) { showToast(e.message, 'error') }
  }

  const destroyDeployment = async () => {
    if (!selectedId || !confirm(`Destroy deployment ${selectedId}? This will delete all associated Azure resource groups.`)) return
    try {
      await api(`environments/${selectedId}`, { method: 'DELETE' })
      showToast('Deployment destroyed', 'success')
      setSelectedId(null)
      setDetail(null)
      load()
    } catch (e: any) { showToast(e.message, 'error') }
  }

  const runAudit = async () => {
    setAuditing(true)
    try {
      const r = await api<any>('environments/audit', { method: 'POST' })
      setAuditResults(r)
    } catch (e: any) { showToast(e.message, 'error') }
    setAuditing(false)
  }

  return (
    <>
      <div className="page__header">
        <h1>Environments</h1>
        <div className="page__actions">
          <button className="btn btn--secondary btn--sm" onClick={runAudit} disabled={auditing}>
            {auditing ? 'Auditing...' : 'Run Audit'}
          </button>
          <button className="btn btn--ghost btn--sm" onClick={load}>Refresh</button>
        </div>
      </div>

      {loading && <div className="spinner" />}

      {!loading && deployments.length === 0 && (
        <p className="text-muted">No deployments registered. Deployments are automatically tracked when you provision infrastructure.</p>
      )}

      {deployments.filter(d => d.resource_count > 0).length > 0 && (
        <div className="table-container">
          <table className="table">
            <thead>
              <tr>
                <th>Deploy ID</th>
                <th>Tag</th>
                <th>Kind</th>
                <th>Status</th>
                <th>Resources</th>
                <th>Updated</th>
              </tr>
            </thead>
            <tbody>
              {deployments.filter(d => d.resource_count > 0).map(d => (
                <tr key={d.deploy_id} className={selectedId === d.deploy_id ? 'table__row--selected' : ''} onClick={() => loadDetail(d.deploy_id)} style={{ cursor: 'pointer' }}>
                  <td><code>{d.deploy_id}</code></td>
                  <td><code>{d.tag}</code></td>
                  <td>{d.kind}</td>
                  <td><span className={`badge ${d.status === 'active' ? 'badge--ok' : d.status === 'destroyed' ? 'badge--err' : 'badge--muted'}`}>{d.status}</span></td>
                  <td>{d.resource_count}</td>
                  <td>{d.updated_at?.slice(0, 19)}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}

      {detail && selectedId && (
        <div className="card mt-2">
          <div className="card__header">
            <h3>Deployment {selectedId}</h3>
            <button className="btn btn--danger btn--sm" onClick={destroyDeployment}>Destroy</button>
          </div>
          <div className="detail-grid">
            <div><strong>Tag:</strong> <code>{detail.tag}</code></div>
            <div><strong>Kind:</strong> {detail.kind}</div>
            <div><strong>Status:</strong> {detail.status}</div>
            <div><strong>Created:</strong> {detail.created_at?.slice(0, 19)}</div>
            <div><strong>RGs:</strong> {(detail.resource_groups || []).join(', ') || '-'}</div>
          </div>
          {detail.resources && detail.resources.length > 0 && (
            <>
              <h4 className="mt-2">Resources</h4>
              <div className="table-container">
                <table className="table">
                  <thead><tr><th>Type</th><th>Name</th><th>RG</th><th>Purpose</th></tr></thead>
                  <tbody>
                    {detail.resources.map((r, i) => (
                      <tr key={i}>
                        <td>{r.resource_type}</td>
                        <td><code>{r.resource_name}</code></td>
                        <td>{r.resource_group}</td>
                        <td>{r.purpose || '-'}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            </>
          )}
        </div>
      )}

      {auditResults && (
        <div className="card mt-2">
          <h3>Audit Results</h3>
          <div className="detail-grid">
            <div><strong>Tracked Resources:</strong> {auditResults.tracked_resources?.length || 0}</div>
            <div><strong>Orphaned Resources:</strong> {auditResults.orphaned_resources?.length || 0}</div>
            <div><strong>Orphaned RGs:</strong> {auditResults.orphaned_groups?.length || 0}</div>
          </div>
          {(!auditResults.orphaned_groups?.length && !auditResults.orphaned_resources?.length) && (
            <p className="text-ok mt-1">No orphaned resources found.</p>
          )}
          {auditResults.orphaned_groups?.length > 0 && (
            <>
              <h4 className="mt-2 text-err">Orphaned Resource Groups</h4>
              <div className="list">
                {auditResults.orphaned_groups.map((g: any) => (
                  <div key={g.name} className="list-item">
                    <div className="list-item__body">
                      <strong>{g.name}</strong> <span className="text-muted">({g.location})</span>
                    </div>
                  </div>
                ))}
              </div>
            </>
          )}
        </div>
      )}
    </>
  )
}

export default function Environments() {
  return <div className="page"><EnvironmentsContent /></div>
}
