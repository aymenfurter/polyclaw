import { useState, useEffect, useCallback } from 'react'
import { useNavigate } from 'react-router-dom'
import { api } from '../api'
import { showToast } from '../components/Toast'
import { EnvironmentsContent } from './Environments'
import { WorkspaceContent } from './Workspace'
import { FoundryIQContent } from './FoundryIQ'
import type { SetupStatus, FoundryIQConfig, MonitoringConfig } from '../types'

type Tab = 'overview' | 'environments' | 'voice' | 'memory' | 'workspace' | 'monitoring'

interface PreflightCheck {
  check: string
  ok: boolean
  detail: string
  sub_checks?: { name: string; ok: boolean; detail: string }[]
  endpoints?: { method: string; path: string; status: number | string; ok: boolean }[]
}

interface PreflightResult {
  status: string
  checks: PreflightCheck[]
}

const CHECK_LABELS: Record<string, string> = {
  bot_credentials: 'Bot Credentials',
  jwt_validation: 'JWT Validation',
  tunnel: 'Tunnel',
  tenant_id: 'Tenant ID',
  endpoint_auth: 'Endpoint Auth',
  telegram_security: 'Telegram Security',
  acs_voice: 'ACS / Voice',
  acs_callback_security: 'ACS Callback Security',
}

export default function InfrastructureSettings() {
  const navigate = useNavigate()
  const [tab, setTab] = useState<Tab>('overview')
  const [status, setStatus] = useState<SetupStatus | null>(null)
  const [preflight, setPreflight] = useState<PreflightResult | null>(null)
  const [loading, setLoading] = useState<Record<string, boolean>>({})

  const loadAll = useCallback(async () => {
    try {
      const s = await api<SetupStatus>('setup/status')
      setStatus(s)
    } catch { /* ignore */ }
  }, [])

  useEffect(() => { loadAll() }, [loadAll])

  const runPreflight = async () => {
    setLoading(p => ({ ...p, preflight: true }))
    try {
      const r = await api<PreflightResult>('setup/preflight')
      setPreflight(r)
    } catch (e: any) { showToast(e.message, 'error') }
    setLoading(p => ({ ...p, preflight: false }))
  }

  const startTunnel = async () => {
    setLoading(p => ({ ...p, tunnel: true }))
    try {
      await api('setup/tunnel/start', { method: 'POST' })
      showToast('Tunnel started', 'success')
      loadAll()
    } catch (e: any) { showToast(e.message, 'error') }
    setLoading(p => ({ ...p, tunnel: false }))
  }

  const stopTunnel = async () => {
    setLoading(p => ({ ...p, tunnel: true }))
    try {
      await api('setup/tunnel/stop', { method: 'POST' })
      showToast('Tunnel stopped', 'success')
      loadAll()
    } catch (e: any) { showToast(e.message, 'error') }
    setLoading(p => ({ ...p, tunnel: false }))
  }

  const deployInfra = async () => {
    setLoading(p => ({ ...p, deploy: true }))
    try {
      await api('setup/infra/deploy', { method: 'POST' })
      showToast('Infrastructure deployment started', 'success')
      loadAll()
    } catch (e: any) { showToast(e.message, 'error') }
    setLoading(p => ({ ...p, deploy: false }))
  }

  const decommission = async () => {
    if (!confirm('Decommission infrastructure? This will delete cloud resources.')) return
    setLoading(p => ({ ...p, decommission: true }))
    try {
      await api('setup/infra/decommission', { method: 'POST' })
      showToast('Decommissioning started', 'success')
      loadAll()
    } catch (e: any) { showToast(e.message, 'error') }
    setLoading(p => ({ ...p, decommission: false }))
  }

  const restartContainer = async () => {
    if (!confirm('Restart the agent container? This will briefly interrupt active sessions.')) return
    setLoading(p => ({ ...p, containerRestart: true }))
    try {
      const res = await api<{ message: string }>('setup/container/restart', { method: 'POST' })
      showToast(res.message || 'Agent container restarted', 'success')
    } catch (e: any) { showToast(e.message, 'error') }
    setLoading(p => ({ ...p, containerRestart: false }))
  }


  return (
    <div className="page">
      <div className="page__header">
        <h1>Infrastructure</h1>
        <div className="page__actions">
          {status && (
            <div className="page__status-dots">
              <StatusBadge ok={status.azure?.logged_in} label="Azure" />
              <StatusBadge ok={status.copilot?.authenticated} label="GitHub" />
              <StatusBadge ok={status.tunnel?.active} label="Tunnel" />
              <StatusBadge ok={status.bot_configured} label="Bot" />
            </div>
          )}
          <button
            className="btn btn--outline btn--sm"
            onClick={restartContainer}
            disabled={loading.containerRestart}
            title="Restart the agent container to apply configuration changes"
          >
            {loading.containerRestart ? 'Restarting...' : 'Restart Agent Container'}
          </button>
        </div>
      </div>
      <p className="text-muted" style={{ marginTop: -16, marginBottom: 16, fontSize: 13 }}>
        Configuration changes require a container restart to take effect.
      </p>

      <div className="settings__actions">
        <button className="btn btn--outline" onClick={() => navigate('/setup')}>
          Reopen Setup Wizard
        </button>
      </div>

      <div className="tabs">
        {([
          ['overview', 'Overview'],
          ['memory', 'Memory / Foundry IQ'],
          ['environments', 'Environments'],
          ['voice', 'Voice'],
          ['workspace', 'Workspace'],
          ['monitoring', 'Monitoring'],
        ] as [Tab, string][]).map(([t, label]) => (
          <button key={t} className={`tab ${tab === t ? 'tab--active' : ''}`} onClick={() => setTab(t)}>
            {label}
          </button>
        ))}
      </div>

      {/* Overview: Platform Status + Preflight + Provisioning */}
      {tab === 'overview' && (
        <>
        {status && (
        <div className="card">
          <h3>Platform Status</h3>
          <div className="detail-grid">
            <div><strong>Azure:</strong> {status.azure?.logged_in ? status.azure.subscription || 'Logged in' : 'Not logged in'}</div>
            <div><strong>GitHub Copilot:</strong> {status.copilot?.authenticated ? 'Authenticated' : 'Not authenticated'}</div>
            <div><strong>Tunnel:</strong> {status.tunnel?.active ? status.tunnel.url : 'Inactive'}</div>
            <div><strong>Bot:</strong> {status.bot_configured ? 'Configured' : 'Not configured'}</div>
            <div><strong>Voice:</strong> {status.voice_call_configured ? 'Configured' : 'Not configured'}</div>
          </div>
        </div>
        )}

        <div className="card">
          <h3>Preflight Checks</h3>
          <p className="text-muted">Security and readiness checks for your deployment.</p>
          <button className="btn btn--primary mt-1" onClick={runPreflight} disabled={loading.preflight}>
            {loading.preflight ? 'Running...' : 'Run Preflight Checks'}
          </button>

          {preflight && (
            <div className="mt-2">
              <span className={`badge ${preflight.status === 'ok' ? 'badge--ok' : 'badge--warn'}`}>
                {preflight.status === 'ok' ? 'All Checks Passed' : 'Warnings'}
              </span>

              <div className="preflight-grid mt-2">
                {preflight.checks.map(c => (
                  <div key={c.check} className="preflight-row">
                    <div className="preflight-row__header">
                      <span className={`status-dot__indicator ${c.ok ? 'status-dot__indicator--ok' : 'status-dot__indicator--err'}`} />
                      <strong>{CHECK_LABELS[c.check] || c.check}</strong>
                      <span className="text-muted ml-2">{c.detail}</span>
                    </div>

                    {c.sub_checks && c.sub_checks.length > 0 && (
                      <details className="preflight-details" open={!c.ok}>
                        <summary>{c.sub_checks.filter(s => s.ok).length}/{c.sub_checks.length} sub-checks passed</summary>
                        {c.sub_checks.map(sc => (
                          <div key={sc.name} className="preflight-row preflight-row--sub">
                            <span className={`status-dot__indicator ${sc.ok ? 'status-dot__indicator--ok' : 'status-dot__indicator--err'}`} />
                            <span>{sc.name}</span>
                            <span className="text-muted ml-2">{sc.detail}</span>
                          </div>
                        ))}
                      </details>
                    )}

                    {c.endpoints && c.endpoints.length > 0 && (
                      <details className="preflight-details" open={!c.ok}>
                        <summary>{c.endpoints.filter(e => e.ok).length}/{c.endpoints.length} endpoints secured</summary>
                        <table className="preflight-table">
                          <thead><tr><th>Method</th><th>Path</th><th>Status</th><th></th></tr></thead>
                          <tbody>
                            {c.endpoints.map(ep => (
                              <tr key={`${ep.method}-${ep.path}`} className={ep.ok ? '' : 'text-err'}>
                                <td>{ep.method}</td>
                                <td>{ep.path}</td>
                                <td>{ep.status}</td>
                                <td>{ep.ok ? 'OK' : 'EXPOSED'}</td>
                              </tr>
                            ))}
                          </tbody>
                        </table>
                      </details>
                    )}
                  </div>
                ))}
              </div>
            </div>
          )}
        </div>

        <div className="infra">
          {/* Tunnel Card */}
          <div className="infra__card">
            <div className="infra__card-header">
              <div className="infra__icon infra__icon--tunnel">
                <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/></svg>
              </div>
              <div className="infra__card-title">
                <h4>Tunnel</h4>
                <p className="text-muted">Cloudflare tunnel for exposing the bot endpoint publicly.</p>
              </div>
              <span className={`badge ${status?.tunnel?.active ? 'badge--ok' : 'badge--muted'}`}>
                {status?.tunnel?.active ? 'Active' : 'Inactive'}
              </span>
            </div>

            {status?.tunnel?.active ? (
              <div className="infra__card-body">
                {status.tunnel?.url && (
                  <div className="infra__url-box">
                    <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M10 13a5 5 0 0 0 7.54.54l3-3a5 5 0 0 0-7.07-7.07l-1.72 1.71"/><path d="M14 11a5 5 0 0 0-7.54-.54l-3 3a5 5 0 0 0 7.07 7.07l1.71-1.71"/></svg>
                    <code>{status.tunnel.url}</code>
                  </div>
                )}
                <button className="btn btn--danger btn--sm" onClick={stopTunnel} disabled={loading.tunnel}>
                  {loading.tunnel ? 'Stopping...' : 'Stop Tunnel'}
                </button>
              </div>
            ) : (
              <div className="infra__card-body">
                <button className="btn btn--primary btn--sm" onClick={startTunnel} disabled={loading.tunnel}>
                  {loading.tunnel ? 'Starting...' : 'Start Tunnel'}
                </button>
              </div>
            )}
          </div>

          {/* Deploy / Decommission Cards */}
          {status?.azure?.logged_in ? (
            <div className="infra__actions-grid">
              <div className="infra__action-card">
                <div className="infra__icon infra__icon--deploy">
                  <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M4 14.899A7 7 0 1 1 15.71 8h1.79a4.5 4.5 0 0 1 2.5 8.242"/><path d="M12 12v9"/><path d="m8 17 4 4 4-4"/></svg>
                </div>
                <h4>Deploy Infrastructure</h4>
                <p className="text-muted">Provision Azure Bot Framework resources, register the bot channel, and wire up the messaging endpoint.</p>
                <button className="btn btn--primary mt-1" onClick={deployInfra} disabled={loading.deploy}>
                  {loading.deploy ? 'Deploying...' : 'Deploy'}
                </button>
              </div>

              <div className="infra__action-card infra__action-card--danger">
                <div className="infra__icon infra__icon--decom">
                  <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M3 6h18"/><path d="M19 6v14c0 1-1 2-2 2H7c-1 0-2-1-2-2V6"/><path d="M8 6V4c0-1 1-2 2-2h4c1 0 2 1 2 2v2"/><line x1="10" y1="11" x2="10" y2="17"/><line x1="14" y1="11" x2="14" y2="17"/></svg>
                </div>
                <h4>Decommission</h4>
                <p className="text-muted">Tear down all provisioned Azure resources. This is irreversible and will delete cloud infrastructure.</p>
                <button className="btn btn--danger mt-1" onClick={decommission} disabled={loading.decommission}>
                  {loading.decommission ? 'Decommissioning...' : 'Decommission'}
                </button>
              </div>
            </div>
          ) : (
            <div className="infra__card infra__card--muted">
              <div className="infra__card-header">
                <div className="infra__icon infra__icon--lock">
                  <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><rect x="3" y="11" width="18" height="11" rx="2" ry="2"/><path d="M7 11V7a5 5 0 0 1 10 0v4"/></svg>
                </div>
                <div className="infra__card-title">
                  <h4>Azure Login Required</h4>
                  <p className="text-muted">Sign in to Azure to deploy or decommission infrastructure.</p>
                </div>
              </div>
              <div className="infra__card-body">
                <button className="btn btn--primary btn--sm" onClick={() => navigate('/setup')}>
                  Open Setup Wizard
                </button>
              </div>
            </div>
          )}
        </div>
        </>
      )}

      {/* Environments */}
      {tab === 'environments' && <EnvironmentsContent />}

      {/* Voice */}
      {tab === 'voice' && (
        <VoiceTab status={status} onReload={loadAll} />
      )}

      {/* Memory / Foundry IQ */}
      {tab === 'memory' && (
        <MemoryTab azureLoggedIn={!!status?.azure?.logged_in} />
      )}

      {/* Workspace */}
      {tab === 'workspace' && <WorkspaceContent />}

      {/* Monitoring */}
      {tab === 'monitoring' && <MonitoringTab />}
    </div>
  )
}

