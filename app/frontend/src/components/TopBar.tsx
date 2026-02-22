import { useState, useEffect, useRef } from 'react'
import { useNavigate, useLocation } from 'react-router-dom'
import { api } from '../api'
import { IconPanelLeft, IconChevronDown, IconPalette, IconSliders, IconUser, IconShield, IconActivity } from './Icons'
import type { AgentProfile } from '../types'

interface Props {
  onTogglePanel: () => void
}

const LINKS = [
  { path: '/customization', label: 'Customization', Icon: IconPalette },
  { path: '/messaging', label: 'Messaging', Icon: IconSliders },
  { path: '/infrastructure', label: 'Infrastructure', Icon: IconSliders },
  { path: '/guardrails', label: 'Hardening', Icon: IconShield },
  { path: '/tool-activity', label: 'Tool Activity', Icon: IconActivity },
  { path: '/profile', label: 'Agent Profile', Icon: IconUser },
] as const

export default function TopBar({ onTogglePanel }: Props) {
  const navigate = useNavigate()
  const location = useLocation()
  const [profile, setProfile] = useState<AgentProfile | null>(null)
  const [open, setOpen] = useState(false)
  const ref = useRef<HTMLDivElement>(null)

  useEffect(() => {
    api<AgentProfile>('profile').then(setProfile).catch(() => {})
  }, [])

  useEffect(() => {
    const handler = (e: MouseEvent) => {
      if (ref.current && !ref.current.contains(e.target as Node)) setOpen(false)
    }
    document.addEventListener('mousedown', handler)
    return () => document.removeEventListener('mousedown', handler)
  }, [])

  const agentName = profile?.name || 'polyclaw'

  const go = (path: string) => {
    navigate(path)
    setOpen(false)
  }

  return (
    <header className="topbar">
      <div className="topbar__left">
        <button className="topbar__toggle" onClick={onTogglePanel} title="Toggle sessions">
          <IconPanelLeft />
        </button>
        <button className="topbar__brand" onClick={() => navigate('/chat')}>
          <img src="/headertext.png" alt="polyclaw" className="topbar__wordmark" />
        </button>
      </div>
      <div className="topbar__right" ref={ref}>
        <button
          className="topbar__agent"
          onClick={() => setOpen(o => !o)}
          onMouseEnter={() => setOpen(true)}
        >
          <img src="/logo.png" alt="" className="topbar__avatar-img" />
          <span className="topbar__name">{agentName}</span>
          <IconChevronDown width={14} height={14} />
        </button>
        {open && (
          <nav className="topbar__dropdown" onMouseLeave={() => setOpen(false)}>
            {LINKS.map(({ path, label, Icon }) => (
              <button
                key={path}
                className={`topbar__dropdown-item ${location.pathname === path ? 'topbar__dropdown-item--active' : ''}`}
                onClick={() => go(path)}
              >
                <Icon width={16} height={16} />
                <span>{label}</span>
              </button>
            ))}
          </nav>
        )}
      </div>
    </header>
  )
}
