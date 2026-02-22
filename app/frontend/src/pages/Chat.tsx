import { useState, useRef, useEffect, useCallback, useMemo, lazy, Suspense } from 'react'
import { useSearchParams } from 'react-router-dom'
import { useChat } from '../hooks/useChat'
import { IconChevronDown, IconBrain, IconTerminal } from '../components/Icons'
import AdaptiveCardRenderer from '../components/AdaptiveCardRenderer'
import type { ChatMessage, ToolCall, WindowWord, ModelInfo } from '../types'

const isMock = (import.meta as unknown as { env: Record<string, string> }).env?.VITE_MOCK === '1'
const MockReasoningPanel = isMock ? lazy(() => import('../components/MockReasoningPanel')) : null

interface SlashEntry { cmd: string; desc: string; skill?: boolean }

const SLASH_COMMANDS: SlashEntry[] = [
  { cmd: '/new', desc: 'Start a new session' },
  { cmd: '/model', desc: 'Switch AI model' },
  { cmd: '/models', desc: 'List available models' },
  { cmd: '/status', desc: 'System status' },
  { cmd: '/session', desc: 'Current session info' },
  { cmd: '/sessions', desc: 'List recent sessions' },
  { cmd: '/config', desc: 'View/set runtime config' },
  { cmd: '/clear', desc: 'Clear all memory' },
  { cmd: '/help', desc: 'Show all commands' },
  { cmd: '/skills', desc: 'List installed skills' },
  { cmd: '/plugins', desc: 'List plugins' },
  { cmd: '/mcp', desc: 'Manage MCP servers' },
  { cmd: '/schedules', desc: 'List scheduled tasks' },
  { cmd: '/profile', desc: 'Agent profile' },
  { cmd: '/preflight', desc: 'Run security checks' },
  { cmd: '/call', desc: 'Call configured number' },
  { cmd: '/change', desc: 'Switch session' },
]

