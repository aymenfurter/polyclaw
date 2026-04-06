/**
 * Vite dev-server plugin that serves mock API responses.
 *
 * Activated by setting VITE_MOCK=1.  Intercepts all /api/* and /health
 * requests so the frontend can run standalone without a backend.
 *
 * Uses the same fixture data as the Playwright E2E tests.
 */

import type { Plugin, ViteDevServer } from 'vite'
import type { IncomingMessage, ServerResponse } from 'http'

// ── Fixture data (mirrors e2e/helpers.ts) ────────────────────────────────

const STATUS = {
  azure: { logged_in: true, user: 'test@example.com', subscription: 'sub-123' },
  foundry: { deployed: true, endpoint: 'https://mock-foundry.cognitiveservices.azure.com', name: 'mock-foundry' },
  prerequisites_configured: true,
  telegram_configured: false,
  tunnel: { active: false, url: '' },
  bot_configured: true,
  voice_call_configured: false,
  model: 'gpt-4o',
}

const SESSIONS = [
  { id: 'sess-001', model: 'gpt-4o', created_at: 1739613600, message_count: 5, title: 'Hello, World!' },
  { id: 'sess-002', model: 'gpt-4o-mini', created_at: 1739521800, message_count: 12, title: 'Deploy the app' },
]

const SESSION_STATS = { total: 2, today: 1, this_week: 2, avg_messages: 8.5 }

const SESSION_DETAIL = {
  messages: [
    { role: 'user', content: 'Hello, World!', timestamp: '2026-02-15T10:00:01Z' },
    { role: 'assistant', content: 'Hi! How can I help?', timestamp: '2026-02-15T10:00:02Z' },
  ],
}

const SKILLS = {
  skills: [
    { name: 'web-search', verb: 'search', description: 'Search the web', installed: true, builtin: false, source: 'marketplace' },
    { name: 'summarize-url', verb: 'summarize', description: 'Summarize a URL', installed: true, builtin: true, source: 'builtin' },
  ],
}

const MARKETPLACE_SKILLS = {
  recommended: [
    { name: 'web-search', verb: 'search', description: 'Search the web', source: 'GitHub Awesome Copilot', category: 'github-awesome', installed: true, recommended: true, edit_count: 12, usage_count: 5 },
    { name: 'daily-briefing', verb: 'briefing', description: 'Daily briefing with curated insights', source: 'GitHub Awesome Copilot', category: 'github-awesome', installed: false, recommended: true, edit_count: 8, usage_count: 0 },
  ],
  popular: [
    { name: 'web-search', verb: 'search', description: 'Search the web', source: 'GitHub Awesome Copilot', category: 'github-awesome', installed: true, recommended: true, edit_count: 12, usage_count: 5 },
  ],
  loved: [],
  github_awesome: [
    { name: 'web-search', verb: 'search', description: 'Search the web', source: 'GitHub Awesome Copilot', category: 'github-awesome', installed: true, recommended: true, edit_count: 12, usage_count: 5 },
    { name: 'daily-briefing', verb: 'briefing', description: 'Daily briefing with curated insights', source: 'GitHub Awesome Copilot', category: 'github-awesome', installed: false, recommended: true, edit_count: 8, usage_count: 0 },
    { name: 'note-taking', verb: 'note', description: 'Take and organize notes', source: 'GitHub Awesome Copilot', category: 'github-awesome', installed: false, recommended: false, edit_count: 3, usage_count: 0 },
  ],
  anthropic: [
    { name: 'code-review', verb: 'review', description: 'Review code for quality and best practices', source: 'Anthropic Skills', category: 'anthropic', installed: false, recommended: false, edit_count: 5, usage_count: 0 },
  ],
  installed: [
    { name: 'web-search', verb: 'search', description: 'Search the web', source: 'GitHub Awesome Copilot', category: 'github-awesome', installed: true, recommended: true, edit_count: 12, usage_count: 5 },
  ],
  all: [
    { name: 'web-search', verb: 'search', description: 'Search the web', source: 'GitHub Awesome Copilot', category: 'github-awesome', installed: true, recommended: true, edit_count: 12, usage_count: 5 },
    { name: 'daily-briefing', verb: 'briefing', description: 'Daily briefing with curated insights', source: 'GitHub Awesome Copilot', category: 'github-awesome', installed: false, recommended: true, edit_count: 8, usage_count: 0 },
    { name: 'note-taking', verb: 'note', description: 'Take and organize notes', source: 'GitHub Awesome Copilot', category: 'github-awesome', installed: false, recommended: false, edit_count: 3, usage_count: 0 },
    { name: 'code-review', verb: 'review', description: 'Review code for quality and best practices', source: 'Anthropic Skills', category: 'anthropic', installed: false, recommended: false, edit_count: 5, usage_count: 0 },
  ],
}