function StatusBadge({ ok, label }: { ok?: boolean; label: string }) {
  return (
    <span className={`badge ${ok ? 'badge--ok' : 'badge--err'}`} title={label}>
      {label}
    </span>
  )
}

// ---------------------------------------------------------------------------
// Monitoring Tab -- OpenTelemetry / Application Insights configuration
// ---------------------------------------------------------------------------

type MonitoringMode = 'deploy' | 'connect'

/** Map Azure region prefixes to country flag emoji. */
const AZURE_REGION_FLAGS: Record<string, string> = {
  eastus: '\u{1F1FA}\u{1F1F8}',
  eastus2: '\u{1F1FA}\u{1F1F8}',
  westus: '\u{1F1FA}\u{1F1F8}',
  westus2: '\u{1F1FA}\u{1F1F8}',
  westus3: '\u{1F1FA}\u{1F1F8}',
  centralus: '\u{1F1FA}\u{1F1F8}',
  northcentralus: '\u{1F1FA}\u{1F1F8}',
  southcentralus: '\u{1F1FA}\u{1F1F8}',
  westcentralus: '\u{1F1FA}\u{1F1F8}',
  canadacentral: '\u{1F1E8}\u{1F1E6}',
  canadaeast: '\u{1F1E8}\u{1F1E6}',
  brazilsouth: '\u{1F1E7}\u{1F1F7}',
  northeurope: '\u{1F1EE}\u{1F1EA}',
  westeurope: '\u{1F1F3}\u{1F1F1}',
  uksouth: '\u{1F1EC}\u{1F1E7}',
  ukwest: '\u{1F1EC}\u{1F1E7}',
  francecentral: '\u{1F1EB}\u{1F1F7}',
  francesouth: '\u{1F1EB}\u{1F1F7}',
  germanywestcentral: '\u{1F1E9}\u{1F1EA}',
  switzerlandnorth: '\u{1F1E8}\u{1F1ED}',
  switzerlandwest: '\u{1F1E8}\u{1F1ED}',
  norwayeast: '\u{1F1F3}\u{1F1F4}',
  norwaywest: '\u{1F1F3}\u{1F1F4}',
  swedencentral: '\u{1F1F8}\u{1F1EA}',
  polandcentral: '\u{1F1F5}\u{1F1F1}',
  italynorth: '\u{1F1EE}\u{1F1F9}',
  spaincentral: '\u{1F1EA}\u{1F1F8}',
  eastasia: '\u{1F1ED}\u{1F1F0}',
  southeastasia: '\u{1F1F8}\u{1F1EC}',
  japaneast: '\u{1F1EF}\u{1F1F5}',
  japanwest: '\u{1F1EF}\u{1F1F5}',
  koreacentral: '\u{1F1F0}\u{1F1F7}',
  koreasouth: '\u{1F1F0}\u{1F1F7}',
  australiaeast: '\u{1F1E6}\u{1F1FA}',
  australiasoutheast: '\u{1F1E6}\u{1F1FA}',
  australiacentral: '\u{1F1E6}\u{1F1FA}',
  centralindia: '\u{1F1EE}\u{1F1F3}',
  southindia: '\u{1F1EE}\u{1F1F3}',
  westindia: '\u{1F1EE}\u{1F1F3}',
  southafricanorth: '\u{1F1FF}\u{1F1E6}',
  southafricawest: '\u{1F1FF}\u{1F1E6}',
  uaenorth: '\u{1F1E6}\u{1F1EA}',
  uaecentral: '\u{1F1E6}\u{1F1EA}',
  qatarcentral: '\u{1F1F6}\u{1F1E6}',
  israelcentral: '\u{1F1EE}\u{1F1F1}',
  mexicocentral: '\u{1F1F2}\u{1F1FD}',
  newzealandnorth: '\u{1F1F3}\u{1F1FF}',
}

