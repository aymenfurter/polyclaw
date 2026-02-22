/* -----------------------------------------------------------------------
   TypeScript types mirroring the polyclaw backend API responses.
   ----------------------------------------------------------------------- */

// -- Auth ----------------------------------------------------------------

export interface AuthCheckResponse {
  authenticated: boolean
}

// -- Setup Status --------------------------------------------------------

export interface SetupStatus {
  azure?: { logged_in?: boolean; subscription?: string; tenant?: string }
  copilot?: { authenticated?: boolean; username?: string }
  prerequisites_configured?: boolean
  telegram_configured?: boolean
  tunnel?: { active?: boolean; url?: string; restricted?: boolean }
  bot_configured?: boolean
  bot_deployed?: boolean
  voice_call_configured?: boolean
  model?: string
}

// -- Chat ----------------------------------------------------------------

export type ChatMessageRole = 'user' | 'assistant' | 'system' | 'error'

export interface ToolCall {
  tool: string
  call_id: string
  arguments?: string
  result?: string
  status: 'running' | 'done' | 'pending_approval' | 'pending_phone' | 'denied'
}

export interface ChatMessage {
  id: string
  role: ChatMessageRole
  content: string
  timestamp: number
  cards?: AdaptiveCard[]
  media?: MediaFile[]
  reasoning?: string
  toolCalls?: ToolCall[]
  skill?: string
}

export interface AdaptiveCard {
  type: string
  body?: unknown[]
  actions?: unknown[]
  [key: string]: unknown
}

export interface MediaFile {
  kind: string
  name: string
  url?: string
  content_type?: string
}

export type WsIncoming =
  | { type: 'delta'; content: string }
  | { type: 'message'; content: string }
  | { type: 'done' }
  | { type: 'cards'; cards: AdaptiveCard[] }
  | { type: 'media'; files: MediaFile[] }
  | { type: 'event'; event: string; tool?: string; call_id?: string; text?: string; arguments?: string; result?: string; name?: string; content?: string; approved?: boolean }
  | { type: 'error'; content: string }
  | { type: 'system'; content: string }

// -- Sessions ------------------------------------------------------------

export interface Session {
  id: string
  created_at: number
  updated_at?: number
  model: string
  message_count: number
  title?: string
}

export interface SessionDetail extends Session {
  messages: { role: string; content: string; timestamp: number }[]
}

export interface SessionStats {
  total_sessions: number
  total_messages: number
  archival_policy?: string
}

// -- Skills --------------------------------------------------------------

export interface Skill {
  name: string
  verb?: string
  description: string
  source: string
  installed: boolean
  builtin?: boolean
  version?: string
  origin?: string
}

export interface MarketplaceSkill {
  name: string
  verb: string
  description: string
  source: string
  category?: string
  installed: boolean
  version?: string
  author?: string
  recommended?: boolean
  edit_count?: number
  usage_count?: number
  origin?: string
}

export interface MarketplaceResponse {
  recommended: MarketplaceSkill[]
  popular: MarketplaceSkill[]
  loved: MarketplaceSkill[]
  github_awesome: MarketplaceSkill[]
  anthropic: MarketplaceSkill[]
  installed: MarketplaceSkill[]
  all: MarketplaceSkill[]
  rate_limit_warning?: string
}

// -- Plugins -------------------------------------------------------------

export interface Plugin {
  id: string
  name: string
  description: string
  version: string
  icon: string
  source: string
  author?: string
  enabled: boolean
  skill_count: number
  skills?: string[]
  setup_skill?: string
  setup_completed?: boolean
  installed_at?: string
  homepage?: string
}

// -- MCP Servers ---------------------------------------------------------

export interface McpServer {
  name: string
  type: 'local' | 'http' | 'sse' | 'remote'
  enabled: boolean
  builtin?: boolean
  description?: string
  url?: string
  command?: string
  args?: string[]
  env?: Record<string, string>
}

export interface McpRegistryEntry {
  id: string
  name: string
  full_name?: string
  description?: string
  url?: string
  avatar_url?: string
  stars: number
  topics?: string[]
  license?: string
}

// -- Schedules -----------------------------------------------------------

export interface Schedule {
  id: string
  description: string
  prompt: string
  cron: string | null
  run_at: string | null
  created_at?: string
  last_run?: string | null
  enabled: boolean
}

// -- Profile -------------------------------------------------------------

export interface AgentProfile {
  name: string
  emoji?: string
  location?: string
  emotional_state?: string
  preferences?: Record<string, unknown>
  skill_usage?: Record<string, number>
  contributions?: ContributionDay[]
  activity_stats?: ActivityStats
}

