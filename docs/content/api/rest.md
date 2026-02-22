---
title: "REST API"
weight: 2
---

# REST API

All endpoints require `Authorization: Bearer <ADMIN_SECRET>` unless otherwise noted.

## Health

### `GET /health`

**Auth**: None

```json
{ "status": "ok", "version": "5.0.0", "mode": "admin" }
```

`mode` is `"admin"` or `"runtime"` depending on which process is running.

## Auth

### `POST /api/auth/check`

**Auth**: None (validates the provided token)

Returns 200 if the token is valid, 401 otherwise.

## Setup

### `GET /api/setup/status`

Returns setup completion state (identity configured, channels ready).

## Sessions

### `GET /api/sessions`

List all archived sessions.

### `GET /api/sessions/stats`

Aggregate statistics across all sessions (message counts, model usage).

### `GET /api/sessions/policy`

Get the session retention policy.

### `PUT /api/sessions/policy`

Update the session retention policy. Body: `{ max_sessions?, max_age_days? }`.

### `GET /api/sessions/:id`

Get a specific session with message history.

### `DELETE /api/sessions/:id`

Delete a session.

### `DELETE /api/sessions`

Delete all sessions.

## Skills

### `GET /api/skills`

List all available skills with source, description, and usage count.

### `GET /api/skills/installed`

List only explicitly installed (non-built-in) skills.

### `GET /api/skills/catalog`

List the built-in skill catalog.

### `GET /api/skills/marketplace`

List available skills from remote catalogs.

### `POST /api/skills/install`

Install a skill from marketplace. Body: `{ name }` or `{ url }`.

### `POST /api/skills/contribute`

Contribute a user-created skill.

### `DELETE /api/skills/:id`

Delete a user-created skill.

## Plugins

### `GET /api/plugins`

List all plugins.

### `GET /api/plugins/:id`

Get a specific plugin.

### `POST /api/plugins/:id/enable`

Enable a plugin.

### `POST /api/plugins/:id/disable`

Disable a plugin.

### `GET /api/plugins/:id/setup`

Get setup instructions for a plugin.

### `POST /api/plugins/:id/setup`

Complete plugin setup. Body: plugin-specific configuration.

### `POST /api/plugins/import`

Upload a plugin ZIP file. Multipart form data.

### `DELETE /api/plugins/:id`

Remove a plugin.

## MCP Servers

### `GET /api/mcp/servers`

List all MCP server configurations.

### `GET /api/mcp/servers/:id`

Get a specific MCP server.

### `POST /api/mcp/servers`

Add a new MCP server. Body: `{ name, type, command?, args?, env?, url?, tools?, description?, enabled }`.

### `PUT /api/mcp/servers/:id`

Update an MCP server. Body: any subset of the server fields (e.g. `{ enabled }`, `{ name, url }`).

### `POST /api/mcp/servers/:id/enable`

Enable an MCP server.

### `POST /api/mcp/servers/:id/disable`

Disable an MCP server.

### `DELETE /api/mcp/servers/:id`

Delete an MCP server.

### `GET /api/mcp/registry`

Search the public MCP server registry. Query params: `q` (search term), `page`.

## Schedules

### `GET /api/schedules`

List all scheduled tasks.

### `POST /api/schedules`

Create a task. Body: `{ description, cron?, run_at?, prompt }`.

### `PUT /api/schedules/:id`

Update a task. Body: `{ enabled?, cron?, prompt? }`.

### `DELETE /api/schedules/:id`

Delete a task.

## Profile

### `GET /api/profile`

Get the agent profile (name, emoji, stats, heatmap).

### `POST /api/profile`

Update profile fields. Body: partial profile object.

## Proactive

### `GET /api/proactive`

Get proactive messaging state (enabled, pending, preferences).

### `PUT /api/proactive/enabled`

Set proactive messaging on or off. Body: `{ enabled }`.

### `PUT /api/proactive/preferences`

Update proactive preferences. Body: `{ min_gap_hours?, max_daily?, avoided_topics?, preferred_times? }`.

### `DELETE /api/proactive/pending`

Cancel any pending scheduled proactive message.

