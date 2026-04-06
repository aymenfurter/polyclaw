import { useState, useEffect, useCallback, useRef } from 'react'
import { useNavigate } from 'react-router-dom'
import { api } from '../api'
import { showToast } from '../components/Toast'
import type { SetupStatus } from '../types'

type Step = 'azure' | 'foundry'

interface AzureSubscription {
  id: string
  name: string
  is_default: boolean
  state: string
}

const STEPS: { key: Step; label: string }[] = [
  { key: 'azure', label: 'Azure' },
  { key: 'foundry', label: 'Foundry' },
]

export default function SetupWizard() {
  const navigate = useNavigate()
  const [status, setStatus] = useState<SetupStatus | null>(null)
  const [currentStep, setCurrentStep] = useState<Step>('azure')
  const [loading, setLoading] = useState<Record<string, boolean>>({})
  const manualStepRef = useRef(false)

  const [azureDevice, setAzureDevice] = useState<{ code: string; url: string } | null>(null)
  const [countdown, setCountdown] = useState<number | null>(null)
  const azureDeviceRef = useRef(false)

  // Subscription picker state
  const [subscriptions, setSubscriptions] = useState<AzureSubscription[]>([])
  const [selectedSub, setSelectedSub] = useState('')

  const azureReady = !!status?.azure?.logged_in && !status?.azure?.needs_subscription

  const refresh = useCallback(async () => {
    try {
      const s = await api<SetupStatus>('setup/status')
      setStatus(s)

      // Load subscriptions when logged in but no default sub
      if (s.azure?.logged_in && s.azure?.needs_subscription) {
        const subs = await api<AzureSubscription[]>('setup/azure/subscriptions')
        setSubscriptions(subs)
        if (subs.length === 1) setSelectedSub(subs[0].id)
      }

      if (!manualStepRef.current && !azureDeviceRef.current) {
        const azDone = !!s.azure?.logged_in && !s.azure?.needs_subscription
        const fDone = azDone && !!s.foundry?.deployed
        if (fDone) { navigate('/chat'); return }
        if (azDone && currentStep === 'azure') setCurrentStep('foundry')
      }
    } catch { /* ignore */ }
  }, [currentStep, navigate])

  useEffect(() => { refresh() }, [refresh])

  const handleAzureLogin = async (force?: boolean) => {
    setLoading(p => ({ ...p, azure: true }))
    azureDeviceRef.current = true
    try {
      if (force) await api('setup/azure/logout', { method: 'POST' }).catch(() => {})
      const r = await api<{ status: string; code?: string; url?: string; message?: string }>('setup/azure/login', { method: 'POST' })
      if (r.status === 'already_logged_in') {
        showToast('Already signed in to Azure', 'success')
        azureDeviceRef.current = false
        await refresh()
      } else if (r.status === 'needs_subscription') {
        azureDeviceRef.current = false
        await refresh()
      } else if (r.code && r.url) {
        setAzureDevice({ code: r.code, url: r.url })
        setCountdown(3)
        let t = 3
        const iv = setInterval(() => {
          t -= 1
          setCountdown(t)
          if (t <= 0) { clearInterval(iv); setCountdown(null); window.open(r.url!, '_blank') }
        }, 1000)
        for (let i = 0; i < 120; i++) {
          await new Promise(res => setTimeout(res, 3000))
          const check = await api<{ status: string }>('setup/azure/check')
          if (check.status === 'logged_in' || check.status === 'needs_subscription') {
            showToast('Azure authenticated!', 'success')
            setAzureDevice(null)
            azureDeviceRef.current = false
            break
          }
        }
        await refresh()
      } else {
        azureDeviceRef.current = false
        showToast(r.message || 'Azure login initiated', 'info')
      }
    } catch (e: any) {
      azureDeviceRef.current = false
      showToast(e.message, 'error')
    }
    setLoading(p => ({ ...p, azure: false }))
  }

  const handleSetSubscription = async () => {
    if (!selectedSub) return
    setLoading(p => ({ ...p, subscription: true }))
    try {
      await api('setup/azure/subscription', {
        method: 'POST',
        body: JSON.stringify({ subscription_id: selectedSub }),
      })
      const sub = subscriptions.find(s => s.id === selectedSub)
      showToast(`Subscription set: ${sub?.name || selectedSub}`, 'success')
      setSubscriptions([])
      await refresh()
    } catch (e: any) { showToast(e.message, 'error') }
    setLoading(p => ({ ...p, subscription: false }))
  }

  const handleFoundryDeploy = async () => {
    setLoading(p => ({ ...p, foundry: true }))
    try {
      const r = await api<{ status: string; foundry_endpoint?: string; deployed_models?: string[]; error?: string }>('setup/foundry/deploy', {
        method: 'POST',
        body: JSON.stringify({ resource_group: 'polyclaw-rg', location: 'eastus' }),
      })
      if (r.status === 'ok') {
        showToast(`Foundry deployed: ${r.deployed_models?.join(', ') || 'models ready'}`, 'success')
      } else {
        showToast(r.error || 'Deployment failed', 'error')
      }
      await refresh()
    } catch (e: any) { showToast(e.message, 'error') }
    setLoading(p => ({ ...p, foundry: false }))
  }

  const setupDone = azureReady && status?.foundry?.deployed

  return (
    <div className="page page--setup">
      <div className="setup">
        <div className="setup__header">
          <img src="/logo.png" alt="polyclaw" className="setup__logo" />
          <p>Complete the initial setup to get started. Azure sign-in and Foundry deployment are required.</p>
        </div>

        <div className="setup__steps">
          {STEPS.map((step, i) => {
            const azDone = azureReady
            const fDone = azDone && !!status?.foundry?.deployed
            const done = step.key === 'azure' ? azDone : fDone
            return (
              <button
                key={step.key}
                className={`setup__step ${currentStep === step.key ? 'setup__step--active' : ''} ${done ? 'setup__step--done' : ''}`}
                onClick={() => { manualStepRef.current = true; setCurrentStep(step.key) }}
              >
                <span className="setup__step-num">{done ? '\u2713' : i + 1}</span>
                <span className="setup__step-label">{step.label}</span>
              </button>
            )
          })}
        </div>

        <div className="setup__content card">
          {currentStep === 'azure' && (
            <div className="setup__panel">
              <h2>Azure</h2>
              <p>Sign in to Azure to enable cloud resource management and infrastructure provisioning.</p>

              {/* Device code flow in progress */}
              {azureDevice ? (
                <div className="setup__device-code">
                  <p>Copy the code below, then sign in at the link:</p>
                  <div className="setup__code-display">
                    <span className="setup__code-value">{azureDevice.code}</span>
                    <button className="btn btn--secondary btn--sm setup__copy-btn" onClick={() => { navigator.clipboard.writeText(azureDevice.code); showToast('Code copied!', 'success') }}>Copy</button>
                  </div>
                  {countdown !== null ? (
                    <p className="text-muted mt-2">Opening browser in {countdown}...</p>
                  ) : (
                    <>
                      <a href={azureDevice.url} target="_blank" rel="noopener" className="setup__code-link">{azureDevice.url}</a>
                      <p className="text-muted mt-2">Waiting for authentication...</p>
                    </>
                  )}
                </div>

              /* Logged in but needs subscription selection */
              ) : status?.azure?.logged_in && status.azure.needs_subscription ? (
                <div className="setup__panel">
                  <span className="badge badge--ok">Authenticated</span>
                  <p className="text-muted mt-1">No default subscription set. Select which Azure subscription to use:</p>
                  {subscriptions.length > 0 ? (
                    <>
                      <div className="setup__sub-list">
                        {subscriptions.map(sub => (
                          <label key={sub.id} className="setup__sub-option">
                            <input
                              type="radio"
                              name="subscription"
                              value={sub.id}
                              checked={selectedSub === sub.id}
                              onChange={() => setSelectedSub(sub.id)}
                            />
                            <div className="setup__sub-info">
                              <span className="setup__sub-name">{sub.name}</span>
                              <span className="setup__sub-id">{sub.id}</span>
                            </div>
                          </label>
                        ))}
                      </div>
                      <button
                        className="btn btn--primary mt-2"
                        onClick={handleSetSubscription}
                        disabled={!selectedSub || loading.subscription}
                      >
                        {loading.subscription ? 'Setting...' : 'Use This Subscription'}
                      </button>
                    </>
                  ) : (
                    <p className="text-muted">Loading subscriptions...</p>
                  )}
                </div>

              /* Fully authenticated with subscription */
              ) : azureReady ? (
                <div className="setup__done">
                  <span className="badge badge--ok">Authenticated</span>
                  {status?.azure?.subscription && <p className="text-muted">Subscription: {status.azure.subscription}</p>}
                  <div style={{ display: 'flex', gap: 8, marginTop: 8 }}>
                    <button className="btn btn--secondary" onClick={() => { manualStepRef.current = false; setCurrentStep('foundry') }}>Continue</button>
                    <button className="btn btn--outline" onClick={() => handleAzureLogin(true)} disabled={loading.azure}>
                      {loading.azure ? 'Starting...' : 'Re-authenticate'}
                    </button>
                  </div>
                </div>

              /* Not logged in */
              ) : (
                <button className="btn btn--primary" onClick={() => handleAzureLogin()} disabled={loading.azure}>
                  {loading.azure ? 'Starting...' : 'Sign in with Azure CLI'}
                </button>
              )}
            </div>
          )}

          {currentStep === 'foundry' && (
            <div className="setup__panel">
              <h2>Microsoft Foundry</h2>
              <p>Deploy AI models (gpt-4.1, gpt-5, gpt-5-mini) to your Azure subscription via Bicep. This also creates a Key Vault for secrets management.</p>
              {status?.foundry?.deployed ? (
                <div className="setup__done">
                  <span className="badge badge--ok">Deployed</span>
                  <p className="text-muted">Endpoint: {status.foundry.endpoint}</p>
                  {status.foundry.name && <p className="text-muted">Resource: {status.foundry.name}</p>}
                  <div style={{ display: 'flex', gap: 8, marginTop: 8 }}>
                    <button className="btn btn--primary" onClick={() => navigate('/chat')}>Start Chatting</button>
                    <button className="btn btn--outline" onClick={handleFoundryDeploy} disabled={loading.foundry}>
                      {loading.foundry ? 'Deploying...' : 'Redeploy'}
                    </button>
                  </div>
                </div>
              ) : (
                <div>
                  <button className="btn btn--primary" onClick={handleFoundryDeploy} disabled={loading.foundry}>
                    {loading.foundry ? 'Deploying infrastructure...' : 'Deploy Foundry Infrastructure'}
                  </button>
                  <p className="text-muted mt-2">Creates an AI Services resource with model deployments and a Key Vault. Uses Entra ID authentication (no API keys).</p>
                </div>
              )}
            </div>
          )}
        </div>

        {setupDone && (
          <div className="setup__complete">
            <p>Setup complete! Configure channels, bot service, and more from <button className="btn btn--link" onClick={() => navigate('/infrastructure')}>Infrastructure</button>.</p>
            <button className="btn btn--primary btn--lg" onClick={() => navigate('/chat')}>Start Chatting</button>
          </div>
        )}
      </div>
    </div>
  )
}
