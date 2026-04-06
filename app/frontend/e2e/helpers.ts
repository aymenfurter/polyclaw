/**
 * Shared mock-API helpers for Playwright E2E tests.
 *
 * Every test calls `mockApi(page)` in a beforeEach to intercept all /api/*
 * requests with deterministic JSON.  Individual tests can override specific
 * routes afterwards with `page.route(...)`.
 */

import { type Page } from '@playwright/test'

// ── Fixture data ──────────────────────────────────────────────────────────

export const MOCK_STATUS = {
  azure: { logged_in: true, user: 'test@example.com', subscription: 'sub-123' },
  foundry: { deployed: true, endpoint: 'https://mock-foundry.cognitiveservices.azure.com', name: 'mock-foundry' },
  prerequisites_configured: true,
  telegram_configured: false,
  tunnel: { active: false, url: '' },
  bot_configured: true,
  voice_call_configured: false,
  model: 'gpt-4o',
}

export const MOCK_STATUS_NEEDS_SETUP = {
  azure: { logged_in: false },
  foundry: { deployed: false },
  prerequisites_configured: false,
  telegram_configured: false,
  tunnel: { active: false },
  bot_configured: false,
  voice_call_configured: false,
}

export const MOCK_SESSIONS = [
  {
    id: 'sess-001',
    model: 'gpt-4o',
    started_at: '2026-02-15T10:00:00Z',
    message_count: 5,
    first_message: 'Hello, World!',
  },
  {
    id: 'sess-002',
    model: 'gpt-4o-mini',
    started_at: '2026-02-14T08:30:00Z',
    message_count: 12,
    first_message: 'Deploy the app',
  },
]

export const MOCK_SESSION_STATS = {
  total: 2,
  today: 1,
  this_week: 2,
  avg_messages: 8.5,
}

export const MOCK_SESSION_DETAIL = {
  messages: [
    { role: 'user', content: 'Hello, World!', timestamp: '2026-02-15T10:00:01Z' },
    { role: 'assistant', content: 'Hi! How can I help?', timestamp: '2026-02-15T10:00:02Z' },
  ],
}

export const MOCK_SKILLS = {
  skills: [
    { name: 'web-search', verb: 'search', description: 'Search the web', installed: true, builtin: false, source: 'marketplace' },
    { name: 'summarize-url', verb: 'summarize', description: 'Summarize a URL', installed: true, builtin: true, source: 'builtin' },
  ],
}

export const MOCK_MARKETPLACE_SKILLS = {
  recommended: [
    { name: 'web-search', verb: 'search', description: 'Search the web', source: 'GitHub Awesome Copilot', category: 'github-awesome', installed: true, recommended: true, edit_count: 12, usage_count: 5 },
    { name: 'daily-briefing', verb: 'briefing', description: 'Daily briefing', source: 'GitHub Awesome Copilot', category: 'github-awesome', installed: false, recommended: true, edit_count: 8, usage_count: 0 },
  ],
  popular: [],
  loved: [],
  github_awesome: [
    { name: 'web-search', verb: 'search', description: 'Search the web', source: 'GitHub Awesome Copilot', category: 'github-awesome', installed: true, recommended: true, edit_count: 12, usage_count: 5 },
    { name: 'daily-briefing', verb: 'briefing', description: 'Daily briefing', source: 'GitHub Awesome Copilot', category: 'github-awesome', installed: false, recommended: true, edit_count: 8, usage_count: 0 },
    { name: 'note-taking', verb: 'note', description: 'Take notes', source: 'GitHub Awesome Copilot', category: 'github-awesome', installed: false, recommended: false, edit_count: 3, usage_count: 0 },
  ],
  anthropic: [],
  installed: [
    { name: 'web-search', verb: 'search', description: 'Search the web', source: 'GitHub Awesome Copilot', category: 'github-awesome', installed: true, recommended: true, edit_count: 12, usage_count: 5 },
  ],
  all: [
    { name: 'web-search', verb: 'search', description: 'Search the web', source: 'GitHub Awesome Copilot', category: 'github-awesome', installed: true, recommended: true, edit_count: 12, usage_count: 5 },
    { name: 'daily-briefing', verb: 'briefing', description: 'Daily briefing', source: 'GitHub Awesome Copilot', category: 'github-awesome', installed: false, recommended: true, edit_count: 8, usage_count: 0 },
    { name: 'note-taking', verb: 'note', description: 'Take notes', source: 'GitHub Awesome Copilot', category: 'github-awesome', installed: false, recommended: false, edit_count: 3, usage_count: 0 },
  ],
}

