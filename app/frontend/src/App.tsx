import { useState, useCallback, lazy, Suspense } from 'react'
import { Routes, Route, Navigate, useLocation } from 'react-router-dom'
import { useAuth } from './hooks/useAuth'
import { useStatus } from './hooks/useStatus'
import TopBar from './components/TopBar'
import SessionsPanel from './components/SessionsPanel'
import LoginOverlay from './components/LoginOverlay'
import Disclaimer from './components/Disclaimer'
import ToastContainer from './components/Toast'

const Chat = lazy(() => import('./pages/Chat'))
const SetupWizard = lazy(() => import('./pages/SetupWizard'))
const Sessions = lazy(() => import('./pages/Sessions'))
const Skills = lazy(() => import('./pages/Skills'))
const Plugins = lazy(() => import('./pages/Plugins'))
const McpServers = lazy(() => import('./pages/McpServers'))
const Schedules = lazy(() => import('./pages/Schedules'))
const Profile = lazy(() => import('./pages/Profile'))
const MessagingSettings = lazy(() => import('./pages/MessagingSettings'))
const InfrastructureSettings = lazy(() => import('./pages/InfrastructureSettings'))
const Proactive = lazy(() => import('./pages/Proactive'))
const Environments = lazy(() => import('./pages/Environments'))
const FoundryIQ = lazy(() => import('./pages/FoundryIQ'))
const Workspace = lazy(() => import('./pages/Workspace'))
const Customization = lazy(() => import('./pages/Customization'))
const Guardrails = lazy(() => import('./pages/Guardrails'))
const ToolActivity = lazy(() => import('./pages/ToolActivity'))

function Loader() {
  return <div className="page-loader"><div className="spinner" /></div>
}

export default function App() {
  const [disclaimerOk, setDisclaimerOk] = useState(!!localStorage.getItem('polyclaw_disclaimer_accepted'))
  const { authenticated, loading, login } = useAuth()
  const { needsSetup } = useStatus(30_000)
  const [panelOpen, setPanelOpen] = useState(false)
  const location = useLocation()

  const handleDisclaimer = useCallback(() => setDisclaimerOk(true), [])

  if (!disclaimerOk) return <Disclaimer onAccept={handleDisclaimer} />
  if (loading) return <Loader />
  if (!authenticated) return <LoginOverlay onLogin={login} />

  // Redirect to setup if backend is not healthy (except if already on /setup)
  if (needsSetup && location.pathname !== '/setup') {
    return <Navigate to="/setup" replace />
  }

  if (!disclaimerOk) return <Disclaimer onAccept={handleDisclaimer} />
  if (loading) return <Loader />
  if (!authenticated) return <LoginOverlay onLogin={login} />

  return (
    <div className="app">
      <TopBar onTogglePanel={() => setPanelOpen(p => !p)} />
      <div className="app__body">
        <SessionsPanel open={panelOpen} onClose={() => setPanelOpen(false)} />
        <div className="app__main">
          <Suspense fallback={<Loader />}>
            <Routes>
              <Route path="/chat" element={<Chat />} />
              <Route path="/setup" element={<SetupWizard />} />
              <Route path="/sessions" element={<Sessions />} />
              <Route path="/skills" element={<Skills />} />
              <Route path="/plugins" element={<Plugins />} />
              <Route path="/mcp" element={<McpServers />} />
              <Route path="/schedules" element={<Schedules />} />
              <Route path="/profile" element={<Profile />} />
              <Route path="/messaging" element={<MessagingSettings />} />
              <Route path="/infrastructure" element={<InfrastructureSettings />} />
              {/* Keep old routes as redirects */}
              <Route path="/settings" element={<Navigate to="/messaging" replace />} />
              <Route path="/proactive" element={<Proactive />} />
              <Route path="/environments" element={<Environments />} />
              <Route path="/foundry-iq" element={<Navigate to="/infrastructure" replace />} />
              <Route path="/workspace" element={<Workspace />} />
              <Route path="/customization" element={<Customization />} />
              <Route path="/guardrails" element={<Guardrails />} />
              <Route path="/tool-activity" element={<ToolActivity />} />
              <Route path="/identity" element={<Navigate to="/guardrails" replace />} />
              <Route path="*" element={<Navigate to="/chat" replace />} />
            </Routes>
          </Suspense>
        </div>
      </div>
      <ToastContainer />
    </div>
  )
}