export default function Chat() {
  const {
    messages, connected, thinking, activeTools, monologue, reasoningWindow,
    suggestions, skills, models, currentModel, sendMessage, newSession, resumeSession,
    feedReasoning, clearReasoning, approveToolCall,
  } = useChat()

  const [input, setInput] = useState('')
  const [selectedSkill, setSelectedSkill] = useState('')
  const [skillPickerOpen, setSkillPickerOpen] = useState(false)
  const [acIndex, setAcIndex] = useState(0)
  const [showAc, setShowAc] = useState(false)
  const [tickerMode, setTickerMode] = useState<TickerMode>('highlight')
  const messagesEndRef = useRef<HTMLDivElement>(null)
  const inputRef = useRef<HTMLTextAreaElement>(null)
  const pickerRef = useRef<HTMLDivElement>(null)
  const [searchParams] = useSearchParams()

  // Resume session from URL param (reacts to navigation changes)
  const sessionParam = searchParams.get('session')
  useEffect(() => {
    if (sessionParam) resumeSession(sessionParam)
  }, [sessionParam, resumeSession])

  // Auto-scroll
  useEffect(() => {
    messagesEndRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [messages])

  // Focus input
  useEffect(() => { inputRef.current?.focus() }, [])

  // Close skill picker on outside click
  useEffect(() => {
    const handler = (e: MouseEvent) => {
      if (pickerRef.current && !pickerRef.current.contains(e.target as Node)) setSkillPickerOpen(false)
    }
    document.addEventListener('mousedown', handler)
    return () => document.removeEventListener('mousedown', handler)
  }, [])

  // Merge built-in commands with installed skill verbs
  const allCommands = useMemo<SlashEntry[]>(() => {
    const builtinCmds = new Set(SLASH_COMMANDS.map(c => c.cmd))
    const skillEntries: SlashEntry[] = skills
      .filter(s => s.installed)
      .map(s => {
        const verb = (s.verb || s.name).toLowerCase().replace(/\s+/g, '-')
        return { cmd: `/${verb}`, desc: s.description, skill: true }
      })
      .filter(e => !builtinCmds.has(e.cmd))
    return [...SLASH_COMMANDS, ...skillEntries]
  }, [skills])

  // Autocomplete filtering
  const acFiltered = input.startsWith('/') && !input.includes(' ')
    ? allCommands.filter(c => c.cmd.startsWith(input.toLowerCase()))
    : []
  const acVisible = acFiltered.length > 0 && !(acFiltered.length === 1 && acFiltered[0].cmd === input)

  useEffect(() => {
    setShowAc(acVisible)
    setAcIndex(0)
  }, [acVisible])

  const handleSend = useCallback(() => {
    const text = selectedSkill ? `/${selectedSkill} ${input}` : input
    if (!text.trim()) return
    sendMessage(text.trim())
    setInput('')
    setSelectedSkill('')
  }, [input, selectedSkill, sendMessage])

  const handleKeyDown = (e: React.KeyboardEvent<HTMLTextAreaElement>) => {
    if (showAc) {
      if (e.key === 'ArrowUp') { e.preventDefault(); setAcIndex(i => Math.max(0, i - 1)) }
      else if (e.key === 'ArrowDown') { e.preventDefault(); setAcIndex(i => Math.min(acFiltered.length - 1, i + 1)) }
      else if (e.key === 'Tab' || (e.key === 'Enter' && acFiltered[acIndex])) {
        e.preventDefault()
        setInput(acFiltered[acIndex].cmd + ' ')
        setShowAc(false)
      }
      else if (e.key === 'Escape') setShowAc(false)
      return
    }
    if (e.key === 'Enter' && !e.shiftKey) {
      e.preventDefault()
      handleSend()
    }
  }

  const isEmpty = messages.length === 0

  return (
    <div className="chat">
      {isEmpty ? (
        <div className="chat__empty">
          <img src="/logo.png" alt="polyclaw" className="chat__empty-logo" />
          <h1 className="chat__empty-title">What can I help you with?</h1>
          <p className="chat__empty-sub">
            Start a conversation, run a slash command, or pick a skill.
          </p>
          {suggestions.length > 0 && (
            <div className="chat__suggestions">
              <span className="chat__suggestions-label">Try asking</span>
              <div className="chat__suggestions-chips">
                {suggestions.map((s, i) => (
                  <button
                    key={i}
                    className="chat__suggestion-chip"
                    onClick={() => { setInput(s.text); inputRef.current?.focus() }}
                  >
                    {s.text}
                  </button>
                ))}
              </div>
            </div>
          )}
          {MockReasoningPanel && (
            <Suspense fallback={null}>
              <MockReasoningPanel feedReasoning={feedReasoning} clearReasoning={clearReasoning} tickerMode={tickerMode} setTickerMode={(m: string) => setTickerMode(m as TickerMode)} modes={TICKER_MODES} />
            </Suspense>
          )}
        </div>
      ) : (
        <div className="chat__messages">
          <div className="chat__messages-inner">
          {MockReasoningPanel && (
            <Suspense fallback={null}>
              <MockReasoningPanel feedReasoning={feedReasoning} clearReasoning={clearReasoning} tickerMode={tickerMode} setTickerMode={(m: string) => setTickerMode(m as TickerMode)} modes={TICKER_MODES} />
            </Suspense>
          )}
          {messages.map(msg => (
            <MessageBubble key={msg.id} msg={msg} onApproveToolCall={approveToolCall} />
          ))}
          <div ref={messagesEndRef} />
          </div>
        </div>
      )}

      <div className="chat__composer">
          <div className={`chat__input-box ${thinking ? 'chat__input-box--thinking' : ''} ${!connected ? 'chat__input-box--disconnected' : ''}`}>
          {showAc && (
            <div className="autocomplete">
              {acFiltered.slice(0, 10).map((c, i) => (
                <button
                  key={c.cmd}
                  className={`autocomplete__item ${i === acIndex ? 'autocomplete__item--active' : ''}`}
                  onMouseDown={e => { e.preventDefault(); setInput(c.cmd + ' '); setShowAc(false) }}
                >
                  <span className="autocomplete__cmd">{c.cmd}</span>
                  {c.skill && <span className="autocomplete__tag">skill</span>}
                  <span className="autocomplete__desc">{c.desc}</span>
                </button>
              ))}
            </div>
          )}
          {skillPickerOpen && (
            <div className="chat__skill-picker" ref={pickerRef}>
              <div className="chat__picker-col chat__picker-col--model">
                <span className="chat__picker-label">Model</span>
                <div className="chat__picker-scroll">
                  {models.map(m => (
                    <button
                      key={m.id}
                      className={`chat__skill-picker-item ${m.id === currentModel ? 'chat__skill-picker-item--active' : ''}`}
                      onMouseDown={e => {
                        e.preventDefault()
                        setSkillPickerOpen(false)
                        if (m.id !== currentModel) sendMessage(`/model ${m.id}`)
                      }}
                    >
                      {m.id}
                      {m.id === currentModel && <span className="chat__picker-current">current</span>}
                      {m.policy && m.policy !== 'enabled' && <span className="chat__picker-badge">{m.policy}</span>}
                    </button>
                  ))}
                </div>
              </div>
              <div className="chat__picker-col chat__picker-col--mode">
                <span className="chat__picker-label">Mode</span>
                <div className="chat__picker-scroll">
                  <button
                    className={`chat__skill-picker-item ${!selectedSkill ? 'chat__skill-picker-item--active' : ''}`}
                    onMouseDown={e => { e.preventDefault(); setSelectedSkill(''); setSkillPickerOpen(false) }}
                  >
                    Chat
                  </button>
                  {skills.map(s => {
                    const verb = s.verb || s.name
                    return (
                    <button
                      key={s.name}
                      className={`chat__skill-picker-item ${selectedSkill === verb ? 'chat__skill-picker-item--active' : ''}`}
                      onMouseDown={e => { e.preventDefault(); setSelectedSkill(verb); setSkillPickerOpen(false) }}
                    >
                      /{verb}
                    </button>
                    )
                  })}
                </div>
              </div>
            </div>
          )}
          <textarea
            ref={inputRef}
            className="chat__textarea"
            value={input}
            onChange={e => setInput(e.target.value)}
            onKeyDown={handleKeyDown}
            placeholder={connected ? 'Message polyclaw...' : 'Connecting...'}
            disabled={!connected}
            rows={1}
          />
          <div className="chat__bottom-bar">
            <div className="chat__monologue">
              {reasoningWindow.length > 0 && (
                <ReasoningTicker words={reasoningWindow} mode={tickerMode} />
              )}
              {reasoningWindow.length === 0 && (thinking || activeTools.length > 0) && monologue && (
                <span className="chat__monologue-text">{monologue}</span>
              )}
            </div>
            <div className="chat__send-group">
              {currentModel && (
                <span className="chat__model-label" title={currentModel}>{currentModel}</span>
              )}
              <button
                className="chat__skill-trigger"
                onClick={() => setSkillPickerOpen(o => !o)}
                title="Select skill"
              >
                {selectedSkill ? `/${selectedSkill}` : 'Chat'}
                <IconChevronDown width={12} height={12} />
              </button>
              <button
                className="chat__send"
                onClick={handleSend}
                disabled={!input.trim() || !connected}
                title="Send"
              >
                Send
              </button>
            </div>
          </div>
        </div>
      </div>
    </div>
  )
}

function MessageBubble({ msg, onApproveToolCall }: { msg: ChatMessage; onApproveToolCall?: (callId: string, approved: boolean) => void }) {
  const isUser = msg.role === 'user'
  const isError = msg.role === 'error'
  const isSystem = msg.role === 'system'
  const [showReasoning, setShowReasoning] = useState(false)
  const [showTools, setShowTools] = useState(false)
  const hasReasoning = !!msg.reasoning
  const hasTools = !!(msg.toolCalls && msg.toolCalls.length > 0)
  const pendingApprovals = msg.toolCalls?.filter(tc => tc.status === 'pending_approval' || tc.status === 'pending_phone') || []
  const hasPendingApproval = pendingApprovals.length > 0

  return (
    <div className={`bubble bubble--${msg.role}`}>
      {msg.skill && (
        <div className="bubble__skill-badge">
          <span className="bubble__skill-icon">&#9889;</span>
          <span className="bubble__skill-name">{msg.skill}</span>
        </div>
      )}
      {isSystem && <span className="bubble__label">System</span>}
      {isError && <span className="bubble__label bubble__label--err">Error</span>}
      {msg.content && (
        <div
          dangerouslySetInnerHTML={isUser ? undefined : { __html: renderMarkdown(msg.content) }}
        >
          {isUser ? msg.content : undefined}
        </div>
      )}
      {msg.media && msg.media.length > 0 && (
        <div style={{ marginTop: 6 }}>
          {msg.media.map((f, i) => (
            <a key={i} href={f.url || `/api/media/${f.name}`} target="_blank" rel="noopener" className="chat-media-file">
              {f.name}
            </a>
          ))}
        </div>
      )}
      {msg.cards && msg.cards.length > 0 && (
        <div style={{ marginTop: 6 }}>
          {msg.cards.map((card, i) => (
            <AdaptiveCardRenderer key={i} card={card as Record<string, unknown>} />
          ))}
        </div>
      )}
      {/* Prominent approval banner -- always visible, not inside collapsed tools */}
      {hasPendingApproval && pendingApprovals.map(tc => (
        <div key={tc.call_id} className="bubble__approval-banner">
          {tc.status === 'pending_phone' ? (
            <span className="bubble__approval-label">Phone verification for <strong>{tc.tool}</strong> in progress...</span>
          ) : (
            <>
              <span className="bubble__approval-label">Allow <strong>{tc.tool}</strong> to execute?</span>
              <div className="bubble__approval-actions">
                <button className="btn btn--primary btn--sm" onClick={() => onApproveToolCall?.(tc.call_id, true)}>Allow</button>
                <button className="btn btn--danger btn--sm" onClick={() => onApproveToolCall?.(tc.call_id, false)}>Deny</button>
              </div>
            </>
          )}
        </div>
      ))}
      {(hasReasoning || hasTools) && (
        <div className="bubble__actions">
          {hasReasoning && (
            <button
              className={`bubble__action-btn ${showReasoning ? 'bubble__action-btn--active' : ''}`}
              onClick={() => setShowReasoning(v => !v)}
              title="Reasoning"
            >
              <IconBrain width={14} height={14} />
            </button>
          )}
          {hasTools && (
            <button
              className={`bubble__action-btn ${showTools ? 'bubble__action-btn--active' : ''}`}
              onClick={() => setShowTools(v => !v)}
              title={`${msg.toolCalls!.length} tool call${msg.toolCalls!.length > 1 ? 's' : ''}`}
            >
              <IconTerminal width={14} height={14} />
              <span className="bubble__action-count">{msg.toolCalls!.length}</span>
            </button>
          )}
        </div>
      )}
      {showReasoning && msg.reasoning && (
        <div className="bubble__reasoning">
          <div className="bubble__reasoning-text">{msg.reasoning}</div>
        </div>
      )}
      {(showTools || hasPendingApproval) && msg.toolCalls && msg.toolCalls.length > 0 && (
        <div className="bubble__tools">
          {msg.toolCalls.map(tc => (
            <ToolCallRow key={tc.call_id} tc={tc} onApprove={onApproveToolCall} />
          ))}
        </div>
      )}
    </div>
  )
}

function ToolCallRow({ tc, onApprove }: { tc: ToolCall; onApprove?: (callId: string, approved: boolean) => void }) {
  const [expanded, setExpanded] = useState(false)
  const argsShort = tc.arguments && tc.arguments.length > 60 ? tc.arguments.slice(0, 57) + '...' : tc.arguments

  const statusClass =
    tc.status === 'done' ? 'tool-call__status--done'
    : tc.status === 'pending_approval' ? 'tool-call__status--pending'
    : tc.status === 'pending_phone' ? 'tool-call__status--pending'
    : tc.status === 'denied' ? 'tool-call__status--denied'
    : 'tool-call__status--running'

  return (
    <div className="tool-call">
      <button className="tool-call__header" onClick={() => setExpanded(v => !v)}>
        <span className={`tool-call__status ${statusClass}`} />
        <span className="tool-call__name">{tc.tool}</span>
        {argsShort && <span className="tool-call__args">{argsShort}</span>}
        <span className="tool-call__chevron">{expanded ? '\u25B4' : '\u25BE'}</span>
      </button>

      {tc.status === 'denied' && (
        <div className="tool-call__denied">
          <span className="tool-call__denied-label">Denied by user</span>
        </div>
      )}
      {expanded && (
        <div className="tool-call__detail">
          {tc.arguments && (
            <div className="tool-call__section">
              <span className="tool-call__section-label">Arguments</span>
              <pre className="tool-call__pre">{tc.arguments}</pre>
            </div>
          )}
          {tc.result && (
            <div className="tool-call__section">
              <span className="tool-call__section-label">Result</span>
              <pre className="tool-call__pre">{tc.result}</pre>
            </div>
          )}
          {!tc.result && tc.status === 'running' && (
            <span className="tool-call__running">Running...</span>
          )}
        </div>
      )}
    </div>
  )
}

/* ── Reasoning Ticker ─────────────────────────────────────────────────────
   Three-container layout: left context | FIXED center | right context.
   The center word is position:absolute at left:50% + translateX(-50%)
   so it is mathematically locked to the container center. Words rotate
   through this fixed position. Left/right context fills the sides.
   ──────────────────────────────────────────────────────────────────────── */

export type TickerMode =
  | 'bionic'         // 1: bold-first-half on focus, opacity fade
  | 'highlight'      // 2: gold background pill on focus word
  | 'underline'      // 3: animated gold underline on focus
  | 'scale'          // 4: focus word scales up, neighbors shrink
  | 'glow'           // 5: text-shadow glow on focus
  | 'color-temp'     // 6: past=blue, focus=gold, future=gray
  | 'typewriter'     // 7: only show words up to focus, cursor blinks
  | 'bold-wave'      // 8: bold sweeps outward from focus
  | 'outline'        // 9: focus word has outlined/stroked text
  | 'fade-trail'     // 10: focus bright, trail fades like comet

const TICKER_MODES: { id: TickerMode; label: string }[] = [
  { id: 'bionic',      label: 'Bionic' },
  { id: 'highlight',   label: 'Highlight' },
  { id: 'underline',   label: 'Underline' },
  { id: 'scale',       label: 'Scale' },
  { id: 'glow',        label: 'Glow' },
  { id: 'color-temp',  label: 'Warm/Cool' },
  { id: 'typewriter',  label: 'Typewriter' },
  { id: 'bold-wave',   label: 'Bold Wave' },
  { id: 'outline',     label: 'Outline' },
  { id: 'fade-trail',  label: 'Comet' },
]

function bionicSplit(text: string): [string, string] {
  const pivot = Math.ceil(text.length * 0.5)
  return [text.slice(0, pivot), text.slice(pivot)]
}

/** Style for a context word (not the focal word). */
function contextStyle(mode: TickerMode, dist: number, isFuture: boolean): React.CSSProperties {
  const d = Math.abs(dist)
  switch (mode) {
    case 'color-temp':
      return isFuture
        ? { color: 'var(--text-3)', opacity: Math.max(0.25, 1 - d * 0.2) }
        : { color: 'var(--blue)', opacity: Math.max(0.25, 1 - d * 0.2) }
    case 'typewriter':
      return isFuture ? { visibility: 'hidden' } : { opacity: Math.max(0.4, 1 - d * 0.15) }
    case 'fade-trail':
      return isFuture
        ? { opacity: Math.max(0.08, 0.4 - d * 0.12) }
        : { opacity: Math.max(0.12, 0.7 - d * 0.2) }
    case 'bold-wave':
      return { fontWeight: d <= 1 ? 700 : 400, opacity: Math.max(0.2, 1 - d * 0.18) }
    default:
      return { opacity: Math.max(0.18, 1 - d * 0.2) }
  }
}

/** Additional inline style for the focal center word. */
function focalExtraStyle(mode: TickerMode): React.CSSProperties {
  switch (mode) {
    case 'highlight':
      return { background: 'var(--gold)', color: 'var(--black)', borderRadius: '3px', padding: '0 5px' }
    case 'underline':
      return { borderBottom: '2px solid var(--gold)', paddingBottom: '1px' }
    case 'scale':
      return { transform: 'translateX(-50%) scale(1.3)', transformOrigin: 'center bottom' }
    case 'glow':
      return { textShadow: '0 0 8px var(--gold), 0 0 18px var(--gold-dim)' }
    case 'outline':
      return { WebkitTextStroke: '0.6px var(--gold)', color: 'transparent' }
    default:
      return {}
  }
}

function ReasoningTicker({ words, mode = 'highlight' }: { words: WindowWord[]; mode?: TickerMode }) {
  if (!words.length) return null
  const focal = words.find(w => w.distance === 0)
  if (!focal) return null
  const focalArrayIdx = words.indexOf(focal)

  // Split into left context and right context
  const leftWords = words.slice(0, focalArrayIdx)
  const rightWords = words.slice(focalArrayIdx + 1)

  // Bionic split for focal word
  const useBionic = mode === 'bionic'
  const [boldPart, restPart] = useBionic ? bionicSplit(focal.text) : [focal.text, '']

  return (
    <div className={`ticker ticker--${mode}`} aria-label="Reasoning">
      {/* Left context: right-aligned, flows toward center */}
      <div className="ticker__left">
        {leftWords.map((w, i) => (
          <span
            key={w.idx}
            className="ticker__ctx"
            style={contextStyle(mode, -(leftWords.length - i), false)}
          >{w.text}</span>
        ))}
      </div>

      {/* Focal word: absolutely centered, never moves */}
      <div className="ticker__center" style={focalExtraStyle(mode)}>
        {useBionic
          ? <><strong className="ticker__b">{boldPart}</strong>{restPart}</>
          : focal.text
        }
        {mode === 'typewriter' && <span className="ticker__cursor" />}
      </div>

      {/* Right context: left-aligned, flows away from center */}
      <div className="ticker__right">
        {rightWords.map((w, i) => (
          <span
            key={w.idx}
            className="ticker__ctx"
            style={contextStyle(mode, i + 1, true)}
          >{w.text}</span>
        ))}
      </div>
    </div>
  )
}

function renderMarkdown(text: string): string {
  let html = escapeHtml(text)
  html = html.replace(/```(\w*)\n([\s\S]*?)```/g, '<pre><code class="lang-$1">$2</code></pre>')
  html = html.replace(/`([^`]+)`/g, '<code>$1</code>')
  html = html.replace(/\*\*(.+?)\*\*/g, '<strong>$1</strong>')
  html = html.replace(/\*(.+?)\*/g, '<em>$1</em>')
  html = html.replace(/\[([^\]]+)\]\(([^)]+)\)/g, '<a href="$2" target="_blank" rel="noopener">$1</a>')
  html = renderTables(html)
  html = html.replace(/\n/g, '<br/>')
  return html
}

/** Convert markdown tables (pipe-delimited rows) to HTML <table> elements. */
function renderTables(html: string): string {
  return html.replace(
    /(^|\n)(\|.+\|[ ]*\n\|[ :\-|]+\|[ ]*\n(?:\|.+\|[ ]*(?:\n|$))+)/g,
    (_match, prefix, table) => {
      const rows: string[] = table.trim().split('\n')
      if (rows.length < 2) return prefix + table

      // Parse alignment from separator row
      const sepCells = rows[1].split('|').filter((_: string, i: number, a: string[]) => i > 0 && i < a.length - 1)
      const aligns = sepCells.map((c: string) => {
        const t = c.trim()
        if (t.startsWith(':') && t.endsWith(':')) return 'center'
        if (t.endsWith(':')) return 'right'
        return 'left'
      })

      const parseRow = (row: string) =>
        row.split('|').filter((_: string, i: number, a: string[]) => i > 0 && i < a.length - 1).map((c: string) => c.trim())

      const headCells = parseRow(rows[0])
      const thead = '<thead><tr>' + headCells.map((c: string, i: number) =>
        `<th style="text-align:${aligns[i] || 'left'}">${c}</th>`
      ).join('') + '</tr></thead>'

      const bodyRows = rows.slice(2).filter((r: string) => r.trim())
      const tbody = '<tbody>' + bodyRows.map((r: string) => {
        const cells = parseRow(r)
        return '<tr>' + cells.map((c: string, i: number) =>
          `<td style="text-align:${aligns[i] || 'left'}">${c}</td>`
        ).join('') + '</tr>'
      }).join('') + '</tbody>'

      return prefix + '<div class="bubble__table-wrap"><table class="bubble__table">' + thead + tbody + '</table></div>'
    }
  )
}

function escapeHtml(text: string): string {
  const div = document.createElement('div')
  div.textContent = text
  return div.innerHTML
}