export const MOCK_PLUGINS = {
  plugins: [
    {
      id: 'github-status', name: 'GitHub Status', version: '1.0.0',
      description: 'Monitor GitHub service status', icon: 'star', enabled: true,
      skill_count: 1, source: 'builtin', author: 'polyclaw', homepage: 'https://example.com',
      setup_skill: false, setup_completed: false, skills: ['gh-status'],
    },
    {
      id: 'wikipedia-lookup', name: 'Wikipedia Lookup', version: '0.2.0',
      description: 'Search Wikipedia articles', icon: 'brain', enabled: false,
      skill_count: 1, source: 'user', author: null, homepage: null,
      setup_skill: false, setup_completed: false, skills: ['wiki'],
    },
  ],
}

export const MOCK_MCP_SERVERS = {
  servers: [
    {
      name: 'filesystem', type: 'local', description: 'Local file system access',
      enabled: true, builtin: true, command: 'npx', args: ['-y', '@modelcontextprotocol/server-filesystem'],
      env: {}, url: null,
    },
    {
      name: 'my-http-server', type: 'http', description: 'Custom HTTP server',
      enabled: false, builtin: false, command: null, args: null,
      env: null, url: 'https://mcp.example.com/sse',
    },
  ],
}

export const MOCK_MCP_REGISTRY = {
  servers: [
    {
      id: 'modelcontextprotocol/server-github', name: 'GitHub MCP',
      full_name: 'modelcontextprotocol/server-github',
      description: 'GitHub server for MCP', stars: 2500, license: 'MIT',
      url: 'https://github.com/modelcontextprotocol/server-github',
      avatar_url: null, topics: ['mcp', 'github'],
    },
  ],
}

export const MOCK_SCHEDULES = {
  schedules: [
    {
      id: 'sched-001', name: 'Morning Report', schedule: '0 9 * * *',
      prompt: 'Generate a morning report', enabled: true,
      run_count: 14, last_run: '2026-02-15T09:00:00Z', next_run: '2026-02-16T09:00:00Z',
    },
    {
      id: 'sched-002', name: 'Weekly Digest', schedule: '0 18 * * 5',
      prompt: 'Write weekly summary', enabled: false,
      run_count: 3, last_run: null, next_run: null,
    },
  ],
}

export const MOCK_PROACTIVE = {
  enabled: true,
  messages_sent_today: 3,
  hours_since_last_sent: 1.5,
  conversation_refs: 2,
  pending: { deliver_at: '2026-02-15T14:00:00Z', message: 'You have a PR review waiting' },
  preferences: {
    min_gap_hours: 4, max_daily: 5,
    preferred_times: '09:00-12:00', avoided_topics: ['billing'],
  },
  history: [
    { delivered_at: '2026-02-15T10:30:00Z', message: 'Reminder: team standup', reaction: 'ack' },
  ],
}

export const MOCK_PROFILE = {
  name: 'Polyclaw Agent',
  personality: 'Helpful and proactive assistant',
  instructions: 'You are a coding assistant.',
  avatar_url: null,
  contributions: [
    { date: '2026-02-14', user: 3, scheduled: 1 },
    { date: '2026-02-15', user: 5, scheduled: 2 },
  ],
}

export const MOCK_MODELS = {
  models: [
    { id: 'gpt-4o', name: 'GPT-4o', billing_multiplier: 1, policy: 'allowed' },
    { id: 'gpt-4o-mini', name: 'GPT-4o Mini', billing_multiplier: 0.3, policy: 'allowed' },
    { id: 'o3-mini', name: 'o3-mini', billing_multiplier: 1, policy: 'allowed', reasoning_efforts: ['low', 'medium', 'high'] },
  ],
  current: 'gpt-4o',
}

export const MOCK_CONFIG = {
  COPILOT_MODEL: 'gpt-4o',
  AGENT_NAME: 'Polyclaw',
  SYSTEM_PROMPT: 'You are Polyclaw.',
}

export const MOCK_SANDBOX = {
  enabled: true,
  sync_data: true,
  session_pool_endpoint: 'https://sandbox.example.com',
  is_provisioned: true,
  pool_name: 'polyclaw-pool',
  resource_group: 'rg-sandbox',
  location: 'eastus',
  whitelist: ['requests', 'pandas'],
}

export const MOCK_DEPLOYMENTS = {
  deployments: [
    {
      deploy_id: 'dep-001', tag: 'v3.0.0', kind: 'aca', status: 'active',
      resource_count: 6, created_at: '2026-02-10T12:00:00Z', updated_at: '2026-02-15T08:00:00Z',
      resource_groups: ['rg-polyclaw-prod'],
      resources: [
        { resource_type: 'ContainerApp', resource_name: 'polyclaw-app', resource_group: 'rg-polyclaw-prod', purpose: 'main' },
      ],
    },
  ],
}