### `POST /api/proactive/reaction`

Record a user reaction to a proactive message (used to tune future cadence). Body: `{ reaction }`.

### `POST /api/proactive/dry-run`

Simulate the next proactive message cycle without sending.

### `POST /api/proactive/memory/form`

Force immediate memory consolidation for the proactive context.

## Voice

### `POST /api/voice/call`

Initiate a voice call. Body: `{ number }`.

### `GET /api/voice/status`

Get current call status.

## Models

### `GET /api/models`

List available LLM models from the Copilot SDK.

## Sandbox

### `GET /api/sandbox/config`

Get sandbox configuration.

### `POST /api/sandbox/config`

Update sandbox config. Body: `{ enabled, session_pool_endpoint? }`.

### `POST /api/sandbox/test`

Run a connectivity test against the configured sandbox endpoint.

### `POST /api/sandbox/provision`

Provision a new Azure Container Apps session pool.

### `DELETE /api/sandbox/provision`

Decommission the session pool.

## Environments

### `GET /api/environments`

List all deployment environments.

### `GET /api/environments/:deploy_id`

Get details for a specific deployment environment.

### `DELETE /api/environments/:deploy_id`

Destroy a deployment environment (full teardown).

### `POST /api/environments/:deploy_id/cleanup`

Clean up resources for an environment without destroying the record.

### `DELETE /api/environments/:deploy_id/record`

Remove the local tracking record without destroying cloud resources.

### `GET /api/environments/audit`

Run an audit across all deployment environments.

### `POST /api/environments/audit/cleanup`

Clean up orphaned resources found by the audit.

### `POST /api/environments/misconfig`

Check for misconfigured resources across environments.

## Workspace

### `GET /api/workspace/list`

List workspace files.

### `GET /api/workspace/read`

Get file content.

## Network

### `GET /api/network/info`

Get network info (tunnel status, endpoints, connections).

### `GET /api/network/endpoints`

Get the resolved public endpoints for all configured services.

### `GET /api/network/probe`

Probe connectivity to external services and return latency/status per endpoint.

### `GET /api/network/resource-audit`

Audit Azure resource health and network connectivity.

## Bot Framework

### `POST /api/messages`

**Auth**: Bot Framework SDK validation (not Bearer token)

Bot Framework webhook endpoint. Receives activities from Azure Bot Service.

### `GET /api/messages`

Returns recently received Bot Framework activities (used for diagnostics).

## ACS Callbacks

### `POST /acs`

**Auth**: JWT validation

Azure Communication Services callback endpoint. Also available at `/api/voice/acs-callback`.

### `POST /acs/incoming`

**Auth**: JWT validation

ACS incoming call handler. Also available at `/api/voice/acs-callback/incoming`.

### `GET /realtime-acs`

**Auth**: JWT validation

WebSocket endpoint for ACS media streaming. Also available at `/api/voice/media-streaming`.

## Tool Activity

Enterprise audit trail for every tool invocation.

### `GET /api/tool-activity`

List all recorded tool activity entries. Supports query params for filtering.

### `GET /api/tool-activity/summary`

Aggregate summary: call counts, error rates, top tools.

### `GET /api/tool-activity/timeline`

Time-series data for tool invocations (for charts).

### `GET /api/tool-activity/sessions`

Breakdown of tool activity grouped by session.

### `GET /api/tool-activity/export`

Export all entries as CSV.

### `GET /api/tool-activity/:entry_id`

Get a specific activity entry.

### `POST /api/tool-activity/:entry_id/flag`

Manually flag an entry for review.

### `POST /api/tool-activity/:entry_id/unflag`

Remove a manual flag from an entry.

### `POST /api/tool-activity/import`

Import activity entries from a CSV file. Multipart form data.

## Guardrails

Per-tool and per-model policy management. See [Guardrails & HITL](/features/guardrails/) for concepts.

### `GET /api/guardrails/config`

Get the global guardrails configuration.

### `PUT /api/guardrails/config`

Update global guardrails configuration.

### `GET /api/guardrails/rules`

List all interception rules.

### `POST /api/guardrails/rules`

Add a new rule.