/** Friendly display names for Azure regions. */
const AZURE_REGION_LABELS: Record<string, string> = {
  eastus: 'East US',
  eastus2: 'East US 2',
  westus: 'West US',
  westus2: 'West US 2',
  westus3: 'West US 3',
  centralus: 'Central US',
  northcentralus: 'North Central US',
  southcentralus: 'South Central US',
  westcentralus: 'West Central US',
  canadacentral: 'Canada Central',
  canadaeast: 'Canada East',
  brazilsouth: 'Brazil South',
  northeurope: 'North Europe',
  westeurope: 'West Europe',
  uksouth: 'UK South',
  ukwest: 'UK West',
  francecentral: 'France Central',
  francesouth: 'France South',
  germanywestcentral: 'Germany West Central',
  switzerlandnorth: 'Switzerland North',
  switzerlandwest: 'Switzerland West',
  norwayeast: 'Norway East',
  norwaywest: 'Norway West',
  swedencentral: 'Sweden Central',
  polandcentral: 'Poland Central',
  italynorth: 'Italy North',
  spaincentral: 'Spain Central',
  eastasia: 'East Asia',
  southeastasia: 'Southeast Asia',
  japaneast: 'Japan East',
  japanwest: 'Japan West',
  koreacentral: 'Korea Central',
  koreasouth: 'Korea South',
  australiaeast: 'Australia East',
  australiasoutheast: 'Australia Southeast',
  australiacentral: 'Australia Central',
  centralindia: 'Central India',
  southindia: 'South India',
  westindia: 'West India',
  southafricanorth: 'South Africa North',
  southafricawest: 'South Africa West',
  uaenorth: 'UAE North',
  uaecentral: 'UAE Central',
  qatarcentral: 'Qatar Central',
  israelcentral: 'Israel Central',
  mexicocentral: 'Mexico Central',
  newzealandnorth: 'New Zealand North',
}

function getRegionFlag(location: string): string {
  const key = location.toLowerCase().replace(/[\s-_]/g, '')
  return AZURE_REGION_FLAGS[key] || '\u{1F30D}'
}

function getRegionLabel(location: string): string {
  const key = location.toLowerCase().replace(/[\s-_]/g, '')
  return AZURE_REGION_LABELS[key] || location
}

interface ProvisionStepResult {
  step: string
  status: string
  detail: string
}

function PipelineFlow({ active }: { active: boolean }) {
  const nodeClass = active ? 'mon__pipeline-node mon__pipeline-node--active' : 'mon__pipeline-node'
  const arrow = (
    <span className="mon__pipeline-arrow">
      <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="m9 18 6-6-6-6"/></svg>
    </span>
  )
  return (
    <div className="mon__pipeline">
      <span className={nodeClass}>
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><rect x="2" y="3" width="20" height="14" rx="2"/><path d="M8 21h8"/><path d="M12 17v4"/></svg>
        Agent Runtime
      </span>
      {arrow}
      <span className={nodeClass}>
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><circle cx="12" cy="12" r="10"/><path d="m15 9-6 6"/><path d="M9 9h6v6"/></svg>
        OTel Distro
      </span>
      {arrow}
      <span className={nodeClass}>
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M4 14.899A7 7 0 1 1 15.71 8h1.79a4.5 4.5 0 0 1 2.5 8.242"/><path d="M12 12v9"/></svg>
        App Insights
      </span>
      {arrow}
      <span className={nodeClass}>
        <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M3 3v18h18"/><path d="m19 9-5 5-4-4-3 3"/></svg>
        Log Analytics
      </span>
    </div>
  )
}

function TelemetryFeatureCards() {
  return (
    <div className="mon__features">
      <div className="mon__feature-card">
        <div className="mon__feature-icon mon__feature-icon--traces">
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M22 12h-4l-3 9L9 3l-3 9H2"/></svg>
        </div>
        <div className="mon__feature-text">
          <h5>Traces</h5>
          <p>HTTP requests, outgoing calls, Azure SDK operations</p>
        </div>
      </div>
      <div className="mon__feature-card">
        <div className="mon__feature-icon mon__feature-icon--metrics">
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M3 3v18h18"/><path d="M18 17V9"/><path d="M13 17V5"/><path d="M8 17v-3"/></svg>
        </div>
        <div className="mon__feature-text">
          <h5>Metrics</h5>
          <p>Request duration, count, error rate, performance counters</p>
        </div>
      </div>
      <div className="mon__feature-card">
        <div className="mon__feature-icon mon__feature-icon--logs">
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M14 2H6a2 2 0 0 0-2 2v16a2 2 0 0 0 2 2h12a2 2 0 0 0 2-2V8z"/><path d="M14 2v6h6"/><path d="M16 13H8"/><path d="M16 17H8"/></svg>
        </div>
        <div className="mon__feature-text">
          <h5>Logs</h5>
          <p>Python logging (WARNING+), exceptions with stack traces</p>
        </div>
      </div>
      <div className="mon__feature-card">
        <div className="mon__feature-icon mon__feature-icon--deps">
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><circle cx="18" cy="18" r="3"/><circle cx="6" cy="6" r="3"/><path d="M13 6h3a2 2 0 0 1 2 2v7"/><path d="M6 9v12"/></svg>
        </div>
        <div className="mon__feature-text">
          <h5>Dependencies</h5>
          <p>Azure services, external APIs, databases tracked automatically</p>
        </div>
      </div>
      <div className="mon__feature-card">
        <div className="mon__feature-icon mon__feature-icon--live">
          <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M2 12s3-7 10-7 10 7 10 7-3 7-10 7-10-7-10-7Z"/><circle cx="12" cy="12" r="3"/></svg>
        </div>
        <div className="mon__feature-text">
          <h5>Live Metrics</h5>
          <p>Real-time request rate, failure rate, and performance data</p>
        </div>
      </div>
    </div>
  )
}

function DeployArchPreview() {
  const arrow = (
    <span className="mon__arch-connector">
      <svg width="14" height="14" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="m9 18 6-6-6-6"/></svg>
    </span>
  )
  return (
    <div className="mon__arch-preview">
      <div className="mon__arch-step">
        <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round"><path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"/></svg>
        <span>Resource Group</span>
      </div>
      {arrow}
      <div className="mon__arch-step">
        <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round"><path d="M3 3v18h18"/><path d="m19 9-5 5-4-4-3 3"/></svg>
        <span>Log Analytics Workspace</span>
      </div>
      {arrow}
      <div className="mon__arch-step">
        <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round"><path d="M22 12h-4l-3 9L9 3l-3 9H2"/></svg>
        <span>Application Insights</span>
      </div>
      {arrow}
      <div className="mon__arch-step">
        <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.5" strokeLinecap="round" strokeLinejoin="round"><circle cx="12" cy="12" r="10"/><path d="m15 9-6 6"/><path d="M9 9h6v6"/></svg>
        <span>OTel Auto-Instrument</span>
      </div>
    </div>
  )
}

function ProvisionSteps({ steps }: { steps: ProvisionStepResult[] }) {
  if (!steps.length) return null
  const labels: Record<string, string> = {
    cli_extension: 'Install CLI extension',
    resource_group: 'Resource group',
    create_workspace: 'Log Analytics workspace',
    create_app_insights: 'Application Insights',
    save_config: 'Save configuration',
    otel_bootstrap: 'Activate OTel export',
  }
  return (
    <div className="mon__steps">
      {steps.map((s, i) => (
        <div key={i} className="mon__step">
          <span className={`mon__step-icon mon__step-icon--${s.status === 'ok' ? 'ok' : 'fail'}`}>
            {s.status === 'ok' ? '\u2713' : '\u2717'}
          </span>
          <span className="mon__step-label">{labels[s.step] || s.step}</span>
          <span className="mon__step-detail">{s.detail}</span>
        </div>
      ))}
    </div>
  )
}