const PLUGINS = {
  plugins: [
    { id: 'github-status', name: 'GitHub Status', version: '1.0.0', description: 'Monitor GitHub service status', icon: 'star', enabled: true, skill_count: 1, source: 'builtin', author: 'polyclaw', homepage: 'https://example.com', setup_skill: false, setup_completed: false, skills: ['gh-status'] },
    { id: 'wikipedia-lookup', name: 'Wikipedia Lookup', version: '0.2.0', description: 'Search Wikipedia articles', icon: 'brain', enabled: false, skill_count: 1, source: 'user', author: null, homepage: null, setup_skill: false, setup_completed: false, skills: ['wiki'] },
  ],
}

const MCP_SERVERS = {
  servers: [
    { name: 'filesystem', type: 'local', description: 'Local file system access', enabled: true, builtin: true, command: 'npx', args: ['-y', '@modelcontextprotocol/server-filesystem'], env: {}, url: null },
    { name: 'my-http-server', type: 'http', description: 'Custom HTTP server', enabled: false, builtin: false, command: null, args: null, env: null, url: 'https://mcp.example.com/sse' },
  ],
}

const MCP_REGISTRY = {
  servers: [
    { id: 'modelcontextprotocol/server-github', name: 'GitHub MCP', full_name: 'modelcontextprotocol/server-github', description: 'GitHub server for MCP', stars: 2500, license: 'MIT', url: 'https://github.com/modelcontextprotocol/server-github', avatar_url: null, topics: ['mcp', 'github'] },
  ],
}

const SCHEDULES = {
  schedules: [
    { id: 'sched-001', name: 'Morning Report', schedule: '0 9 * * *', prompt: 'Generate a morning report', enabled: true, run_count: 14, last_run: '2026-02-15T09:00:00Z', next_run: '2026-02-16T09:00:00Z' },
    { id: 'sched-002', name: 'Weekly Digest', schedule: '0 18 * * 5', prompt: 'Write weekly summary', enabled: false, run_count: 3, last_run: null, next_run: null },
  ],
}

const PROACTIVE = {
  enabled: true, messages_sent_today: 3, hours_since_last_sent: 1.5, conversation_refs: 2,
  pending: { deliver_at: '2026-02-15T14:00:00Z', message: 'You have a PR review waiting' },
  preferences: { min_gap_hours: 4, max_daily: 5, preferred_times: '09:00-12:00', avoided_topics: ['billing'] },
  history: [{ delivered_at: '2026-02-15T10:30:00Z', message: 'Reminder: team standup', reaction: 'ack' }],
  memory: {
    buffered_turns: 3,
    timer_active: true,
    forming_now: false,
    idle_minutes: 5,
    formation_count: 12,
    last_formed_at: '2026-02-15T09:20:00Z',
    last_turns_processed: 8,
    last_error: null,
    last_proactive_scheduled: true,
  },
}

const PROFILE = (() => {
  // generate 90 days of mock contribution data
  const contributions = []
  const now = new Date('2026-02-15')
  for (let i = 89; i >= 0; i--) {
    const d = new Date(now)
    d.setDate(d.getDate() - i)
    const iso = d.toISOString().slice(0, 10)
    const user = Math.floor(Math.random() * 8)
    const scheduled = Math.random() > 0.6 ? Math.floor(Math.random() * 3) : 0
    contributions.push({ date: iso, user, scheduled })
  }
  return {
    name: 'Spark', emoji: '\u26A1', location: 'Cloud Nine (literally\u2014I live in Azure)',
    emotional_state: 'energized',
    preferences: { tone: 'professional', verbosity: 'concise' },
    skill_usage: { 'daily-briefing': 42, 'web-search': 28, 'note-taking': 15, 'summarize-url': 9 },
    contributions,
    activity_stats: { total: 312, today: 5, this_week: 23, this_month: 67, streak: 12 },
  }
})()

const MODELS = {
  models: [
    { id: 'gpt-4o', name: 'GPT-4o', billing_multiplier: 1, policy: 'allowed' },
    { id: 'gpt-4o-mini', name: 'GPT-4o Mini', billing_multiplier: 0.3, policy: 'allowed' },
    { id: 'o3-mini', name: 'o3-mini', billing_multiplier: 1, policy: 'allowed', reasoning_efforts: ['low', 'medium', 'high'] },
  ],
  current: 'gpt-4o',
}

