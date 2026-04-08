import { NavLink } from 'react-router-dom'
import type { SetupStatus } from '../types'

interface Props {
  status: SetupStatus | null
  collapsed: boolean
  onToggle: () => void
}

const NAV_ITEMS = [
  { to: '/chat', icon: '💬', label: 'Chat' },
  { to: '/sessions', icon: '📋', label: 'Sessions' },
  { to: '/skills', icon: '⚡', label: 'Skills' },
  { to: '/plugins', icon: '🧩', label: 'Plugins' },
  { to: '/mcp', icon: '🔌', label: 'MCP Servers' },
  { to: '/schedules', icon: '📅', label: 'Schedules' },
  { to: '/profile', icon: '👤', label: 'Profile' },
  { to: '/messaging', icon: '✉️', label: 'AI Model' },
  { to: '/infrastructure', icon: '🏗️', label: 'Infrastructure' },
  { to: '/guardrails', icon: '🛡️', label: 'Hardening' },
  { to: '/tool-activity', icon: '🔍', label: 'Tool Activity' },
]

export default function Sidebar({ status, collapsed, onToggle }: Props) {
  return (
    <aside className={`sidebar ${collapsed ? 'sidebar--collapsed' : ''}`}>
      <div className="sidebar__header">
        <button className="sidebar__toggle" onClick={onToggle} title={collapsed ? 'Expand' : 'Collapse'}>
          {collapsed ? '▸' : '◂'}
        </button>
        {!collapsed && (
          <div className="sidebar__brand">
            <img src="/static/logo.png" alt="Polyclaw" className="sidebar__logo" onError={e => (e.currentTarget.style.display = 'none')} />
            <span className="sidebar__title">Polyclaw</span>
          </div>
        )}
      </div>

      <nav className="sidebar__nav">
        {NAV_ITEMS.map(item => (
          <NavLink
            key={item.to}
            to={item.to}
            className={({ isActive }) => `sidebar__link ${isActive ? 'sidebar__link--active' : ''}`}
            title={item.label}
          >
            <span className="sidebar__icon">{item.icon}</span>
            {!collapsed && <span className="sidebar__label">{item.label}</span>}
          </NavLink>
        ))}
      </nav>

      {!collapsed && status && (
        <div className="sidebar__status">
          <StatusDot ok={status.azure?.logged_in} label="Azure" />
          <StatusDot ok={status.foundry?.deployed} label="Foundry" />
          <StatusDot ok={status.tunnel?.active} label="Tunnel" />
          <StatusDot ok={status.bot_configured} label="Bot" />
        </div>
      )}
    </aside>
  )
}

function StatusDot({ ok, label }: { ok?: boolean; label: string }) {
  return (
    <div className="status-dot" title={`${label}: ${ok ? 'OK' : 'Not configured'}`}>
      <span className={`status-dot__indicator ${ok ? 'status-dot__indicator--ok' : 'status-dot__indicator--err'}`} />
      <span className="status-dot__label">{label}</span>
    </div>
  )
}