export interface ContributionDay {
  date: string
  user: number
  scheduled: number
}

export interface ActivityStats {
  total: number
  today: number
  this_week: number
  this_month: number
  streak: number
}

// -- Proactive -----------------------------------------------------------

export interface MemoryAgentStatus {
  buffered_turns: number
  timer_active: boolean
  forming_now: boolean
  idle_minutes: number
  formation_count: number
  last_formed_at: string | null
  last_turns_processed: number
  last_error: string | null
  last_proactive_scheduled: boolean
}

export interface ProactiveState {
  enabled: boolean
  messages_sent_today?: number
  hours_since_last_sent?: number | null
  conversation_refs?: number
  pending?: {
    deliver_at: string
    message: string
    context?: string
  }
  preferences?: {
    min_gap_hours?: number
    max_daily?: number
    preferred_times?: string
    avoided_topics?: string[]
  }
  history?: ProactiveHistoryItem[]
  memory?: MemoryAgentStatus
}

export interface ProactiveHistoryItem {
  delivered_at: string
  message: string
  context?: string
  reaction?: string
}

// -- Environments --------------------------------------------------------

export interface Deployment {
  deploy_id: string
  tag: string
  kind: string
  status: string
  resource_groups?: string[]
  resource_count: number
  created_at?: string
  updated_at?: string
  resources?: DeploymentResource[]
  config?: Record<string, unknown>
}

export interface DeploymentResource {
  resource_type: string
  resource_name: string
  resource_group: string
  purpose?: string
}

// -- Foundry IQ ----------------------------------------------------------

export interface FoundryIQConfig {
  enabled: boolean
  provisioned?: boolean
  is_configured?: boolean
  search_endpoint?: string
  search_api_key?: string
  index_name?: string
  embedding_endpoint?: string
  embedding_api_key?: string
  embedding_model?: string
  embedding_dimensions?: number
  index_schedule?: string
  last_indexed_at?: string
  resource_group?: string
  location?: string
  search_resource_name?: string
  openai_resource_name?: string
  pool_name?: string
}

// -- Sandbox -------------------------------------------------------------

export interface SandboxConfig {
  enabled: boolean
  sync_data?: boolean
  session_pool_endpoint?: string
  is_provisioned?: boolean
  pool_name?: string
  resource_group?: string
  location?: string
  whitelist?: string[]
  blacklist?: string[]
}

// -- Content Safety ------------------------------------------------------

export interface ContentSafetyConfig {
  deployed: boolean
  endpoint?: string
  filter_mode?: string
}

// -- Workspace -----------------------------------------------------------

export interface WorkspaceEntry {
  name: string
  path: string
  is_dir: boolean
  size?: number
}

// -- Models --------------------------------------------------------------

export interface ModelInfo {
  id: string
  name: string
  billing_multiplier?: number
  reasoning_efforts?: string[] | null
  policy?: string
}

// -- Suggestions ---------------------------------------------------------

export interface Suggestion {
  text: string
  icon?: string
}

// -- Reasoning display ---------------------------------------------------

export interface WindowWord {
  text: string
  idx: number
  distance: number  // 0 = focal, 1..N = further away
}

// -- Infrastructure ------------------------------------------------------

export interface InfraStatus {
  deployed: boolean
  resource_group?: string
  location?: string
  bot_name?: string
  app_service?: string
  tunnel_url?: string
}

// -- Network Info --------------------------------------------------------

export interface NetworkEndpoint {
  method: string
  path: string
  category: string
  tunnel_exposed: boolean
  container: 'admin' | 'runtime' | 'shared'
  auth_type?: string
  source?: 'admin' | 'runtime'
}

export interface ProbedEndpoint extends NetworkEndpoint {
  requires_auth: boolean | null
  tunnel_blocked: boolean | null
  auth_type: 'admin_key' | 'bot_jwt' | 'acs_token' | 'health' | 'open' | undefined
  framework_auth_ok: boolean | null
  probe_error?: string | null
  source: 'admin' | 'runtime'
}

export interface ProbeCounts {
  total: number
  public_no_auth: number
  auth_required: number
  tunnel_accessible: number
  tunnel_blocked: number
  auth_types: Record<string, number>
  framework_auth_ok: number
  framework_auth_fail: number
}

export interface ProbeResult {
  endpoints: ProbedEndpoint[]
  admin: ProbeCounts
  runtime: ProbeCounts
  counts: ProbeCounts
  runtime_reachable: boolean
  tunnel_restricted_during_probe: boolean
}