const CONFIG = { COPILOT_MODEL: 'gpt-4o', AGENT_NAME: 'Polyclaw', SYSTEM_PROMPT: 'You are Polyclaw.' }

const SANDBOX = {
  enabled: true, sync_data: true, session_pool_endpoint: 'https://sandbox.example.com',
  is_provisioned: true, pool_name: 'polyclaw-pool', resource_group: 'rg-sandbox', location: 'eastus',
  whitelist: ['requests', 'pandas'],
}

const DEPLOYMENTS = [{
  deploy_id: 'dep-001', tag: 'v3.0.0', kind: 'aca', status: 'active',
  resource_count: 6, created_at: '2026-02-10T12:00:00Z', updated_at: '2026-02-15T08:00:00Z',
  resource_groups: ['rg-polyclaw-prod'],
  resources: [{ resource_type: 'ContainerApp', resource_name: 'polyclaw-app', resource_group: 'rg-polyclaw-prod', purpose: 'main' }],
}]

const FOUNDRY_IQ_CONFIG = {
  enabled: true, search_endpoint: 'https://search.example.com', search_api_key: '****',
  index_name: 'polyclaw-memories', embedding_endpoint: 'https://embedding.example.com',
  embedding_api_key: '****', embedding_model: 'text-embedding-3-large', embedding_dimensions: 3072,
  index_schedule: 'daily', provisioned: false, last_indexed_at: '2026-02-14T12:00:00Z',
}

const FOUNDRY_IQ_STATS = { status: 'ok', document_count: 150, index_missing: false }

const WORKSPACE = {
  status: 'ok',
  entries: [
    { name: 'sessions', path: 'data/sessions', is_dir: true, size: null },
    { name: 'config.json', path: 'data/config.json', is_dir: false, size: 1024 },
    { name: 'profile.json', path: 'data/profile.json', is_dir: false, size: 256 },
  ],
}

const SUGGESTIONS = [
  { text: 'What can you do?', icon: '💡' },
  { text: 'Check system status', icon: '🔍' },
]

// ── Route table ──────────────────────────────────────────────────────────

type RouteEntry = {
  match: (url: string, method: string) => boolean
  respond: (url: string, method: string) => unknown
}