### `PUT /api/guardrails/rules/bulk`

Bulk-replace all rules.

### `PUT /api/guardrails/rules/:rule_id`

Update a single rule.

### `DELETE /api/guardrails/rules/:rule_id`

Delete a rule.

### `GET /api/guardrails/tools`

List all known tools (built-in + MCP).

### `GET /api/guardrails/inventory`

Full tool inventory with current policy per context.

### `PUT /api/guardrails/policies/:ctx/:tool_id`

Set the policy for a tool in a given context. Body: `{ policy }`.

### `GET /api/guardrails/contexts`

List available execution contexts (e.g. `interactive`, `background`).

### `GET /api/guardrails/presets`

List available policy presets.

### `POST /api/guardrails/presets/:preset_id`

Apply a preset to all tools.

### `POST /api/guardrails/set-all`

Set the same policy on every tool at once. Body: `{ policy }`.

### `POST /api/guardrails/model-columns`

Add a per-model policy column. Body: `{ model }`.

### `DELETE /api/guardrails/model-columns/:model`

Remove a per-model policy column.

### `PUT /api/guardrails/model-policies/:model/:ctx/:tool_id`

Set the policy for a tool/context combination for a specific model.

### `POST /api/guardrails/model-defaults`

Apply per-model default policies based on the model's trust tier.

### `GET /api/guardrails/model-tiers`

List models and their assigned trust tiers.

### `GET /api/guardrails/templates`

List guardrails configuration templates.

### `GET /api/guardrails/templates/:name`

Get a specific template.

### `GET /api/guardrails/background-agents`

List background agent identities and their associated model tiers.

### `GET /api/guardrails/policy-yaml`

Export the current policy as YAML.

### `PUT /api/guardrails/policy-yaml`

Import a YAML policy document. Body: `{ yaml }`.

### `GET /api/guardrails/preflight`

Get the current preflight security check status.

### `POST /api/guardrails/preflight/run`

Re-run all preflight security checks.

## Content Safety

### `GET /api/content-safety/status`

Get the content safety service status and configuration.

### `POST /api/content-safety/deploy`

Provision a Content Safety resource.

### `POST /api/content-safety/test`

Run a test prompt through the configured Content Safety endpoint.

## Identity

Managed identity and RBAC management for the agent runtime.

### `GET /api/identity/info`

Get current managed identity details.

### `GET /api/identity/roles`

List RBAC role assignments held by the runtime identity.

### `POST /api/identity/fix-roles`

Automatically assign any missing required roles.

## Monitoring

OpenTelemetry / Azure Monitor integration.

### `GET /api/monitoring/config`

Get the current monitoring configuration.

### `POST /api/monitoring/config`

Save monitoring configuration. Body: `{ connection_string?, enabled? }`.

### `GET /api/monitoring/status`

Get connection status for the configured monitoring backend.

### `POST /api/monitoring/test`

Send a test trace/event to verify connectivity.

### `POST /api/monitoring/provision`

Provision an Azure Monitor workspace and Application Insights resource.

### `DELETE /api/monitoring/provision`

Decommission the monitoring resources.

## Foundry IQ

Azure AI Search-backed long-term memory index.

### `GET /api/foundry-iq/config`

Get Foundry IQ configuration.

### `PUT /api/foundry-iq/config`

Save configuration. Body: `{ endpoint, index_name, embedding_deployment? }`.

### `GET /api/foundry-iq/stats`

Get index statistics (document count, size, last updated).

### `POST /api/foundry-iq/test-search`

Run a test search query. Body: `{ query }`.

### `POST /api/foundry-iq/test-embedding`

Test the embedding endpoint. Body: `{ text }`.

### `POST /api/foundry-iq/ensure-index`

Create the index if it does not exist.

### `POST /api/foundry-iq/index`

Re-index all memory content.

### `DELETE /api/foundry-iq/index`

Delete the search index.

### `POST /api/foundry-iq/search`

Query the index directly. Body: `{ query, top? }`.

### `POST /api/foundry-iq/provision`

Provision an Azure AI Search resource.

### `DELETE /api/foundry-iq/provision`

Decommission the Azure AI Search resource.