export const MOCK_FOUNDRY_IQ_CONFIG = {
  enabled: true,
  search_endpoint: 'https://search.example.com',
  search_api_key: '****',
  index_name: 'polyclaw-memories',
  embedding_endpoint: 'https://embedding.example.com',
  embedding_api_key: '****',
  embedding_model: 'text-embedding-3-large',
  embedding_dimensions: 3072,
  index_schedule: 'daily',
  provisioned: false,
  last_indexed_at: '2026-02-14T12:00:00Z',
}

export const MOCK_FOUNDRY_IQ_STATS = {
  status: 'ok',
  document_count: 150,
  index_missing: false,
}

export const MOCK_WORKSPACE = {
  status: 'ok',
  entries: [
    { name: 'sessions', path: 'data/sessions', is_dir: true, size: null },
    { name: 'config.json', path: 'data/config.json', is_dir: false, size: 1024 },
    { name: 'profile.json', path: 'data/profile.json', is_dir: false, size: 256 },
  ],
}

export const MOCK_SUGGESTIONS = [
  { text: 'What can you do?', icon: '💡' },
  { text: 'Check system status', icon: '🔍' },
]

// ── Route interceptor ─────────────────────────────────────────────────────

/**
 * Intercepts all /api/* and /health routes with mock responses.
 * Call in beforeEach — individual tests can add page.route() overrides after.
 */
