import { useState, useEffect, useCallback, useRef } from 'react'
import { useNavigate } from 'react-router-dom'
import { api } from '../api'
import { showToast } from '../components/Toast'
import type { SetupStatus, SandboxConfig, FoundryIQConfig, ContentSafetyConfig } from '../types'

type Step = 'azure' | 'github' | 'config' | 'deploy'

interface VoiceConfig {
  acs_resource_name?: string
  acs_source_number?: string
  [k: string]: unknown
}

const STEPS: { key: Step; label: string; description: string }[] = [
  { key: 'azure', label: 'Azure', description: 'Sign in with Azure CLI to manage cloud resources' },
  { key: 'github', label: 'GitHub', description: 'Authenticate with GitHub to power the AI agent' },
  { key: 'config', label: 'Channels', description: 'Connect messaging channels like Telegram' },
  { key: 'deploy', label: 'Bot', description: 'Provision Azure Bot Service and connect channels' },
]

export default function SetupWizard() {
  const navigate = useNavigate()
  const [status, setStatus] = useState<SetupStatus | null>(null)
  const [currentStep, setCurrentStep] = useState<Step>('azure')
  const [loading, setLoading] = useState<Record<string, boolean>>({})
  const manualStepRef = useRef(false)

  // Optional infra state
  const [voiceConfig, setVoiceConfig] = useState<VoiceConfig | null>(null)
  const [sandboxConfig, setSandboxConfig] = useState<SandboxConfig | null>(null)
  const [foundryConfig, setFoundryConfig] = useState<FoundryIQConfig | null>(null)
  const [contentSafetyConfig, setContentSafetyConfig] = useState<ContentSafetyConfig | null>(null)

  // Device code state
  const [azureDevice, setAzureDevice] = useState<{ code: string; url: string } | null>(null)
  const [githubDevice, setGithubDevice] = useState<{ code: string; url: string } | null>(null)
  const [countdown, setCountdown] = useState<number | null>(null)
  const azureDeviceRef = useRef(false)
  const githubDeviceRef = useRef(false)

  const refresh = useCallback(async () => {
    try {
      const s = await api<SetupStatus>('setup/status')
      setStatus(s)
      // Auto-advance steps (skip if device code flow active or user clicked a step)
      if (!manualStepRef.current) {
        if (!azureDeviceRef.current && s.azure?.logged_in && currentStep === 'azure') setCurrentStep('github')
        if (!githubDeviceRef.current && s.azure?.logged_in && s.copilot?.authenticated && currentStep === 'github') setCurrentStep('config')
      }
    } catch { /* ignore */ }
    // Load optional infra status
    try { setVoiceConfig(await api<VoiceConfig>('setup/voice/config')) } catch { /* ignore */ }
    try { setSandboxConfig(await api<SandboxConfig>('sandbox/config')) } catch { /* ignore */ }
    try { setFoundryConfig(await api<FoundryIQConfig>('foundry-iq/config')) } catch { /* ignore */ }
    try { setContentSafetyConfig(await api<ContentSafetyConfig>('content-safety/status')) } catch { /* ignore */ }
  }, [currentStep])

  useEffect(() => { refresh() }, [refresh])

  const setupDone = status?.azure?.logged_in && status?.copilot?.authenticated && status?.bot_configured
  const botDeployed = !!status?.bot_deployed

  /** Show code, start countdown, open URL after 3s, then poll. */
  const startDeviceFlow = (code: string, url: string, setDevice: typeof setAzureDevice, openUrl: string) => {
    setDevice({ code, url })
    setCountdown(3)
    let t = 3
    const iv = setInterval(() => {
      t -= 1
      setCountdown(t)
      if (t <= 0) {
        clearInterval(iv)
        setCountdown(null)
        window.open(openUrl, '_blank')
      }
    }, 1000)
  }

  const handleAzureLogin = async (force?: boolean) => {
    setLoading(p => ({ ...p, azure: true }))
    azureDeviceRef.current = true
    try {
      // When re-authenticating, log out first so the backend starts a fresh device flow
      if (force) {
        await api('setup/azure/logout', { method: 'POST' }).catch(() => {})
      }
      const r = await api<{ status: string; code?: string; url?: string; message?: string }>('setup/azure/login', { method: 'POST' })
      if (r.status === 'already_logged_in') {
        showToast('Already signed in to Azure', 'success')
        azureDeviceRef.current = false
        await refresh()
      } else if (r.code && r.url) {
        startDeviceFlow(r.code, r.url, setAzureDevice, r.url)
        // Poll for completion
        for (let i = 0; i < 120; i++) {
          await new Promise(res => setTimeout(res, 3000))
          const check = await api<{ status: string }>('setup/azure/check')
          if (check.status === 'logged_in') {
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

  const handleCopilotLogin = async () => {
    setLoading(p => ({ ...p, github: true }))
    githubDeviceRef.current = true
    try {
      const r = await api<{ status: string; message?: string; code?: string; url?: string; user_code?: string; verification_uri?: string }>('setup/copilot/login', { method: 'POST' })
      const code = r.code || r.user_code
      const url = r.url || r.verification_uri
      if (code && url) {
        startDeviceFlow(code, url, setGithubDevice, url)
        // Poll for completion
        for (let i = 0; i < 120; i++) {
          await new Promise(res => setTimeout(res, 3000))
          const check = await api<{ authenticated: boolean }>('setup/copilot/status')
          if (check.authenticated) {
            showToast('GitHub authenticated!', 'success')
            setGithubDevice(null)
            githubDeviceRef.current = false
            break
          }
        }
      } else {
        githubDeviceRef.current = false
        showToast(r.message || 'Login initiated', 'info')
      }
      await refresh()
    } catch (e: any) {
      githubDeviceRef.current = false
      showToast(e.message, 'error')
    }
    setLoading(p => ({ ...p, github: false }))
  }

  const handleSaveConfig = async (e: React.FormEvent<HTMLFormElement>) => {
    e.preventDefault()
    setLoading(p => ({ ...p, config: true }))
    const fd = new FormData(e.currentTarget)
    const token = (fd.get('telegram_token') as string || '').trim()
    const whitelist = (fd.get('telegram_whitelist') as string || '').trim()
    const body = {
      telegram: { token, whitelist },
      bot: {},
    }
    try {
      await api('setup/configuration/save', { method: 'POST', body: JSON.stringify(body) })
      showToast('Configuration saved!', 'success')
      await refresh()
      setCurrentStep('deploy')
    } catch (e: any) {
      showToast(e.message, 'error')
    }
    setLoading(p => ({ ...p, config: false }))
  }

  return (
    <div className="page page--setup">
      <div className="setup">
        <div className="setup__header">
          <img src="/logo.png" alt="polyclaw" className="setup__logo" />
          <p>Complete the initial setup to get started. Azure and GitHub authentication are required.</p>
        </div>

        {/* Progress Steps */}
        <div className="setup__steps">
          {STEPS.map((step, i) => {
            const azureDone = !!status?.azure?.logged_in
            const githubDone = azureDone && !!status?.copilot?.authenticated
            const configDone = githubDone && !!status?.telegram_configured
            const deployDone = configDone && !!status?.bot_deployed
            const done = step.key === 'azure' ? azureDone
              : step.key === 'github' ? githubDone
              : step.key === 'config' ? configDone
              : deployDone
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

        {/* Step Content */}
        <div className="setup__content card">
          {currentStep === 'azure' && (
            <div className="setup__panel">
              <h2>Azure</h2>
              <p>Sign in to Azure to enable cloud resource management, infrastructure provisioning, and bot deployment.</p>
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
              ) : status?.azure?.logged_in ? (
                <div className="setup__done">
                  <span className="badge badge--ok">Authenticated</span>
                  {status.azure.subscription && (
                    <p className="text-muted">Subscription: {status.azure.subscription}</p>
                  )}
                  <div style={{ display: 'flex', gap: 8, marginTop: 8 }}>
                    <button className="btn btn--secondary" onClick={() => { manualStepRef.current = false; setCurrentStep('github') }}>Continue</button>
                    <button className="btn btn--outline" onClick={() => handleAzureLogin(true)} disabled={loading.azure}>
                      {loading.azure ? 'Starting...' : 'Re-authenticate'}
                    </button>
                  </div>
                </div>
              ) : (
                <button className="btn btn--primary" onClick={() => handleAzureLogin()} disabled={loading.azure}>
                  {loading.azure ? 'Starting...' : 'Sign in with Azure CLI'}
                </button>
              )}
            </div>
          )}

          {currentStep === 'github' && (
            <div className="setup__panel">
              <h2>GitHub</h2>
              <p>Authenticate with GitHub to enable the AI agent powered by Copilot.</p>
              {githubDevice ? (
                <div className="setup__device-code">
                  <p>Copy the code below, then sign in at the link:</p>
                  <div className="setup__code-display">
                    <span className="setup__code-value">{githubDevice.code}</span>
                    <button className="btn btn--secondary btn--sm setup__copy-btn" onClick={() => { navigator.clipboard.writeText(githubDevice.code); showToast('Code copied!', 'success') }}>Copy</button>
                  </div>
                  {countdown !== null ? (
                    <p className="text-muted mt-2">Opening browser in {countdown}...</p>
                  ) : (
                    <>
                      <a href={githubDevice.url} target="_blank" rel="noopener" className="setup__code-link">{githubDevice.url}</a>
                      <p className="text-muted mt-2">Waiting for authentication...</p>
                    </>
                  )}
                </div>
              ) : status?.copilot?.authenticated ? (
                <div className="setup__done">
                  <span className="badge badge--ok">Authenticated</span>
                  {status.copilot.username && (
                    <p className="text-muted">User: {status.copilot.username}</p>
                  )}
                  <div style={{ display: 'flex', gap: 8, marginTop: 8 }}>
                    <button className="btn btn--secondary" onClick={() => { manualStepRef.current = false; setCurrentStep('config') }}>Continue</button>
                    <button className="btn btn--outline" onClick={handleCopilotLogin} disabled={loading.github}>
                      {loading.github ? 'Starting...' : 'Re-authenticate'}
                    </button>
                  </div>
                </div>
              ) : (
                <div>
                  <button className="btn btn--primary" onClick={handleCopilotLogin} disabled={loading.github}>
                    {loading.github ? 'Waiting for auth...' : 'Authenticate with GitHub'}
                  </button>
                  <p className="text-muted mt-2">A device code will be shown. Enter it at github.com to complete authentication.</p>
                </div>
              )}
            </div>
          )}

          {currentStep === 'config' && (
            <div className="setup__panel">
              <h2>Messaging Channels</h2>
              <p>Connect a Telegram bot to chat with polyclaw on Telegram. This is optional -- you can skip and configure it later from Settings.</p>
              <form onSubmit={handleSaveConfig} className="form">
                <div className="form__group">
                  <label className="form__label">Telegram Bot Token</label>
                  <input name="telegram_token" type="password" className="input" placeholder="e.g. 123456789:ABCdef..." />
                  <span className="form__hint">Get this from <a href="https://t.me/BotFather" target="_blank" rel="noopener">@BotFather</a> on Telegram.</span>
                </div>
                <div className="form__group">
                  <label className="form__label">Allowed User IDs</label>
                  <input name="telegram_whitelist" className="input" placeholder="Comma-separated Telegram user IDs" />
                  <span className="form__hint">Only these users can interact with the bot. Leave empty to allow all.</span>
                </div>
                <div className="form__actions">
                  <button type="submit" className="btn btn--primary" disabled={loading.config}>
                    {loading.config ? 'Saving...' : 'Save & Continue'}
                  </button>
                  <button type="button" className="btn btn--secondary" onClick={() => {
                    // Skip config, just save empty and move on
                    api('setup/configuration/save', { method: 'POST', body: JSON.stringify({ telegram: {}, bot: {} }) })
                      .then(() => { refresh(); setCurrentStep('deploy') })
                      .catch(() => {})
                  }}>Skip for now</button>
                </div>
              </form>
            </div>
          )}

          {currentStep === 'deploy' && (
            <div className="setup__panel">
              <h2>Bot</h2>
              {botDeployed ? (
                <div className="setup__done">
                  <span className="badge badge--ok">Deployed</span>
                  <p className="text-muted">Azure Bot Service is running. Telegram and other channels are connected.</p>
                  <button className="btn btn--primary" onClick={() => navigate('/chat')}>Start Chatting</button>
                </div>
              ) : (
                <>
                  <p>Deploy the Azure Bot Service to enable Telegram and other messaging channels. This will:</p>
                  <ul className="setup__deploy-list">
                    <li>Start a Cloudflare tunnel to expose your bot</li>
                    <li>Create an Azure Bot Service with an App Registration</li>
                    {status?.telegram_configured && <li>Connect Telegram as a messaging channel</li>}
                  </ul>
                  <div className="form__actions">
                    <button className="btn btn--primary" disabled={loading.deploy} onClick={async () => {
                      setLoading(p => ({ ...p, deploy: true }))
                      try {
                        await api('setup/infra/deploy', { method: 'POST' })
                        showToast('Bot deployed successfully!', 'success')
                        await refresh()
                      } catch (e: any) {
                        showToast(e.message, 'error')
                      }
                      setLoading(p => ({ ...p, deploy: false }))
                    }}>
                      {loading.deploy ? 'Deploying...' : 'Deploy Bot Service'}
                    </button>
                    <button className="btn btn--secondary" onClick={() => navigate('/chat')}>Skip for now</button>
                  </div>
                </>
              )}
            </div>
          )}
        </div>

        {setupDone && botDeployed && (
          <div className="setup__complete">
            <p>Setup complete! Your bot is deployed and ready to use.</p>
            <button className="btn btn--primary btn--lg" onClick={() => navigate('/chat')}>
              Start Chatting
            </button>
          </div>
        )}

        {setupDone && !botDeployed && currentStep !== 'deploy' && (
          <div className="setup__complete">
            <p>Configuration saved. Deploy the bot to enable Telegram and other channels.</p>
            <button className="btn btn--primary btn--lg" onClick={() => setCurrentStep('deploy')}>
              Deploy Bot Service
            </button>
            <button className="btn btn--secondary" style={{ marginLeft: '0.5rem' }} onClick={() => navigate('/chat')}>
              Skip &amp; Chat via Web
            </button>
          </div>
        )}

        {/* Optional Infrastructure */}
        {setupDone && status?.azure?.logged_in && (
          <div className="setup__optional">
            <h2 className="setup__optional-title">Optional Infrastructure</h2>
            <p className="setup__optional-desc">Provision additional Azure resources. These are not required to use polyclaw.</p>

            <div className="setup__optional-grid">
              <div className="setup__opt-row">
                <div className="setup__opt-info">
                  <span className="setup__opt-name">Content Safety <span className="badge badge--warn badge--sm">Recommended</span></span>
                  <span className="setup__opt-desc">Prompt Shields injection detection</span>
                </div>
                {contentSafetyConfig?.deployed
                  ? <span className="badge badge--ok">Deployed</span>
                  : <button className="btn btn--secondary btn--sm" disabled={loading.contentSafety} onClick={async () => {
                      setLoading(p => ({ ...p, contentSafety: true }))
                      try { await api('content-safety/deploy', { method: 'POST' }); showToast('Content Safety deployed', 'success'); await refresh() }
                      catch (e: any) { showToast(e.message, 'error') }
                      setLoading(p => ({ ...p, contentSafety: false }))
                    }}>{loading.contentSafety ? 'Deploying...' : 'Deploy'}</button>}
              </div>

              <div className="setup__opt-row">
                <div className="setup__opt-info">
                  <span className="setup__opt-name">Voice Calling</span>
                  <span className="setup__opt-desc">ACS + OpenAI Realtime</span>
                </div>
                {voiceConfig?.acs_resource_name
                  ? <span className="badge badge--ok">Provisioned</span>
                  : <button className="btn btn--secondary btn--sm" disabled={loading.voice} onClick={async () => {
                      setLoading(p => ({ ...p, voice: true }))
                      try { await api('setup/voice/deploy', { method: 'POST' }); showToast('Voice provisioning started', 'success'); await refresh() }
                      catch (e: any) { showToast(e.message, 'error') }
                      setLoading(p => ({ ...p, voice: false }))
                    }}>{loading.voice ? 'Provisioning...' : 'Provision'}</button>}
              </div>

              <div className="setup__opt-row">
                <div className="setup__opt-info">
                  <span className="setup__opt-name">Agent Sandbox <span className="badge badge--accent badge--sm">Experimental</span></span>
                  <span className="setup__opt-desc">Sandboxed code execution</span>
                </div>
                {sandboxConfig?.is_provisioned
                  ? <span className="badge badge--ok">Provisioned</span>
                  : <button className="btn btn--secondary btn--sm" disabled={loading.sandbox} onClick={async () => {
                      setLoading(p => ({ ...p, sandbox: true }))
                      try { await api('sandbox/provision', { method: 'POST' }); showToast('Sandbox provisioning started', 'success'); await refresh() }
                      catch (e: any) { showToast(e.message, 'error') }
                      setLoading(p => ({ ...p, sandbox: false }))
                    }}>{loading.sandbox ? 'Provisioning...' : 'Provision'}</button>}
              </div>

              <div className="setup__opt-row">
                <div className="setup__opt-info">
                  <span className="setup__opt-name">Foundry IQ</span>
                  <span className="setup__opt-desc">AI Search + embeddings</span>
                </div>
                {foundryConfig?.provisioned
                  ? <span className="badge badge--ok">Provisioned</span>
                  : <button className="btn btn--secondary btn--sm" disabled={loading.foundry} onClick={async () => {
                      setLoading(p => ({ ...p, foundry: true }))
                      try { await api('foundry-iq/provision', { method: 'POST' }); showToast('Foundry IQ provisioning started', 'success'); await refresh() }
                      catch (e: any) { showToast(e.message, 'error') }
                      setLoading(p => ({ ...p, foundry: false }))
                    }}>{loading.foundry ? 'Provisioning...' : 'Provision'}</button>}
              </div>
            </div>
          </div>
        )}
      </div>
    </div>
  )
}
