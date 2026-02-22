import { useState, useEffect, useRef, useCallback } from 'react'
import { createChatSocket, api, type ChatSocket } from '../api'
import type { ChatMessage, ChatMessageRole, WsIncoming, Suggestion, Skill, ToolCall, WindowWord, ModelInfo, SessionDetail } from '../types'

let msgId = 0
const nextId = () => `msg-${++msgId}`

/**
 * Sliding-window reasoning stream with variable-speed pacing.
 *
 * As reasoning tokens arrive they are split into words and queued.
 * A timer advances a cursor through the words, emitting a "window"
 * snapshot (WINDOW_SIZE words centered on the cursor) each tick.
 *
 * Timing adapts to word complexity:
 *   base 160ms  +  12ms per character over 4  +  50ms after sentence-end
 * This gives roughly 300-380 WPM with natural pauses.
 */
const WINDOW_HALF = 4          // words visible each side of focus
const WINDOW_SIZE = WINDOW_HALF * 2 + 1
const BASE_MS = 160
const PER_CHAR_MS = 12
const CHAR_THRESHOLD = 4
const PAUSE_MS = 50            // extra pause after . ! ?
const SPEED_MULTIPLIER = 0.6   // lower is faster

function wordDelay(word: string): number {
  let ms = BASE_MS
  if (word.length > CHAR_THRESHOLD) ms += (word.length - CHAR_THRESHOLD) * PER_CHAR_MS
  if (/[.!?]$/.test(word)) ms += PAUSE_MS
  return Math.max(55, Math.round(ms * SPEED_MULTIPLIER))
}

class ReasoningStream {
  private words: string[] = []
  private cursor = -1
  private timer: ReturnType<typeof setTimeout> | null = null
  private onWindow: (w: WindowWord[]) => void

  constructor(onWindow: (w: WindowWord[]) => void) {
    this.onWindow = onWindow
  }

  feed(text: string) {
    const incoming = text.split(/\s+/).filter(Boolean)
    this.words.push(...incoming)
    if (!this.timer && this.words.length) this.advance()
  }

  private advance() {
    this.cursor++
    if (this.cursor >= this.words.length) {
      // Buffer exhausted -- wait for more or stop
      this.timer = null
      return
    }
    this.emit()
    const delay = wordDelay(this.words[this.cursor])
    this.timer = setTimeout(() => this.advance(), delay)
  }

  private emit() {
    const start = Math.max(0, this.cursor - WINDOW_HALF)
    const end = Math.min(this.words.length, this.cursor + WINDOW_HALF + 1)
    const win: WindowWord[] = []
    for (let i = start; i < end; i++) {
      win.push({ text: this.words[i], idx: i, distance: Math.abs(i - this.cursor) })
    }
    this.onWindow(win)
  }

  stop() {
    if (this.timer) { clearTimeout(this.timer); this.timer = null }
    this.words = []
    this.cursor = -1
  }
}