function MonitoringTab() {
  const [config, setConfig] = useState<MonitoringConfig | null>(null)
  const [loading, setLoading] = useState<Record<string, boolean>>({})
  const [mode, setMode] = useState<MonitoringMode>('deploy')

  // Connect-existing state
  const [connectionString, setConnectionString] = useState('')
  const [enabled, setEnabled] = useState(false)
  const [samplingRatio, setSamplingRatio] = useState(1.0)
  const [enableLiveMetrics, setEnableLiveMetrics] = useState(false)
  const [testResult, setTestResult] = useState<{ status: string; message: string; instrumentation_key?: string; ingestion_endpoint?: string } | null>(null)

  // Deploy-new state
  const [deployLocation, setDeployLocation] = useState('eastus')
  const [deployRg, setDeployRg] = useState('polyclaw-monitoring-rg')
  const [provisionSteps, setProvisionSteps] = useState<ProvisionStepResult[]>([])

  const loadConfig = useCallback(async () => {
    try {
      const cfg = await api<MonitoringConfig>('monitoring/config')
      setConfig(cfg)
      setEnabled(cfg.enabled)
      setSamplingRatio(cfg.sampling_ratio)
      setEnableLiveMetrics(cfg.enable_live_metrics)
    } catch { /* ignore */ }
  }, [])

  useEffect(() => { loadConfig() }, [loadConfig])

  const handleSave = async () => {
    setLoading(p => ({ ...p, save: true }))
    try {
      const body: Record<string, unknown> = {
        enabled,
        sampling_ratio: samplingRatio,
        enable_live_metrics: enableLiveMetrics,
      }
      if (connectionString) {
        body.connection_string = connectionString
      }
      const res = await api<{ status: string; message: string }>('monitoring/config', {
        method: 'POST',
        body: JSON.stringify(body),
      })
      showToast(res.message, res.status === 'ok' ? 'success' : 'error')
      setConnectionString('')
      await loadConfig()
    } catch (e: unknown) { showToast(e instanceof Error ? e.message : String(e), 'error') }
    setLoading(p => ({ ...p, save: false }))
  }

  const handleTest = async () => {
    const cs = connectionString || ''
    if (!cs) {
      showToast('Enter a connection string to test', 'error')
      return
    }
    setLoading(p => ({ ...p, test: true }))
    setTestResult(null)
    try {
      const res = await api<{ status: string; message: string; instrumentation_key?: string; ingestion_endpoint?: string }>('monitoring/test', {
        method: 'POST',
        body: JSON.stringify({ connection_string: cs }),
      })
      setTestResult(res)
    } catch (e: unknown) {
      setTestResult({ status: 'error', message: e instanceof Error ? e.message : String(e) })
    }
    setLoading(p => ({ ...p, test: false }))
  }

  const handleProvision = async () => {
    setLoading(p => ({ ...p, deploy: true }))
    setProvisionSteps([])
    try {
      const res = await api<{ status: string; message: string; steps?: ProvisionStepResult[] }>('monitoring/provision', {
        method: 'POST',
        body: JSON.stringify({ location: deployLocation, resource_group: deployRg }),
      })
      if (res.steps) setProvisionSteps(res.steps)
      showToast(res.message, res.status === 'ok' ? 'success' : 'error')
      await loadConfig()
    } catch (e: unknown) { showToast(e instanceof Error ? e.message : String(e), 'error') }
    setLoading(p => ({ ...p, deploy: false }))
  }

  const handleDecommission = async () => {
    if (!confirm('Decommission Application Insights? This will delete the App Insights resource, Log Analytics workspace, and stop telemetry export.')) return
    setLoading(p => ({ ...p, decommission: true }))
    try {
      const res = await api<{ status: string; message: string }>('monitoring/provision', {
        method: 'DELETE',
      })
      showToast(res.message, res.status === 'ok' ? 'success' : 'error')
      await loadConfig()
    } catch (e: unknown) { showToast(e instanceof Error ? e.message : String(e), 'error') }
    setLoading(p => ({ ...p, decommission: false }))
  }

  if (!config) return <div className="spinner" />

  const otelActive = config.otel_status?.active

  // -- Already provisioned or configured view --
  if (config.provisioned || config.connection_string_set) {
    return (
      <div className="voice">
        {/* Status card */}
        <div className="voice__status-card">
          <div className="voice__status-header">
            <h3>OpenTelemetry Monitoring</h3>
            <span className={`badge ${otelActive ? 'badge--ok' : config.enabled ? 'badge--warn' : 'badge--muted'}`}>
              {otelActive ? 'Active' : config.enabled ? 'Enabled (not exporting)' : 'Disabled'}
            </span>
            {config.provisioned && <span className="badge badge--ok">Provisioned</span>}
          </div>

          <PipelineFlow active={!!otelActive} />

          <div className="mon__info-grid" style={{ marginTop: 14 }}>
            {/* Status */}
            <div className="mon__info-card">
              <div className="mon__info-icon">
                <span className={`status-dot__indicator ${otelActive ? 'status-dot__indicator--ok' : 'status-dot__indicator--err'}`} style={{ width: 10, height: 10 }} />
              </div>
              <div className="mon__info-body">
                <label>Status</label>
                <span>{otelActive ? 'Exporting telemetry' : 'Not exporting'}</span>
              </div>
            </div>

            {/* App Insights */}
            {config.app_insights_name && (
              <div className="mon__info-card">
                <div className="mon__info-icon">
                  <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M4 14.899A7 7 0 1 1 15.71 8h1.79a4.5 4.5 0 0 1 2.5 8.242"/><path d="M12 12v9"/></svg>
                </div>
                <div className="mon__info-body">
                  <label>App Insights</label>
                  {config.portal_url ? (
                    <a href={config.portal_url} target="_blank" rel="noopener noreferrer" className="mon__info-link">
                      {config.app_insights_name}
                      <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"/><polyline points="15 3 21 3 21 9"/><line x1="10" y1="14" x2="21" y2="3"/></svg>
                    </a>
                  ) : (
                    <span>{config.app_insights_name}</span>
                  )}
                </div>
              </div>
            )}

            {/* Log Analytics */}
            {config.workspace_name && (
              <div className="mon__info-card">
                <div className="mon__info-icon">
                  <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M3 3v18h18"/><path d="m19 9-5 5-4-4-3 3"/></svg>
                </div>
                <div className="mon__info-body">
                  <label>Log Analytics Workspace</label>
                  <span>{config.workspace_name}</span>
                </div>
              </div>
            )}

            {/* Resource Group */}
            {config.resource_group && (
              <div className="mon__info-card">
                <div className="mon__info-icon">
                  <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M22 19a2 2 0 0 1-2 2H4a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2h5l2 3h9a2 2 0 0 1 2 2z"/></svg>
                </div>
                <div className="mon__info-body">
                  <label>Resource Group</label>
                  <span>{config.resource_group}</span>
                </div>
              </div>
            )}

            {/* Location with flag */}
            {config.location && (
              <div className="mon__info-card">
                <div className="mon__info-icon mon__info-icon--flag">
                  {getRegionFlag(config.location)}
                </div>
                <div className="mon__info-body">
                  <label>Location</label>
                  <span>{getRegionLabel(config.location)}</span>
                </div>
              </div>
            )}

            {/* Connection String -- masked */}
            {config.connection_string_set && (
              <div className="mon__info-card mon__info-card--wide">
                <div className="mon__info-icon">
                  <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><rect x="3" y="11" width="18" height="11" rx="2" ry="2"/><path d="M7 11V7a5 5 0 0 1 10 0v4"/></svg>
                </div>
                <div className="mon__info-body">
                  <label>Connection String</label>
                  <span className="mon__secret-value">{config.connection_string_masked}</span>
                </div>
              </div>
            )}

            {/* Tracer Provider */}
            {otelActive && config.otel_status?.tracer_provider && (
              <div className="mon__info-card">
                <div className="mon__info-icon">
                  <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M12 22s8-4 8-10V5l-8-3-8 3v7c0 6 8 10 8 10z"/></svg>
                </div>
                <div className="mon__info-body">
                  <label>Tracer Provider</label>
                  <span>{config.otel_status.tracer_provider}</span>
                </div>
              </div>
            )}

            {/* Grafana Agent Dashboard */}
            {config.grafana_dashboard_url && (
              <div className="mon__info-card mon__info-card--wide mon__info-card--action">
                <div className="mon__info-icon">
                  <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M3 3v18h18"/><path d="M18 17V9"/><path d="M13 17V5"/><path d="M8 17v-3"/></svg>
                </div>
                <div className="mon__info-body">
                  <label>Agent Dashboard</label>
                  <span>Performance, tokens, cost, errors, and traces</span>
                </div>
                <a
                  href={config.grafana_dashboard_url}
                  target="_blank"
                  rel="noopener noreferrer"
                  className="btn btn--primary btn--sm"
                >
                  Open in Grafana
                  <svg width="12" height="12" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round" style={{ marginLeft: 4 }}><path d="M18 13v6a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h6"/><polyline points="15 3 21 3 21 9"/><line x1="10" y1="14" x2="21" y2="3"/></svg>
                </a>
              </div>
            )}
          </div>
        </div>

        {/* Configuration panel */}
        <div className="voice__panel">
          <div className="voice__panel-header">
            <div>
              <h4>Configuration</h4>
              <p className="text-muted">Adjust monitoring settings. Changes take effect immediately.</p>
            </div>
          </div>
          <div className="voice__panel-body">
            <div className="form">
              <label className="form__check">
                <input type="checkbox" checked={enabled} onChange={e => setEnabled(e.target.checked)} />
                Enable OpenTelemetry monitoring
              </label>

              {!config.provisioned && (
                <div className="form__group">
                  <label className="form__label">Application Insights Connection String</label>
                  <input
                    className="input"
                    value={connectionString}
                    onChange={e => setConnectionString(e.target.value)}
                    placeholder={config.connection_string_set ? '(configured -- enter new value to replace)' : 'InstrumentationKey=...;IngestionEndpoint=...'}
                    type="password"
                  />
                  <span className="form__hint">
                    Find this in the Azure portal under your Application Insights resource &gt; Overview &gt; Connection String.
                  </span>
                </div>
              )}

              {connectionString && (
                <div style={{ marginBottom: 12 }}>
                  <button className="btn btn--outline btn--sm" onClick={handleTest} disabled={loading.test}>
                    {loading.test ? 'Validating...' : 'Validate Connection String'}
                  </button>
                  {testResult && (
                    <div style={{ marginTop: 8 }}>
                      <span className={`badge ${testResult.status === 'ok' ? 'badge--ok' : 'badge--err'}`}>
                        {testResult.message}
                      </span>
                      {testResult.instrumentation_key && (
                        <div className="text-muted" style={{ fontSize: 12, marginTop: 4 }}>
                          Key: {testResult.instrumentation_key} | Endpoint: {testResult.ingestion_endpoint}
                        </div>
                      )}
                    </div>
                  )}
                </div>
              )}

              <div className="form__group">
                <label className="form__label">Sampling Ratio</label>
                <div className="mon__sampling-value">
                  {(samplingRatio * 100).toFixed(0)}<small>% of traces exported</small>
                </div>
                <div className="mon__sampling-bar">
                  <div className="mon__sampling-fill" style={{ width: `${samplingRatio * 100}%` }} />
                </div>
                <input
                  type="range"
                  min="0.01"
                  max="1"
                  step="0.01"
                  value={samplingRatio}
                  onChange={e => setSamplingRatio(parseFloat(e.target.value))}
                  style={{ width: '100%', marginTop: 4 }}
                />
                <span className="form__hint">
                  100% = all traces, 5% = 1 in 20. Lower values reduce cost and noise. Metrics and logs are unaffected.
                </span>
              </div>

              <label className="form__check">
                <input type="checkbox" checked={enableLiveMetrics} onChange={e => setEnableLiveMetrics(e.target.checked)} />
                Enable Live Metrics (real-time dashboard in Azure portal)
              </label>

              <div className="form__actions">
                <button className="btn btn--primary" onClick={handleSave} disabled={loading.save}>
                  {loading.save ? 'Saving...' : 'Save Configuration'}
                </button>
              </div>
            </div>
          </div>
        </div>

        {/* What gets collected */}
        <div className="voice__panel">
          <div className="voice__panel-header">
            <div>
              <h4>What Gets Collected</h4>
              <p className="text-muted">The Azure Monitor OpenTelemetry Distro automatically instruments these signals.</p>
            </div>
          </div>
          <div className="voice__panel-body">
            <TelemetryFeatureCards />
          </div>
        </div>

        {/* Decommission (only for provisioned resources) */}
        {config.provisioned && (
          <div className="voice__danger-strip">
            <p>Remove Application Insights and Log Analytics resources and stop telemetry export.</p>
            <button className="btn btn--danger btn--sm" onClick={handleDecommission} disabled={loading.decommission}>
              {loading.decommission ? 'Decommissioning...' : 'Decommission'}
            </button>
          </div>
        )}
      </div>
    )
  }

  // -- Not provisioned: setup view with deploy/connect mode --
  return (
    <div className="voice">
      {/* Mode selector bar */}
      <div className="voice__mode-bar">
        <button
          className={`voice__mode-btn${mode === 'deploy' ? ' voice__mode-btn--active' : ''}`}
          onClick={() => setMode('deploy')}
        >
          <div className="voice__mode-icon voice__mode-icon--new">
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M4 14.899A7 7 0 1 1 15.71 8h1.79a4.5 4.5 0 0 1 2.5 8.242"/><path d="M12 12v9"/><path d="m8 17 4 4 4-4"/></svg>
          </div>
          <div>
            <h4>Deploy New</h4>
            <p>Provision Application Insights + Log Analytics workspace</p>
          </div>
        </button>
        <button
          className={`voice__mode-btn${mode === 'connect' ? ' voice__mode-btn--active' : ''}`}
          onClick={() => setMode('connect')}
        >
          <div className="voice__mode-icon voice__mode-icon--link">
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M10 13a5 5 0 0 0 7.54.54l3-3a5 5 0 0 0-7.07-7.07l-1.72 1.71"/><path d="M14 11a5 5 0 0 0-7.54-.54l-3 3a5 5 0 0 0 7.07 7.07l1.71-1.71"/></svg>
          </div>
          <div>
            <h4>Connect Existing</h4>
            <p>Provide a connection string from an existing Application Insights resource</p>
          </div>
        </button>
      </div>

      {/* Deploy new */}
      {mode === 'deploy' && (
        <div className="voice__panel">
          <div className="voice__panel-header">
            <div>
              <h4>Deploy New Application Insights</h4>
              <p className="text-muted">Provisions the full monitoring stack in a single step. The runtime is automatically instrumented to export telemetry.</p>
            </div>
          </div>
          <div className="voice__panel-body">
            <DeployArchPreview />
            <div className="form">
              <div className="form__row">
                <div className="form__group">
                  <label className="form__label">Resource Group</label>
                  <input className="input" value={deployRg} onChange={e => setDeployRg(e.target.value)} />
                </div>
                <div className="form__group">
                  <label className="form__label">Location</label>
                  <input className="input" value={deployLocation} onChange={e => setDeployLocation(e.target.value)} />
                  <span className="form__hint">Azure region (e.g. eastus, westeurope, swedencentral).</span>
                </div>
              </div>
              <div className="form__actions">
                <button className="btn btn--primary" onClick={handleProvision} disabled={loading.deploy}>
                  {loading.deploy ? 'Provisioning...' : 'Deploy Application Insights'}
                </button>
              </div>
              <ProvisionSteps steps={provisionSteps} />
            </div>
          </div>
        </div>
      )}

      {/* Connect existing */}
      {mode === 'connect' && (
        <div className="voice__panel">
          <div className="voice__panel-header">
            <div>
              <h4>Connect to Existing Application Insights</h4>
              <p className="text-muted">Provide the connection string from an existing Application Insights resource in the Azure portal.</p>
            </div>
          </div>
          <div className="voice__panel-body">
            <div className="form">
              <label className="form__check">
                <input type="checkbox" checked={enabled} onChange={e => setEnabled(e.target.checked)} />
                Enable OpenTelemetry monitoring
              </label>

              <div className="form__group">
                <label className="form__label">Application Insights Connection String</label>
                <input
                  className="input"
                  value={connectionString}
                  onChange={e => setConnectionString(e.target.value)}
                  placeholder="InstrumentationKey=...;IngestionEndpoint=..."
                  type="password"
                />
                <span className="form__hint">
                  Find this in the Azure portal under your Application Insights resource &gt; Overview &gt; Connection String.
                </span>
              </div>

              {connectionString && (
                <div style={{ marginBottom: 12 }}>
                  <button className="btn btn--outline btn--sm" onClick={handleTest} disabled={loading.test}>
                    {loading.test ? 'Validating...' : 'Validate Connection String'}
                  </button>
                  {testResult && (
                    <div style={{ marginTop: 8 }}>
                      <span className={`badge ${testResult.status === 'ok' ? 'badge--ok' : 'badge--err'}`}>
                        {testResult.message}
                      </span>
                      {testResult.instrumentation_key && (
                        <div className="text-muted" style={{ fontSize: 12, marginTop: 4 }}>
                          Key: {testResult.instrumentation_key} | Endpoint: {testResult.ingestion_endpoint}
                        </div>
                      )}
                    </div>
                  )}
                </div>
              )}

              <div className="form__group">
                <label className="form__label">Sampling Ratio</label>
                <div className="mon__sampling-value">
                  {(samplingRatio * 100).toFixed(0)}<small>% of traces exported</small>
                </div>
                <div className="mon__sampling-bar">
                  <div className="mon__sampling-fill" style={{ width: `${samplingRatio * 100}%` }} />
                </div>
                <input
                  type="range"
                  min="0.01"
                  max="1"
                  step="0.01"
                  value={samplingRatio}
                  onChange={e => setSamplingRatio(parseFloat(e.target.value))}
                  style={{ width: '100%', marginTop: 4 }}
                />
                <span className="form__hint">
                  100% = all traces, 5% = 1 in 20. Lower values reduce cost and noise. Metrics and logs are unaffected.
                </span>
              </div>

              <label className="form__check">
                <input type="checkbox" checked={enableLiveMetrics} onChange={e => setEnableLiveMetrics(e.target.checked)} />
                Enable Live Metrics (real-time dashboard in Azure portal)
              </label>

              <div className="form__actions">
                <button className="btn btn--primary" onClick={handleSave} disabled={loading.save}>
                  {loading.save ? 'Saving...' : 'Save Configuration'}
                </button>
              </div>
            </div>
          </div>
        </div>
      )}

      {/* Telemetry overview (always visible) */}
      <div className="voice__panel">
        <div className="voice__panel-header">
          <div>
            <h4>What Gets Collected</h4>
            <p className="text-muted">The Azure Monitor OpenTelemetry Distro automatically instruments these signals.</p>
          </div>
        </div>
        <div className="voice__panel-body">
          <TelemetryFeatureCards />
        </div>
      </div>
    </div>
  )
}