export async function mockApi(page: Page) {
  // Auth check — always pass
  await page.route('**/api/auth/check', route =>
    route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ authenticated: true }) }),
  )

  // Setup status
  await page.route('**/api/setup/status', route =>
    route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(MOCK_STATUS) }),
  )

  // Config
  await page.route('**/api/setup/config', route => {
    if (route.request().method() === 'POST') {
      return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ status: 'ok' }) })
    }
    return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(MOCK_CONFIG) })
  })

  // Models
  await page.route('**/api/models', route =>
    route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(MOCK_MODELS) }),
  )

  // Sessions — single catch-all handler to avoid route conflicts
  await page.route('**/api/sessions**', route => {
    const url = route.request().url()
    if (url.includes('/sessions/stats')) {
      return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(MOCK_SESSION_STATS) })
    }
    if (/\/sessions\/[^/]+/.test(url) && !url.includes('/stats')) {
      if (route.request().method() === 'DELETE') {
        return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ status: 'ok' }) })
      }
      return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(MOCK_SESSION_DETAIL) })
    }
    return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(MOCK_SESSIONS) })
  })

  // Chat suggestions
  await page.route('**/api/chat/suggestions', route =>
    route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(MOCK_SUGGESTIONS) }),
  )

  // Skills — single catch-all handler
  await page.route('**/api/skills**', route => {
    const url = route.request().url()
    if (url.includes('/skills/marketplace')) {
      return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(MOCK_MARKETPLACE_SKILLS) })
    }
    if (url.includes('/skills/install')) {
      return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ status: 'ok' }) })
    }
    if (/\/skills\/[^/]+/.test(url)) {
      return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ status: 'ok' }) })
    }
    return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(MOCK_SKILLS) })
  })

  // Plugins — single catch-all handler
  await page.route('**/api/plugins**', route => {
    const url = route.request().url()
    const method = route.request().method()
    if (url.includes('/plugins/import')) {
      return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ status: 'ok', plugin: { name: 'imported-plugin' } }) })
    }
    if (/\/plugins\/[^/]+\/(enable|disable)/.test(url)) {
      return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ status: 'ok' }) })
    }
    if (/\/plugins\/[^/]+$/.test(url)) {
      if (method === 'DELETE') {
        return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ status: 'ok' }) })
      }
      return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(MOCK_PLUGINS.plugins[0]) })
    }
    return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(MOCK_PLUGINS) })
  })

  // MCP — single catch-all handler
  await page.route('**/api/mcp/**', route => {
    const url = route.request().url()
    const method = route.request().method()
    if (url.includes('/mcp/registry')) {
      return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(MOCK_MCP_REGISTRY) })
    }
    if (/\/mcp\/servers\/[^/]+\/(enable|disable)/.test(url)) {
      return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ status: 'ok' }) })
    }
    if (/\/mcp\/servers\/[^/]+/.test(url)) {
      return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ status: 'ok' }) })
    }
    if (url.includes('/mcp/servers')) {
      if (method !== 'GET') {
        return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ status: 'ok' }) })
      }
      return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(MOCK_MCP_SERVERS) })
    }
    return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ status: 'ok' }) })
  })

  // Schedules — single catch-all
  await page.route('**/api/schedules**', route => {
    const url = route.request().url()
    if (/\/schedules\/[^/]+/.test(url)) {
      return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ status: 'ok' }) })
    }
    if (route.request().method() !== 'GET') {
      return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ status: 'ok' }) })
    }
    return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(MOCK_SCHEDULES) })
  })

  // Proactive — single catch-all
  await page.route('**/api/proactive**', route => {
    const url = route.request().url()
    if (url.includes('/proactive/enabled') || url.includes('/proactive/pending') || url.includes('/proactive/preferences')) {
      return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ status: 'ok' }) })
    }
    return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(MOCK_PROACTIVE) })
  })

  // Profile
  await page.route('**/api/profile', route => {
    if (route.request().method() === 'PUT') {
      return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ status: 'ok' }) })
    }
    return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(MOCK_PROFILE) })
  })

  // Sandbox
  await page.route('**/api/sandbox/config', route => {
    if (route.request().method() === 'POST') {
      return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ status: 'ok' }) })
    }
    return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(MOCK_SANDBOX) })
  })

  // Environments — single catch-all
  await page.route('**/api/environments**', route => {
    const url = route.request().url()
    if (url.includes('/environments/audit')) {
      return route.fulfill({
        status: 200, contentType: 'application/json',
        body: JSON.stringify({ tracked_resources: [], orphaned_resources: [], orphaned_groups: [] }),
      })
    }
    if (/\/environments\/[^/]+/.test(url)) {
      if (route.request().method() === 'DELETE') {
        return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ status: 'ok' }) })
      }
      return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(MOCK_DEPLOYMENTS.deployments[0]) })
    }
    return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(MOCK_DEPLOYMENTS) })
  })

  // Foundry IQ — single catch-all
  await page.route('**/api/foundry-iq/**', route => {
    const url = route.request().url()
    if (url.includes('/foundry-iq/stats')) {
      return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(MOCK_FOUNDRY_IQ_STATS) })
    }
    if (url.includes('/foundry-iq/config')) {
      if (route.request().method() === 'PUT') {
        return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ status: 'ok' }) })
      }
      return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(MOCK_FOUNDRY_IQ_CONFIG) })
    }
    if (url.includes('/foundry-iq/ensure-index')) {
      return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ status: 'ok' }) })
    }
    if (url.includes('/foundry-iq/index')) {
      return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ status: 'ok', indexed: 10, total_files: 5, total_chunks: 50 }) })
    }
    if (url.includes('/foundry-iq/search')) {
      return route.fulfill({
        status: 200, contentType: 'application/json',
        body: JSON.stringify({
          status: 'ok',
          results: [{ title: 'Test Doc', content: 'Test content for search result', score: 0.95, reranker_score: 0.92 }],
        }),
      })
    }
    return route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ status: 'ok' }) })
  })

  // Workspace
  await page.route('**/api/workspace/list*', route =>
    route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify(MOCK_WORKSPACE) }),
  )
  await page.route('**/api/workspace/read*', route =>
    route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ status: 'ok', content: '{"key": "value"}', binary: false, size: 16 }) }),
  )

  // Setup actions
  await page.route('**/api/setup/azure/login', route =>
    route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ status: 'ok' }) }),
  )
  await page.route('**/api/setup/configuration/save', route =>
    route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ status: 'ok' }) }),
  )
  await page.route('**/api/setup/tunnel/start', route =>
    route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ status: 'ok' }) }),
  )
  await page.route('**/api/setup/channels/telegram/*', route =>
    route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ status: 'ok' }) }),
  )
  await page.route('**/api/setup/infra/*', route =>
    route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ status: 'ok' }) }),
  )

  // Health
  await page.route('**/health', route =>
    route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ status: 'ok' }) }),
  )

  // Static assets — just return 404 silently so images don't break tests
  await page.route('**/static/**', route =>
    route.fulfill({ status: 404, body: '' }),
  )

  // WebSocket — abort to prevent real connection attempts
  await page.route('**/api/chat/ws*', route => route.abort())
}

/**
 * Bypass the disclaimer + login gates so tests land directly on the app.
 * Should be called BEFORE page.goto().
 */
export async function bypassAuth(page: Page) {
  await page.addInitScript(() => {
    localStorage.setItem('polyclaw_disclaimer_accepted', '1')
    localStorage.setItem('polyclaw_secret', 'test-secret')
  })
}