export function useChat() {
  const [messages, setMessages] = useState<ChatMessage[]>([])
  const [connected, setConnected] = useState(false)
  const [thinking, setThinking] = useState(false)
  const [activeTools, setActiveTools] = useState<string[]>([])
  const [monologue, setMonologue] = useState('')
  const [reasoningWindow, setReasoningWindow] = useState<WindowWord[]>([])
  const [suggestions, setSuggestions] = useState<Suggestion[]>([])
  const [skills, setSkills] = useState<Skill[]>([])
  const [models, setModels] = useState<ModelInfo[]>([])
  const [currentModel, setCurrentModel] = useState('')
  const socketRef = useRef<ChatSocket | null>(null)
  const replyRef = useRef<{ id: string; text: string } | null>(null)
  const reasoningRef = useRef('')
  const toolCallsRef = useRef<ToolCall[]>([])
  const skillRef = useRef('')
  const pendingModelRefresh = useRef(false)
  const streamRef = useRef<ReasoningStream | null>(null)
  const pendingResumeRef = useRef<string | null>(null)
  // Fetch suggestions + installed skills + models
  useEffect(() => {
    api<{ suggestions: (Suggestion | string)[] }>('chat/suggestions')
      .then(r => setSuggestions(
        (r.suggestions || [])
          .map(s => (typeof s === 'string' ? { text: s } : s))
          .filter(s => s.text?.trim()),
      ))
      .catch(() => {})
    api<{ skills: Skill[] }>('skills')
      .then(r => setSkills((r.skills || []).filter(s => s.installed)))
      .catch(() => {})
    api<{ models: ModelInfo[]; current: string }>('chat/models')
      .then(r => {
        setModels(r.models || [])
        if (r.current) setCurrentModel(r.current)
      })
      .catch(() => {})
  }, [])

  // Helper: update the current assistant message's metadata
  const updateReplyMeta = useCallback((updater: (m: ChatMessage) => ChatMessage) => {
    if (!replyRef.current) return
    const rid = replyRef.current.id
    setMessages(prev => prev.map(m => m.id === rid ? updater(m) : m))
  }, [])

  // Connect WebSocket
  useEffect(() => {
    const sock = createChatSocket()
    socketRef.current = sock

    sock.onOpen(() => {
      setConnected(true)
      // Send any queued resume that was attempted before the socket opened
      const pendingSid = pendingResumeRef.current
      if (pendingSid) {
        pendingResumeRef.current = null
        sock.send('resume_session', { session_id: pendingSid })
      }
    })
    sock.onClose(() => setConnected(false))

    sock.onMessage((raw) => {
      const data = raw as WsIncoming
      switch (data.type) {
        case 'delta': {
          if (!replyRef.current) {
            const id = nextId()
            replyRef.current = { id, text: '' }
            setThinking(false)
            setMessages(prev => [...prev, { id, role: 'assistant', content: '', timestamp: Date.now() }])
          }
          replyRef.current.text += (data as { content: string }).content
          const text = replyRef.current.text
          const rid = replyRef.current.id
          setMessages(prev => prev.map(m => m.id === rid ? { ...m, content: text } : m))
          break
        }
        case 'message': {
          setThinking(false)
          if (replyRef.current) {
            const rid = replyRef.current.id
            const content = (data as { content: string }).content
            setMessages(prev => prev.map(m => m.id === rid ? { ...m, content } : m))
            replyRef.current = null
          } else {
            setMessages(prev => [...prev, {
              id: nextId(),
              role: 'assistant',
              content: (data as { content: string }).content || '',
              timestamp: Date.now(),
            }])
          }
          break
        }
        case 'done': {
          // Refresh model if a /model command was just processed
          if (pendingModelRefresh.current) {
            pendingModelRefresh.current = false
            api<{ models: ModelInfo[]; current: string }>('chat/models')
              .then(r => {
                setModels(r.models || [])
                if (r.current) {
                  setCurrentModel(r.current)
                  api('setup/config', {
                    method: 'POST',
                    body: JSON.stringify({ COPILOT_MODEL: r.current }),
                  }).catch(() => {})
                }
              })
              .catch(() => {})
          }
          // Attach accumulated reasoning and tool calls to the last assistant message
          if (replyRef.current) {
            const rid = replyRef.current.id
            const reasoning = reasoningRef.current || undefined
            const toolCalls = toolCallsRef.current.length ? [...toolCallsRef.current] : undefined
            const skill = skillRef.current || undefined
            setMessages(prev => prev.map(m => m.id === rid ? { ...m, reasoning, toolCalls, skill } : m))
          }
          setThinking(false)
          setActiveTools([])
          setMonologue('')
          setReasoningWindow([])
          replyRef.current = null
          reasoningRef.current = ''
          toolCallsRef.current = []
          skillRef.current = ''
          streamRef.current?.stop()
          streamRef.current = null
          break
        }
        case 'event': {
          const evt = data as { event: string; tool?: string; call_id?: string; text?: string; arguments?: string; result?: string; name?: string; approved?: boolean }
          if (evt.event === 'reasoning' && evt.text) {
            reasoningRef.current += evt.text
            // Feed words into the sliding-window reasoning stream
            if (!streamRef.current) {
              streamRef.current = new ReasoningStream(w => setReasoningWindow(w))
            }
            streamRef.current.feed(evt.text)
          } else if (evt.event === 'approval_request' && evt.call_id) {
            // HITL: a tool needs user approval before running
            setMonologue(`Approval needed: ${evt.tool || 'unknown'}`)
            // Ensure assistant message exists
            if (!replyRef.current) {
              const id = nextId()
              replyRef.current = { id, text: '' }
              setMessages(prev => [...prev, { id, role: 'assistant', content: '', timestamp: Date.now() }])
            }
            // Deduplicate: the SDK fires a separate tool_start event
            // with its own call_id before the HITL hook emits this
            // approval_request with a different call_id. Merge into
            // the existing entry so we don't show the tool twice.
            const approvalIdx = toolCallsRef.current.findIndex(tc =>
              tc.tool === (evt.tool || 'unknown') && tc.status !== 'done'
            )
            if (approvalIdx >= 0) {
              toolCallsRef.current = toolCallsRef.current.map((tc, i) =>
                i === approvalIdx
                  ? { ...tc, call_id: evt.call_id!, arguments: evt.arguments ?? tc.arguments, status: 'pending_approval' as const }
                  : tc
              )
            } else {
              toolCallsRef.current = [...toolCallsRef.current, {
                tool: evt.tool || 'unknown',
                call_id: evt.call_id,
                arguments: evt.arguments,
                status: 'pending_approval' as const,
              }]
            }
            updateReplyMeta(m => ({ ...m, toolCalls: [...toolCallsRef.current] }))
          } else if (evt.event === 'approval_resolved' && evt.call_id) {
            // HITL: user responded to approval request
            const newStatus = evt.approved ? 'running' as const : 'denied' as const
            toolCallsRef.current = toolCallsRef.current.map(tc =>
              tc.call_id === evt.call_id ? { ...tc, status: newStatus } : tc
            )
            updateReplyMeta(m => ({ ...m, toolCalls: [...toolCallsRef.current] }))
            if (!evt.approved) {
              setActiveTools(prev => prev.filter(t => t !== evt.tool))
            }
          } else if (evt.event === 'phone_verification_started' && evt.call_id) {
            // PITL: phone verification call in progress
            setMonologue(`Phone verification: ${evt.tool || 'unknown'}`)
            if (!replyRef.current) {
              const id = nextId()
              replyRef.current = { id, text: '' }
              setMessages(prev => [...prev, { id, role: 'assistant', content: '', timestamp: Date.now() }])
            }
            const approvalIdx = toolCallsRef.current.findIndex(tc =>
              tc.tool === (evt.tool || 'unknown') && tc.status !== 'done'
            )
            if (approvalIdx >= 0) {
              toolCallsRef.current = toolCallsRef.current.map((tc, i) =>
                i === approvalIdx
                  ? { ...tc, call_id: evt.call_id!, arguments: evt.arguments ?? tc.arguments, status: 'pending_phone' as const }
                  : tc
              )
            } else {
              toolCallsRef.current = [...toolCallsRef.current, {
                tool: evt.tool || 'unknown',
                call_id: evt.call_id,
                arguments: evt.arguments,
                status: 'pending_phone' as const,
              }]
            }
            updateReplyMeta(m => ({ ...m, toolCalls: [...toolCallsRef.current] }))
          } else if (evt.event === 'phone_verification_complete' && evt.call_id) {
            // PITL: phone verification resolved
            const newStatus = evt.approved ? 'running' as const : 'denied' as const
            toolCallsRef.current = toolCallsRef.current.map(tc =>
              tc.call_id === evt.call_id ? { ...tc, status: newStatus } : tc
            )
            updateReplyMeta(m => ({ ...m, toolCalls: [...toolCallsRef.current] }))
            if (!evt.approved) {
              setActiveTools(prev => prev.filter(t => t !== evt.tool))
            }
          } else if (evt.event === 'tool_start' && evt.tool) {
            setActiveTools(prev => [...prev, evt.tool!])
            const args = evt.arguments && evt.arguments.length > 60 ? evt.arguments.slice(0, 57) + '...' : evt.arguments
            setMonologue(`${evt.tool}(${args || ''})`)
            // Ensure assistant message exists
            if (!replyRef.current) {
              const id = nextId()
              replyRef.current = { id, text: '' }
              setMessages(prev => [...prev, { id, role: 'assistant', content: '', timestamp: Date.now() }])
            }
            // Deduplicate: if a tool call with the same call_id already
            // exists (from approval_request or a duplicate SDK event),
            // update it in place instead of adding a new entry.
            let existingIdx = evt.call_id
              ? toolCallsRef.current.findIndex(tc => tc.call_id === evt.call_id)
              : toolCallsRef.current.findIndex(tc => tc.tool === evt.tool && tc.status !== 'done' && !tc.result)
            // Fallback: match by tool name + arguments to catch SDK events
            // with different call_ids for the same logical tool invocation.
            if (existingIdx < 0) {
              existingIdx = toolCallsRef.current.findIndex(tc =>
                tc.tool === evt.tool && tc.arguments === evt.arguments
              )
            }
            if (existingIdx >= 0) {
              toolCallsRef.current = toolCallsRef.current.map((tc, i) =>
                // Preserve the HITL call_id when merging -- resolve_approval
                // uses the HITL-assigned call_id, not the SDK's.
                i === existingIdx ? { ...tc, status: 'running' as const, call_id: tc.call_id || evt.call_id || '', arguments: evt.arguments ?? tc.arguments } : tc
              )
            } else {
              toolCallsRef.current = [...toolCallsRef.current, {
                tool: evt.tool,
                call_id: evt.call_id || '',
                arguments: evt.arguments,
                status: 'running',
              }]
            }
            updateReplyMeta(m => ({ ...m, toolCalls: [...toolCallsRef.current] }))
          } else if (evt.event === 'tool_done') {
            setActiveTools(prev => prev.slice(0, -1))
            // Match by call_id first, then fall back to tool name +
            // running status. The HITL flow replaces the SDK call_id
            // with its own, so the tool_done's SDK call_id may differ
            // from the stored entry's HITL call_id.
            let doneIdx = evt.call_id
              ? toolCallsRef.current.findIndex(tc => tc.call_id === evt.call_id)
              : -1
            if (doneIdx < 0) {
              doneIdx = toolCallsRef.current.findIndex(tc =>
                tc.tool === evt.tool && tc.status === 'running'
              )
            }
            if (doneIdx >= 0) {
              toolCallsRef.current = toolCallsRef.current.map((tc, i) =>
                i === doneIdx ? { ...tc, result: evt.result, status: 'done' as const, call_id: evt.call_id || tc.call_id } : tc
              )
              updateReplyMeta(m => ({ ...m, toolCalls: [...toolCallsRef.current] }))
            }
          } else if (evt.event === 'skill' && evt.name) {
            skillRef.current = evt.name
            setMonologue(`skill: ${evt.name}`)
          } else if (evt.event === 'subagent_start' && evt.name) {
            setMonologue(`agent: ${evt.name}`)
          }
          break
        }
        case 'cards':
          setMessages(prev => [...prev, {
            id: nextId(),
            role: 'assistant',
            content: '',
            timestamp: Date.now(),
            cards: (data as { cards: unknown[] }).cards as ChatMessage['cards'],
          }])
          break
        case 'media':
          setMessages(prev => [...prev, {
            id: nextId(),
            role: 'assistant',
            content: '',
            timestamp: Date.now(),
            media: (data as { files: ChatMessage['media'] }).files,
          }])
          break
        case 'system':
          setMessages(prev => [...prev, {
            id: nextId(),
            role: 'system',
            content: (data as { content: string }).content || '',
            timestamp: Date.now(),
          }])
          break
        case 'error':
          setThinking(false)
          replyRef.current = null
          reasoningRef.current = ''
          toolCallsRef.current = []
          skillRef.current = ''
          streamRef.current?.stop()
          streamRef.current = null
          setMonologue('')
          setReasoningWindow([])
          setMessages(prev => [...prev, {
            id: nextId(),
            role: 'error',
            content: (data as { content: string }).content || 'Unknown error',
            timestamp: Date.now(),
          }])
          break
      }
    })

    return () => sock.close()
  }, [updateReplyMeta])

  const sendMessage = useCallback((text: string) => {
    if (!text.trim()) return
    // Add user message
    setMessages(prev => [...prev, {
      id: nextId(),
      role: 'user',
      content: text,
      timestamp: Date.now(),
    }])
    socketRef.current?.send('send', { message: text })
    if (text.match(/^\/model\s/i)) {
      pendingModelRefresh.current = true
    }
    if (!text.startsWith('/')) {
      setThinking(true)
      setMonologue('')
      reasoningRef.current = ''
      toolCallsRef.current = []
      skillRef.current = ''
    }
  }, [])

  const newSession = useCallback(() => {
    socketRef.current?.send('new_session')
    setMessages([])
    replyRef.current = null
    reasoningRef.current = ''
    toolCallsRef.current = []
    skillRef.current = ''
    streamRef.current?.stop()
    streamRef.current = null
    setThinking(false)
    setActiveTools([])
    setMonologue('')
    setReasoningWindow([])
  }, [])

  const resumeSession = useCallback((sessionId: string) => {
    // Load session history via REST and display prior messages
    api<SessionDetail>(`sessions/${sessionId}`)
      .then(detail => {
        const history: ChatMessage[] = (detail.messages || []).map((m, i) => ({
          id: `hist-${i}`,
          role: (['user', 'assistant', 'system', 'error'].includes(m.role)
            ? m.role : 'system') as ChatMessageRole,
          content: m.content,
          timestamp: m.timestamp,
        }))
        setMessages(history)
      })
      .catch(() => {})

    // Queue the resume so onOpen sends it if the socket isn't ready yet
    pendingResumeRef.current = sessionId
    socketRef.current?.send('resume_session', { session_id: sessionId })

    replyRef.current = null
    reasoningRef.current = ''
    toolCallsRef.current = []
    skillRef.current = ''
    streamRef.current?.stop()
    streamRef.current = null
    setReasoningWindow([])
  }, [])

  /** Feed raw reasoning text into the sliding-window ticker (for mock / demo). */
  const feedReasoning = useCallback((text: string) => {
    if (!streamRef.current) {
      streamRef.current = new ReasoningStream(w => setReasoningWindow(w))
    }
    setThinking(true)
    streamRef.current.feed(text)
  }, [])

  /** Stop & clear the reasoning ticker. */
  const clearReasoning = useCallback(() => {
    streamRef.current?.stop()
    streamRef.current = null
    setReasoningWindow([])
    setThinking(false)
  }, [])

  /** Send a tool approval decision (HITL). */
  const approveToolCall = useCallback((callId: string, approved: boolean) => {
    socketRef.current?.send('approve_tool', {
      call_id: callId,
      response: approved ? 'yes' : 'no',
    })
  }, [])

  return {
    messages,
    connected,
    thinking,
    activeTools,
    monologue,
    reasoningWindow,
    suggestions,
    skills,
    models,
    currentModel,
    sendMessage,
    newSession,
    resumeSession,
    setMessages,
    feedReasoning,
    clearReasoning,
    approveToolCall,
  }
}