// ---------------------------------------------------------------------------
// Memory / Foundry IQ Tab -- deploy new or connect existing resources
// ---------------------------------------------------------------------------

type MemoryMode = 'deploy' | 'connect'

function MemoryTab({ azureLoggedIn }: { azureLoggedIn: boolean }) {
  const [config, setConfig] = useState<FoundryIQConfig | null>(null)
  const [loading, setLoading] = useState<Record<string, boolean>>({})
  const [mode, setMode] = useState<MemoryMode>('deploy')
  const [deployLocation, setDeployLocation] = useState('eastus')
  const [deployRg, setDeployRg] = useState('polyclaw-foundryiq-rg')

  const loadConfig = useCallback(async () => {
    try {
      const cfg = await api<FoundryIQConfig>('foundry-iq/config')
      setConfig(cfg)
    } catch { /* ignore */ }
  }, [])

  useEffect(() => { loadConfig() }, [loadConfig])

  const handleProvision = async () => {
    setLoading(p => ({ ...p, deploy: true }))
    try {
      await api('foundry-iq/provision', {
        method: 'POST',
        body: JSON.stringify({ location: deployLocation, resource_group: deployRg }),
      })
      showToast('Foundry IQ resources provisioned', 'success')
      await loadConfig()
    } catch (e: any) { showToast(e.message, 'error') }
    setLoading(p => ({ ...p, deploy: false }))
  }

  const handleDecommission = async () => {
    if (!confirm('Decommission Foundry IQ? This will remove search and OpenAI resources.')) return
    setLoading(p => ({ ...p, decommission: true }))
    try {
      await api('foundry-iq/provision', { method: 'DELETE' })
      showToast('Foundry IQ resources removed', 'success')
      setConfig(null)
      await loadConfig()
    } catch (e: any) { showToast(e.message, 'error') }
    setLoading(p => ({ ...p, decommission: false }))
  }

  if (!config) return <div className="spinner" />

  // -- Already provisioned or configured --
  if (config.provisioned) {
    return (
      <div className="voice">
        <div className="voice__status-card">
          <div className="voice__status-header">
            <h3>Memory / Foundry IQ</h3>
            <span className="badge badge--ok">Provisioned</span>
          </div>
          <div className="voice__resource-grid">
            {config.search_resource_name && (
              <div className="voice__resource-item">
                <label>Search Service</label>
                <span>{config.search_resource_name}</span>
              </div>
            )}
            {config.openai_resource_name && (
              <div className="voice__resource-item">
                <label>OpenAI Account</label>
                <span>{config.openai_resource_name}</span>
              </div>
            )}
            {config.resource_group && (
              <div className="voice__resource-item">
                <label>Resource Group</label>
                <span>{config.resource_group}</span>
              </div>
            )}
            {config.location && (
              <div className="voice__resource-item">
                <label>Location</label>
                <span>{config.location}</span>
              </div>
            )}
          </div>
        </div>

        {/* Inline the full configuration + search UI */}
        <FoundryIQContent />

        {/* Decommission */}
        <div className="voice__danger-strip">
          <p>Remove all Foundry IQ Azure resources and clear configuration.</p>
          <button className="btn btn--danger btn--sm" onClick={handleDecommission} disabled={loading.decommission}>
            {loading.decommission ? 'Decommissioning...' : 'Decommission'}
          </button>
        </div>
      </div>
    )
  }

  // -- Not provisioned: setup view --
  return (
    <div className="voice">
      {/* Mode selector bar */}
      <div className="voice__mode-bar">
        <button
          className={`voice__mode-btn${mode === 'deploy' ? ' voice__mode-btn--active' : ''}`}
          onClick={() => setMode('deploy')}
        >
          <div className="voice__mode-icon voice__mode-icon--new">
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M4 14.899A7 7 0 1 1 15.71 8h1.79a4.5 4.5 0 0 1 2.5 8.242"/><path d="M12 12v9"/><path d="m8 17 4 4 4-4"/></svg>
          </div>
          <div>
            <h4>Deploy New</h4>
            <p>Provision Azure AI Search + OpenAI for memory indexing</p>
          </div>
        </button>
        <button
          className={`voice__mode-btn${mode === 'connect' ? ' voice__mode-btn--active' : ''}`}
          onClick={() => setMode('connect')}
        >
          <div className="voice__mode-icon voice__mode-icon--link">
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M10 13a5 5 0 0 0 7.54.54l3-3a5 5 0 0 0-7.07-7.07l-1.72 1.71"/><path d="M14 11a5 5 0 0 0-7.54-.54l-3 3a5 5 0 0 0 7.07 7.07l1.71-1.71"/></svg>
          </div>
          <div>
            <h4>Connect Existing</h4>
            <p>Provide endpoints for existing search and embedding resources</p>
          </div>
        </button>
      </div>

      {/* Deploy new */}
      {mode === 'deploy' && (
        <div className="voice__panel">
          <div className="voice__panel-header">
            <div>
              <h4>Deploy New Foundry IQ Resources</h4>
              <p className="text-muted">Creates a resource group with Azure AI Search (Basic) and Azure OpenAI with a text-embedding-3-large deployment.</p>
            </div>
          </div>
          <div className="voice__panel-body">
            {!azureLoggedIn ? (
              <p className="text-muted">Sign in to Azure first (Overview tab) to provision resources.</p>
            ) : (
              <div className="form">
                <div className="form__row">
                  <div className="form__group">
                    <label className="form__label">Resource Group</label>
                    <input className="input" value={deployRg} onChange={e => setDeployRg(e.target.value)} />
                  </div>
                  <div className="form__group">
                    <label className="form__label">Location</label>
                    <input className="input" value={deployLocation} onChange={e => setDeployLocation(e.target.value)} />
                    <span className="form__hint">Must support Azure OpenAI embeddings (e.g. eastus, swedencentral).</span>
                  </div>
                </div>
                <div className="form__actions">
                  <button className="btn btn--primary" onClick={handleProvision} disabled={loading.deploy}>
                    {loading.deploy ? 'Provisioning...' : 'Deploy Foundry IQ'}
                  </button>
                </div>
              </div>
            )}
          </div>
        </div>
      )}

      {/* Connect existing */}
      {mode === 'connect' && <FoundryIQContent />}
    </div>
  )
}

