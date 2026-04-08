import { useState, useEffect, useRef, useCallback } from 'react'
import { api } from '../api'
import type { SetupStatus } from '../types'

export function useStatus(intervalMs = 30_000) {
  const [status, setStatus] = useState<SetupStatus | null>(null)
  const timerRef = useRef<ReturnType<typeof setInterval>>(null)

  const refresh = useCallback(async () => {
    try {
      const s = await api<SetupStatus>('setup/status')
      setStatus(s)
    } catch { /* ignore */ }
  }, [])

  useEffect(() => {
    refresh()
    timerRef.current = setInterval(refresh, intervalMs)
    return () => { if (timerRef.current) clearInterval(timerRef.current) }
  }, [refresh, intervalMs])

  const needsSetup = status
    ? !(status.azure?.logged_in && status.foundry?.deployed)
    : null

  return { status, refresh, needsSetup }
}
