---
title: "Server & Middleware"
weight: 2
---

# Server & Middleware

The Polyclaw server is an aiohttp web application that hosts the admin dashboard, chat WebSocket, Bot Framework endpoint, and voice routes.

## Application Setup

Defined in `app/runtime/server/app.py`. The server creates an `aiohttp.web.Application` with middleware, routes, background tasks, and lifecycle hooks.

### Server Modes

The server runs in one of three modes controlled by `POLYCLAW_SERVER_MODE` (or CLI flags):

| Mode | CLI flag | Description |
|---|---|---|
| `combined` | *(default)* | Both admin and agent runtime in a single process |
| `admin` | `--admin-only` | Admin control-plane only (setup wizard, config UI, no agent) |
| `runtime` | `--runtime-only` | Agent runtime data-plane only (agent, bot, voice, no setup UI) |

In the separated architecture the admin container starts first to generate `ADMIN_SECRET` and write it to the shared `.env`. The runtime container reads that secret on startup. If the secret is missing the runtime logs a warning and waits for the admin container to complete setup.

### Entry Points

| Command | Port | Description |
|---|---|---|
| `polyclaw-admin` | 9090 (configurable) | Admin server (mode determined by flags) |
| `polyclaw-bot` | 3978 (configurable) | Bot endpoint only |

## Middleware Stack

Middleware is applied in order to every request:

### 1. Lockdown Middleware

When `LOCKDOWN_MODE=true`, all API requests are rejected with `403`. The intent is to allow operators to disable the agent from the web UI and later re-enable it via a bot service channel (e.g. `/lockdown off` in Teams or Telegram). Bot and voice endpoints remain open during lockdown. This feature is experimental and not yet fully implemented.

### 2. Tunnel Restriction Middleware

When `TUNNEL_RESTRICTED=true`, requests arriving through the Cloudflare tunnel are restricted to bot and voice endpoints only. This prevents public access to the admin API while keeping bot callbacks functional. Lockdown mode enables tunnel restriction automatically.

### 3. Auth Middleware

Bearer token validation on all `/api/*` routes. Compares the `Authorization: Bearer <token>` header against `ADMIN_SECRET`. Also accepts `?token=` or `?secret=` query parameters as alternatives.

Non-API paths -- the SPA frontend (`/`, `/assets/*`, `favicon.ico`, etc.), `/media/*` files, and `/health` -- are served **without** authentication. The frontend enforces its own login gate in JavaScript: the user must enter `ADMIN_SECRET` before the React app renders any dashboard content. This means the HTML, CSS, and JS bundles are publicly accessible, but they contain no secrets or data. All sensitive operations go through `/api/*` endpoints that require the Bearer token.

### 4. Runtime Proxy Middleware (admin-only mode)

In admin-only mode, a proxy middleware forwards unmatched `/api/*` requests to the agent runtime container. This allows the admin UI to transparently communicate with the runtime without the frontend needing to know the runtime URL.

**Public API paths** (exempt from Bearer auth):

| Path | Own security |
|---|---|
| `/health` | None (read-only health check) |
| `/api/messages` | Validated by the Bot Framework SDK (app ID + password) |
| `/api/voice/acs-callback`, `/acs` | Query-param callback token + RS256 JWT against Microsoft JWKS |
| `/api/voice/media-streaming`, `/realtime-acs` | Query-param callback token + RS256 JWT against Microsoft JWKS |
| `/api/auth/check` | Intentionally open. Returns `{"authenticated": true/false}` without exposing secrets. |

All other `/api/voice/*` routes (e.g. `/api/voice/call`, `/api/voice/status`) **do** require Bearer auth like any normal API endpoint.

## Route Groups

Routes are split between admin-only and runtime-only handlers. In `combined` mode both sets are registered.

### Admin-Only Routes

| Handler | Prefix | Purpose |
|---|---|---|
| `SetupRoutes` | `/api/setup/` | Setup wizard, lockdown toggle |
| `VoiceSetupRoutes` | `/api/voice/setup/` | Voice configuration |
| `WorkspaceHandler` | `/api/workspace/` | Workspace files |
| `EnvironmentRoutes` | `/api/environments/` | Deployment environments |
| `SandboxRoutes` | `/api/sandbox/` | Sandbox configuration |
| `FoundryIQRoutes` | `/api/foundry-iq/` | Azure AI Foundry IQ |
| `NetworkRoutes` | `/api/network/` | Network topology |
| `MonitoringRoutes` | `/api/monitoring/` | Application Insights / OTel |
| `ContentSafetyRoutes` | `/api/content-safety/` | Azure Content Safety configuration |
| `IdentityRoutes` | `/api/identity/` | Runtime managed identity |
| `SecurityPreflightRoutes` | `/api/security-preflight/` | Pre-deployment security checks |

### Runtime-Only Routes

| Handler | Prefix | Purpose |
|---|---|---|
| `ChatHandler` | `/api/chat/` | WebSocket chat, suggestions |
| `BotEndpoint` | `/api/messages` | Bot Framework webhook |
| `VoiceRoutes` | `/api/voice/` | Voice call management |
| `SessionRoutes` | `/api/sessions/` | Session CRUD |
| `SkillRoutes` | `/api/skills/` | Skill management |
| `McpRoutes` | `/api/mcp/` | MCP server configuration |
| `PluginRoutes` | `/api/plugins/` | Plugin management |
| `SchedulerRoutes` | `/api/schedules/` | Task scheduling |
| `ProfileRoutes` | `/api/profile/` | Agent profile |
| `GuardrailsRoutes` | `/api/guardrails/` | Guardrails configuration |
| `ToolActivityRoutes` | `/api/tool-activity/` | Tool activity log |
| `ProactiveRoutes` | `/api/proactive/` | Proactive messaging |

### Shared Routes

Registered in all modes:

- `GET /health` -- Health check
- `GET /api/media/{filename}` -- Serve media files
- `POST /api/auth/check` -- Auth verification

### Static Assets

- `/media/*` -- Serves files from the media directory
- `/*` -- SPA catch-all serving the frontend `index.html` (admin and combined modes only)

## Lifecycle Hooks

Startup and cleanup hooks are split by mode:

### on_startup (runtime)

1. Configure OpenTelemetry if monitoring is set up
2. Rebuild the Bot Framework adapter
3. Start background tasks: scheduler loop, proactive delivery loop, Foundry IQ index loop
4. If lockdown mode is active, skip all infrastructure provisioning and return
5. Provision infrastructure: start Cloudflare tunnel, deploy Azure Bot (if configured)

### on_startup (admin)

1. Start deployment reconciliation task

### on_cleanup

1. Cancel background tasks (scheduler, proactive, Foundry IQ, reconcile)
2. In combined mode: decommission infrastructure (unless lockdown is active)
3. Stop the agent and close all sessions

## Health Check

`GET /health` returns:

```json
{
  "status": "ok",
  "version": "5.0.0",
  "mode": "combined"
}
```

In `runtime` or `combined` mode, the response also includes `tunnel_url` with the current Cloudflare tunnel URL (empty string if no tunnel is running).