export interface NetworkComponent {
  name: string
  type: string
  status: string
  endpoint?: string
  url?: string
  path?: string
  deployment?: string
  model?: string
  app_id?: string
  source_number?: string
  restricted?: boolean
  deploy_mode?: string
}

export interface ContainerInfo {
  role: 'admin' | 'runtime' | 'combined'
  label: string
  port: number
  host: string
  exposure: string
  identity?: string
  volumes?: string[]
}

export interface NetworkInfo {
  deploy_mode: 'docker' | 'aca' | 'local'
  admin_port: number
  server_mode: 'combined' | 'admin' | 'runtime'
  tunnel: {
    active: boolean
    url: string | null
    restricted: boolean
  }
  lockdown_mode: boolean
  components: NetworkComponent[]
  endpoints: NetworkEndpoint[]
  containers: ContainerInfo[]
}

// -- Resource Network Audit -----------------------------------------------

export interface ResourceAudit {
  name: string
  resource_group: string
  type: string
  icon: string
  public_access: boolean
  default_action: string
  allowed_ips: string[]
  allowed_vnets: string[]
  private_endpoints: string[]
  https_only?: boolean
  min_tls_version?: string
  extra: Record<string, unknown>
}

export interface ResourceAuditResponse {
  resources: ResourceAudit[]
  error?: string
}

// -- Generic API Response ------------------------------------------------

export interface ApiResponse {
  status: 'ok' | 'error'
  message?: string
  [key: string]: unknown
}

// -- Monitoring / OTel ---------------------------------------------------

export interface MonitoringConfig {
  enabled: boolean
  connection_string_masked: string
  connection_string_set: boolean
  sampling_ratio: number
  enable_live_metrics: boolean
  instrumentation_options: Record<string, unknown>
  otel_status: {
    active: boolean
    tracer_provider?: string
  }
  // Provisioning metadata
  provisioned: boolean
  app_insights_name?: string
  workspace_name?: string
  resource_group?: string
  location?: string
  subscription_id?: string
  portal_url?: string
  grafana_dashboard_url?: string
}

// -- Guardrails / HITL ---------------------------------------------------

export type MitigationStrategy = 'allow' | 'deny' | 'hitl' | 'pitl' | 'aitl' | 'filter'

export interface GuardrailsConfig {
  enabled: boolean
  default_strategy: MitigationStrategy
  context_defaults: Record<string, MitigationStrategy>
  tool_policies: Record<string, Record<string, MitigationStrategy>>
  model_columns: string[]
  model_policies: Record<string, Record<string, Record<string, MitigationStrategy>>>
  hitl_channel: 'chat' | 'phone'
  phone_number: string
  aitl_model: string
  aitl_spotlighting: boolean
  filter_mode: 'prompt_shields'
  content_safety_endpoint: string
  // Backward-compat fields
  hitl_enabled: boolean
  default_action: string
  default_channel: 'chat' | 'phone'
  rules: GuardrailRule[]
}

export interface ToolInventoryItem {
  id: string
  name: string
  category: 'sdk' | 'custom' | 'mcp' | 'skill'
  source: string
  description: string
  enabled?: boolean
  server_type?: string
  builtin?: boolean
}

export interface StrategyInfo {
  id: MitigationStrategy
  label: string
  description: string
  color: string
}

export interface ContextInfo {
  id: string
  label: string
  description: string
}

// Legacy aliases kept for backward compat
export interface GuardrailRule {
  id: string
  name: string
  pattern: string
  scope: 'tool' | 'mcp'
  action: 'allow' | 'deny' | 'ask'
  enabled: boolean
  description: string
  contexts: string[]
  models: string[]
  hitl_channel: 'chat' | 'phone'
}

export interface ExecutionContextInfo {
  id: string
  label: string
  description: string
}

export interface HitlChannelInfo {
  id: string
  label: string
  description: string
}

export interface ToolInfo {
  name: string
  source: string
  description: string
}

export interface McpServerInfo {
  name: string
  enabled: boolean
  description: string
  type: string
  builtin: boolean
}

// -- Security Preflight --------------------------------------------------

export interface PreflightCheck {
  id: string
  category: string
  name: string
  status: 'pending' | 'pass' | 'fail' | 'warn' | 'skip'
  detail: string
  evidence: string
  command: string
}

export interface PreflightResult {
  checks: PreflightCheck[]
  run_at: string | null
  passed: number
  failed: number
  warnings: number
  skipped: number
}