// ---------------------------------------------------------------------------
// Voice Tab -- deploy new or connect to existing ACS + AOAI resources
// ---------------------------------------------------------------------------

interface AzureResource { name: string; resource_group: string; location: string }
interface AoaiDeployment { deployment_name: string; model_name: string; model_version: string; is_realtime?: boolean }
interface VoiceConfig {
  acs_resource_name?: string
  acs_connection_string?: string
  acs_source_number?: string
  voice_target_number?: string
  azure_openai_resource_name?: string
  azure_openai_endpoint?: string
  azure_openai_realtime_deployment?: string
  voice_resource_group?: string
  resource_group?: string
  location?: string
  portal_phone_url?: string
}

type VoiceMode = 'deploy' | 'connect'

function VoiceTab({ status, onReload }: { status: SetupStatus | null; onReload: () => void }) {
  const [voiceConfig, setVoiceConfig] = useState<VoiceConfig | null>(null)
  const [loading, setLoading] = useState<Record<string, boolean>>({})
  const [mode, setMode] = useState<VoiceMode>('connect')

  // Connect-existing state
  const [aoaiList, setAoaiList] = useState<AzureResource[]>([])
  const [acsList, setAcsList] = useState<AzureResource[]>([])
  const [aoaiDeployments, setAoaiDeployments] = useState<AoaiDeployment[]>([])
  const [selectedAoai, setSelectedAoai] = useState<AzureResource | null>(null)
  const [selectedAoaiDep, setSelectedAoaiDep] = useState('')
  const [selectedAcs, setSelectedAcs] = useState<AzureResource | null>(null)
  const [skipAcs, setSkipAcs] = useState(false)
  const [acsPhones, setAcsPhones] = useState<string[]>([])
  const [phoneNumber, setPhoneNumber] = useState('')
  const [connectTargetPhone, setConnectTargetPhone] = useState('')

  // Deploy-new state
  const [deployLocation, setDeployLocation] = useState('swedencentral')
  const [deployRg, setDeployRg] = useState('polyclaw-voice-rg')

  // Phone config state
  const [sourcePhone, setSourcePhone] = useState('')
  const [targetPhone, setTargetPhone] = useState('')
  const [configuredPhones, setConfiguredPhones] = useState<string[]>([])

  const loadConfig = useCallback(async () => {
    try {
      const vc = await api<VoiceConfig>('setup/voice/config')
      setVoiceConfig(vc)
      if (vc.acs_source_number) setSourcePhone(vc.acs_source_number)
      if (vc.voice_target_number) setTargetPhone(vc.voice_target_number)
      // Load purchased phones for the configured ACS resource
      if (vc.acs_resource_name) {
        const rg = vc.voice_resource_group || vc.resource_group || ''
        if (rg) {
          try {
            const phones = await api<{ phone_number: string }[]>(
              `setup/voice/acs/phones?name=${encodeURIComponent(vc.acs_resource_name)}&resource_group=${encodeURIComponent(rg)}`
            )
            setConfiguredPhones(phones.map(p => p.phone_number))
          } catch { /* ignore */ }
        }
      }
    } catch { /* ignore */ }
  }, [])

  useEffect(() => { loadConfig() }, [loadConfig])

  const discoverResources = async () => {
    setLoading(p => ({ ...p, discover: true }))
    try {
      const [aoai, acs] = await Promise.all([
        api<AzureResource[]>('setup/voice/aoai/list'),
        api<AzureResource[]>('setup/voice/acs/list'),
      ])
      setAoaiList(aoai)
      setAcsList(acs)
      if (aoai.length > 0 && !selectedAoai) {
        setSelectedAoai(aoai[0])
        loadDeployments(aoai[0])
      }
    } catch (e: any) { showToast(e.message, 'error') }
    setLoading(p => ({ ...p, discover: false }))
  }

  const loadDeployments = async (resource: AzureResource) => {
    setAoaiDeployments([])
    setSelectedAoaiDep('')
    try {
      const deps = await api<AoaiDeployment[]>(
        `setup/voice/aoai/deployments?name=${encodeURIComponent(resource.name)}&resource_group=${encodeURIComponent(resource.resource_group)}`
      )
      setAoaiDeployments(deps)
      const realtime = deps.find(d => {
        const n = d.model_name || ''
        return n.includes('realtime')
      })
      if (realtime) setSelectedAoaiDep(realtime.deployment_name)
      else if (deps.length > 0) setSelectedAoaiDep(deps[0].deployment_name)
    } catch { /* ignore */ }
  }

  const handleSelectAoai = (idx: number) => {
    const resource = aoaiList[idx]
    setSelectedAoai(resource)
    loadDeployments(resource)
  }

  const loadAcsPhones = async (resource: AzureResource) => {
    setAcsPhones([])
    try {
      const phones = await api<{ phone_number: string }[]>(
        `setup/voice/acs/phones?name=${encodeURIComponent(resource.name)}&resource_group=${encodeURIComponent(resource.resource_group)}`
      )
      setAcsPhones(phones.map(p => p.phone_number))
      if (phones.length > 0 && !phoneNumber) setPhoneNumber(phones[0].phone_number)
    } catch { /* ignore */ }
  }

  const handleSelectAcs = (idx: number) => {
    const resource = acsList[idx]
    setSelectedAcs(resource)
    if (resource) loadAcsPhones(resource)
    else { setAcsPhones([]); setPhoneNumber('') }
  }

  const handleConnectExisting = async () => {
    if (!selectedAoai) { showToast('Select an Azure OpenAI resource', 'error'); return }
    if (!selectedAoaiDep) { showToast('Select a deployment', 'error'); return }
    setLoading(p => ({ ...p, connect: true }))
    try {
      const body: Record<string, string> = {
        aoai_name: selectedAoai.name,
        aoai_resource_group: selectedAoai.resource_group,
        aoai_deployment: selectedAoaiDep,
      }
      if (!skipAcs && selectedAcs) {
        body.acs_name = selectedAcs.name
        body.acs_resource_group = selectedAcs.resource_group
      }
      if (phoneNumber) body.phone_number = phoneNumber
      if (connectTargetPhone) body.target_number = connectTargetPhone
      await api('setup/voice/connect', { method: 'POST', body: JSON.stringify(body) })
      showToast('Connected to existing resources', 'success')
      await loadConfig()
      onReload()
    } catch (e: any) { showToast(e.message, 'error') }
    setLoading(p => ({ ...p, connect: false }))
  }

  const handleDeployNew = async () => {
    setLoading(p => ({ ...p, deploy: true }))
    try {
      await api('setup/voice/deploy', {
        method: 'POST',
        body: JSON.stringify({ location: deployLocation, voice_resource_group: deployRg }),
      })
      showToast('Voice infrastructure deployed', 'success')
      await loadConfig()
      onReload()
    } catch (e: any) { showToast(e.message, 'error') }
    setLoading(p => ({ ...p, deploy: false }))
  }

  const handleSavePhone = async () => {
    setLoading(p => ({ ...p, phone: true }))
    try {
      const body: Record<string, string> = {}
      if (sourcePhone) body.phone_number = sourcePhone
      if (targetPhone) body.target_number = targetPhone
      await api('setup/voice/phone', { method: 'POST', body: JSON.stringify(body) })
      showToast('Phone number(s) saved', 'success')
      await loadConfig()
      onReload()
    } catch (e: any) { showToast(e.message, 'error') }
    setLoading(p => ({ ...p, phone: false }))
  }

  const handleDecommission = async () => {
    if (!confirm('Decommission voice infrastructure? This will remove ACS and AOAI resources.')) return
    setLoading(p => ({ ...p, decommission: true }))
    try {
      await api('setup/voice/decommission', { method: 'POST' })
      showToast('Voice infrastructure decommissioned', 'success')
      setVoiceConfig(null)
      onReload()
    } catch (e: any) { showToast(e.message, 'error') }
    setLoading(p => ({ ...p, decommission: false }))
  }

  const configured = !!voiceConfig?.acs_resource_name || status?.voice_call_configured

  // -- Already configured view --
  if (configured && voiceConfig) {
    return (
      <div className="voice">
        <div className="voice__status-card">
          <div className="voice__status-header">
            <h3>Voice Call Infrastructure</h3>
            <span className="badge badge--ok">Configured</span>
          </div>

          <div className="voice__resource-grid">
            {voiceConfig.acs_resource_name && (
              <div className="voice__resource-item">
                <label>ACS Resource</label>
                <span>{voiceConfig.acs_resource_name}</span>
              </div>
            )}
            {voiceConfig.azure_openai_resource_name && (
              <div className="voice__resource-item">
                <label>Azure OpenAI</label>
                <span>{voiceConfig.azure_openai_resource_name}</span>
              </div>
            )}
            {voiceConfig.azure_openai_realtime_deployment && (
              <div className="voice__resource-item">
                <label>Deployment</label>
                <span>{voiceConfig.azure_openai_realtime_deployment}</span>
              </div>
            )}
            {(voiceConfig.voice_resource_group || voiceConfig.resource_group) && (
              <div className="voice__resource-item">
                <label>Resource Group</label>
                <span>{voiceConfig.voice_resource_group || voiceConfig.resource_group}</span>
              </div>
            )}
            {voiceConfig.location && (
              <div className="voice__resource-item">
                <label>Location</label>
                <span>{voiceConfig.location}</span>
              </div>
            )}
            {voiceConfig.acs_source_number && (
              <div className="voice__resource-item">
                <label>Source Phone</label>
                <span>{voiceConfig.acs_source_number}</span>
              </div>
            )}
            {voiceConfig.voice_target_number && (
              <div className="voice__resource-item">
                <label>Target Phone</label>
                <span>{voiceConfig.voice_target_number}</span>
              </div>
            )}
          </div>

          {voiceConfig.portal_phone_url && (
            <a href={voiceConfig.portal_phone_url} target="_blank" rel="noopener" className="btn btn--outline btn--sm">
              Manage Phone Numbers in Azure Portal
            </a>
          )}
        </div>

        {/* Phone number config */}
        <div className="voice__panel">
          <div className="voice__panel-header">
            <div>
              <h4>Phone Numbers</h4>
              <p className="text-muted">ACS source number and your phone number (the only number the AI is allowed to call).</p>
            </div>
          </div>
          <div className="voice__panel-body">
            <div className="form">
              <div className="form__row">
                <div className="form__group">
                  <label className="form__label">ACS Source Number</label>
                  {configuredPhones.length > 0 ? (
                    <select className="input" value={sourcePhone} onChange={e => setSourcePhone(e.target.value)}>
                      <option value="">Select a purchased number...</option>
                      {configuredPhones.map(p => (
                        <option key={p} value={p}>{p}</option>
                      ))}
                    </select>
                  ) : (
                    <input className="input" value={sourcePhone} onChange={e => setSourcePhone(e.target.value)} placeholder="+14155551234" />
                  )}
                  <span className="form__hint">The phone number purchased in ACS that the AI calls from.</span>
                </div>
                <div className="form__group">
                  <label className="form__label">Your Phone Number</label>
                  <input className="input" value={targetPhone} onChange={e => setTargetPhone(e.target.value)} placeholder="+41781234567" />
                  <span className="form__hint">Your personal number. The AI is only allowed to call this number.</span>
                </div>
              </div>
              <div className="form__actions">
                <button className="btn btn--primary btn--sm" onClick={handleSavePhone} disabled={loading.phone || (!sourcePhone && !targetPhone)}>
                  {loading.phone ? 'Saving...' : 'Save Phone Numbers'}
                </button>
              </div>
            </div>
          </div>
        </div>

        {/* Decommission */}
        <div className="voice__danger-strip">
          <p>Remove voice infrastructure and clear all configuration.</p>
          <button className="btn btn--danger btn--sm" onClick={handleDecommission} disabled={loading.decommission}>
            {loading.decommission ? 'Decommissioning...' : 'Decommission'}
          </button>
        </div>
      </div>
    )
  }

  // -- Not configured: setup view --
  return (
    <div className="voice">
      {/* Mode selector bar */}
      <div className="voice__mode-bar">
        <button
          className={`voice__mode-btn${mode === 'connect' ? ' voice__mode-btn--active' : ''}`}
          onClick={() => { setMode('connect'); discoverResources() }}
        >
          <div className="voice__mode-icon voice__mode-icon--link">
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M10 13a5 5 0 0 0 7.54.54l3-3a5 5 0 0 0-7.07-7.07l-1.72 1.71"/><path d="M14 11a5 5 0 0 0-7.54-.54l-3 3a5 5 0 0 0 7.07 7.07l1.71-1.71"/></svg>
          </div>
          <div>
            <h4>Connect Existing</h4>
            <p>Link to resources already in your subscription</p>
          </div>
        </button>
        <button
          className={`voice__mode-btn${mode === 'deploy' ? ' voice__mode-btn--active' : ''}`}
          onClick={() => setMode('deploy')}
        >
          <div className="voice__mode-icon voice__mode-icon--new">
            <svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="2" strokeLinecap="round" strokeLinejoin="round"><path d="M4 14.899A7 7 0 1 1 15.71 8h1.79a4.5 4.5 0 0 1 2.5 8.242"/><path d="M12 12v9"/><path d="m8 17 4 4 4-4"/></svg>
          </div>
          <div>
            <h4>Deploy New</h4>
            <p>Provision new ACS + Azure OpenAI resources</p>
          </div>
        </button>
      </div>

      {/* Connect existing form */}
      {mode === 'connect' && (
        <div className="voice__panel">
          <div className="voice__panel-header">
            <div>
              <h4>Connect to Existing Resources</h4>
              <p className="text-muted">Select Azure OpenAI and optionally ACS resources from your subscription.</p>
            </div>
            <button className="btn btn--outline btn--sm" onClick={discoverResources} disabled={loading.discover}>
              {loading.discover ? 'Scanning...' : 'Refresh'}
            </button>
          </div>
          <div className="voice__panel-body">
            <div className="form">
              <div className="voice__section-label">Azure OpenAI</div>

              <div className="form__group">
                <label className="form__label">Resource</label>
                {aoaiList.length === 0 ? (
                  <p className="text-muted" style={{ fontSize: 13 }}>
                    {loading.discover ? 'Scanning subscription...' : 'No Azure OpenAI resources found. Click Refresh or use Deploy New.'}
                  </p>
                ) : (
                  <select
                    className="input"
                    value={selectedAoai ? aoaiList.indexOf(selectedAoai) : ''}
                    onChange={e => handleSelectAoai(Number(e.target.value))}
                  >
                    {aoaiList.map((r, i) => (
                      <option key={r.name} value={i}>{r.name} ({r.resource_group} / {r.location})</option>
                    ))}
                  </select>
                )}
              </div>

              {selectedAoai && (
                <div className="form__group">
                  <label className="form__label">Realtime Deployment</label>
                  {aoaiDeployments.length === 0 ? (
                    <p className="text-muted" style={{ fontSize: 13 }}>No deployments found. Deploy a realtime model (e.g. gpt-realtime-mini) first.</p>
                  ) : (
                    <select
                      className="input"
                      value={selectedAoaiDep}
                      onChange={e => setSelectedAoaiDep(e.target.value)}
                    >
                      {aoaiDeployments.map(d => (
                        <option key={d.deployment_name} value={d.deployment_name}>
                          {d.deployment_name} ({d.model_name} {d.model_version})
                        </option>
                      ))}
                    </select>
                  )}
                  <span className="form__hint">Requires a realtime-capable model (gpt-realtime-mini, gpt-4o-realtime-preview).</span>
                </div>
              )}

              <div className="voice__section-label">Communication Services</div>

              <div className="form__group">
                <label className="form__check">
                  <input type="checkbox" checked={skipAcs} onChange={e => { setSkipAcs(e.target.checked); if (e.target.checked) setSelectedAcs(null) }} />
                  Create a new ACS resource automatically
                </label>
                {!skipAcs && (
                  acsList.length === 0 ? (
                    <p className="text-muted" style={{ fontSize: 13 }}>No ACS resources found. Enable the checkbox above to create one.</p>
                  ) : (
                    <select
                      className="input"
                      value={selectedAcs ? acsList.indexOf(selectedAcs) : ''}
                      onChange={e => handleSelectAcs(Number(e.target.value))}
                    >
                      <option value="">Select an ACS resource...</option>
                      {acsList.map((r, i) => (
                        <option key={r.name} value={i}>{r.name} ({r.resource_group})</option>
                      ))}
                    </select>
                  )
                )}
              </div>

              <div className="voice__section-label">Phone Numbers</div>

              <div className="form__group">
                <label className="form__label">ACS Source Number</label>
                {acsPhones.length > 0 ? (
                  <select className="input" value={phoneNumber} onChange={e => setPhoneNumber(e.target.value)}>
                    <option value="">Select a purchased number...</option>
                    {acsPhones.map(p => (
                      <option key={p} value={p}>{p}</option>
                    ))}
                  </select>
                ) : (
                  <input className="input" value={phoneNumber} onChange={e => setPhoneNumber(e.target.value)} placeholder="+14155551234" />
                )}
                <span className="form__hint">{selectedAcs && acsPhones.length === 0 ? 'No purchased numbers found on this ACS resource. You can add one later.' : 'The number the AI calls from. Can be configured later.'}</span>
              </div>

              <div className="form__group">
                <label className="form__label">Your Phone Number</label>
                <input className="input" value={connectTargetPhone} onChange={e => setConnectTargetPhone(e.target.value)} placeholder="+41781234567" />
                <span className="form__hint">Your personal number. The AI is only allowed to call this number.</span>
              </div>

              <div className="form__actions">
                <button
                  className="btn btn--primary"
                  onClick={handleConnectExisting}
                  disabled={loading.connect || !selectedAoai || !selectedAoaiDep}
                >
                  {loading.connect ? 'Connecting...' : 'Connect Resources'}
                </button>
              </div>
            </div>
          </div>
        </div>
      )}

      {/* Deploy new form */}
      {mode === 'deploy' && (
        <div className="voice__panel">
          <div className="voice__panel-header">
            <div>
              <h4>Deploy New Voice Infrastructure</h4>
              <p className="text-muted">Creates a resource group with ACS and Azure OpenAI (gpt-realtime-mini).</p>
            </div>
          </div>
          <div className="voice__panel-body">
            <div className="form">
              <div className="form__row">
                <div className="form__group">
                  <label className="form__label">Resource Group</label>
                  <input className="input" value={deployRg} onChange={e => setDeployRg(e.target.value)} />
                </div>
                <div className="form__group">
                  <label className="form__label">Location</label>
                  <input className="input" value={deployLocation} onChange={e => setDeployLocation(e.target.value)} />
                  <span className="form__hint">Must support Azure OpenAI realtime models (e.g. swedencentral, eastus2).</span>
                </div>
              </div>
              <div className="form__actions">
                <button className="btn btn--primary" onClick={handleDeployNew} disabled={loading.deploy}>
                  {loading.deploy ? 'Deploying...' : 'Deploy Voice Infrastructure'}
                </button>
              </div>
            </div>
          </div>
        </div>
      )}
    </div>
  )
}

