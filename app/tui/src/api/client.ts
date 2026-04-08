/**
 * API client for communicating with the polyclaw admin server.
 *
 * Wraps fetch with authorization, base URL, and JSON parsing.
 */

export interface ApiClientOptions {
  baseUrl: string;
  adminSecret: string;
}

export class ApiClient {
  private baseUrl: string;
  private secret: string;

  constructor(opts: ApiClientOptions) {
    this.baseUrl = opts.baseUrl.replace(/\/+$/, "");
    this.secret = opts.adminSecret;
  }

  /** Full admin URL with secret query string (for opening a browser). */
  get webUiUrl(): string {
    return this.secret
      ? `${this.baseUrl}/?secret=${this.secret}`
      : this.baseUrl;
  }

  // -----------------------------------------------------------------------
  // Low-level helpers
  // -----------------------------------------------------------------------

  private headers(): Record<string, string> {
    const h: Record<string, string> = { "Content-Type": "application/json" };
    if (this.secret) h["Authorization"] = `Bearer ${this.secret}`;
    return h;
  }

  /** Raw fetch with auth headers. */
  async fetchRaw(path: string, init?: RequestInit): Promise<Response> {
    const url = `${this.baseUrl}${path}`;
    return fetch(url, {
      ...init,
      headers: { ...this.headers(), ...(init?.headers as Record<string, string> || {}) },
      signal: init?.signal ?? AbortSignal.timeout(15_000),
    });
  }

  /** Convenience: GET or POST, parse JSON, throw on non-2xx. */
  async fetch<T = Record<string, unknown>>(path: string, init?: RequestInit): Promise<T> {
    const res = await this.fetchRaw(path, init);
    if (!res.ok) {
      const text = await res.text().catch(() => "");
      throw new Error(`${res.status} ${res.statusText}: ${text}`);
    }
    return res.json() as Promise<T>;
  }

  // -----------------------------------------------------------------------
  // Setup
  // -----------------------------------------------------------------------

  async getSetupStatus() {
    return this.fetch<{
      azure?: { logged_in?: boolean; user?: string; subscription?: string };
      prerequisites_configured?: boolean;
      telegram_configured?: boolean;
      tunnel?: { active?: boolean; url?: string };
      bot_configured?: boolean;
      voice_call_configured?: boolean;
      model?: string;
    }>("/api/setup/status");
  }

  async azureLogin() { return this.fetch<Record<string, string>>("/api/setup/azure/login", { method: "POST" }); }
  async azureLogout() { return this.fetch<Record<string, string>>("/api/setup/azure/logout", { method: "POST" }); }
  async azureCheck() { return this.fetch<Record<string, string>>("/api/setup/azure/check"); }
  async startTunnel() { return this.fetch<Record<string, string>>("/api/setup/tunnel/start", { method: "POST" }); }
  async smokeTest() { return this.fetch<Record<string, unknown>>("/api/setup/smoke-test", { method: "POST" }); }

  async getBotConfig() { return this.fetch<Record<string, string>>("/api/setup/bot-config"); }
  async getChannelsConfig() { return this.fetch<Record<string, Record<string, string>>>("/api/setup/channels"); }
  async getInfraStatus() { return this.fetch<Record<string, unknown>>("/api/setup/infra/status"); }
  async getPreflight() { return this.fetch<Record<string, unknown>>("/api/setup/preflight"); }

  async saveConfiguration(body: unknown) { return this.fetch<Record<string, unknown>>("/api/setup/configure", { method: "POST", body: JSON.stringify(body) }); }
  async deployInfra() { return this.fetch<Record<string, unknown>>("/api/setup/infra/deploy", { method: "POST", signal: AbortSignal.timeout(300_000) }); }
  async decommissionInfra() { return this.fetch<Record<string, unknown>>("/api/setup/infra/decommission", { method: "POST", signal: AbortSignal.timeout(120_000) }); }

  // -----------------------------------------------------------------------
  // Sessions
  // -----------------------------------------------------------------------

  async listSessions() { return this.fetch<Record<string, unknown>[]>("/api/sessions"); }
  async getSession(id: string) { return this.fetch<Record<string, unknown>>(`/api/sessions/${id}`); }
  async getSessionStats() { return this.fetch<Record<string, unknown>>("/api/sessions/stats"); }
  async getSessionPolicy() { return this.fetch<Record<string, string>>("/api/sessions/policy"); }

  // -----------------------------------------------------------------------
  // Plugins
  // -----------------------------------------------------------------------

  async listPlugins() { return this.fetch<Record<string, unknown>>("/api/plugins"); }
  async enablePlugin(id: string) { return this.fetch<Record<string, string>>(`/api/plugins/${id}/enable`, { method: "POST" }); }
  async disablePlugin(id: string) { return this.fetch<Record<string, string>>(`/api/plugins/${id}/disable`, { method: "POST" }); }
  async removePlugin(id: string) { return this.fetch<Record<string, string>>(`/api/plugins/${id}`, { method: "DELETE" }); }

  // -----------------------------------------------------------------------
  // MCP
  // -----------------------------------------------------------------------

  async listMcpServers() { return this.fetch<Record<string, unknown>>("/api/mcp/servers"); }
  async enableMcpServer(name: string) { return this.fetch<Record<string, string>>(`/api/mcp/servers/${name}/enable`, { method: "POST" }); }
  async disableMcpServer(name: string) { return this.fetch<Record<string, string>>(`/api/mcp/servers/${name}/disable`, { method: "POST" }); }
  async removeMcpServer(name: string) { return this.fetch<Record<string, string>>(`/api/mcp/servers/${name}`, { method: "DELETE" }); }
  async addMcpServer(body: unknown) { return this.fetch<Record<string, string>>("/api/mcp/servers", { method: "POST", body: JSON.stringify(body) }); }
  async getMcpRegistry(page: number, query: string) { return this.fetch<Record<string, unknown>>(`/api/mcp/registry?page=${page}&q=${encodeURIComponent(query)}`); }

  // -----------------------------------------------------------------------
  // Scheduler
  // -----------------------------------------------------------------------

  async listSchedules() { return this.fetch<Record<string, unknown>[]>("/api/schedules"); }
  async createSchedule(body: unknown) { return this.fetch<Record<string, unknown>>("/api/schedules", { method: "POST", body: JSON.stringify(body) }); }
  async deleteSchedule(id: string) { return this.fetch<Record<string, string>>(`/api/schedules/${id}`, { method: "DELETE" }); }

  // -----------------------------------------------------------------------
  // Profile
  // -----------------------------------------------------------------------

  async getProfile() { return this.fetch<Record<string, unknown>>("/api/profile"); }
  async updateProfile(body: unknown) { return this.fetch<Record<string, string>>("/api/profile", { method: "PUT", body: JSON.stringify(body) }); }

  // -----------------------------------------------------------------------
  // Models
  // -----------------------------------------------------------------------

  async listModels() { return this.fetch<Record<string, unknown>>("/api/models"); }

  // -----------------------------------------------------------------------
  // Workspace / File browser
  // -----------------------------------------------------------------------

  async listWorkspaceDir(path: string) { return this.fetch<Record<string, unknown>>(`/api/sandbox/ls?path=${encodeURIComponent(path)}`); }
  async readWorkspaceFile(path: string) { return this.fetch<Record<string, unknown>>(`/api/sandbox/read?path=${encodeURIComponent(path)}`); }
}