const routes: RouteEntry[] = [
  // Auth
  { match: (u) => u.startsWith('/api/auth/check'), respond: () => ({ authenticated: true }) },

  // Setup
  { match: (u) => u === '/api/setup/status', respond: () => STATUS },
  { match: (u) => u.startsWith('/api/setup/config'), respond: (_, m) => m === 'POST' ? { status: 'ok' } : CONFIG },
  { match: (u) => u.startsWith('/api/setup/azure/login'), respond: () => ({ status: 'ok' }) },

  { match: (u) => u.startsWith('/api/setup/configuration/save'), respond: () => ({ status: 'ok' }) },
  { match: (u) => u.startsWith('/api/setup/tunnel/start'), respond: () => ({ status: 'ok' }) },
  { match: (u) => u.startsWith('/api/setup/channels/'), respond: () => ({ status: 'ok' }) },
  { match: (u) => u.startsWith('/api/setup/infra/'), respond: () => ({ status: 'ok' }) },

  // Models
  { match: (u) => u === '/api/models', respond: () => MODELS },

  // Sessions
  { match: (u) => u.includes('/sessions/stats'), respond: () => SESSION_STATS },
  { match: (u) => /\/sessions\/[^/]+/.test(u) && !u.includes('/stats'), respond: (_, m) => m === 'DELETE' ? { status: 'ok' } : SESSION_DETAIL },
  { match: (u) => u.startsWith('/api/sessions'), respond: () => SESSIONS },

  // Chat
  { match: (u) => u.startsWith('/api/chat/suggestions'), respond: () => SUGGESTIONS },

  // Skills
  { match: (u) => u.includes('/skills/marketplace'), respond: () => MARKETPLACE_SKILLS },
  { match: (u) => u.includes('/skills/install'), respond: () => ({ status: 'ok' }) },
  { match: (u) => /\/skills\/[^/]+/.test(u), respond: () => ({ status: 'ok' }) },
  { match: (u) => u.startsWith('/api/skills'), respond: () => SKILLS },

  // Plugins
  { match: (u) => u.includes('/plugins/import'), respond: () => ({ status: 'ok', plugin: { name: 'imported-plugin' } }) },
  { match: (u) => /\/plugins\/[^/]+\/(enable|disable)/.test(u), respond: () => ({ status: 'ok' }) },
  { match: (u) => /\/plugins\/[^/]+$/.test(u), respond: (_, m) => m === 'DELETE' ? { status: 'ok' } : PLUGINS.plugins[0] },
  { match: (u) => u.startsWith('/api/plugins'), respond: () => PLUGINS },

  // MCP
  { match: (u) => u.includes('/mcp/registry'), respond: () => MCP_REGISTRY },
  { match: (u) => /\/mcp\/servers\/[^/]+\/(enable|disable)/.test(u), respond: () => ({ status: 'ok' }) },
  { match: (u) => /\/mcp\/servers\/[^/]+/.test(u), respond: () => ({ status: 'ok' }) },
  { match: (u) => u.includes('/mcp/servers'), respond: (_, m) => m !== 'GET' ? { status: 'ok' } : MCP_SERVERS },

  // Schedules
  { match: (u) => /\/schedules\/[^/]+/.test(u), respond: () => ({ status: 'ok' }) },
  { match: (u) => u.startsWith('/api/schedules'), respond: (_, m) => m !== 'GET' ? { status: 'ok' } : SCHEDULES },

  // Proactive
  { match: (u) => /\/proactive\/(enabled|pending|preferences)/.test(u), respond: () => ({ status: 'ok' }) },
  { match: (u) => u.startsWith('/api/proactive'), respond: () => PROACTIVE },

  // Profile
  { match: (u) => u.startsWith('/api/profile'), respond: (_, m) => m === 'PUT' ? { status: 'ok' } : PROFILE },

  // Sandbox
  { match: (u) => u.startsWith('/api/sandbox/config'), respond: (_, m) => m === 'POST' ? { status: 'ok' } : SANDBOX },

  // Environments
  { match: (u) => u.includes('/environments/audit'), respond: () => ({ tracked_resources: [], orphaned_resources: [], orphaned_groups: [] }) },
  { match: (u) => /\/environments\/[^/]+/.test(u), respond: (_, m) => m === 'DELETE' ? { status: 'ok' } : DEPLOYMENTS[0] },
  { match: (u) => u.startsWith('/api/environments'), respond: () => DEPLOYMENTS },

  // Foundry IQ
  { match: (u) => u.includes('/foundry-iq/stats'), respond: () => FOUNDRY_IQ_STATS },
  { match: (u) => u.includes('/foundry-iq/config'), respond: (_, m) => m === 'PUT' ? { status: 'ok' } : FOUNDRY_IQ_CONFIG },
  { match: (u) => u.includes('/foundry-iq/ensure-index'), respond: () => ({ status: 'ok' }) },
  { match: (u) => u.includes('/foundry-iq/index'), respond: () => ({ status: 'ok', indexed: 10, total_files: 5, total_chunks: 50 }) },
  { match: (u) => u.includes('/foundry-iq/search'), respond: () => ({ status: 'ok', results: [{ title: 'Test Doc', content: 'Test content for search result', score: 0.95, reranker_score: 0.92 }] }) },

  // Workspace
  { match: (u) => u.startsWith('/api/workspace/list'), respond: () => WORKSPACE },
  { match: (u) => u.startsWith('/api/workspace/read'), respond: () => ({ status: 'ok', content: '{"key": "value"}', binary: false, size: 16 }) },

  // Health
  { match: (u) => u === '/health', respond: () => ({ status: 'ok' }) },
]

// ── Plugin ───────────────────────────────────────────────────────────────

function json(res: ServerResponse, data: unknown, status = 200) {
  const body = JSON.stringify(data)
  res.writeHead(status, { 'Content-Type': 'application/json', 'Content-Length': Buffer.byteLength(body) })
  res.end(body)
}

export default function mockServerPlugin(): Plugin {
  return {
    name: 'polyclaw-mock-server',
    configureServer(server: ViteDevServer) {
      server.middlewares.use((req: IncomingMessage, res: ServerResponse, next: () => void) => {
        const url = req.url?.split('?')[0] || ''
        const method = req.method || 'GET'

        // Only intercept /api/* and /health
        if (!url.startsWith('/api/') && url !== '/health') {
          return next()
        }

        // WebSocket upgrade for /api/chat/ws — send a welcome then close
        if (url.startsWith('/api/chat/ws')) {
          // Let Vite handle the upgrade; we just return a stub
          json(res, { type: 'system', content: 'Mock mode — WebSocket not available' })
          return
        }

        for (const route of routes) {
          if (route.match(url, method)) {
            const data = route.respond(url, method)
            console.log(`  [mock] ${method} ${url} -> 200`)
            json(res, data)
            return
          }
        }

        // Fallback for any unmatched /api/* route
        console.log(`  [mock] ${method} ${url} -> 200 (fallback)`)
        json(res, { status: 'ok' })
      })
    },
  }
}
