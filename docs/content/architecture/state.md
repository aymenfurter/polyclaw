---
title: "State Management"
weight: 3
---

# State Management

Polyclaw uses a file-based state system with JSON stores. All state files live under `POLYCLAW_DATA_DIR` (default: `~/.polyclaw/`).

## State Modules

### Session Store

**File**: `sessions/<session_id>.json`

Each chat session is persisted as a separate JSON file containing message history, metadata, and timestamps.

| Feature | Description |
|---|---|
| One file per session | Easy inspection and backup |
| Archival policies | `24h`, `7d`, `30d`, `never` |
| Session resume | Last 20 messages loaded as context |
| Metadata | Model, title, message count, created/updated timestamps |

### Memory Store

**Directory**: `memory/`

The memory system consolidates chat interactions into long-term memories:

- **`daily/`** -- Daily log files summarizing interactions
- **`topics/`** -- Topic-specific notes extracted from conversations

Memory formation is triggered after `MEMORY_IDLE_MINUTES` (default: 5) of inactivity. The `MEMORY_MODEL` LLM generates structured summaries from buffered chat turns.

### Profile Store

**File**: `agent_profile.json`

Tracks the agent's identity and behavioral state:

| Field | Description |
|---|---|
| `name` | Agent display name |
| `emoji` | Visual identity |
| `location` | Timezone context |
| `emotional_state` | Current mood (affects responses) |
| `preferences` | Communication style preferences |

Related data is stored in separate files:

| File | Description |
|---|---|
| `skill_usage.json` | Usage counts per skill |
| `interactions.json` | Recent interaction log (last 1 000 entries) |

The `get_full_profile()` helper merges the profile with skill usage, per-day contribution counts, and activity statistics.

### MCP Config

**File**: `mcp_servers.json`

Stores MCP server definitions. Supports four server types:

| Type | Description |
|---|---|
| `local` | Spawned as a subprocess |
| `stdio` | Communicates via stdin/stdout |
| `http` | Remote HTTP endpoint |
| `sse` | Server-Sent Events endpoint |

### Proactive State

**File**: `proactive.json`

Manages autonomous proactive messaging:

| Field | Description |
|---|---|
| `enabled` | Whether proactive messaging is active |
| `pending` | Single pending message awaiting delivery |
| `history` | Last 100 delivered messages with reactions |
| `preferences` | Timing, frequency, and topic constraints |

The `messages_sent_today()` and `hours_since_last_sent()` methods compute daily counts and gap tracking from the history rather than persisting them as separate fields.

### Guardrails Config

**Files**: `guardrails.json`, `policy.yaml`

Stores human-in-the-loop (HITL) approval rules, tool-level and context-level policies, model-specific overrides, and Content Safety settings. A YAML policy file is generated alongside the JSON and consumed by the `PolicyEngine` at runtime.

### Monitoring Config

**File**: `monitoring.json`

Stores OpenTelemetry and Application Insights configuration including connection strings, sampling ratio, live metrics toggle, and provisioning metadata.

### Tool Activity Store

**File**: `tool_activity.jsonl`

Append-only JSON-lines log of every tool invocation. Each entry records tool name, arguments, result, duration, risk score, and Content Safety shield results. Supports query, timeline, CSV export, and session-level breakdowns for audit.

### Other State Files

| File | Purpose |
|---|---|
| `SOUL.md` | Agent personality definition |
| `scheduler.json` | Scheduled task definitions |
| `deployments.json` | Deployment records |
| `infra.json` | Infrastructure configuration (bot, channels, voice) |
| `plugins.json` | Plugin enabled/disabled state |
| `sandbox.json` | Sandbox configuration and session pool metadata |
| `foundry_iq.json` | Azure AI Foundry IQ / Search settings |
| `conversation_refs.json` | Bot Framework conversation references |

## Design Principles

- **No database required** -- everything is flat files for simplicity and portability
- **Human-readable** -- JSON, JSONL, and Markdown files can be inspected and edited manually
- **Docker-friendly** -- mount `~/.polyclaw` as a volume for persistence
- **Thread-safe I/O** -- shared stores use `threading.Lock` for concurrent access
