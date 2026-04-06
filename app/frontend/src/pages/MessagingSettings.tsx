import { useState, useEffect, useCallback } from 'react'
import { api } from '../api'
import { showToast } from '../components/Toast'
import { ProactiveContent } from './Proactive'
import type { ModelInfo } from '../types'

type Tab = 'config' | 'proactive'

export default function MessagingSettings() {
  const [tab, setTab] = useState<Tab>('config')
  const [models, setModels] = useState<ModelInfo[]>([])
  const [currentModel, setCurrentModel] = useState('')
  const [loading, setLoading] = useState<Record<string, boolean>>({})

  const loadAll = useCallback(async () => {
    try {
      const [cfg, mdl] = await Promise.all([
        api<Record<string, string>>('setup/config'),
        api<{ models: ModelInfo[]; current: string }>('models'),
      ])
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

  return (
    <div className="page">
      <div className="page__header">
        <h1>AI Model</h1>
      </div>

      <div className="tabs">
        {([
          ['config', 'AI Model'],
          ['proactive', 'Proactive'],
        ] as [Tab, string][]).map(([t, label]) => (
          <button key={t} className={`tab ${tab === t ? 'tab--active' : ''}`} onClick={() => setTab(t)}>
            {label}
          </button>
        ))}
      </div>

      {tab === 'config' && (
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
      )}

      {/* Proactive */}
      {tab === 'proactive' && <ProactiveContent />}
    </div>
  )
}
