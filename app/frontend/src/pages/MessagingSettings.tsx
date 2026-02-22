import { useState, useEffect, useCallback } from 'react'
import { api } from '../api'
import { showToast } from '../components/Toast'
import { ProactiveContent } from './Proactive'
import type { SetupStatus, ModelInfo } from '../types'

type Tab = 'config' | 'proactive'

export default function MessagingSettings() {
  const [tab, setTab] = useState<Tab>('config')
  const [status, setStatus] = useState<SetupStatus | null>(null)
  const [models, setModels] = useState<ModelInfo[]>([])
  const [currentModel, setCurrentModel] = useState('')
  const [loading, setLoading] = useState<Record<string, boolean>>({})

  // Channel state
  const [telegramToken, setTelegramToken] = useState('')

  const loadAll = useCallback(async () => {
    try {
      const [s, cfg, mdl] = await Promise.all([
        api<SetupStatus>('setup/status'),
        api<Record<string, string>>('setup/config'),
        api<{ models: ModelInfo[]; current: string }>('models'),
      ])
      setStatus(s)
      setModels(mdl.models || [])
      setCurrentModel(cfg.COPILOT_MODEL || mdl.current || '')
    } catch { /* ignore */ }
  }, [])

  useEffect(() => { loadAll() }, [loadAll])

  const saveModel = async () => {
    setLoading(p => ({ ...p, model: true }))
    try {
      await api('setup/config', {
        method: 'POST',
        body: JSON.stringify({ COPILOT_MODEL: currentModel }),
      })
      showToast('Model saved', 'success')
    } catch (e: any) { showToast(e.message, 'error') }
    setLoading(p => ({ ...p, model: false }))
  }

  const saveTelegram = async () => {
    if (!telegramToken) return
    setLoading(p => ({ ...p, telegram: true }))
    try {
      await api('setup/channels/telegram/config', {
        method: 'POST',
        body: JSON.stringify({ token: telegramToken }),
      })
      showToast('Telegram configured', 'success')
      loadAll()
    } catch (e: any) { showToast(e.message, 'error') }
    setLoading(p => ({ ...p, telegram: false }))
  }

  const removeTelegram = async () => {
    if (!confirm('Remove Telegram configuration?')) return
    try {
      await api('setup/channels/telegram/remove', { method: 'POST' })
      showToast('Telegram removed', 'success')
      loadAll()
    } catch (e: any) { showToast(e.message, 'error') }
  }

  return (
    <div className="page">
      <div className="page__header">
        <h1>Messaging</h1>
      </div>

      <div className="tabs">
        {([
          ['config', 'AI Model & Channels'],
          ['proactive', 'Proactive'],
        ] as [Tab, string][]).map(([t, label]) => (
          <button key={t} className={`tab ${tab === t ? 'tab--active' : ''}`} onClick={() => setTab(t)}>
            {label}
          </button>
        ))}
      </div>

      {tab === 'config' && (
        <>
          {/* AI Model */}
          <div className="card">
            <h3>Default AI Model</h3>
            <p className="text-muted">Choose the model used for conversations.</p>
            <div className="form">
              <div className="form__group">
                <label className="form__label">Model</label>
                <select className="input" value={currentModel} onChange={e => setCurrentModel(e.target.value)}>
                  {models.map(m => (
                    <option key={m.id} value={m.id} disabled={m.policy === 'disabled'}>
                      {m.name || m.id}
                      {m.billing_multiplier && m.billing_multiplier !== 1 ? ` (${m.billing_multiplier}x)` : ''}
                      {m.policy === 'disabled' ? ' [disabled]' : ''}
                    </option>
                  ))}
                </select>
              </div>
              <button className="btn btn--primary" onClick={saveModel} disabled={loading.model}>
                {loading.model ? 'Saving...' : 'Save Model'}
              </button>
            </div>
          </div>

          {/* Channels */}
          <div className="card">
            <h3>Channel Configuration</h3>

            <div className="card__section">
              <h4>Telegram</h4>
              {status?.telegram_configured ? (
                <div>
                  <span className="badge badge--ok">Configured</span>
                  <button className="btn btn--danger btn--sm ml-2" onClick={removeTelegram}>Remove</button>
                </div>
              ) : (
                <div className="form">
                  <div className="form__group">
                    <label className="form__label">Bot Token</label>
                    <input
                      type="password"
                      className="input"
                      value={telegramToken}
                      onChange={e => setTelegramToken(e.target.value)}
                      placeholder="Bot token from @BotFather"
                    />
                  </div>
                  <button className="btn btn--primary btn--sm" onClick={saveTelegram} disabled={loading.telegram || !telegramToken}>
                    {loading.telegram ? 'Saving...' : 'Configure'}
                  </button>
                </div>
              )}
            </div>
          </div>
        </>
      )}

      {/* Proactive */}
      {tab === 'proactive' && <ProactiveContent />}
    </div>
  )
}
