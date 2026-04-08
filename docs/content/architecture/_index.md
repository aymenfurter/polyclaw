---
title: "Architecture"
weight: 20
---

<div class="arch-diagram">
  <img src="/screenshots/architecture.png" alt="Polyclaw architecture overview" />
</div>

Polyclaw is a monorepo containing a Python backend, a React frontend, and a Node.js terminal UI. The system is designed as a set of loosely coupled layers that communicate through well-defined interfaces.

## System Layers

| Layer | Technology | Role |
|---|---|---|
| **Agent Core** | Python, Copilot SDK, Foundry BYOK | LLM sessions, tool execution, streaming |
| **Infrastructure** | Bicep, Azure CLI | AI Services, Key Vault, Content Safety provisioning |
| **Web Admin** | React 19, Vite, TypeScript | SPA dashboard with WebSocket chat |
| **Bot Endpoint** | aiohttp, Bot Framework SDK | Teams, Telegram, and other channels |
| **Voice** | Azure Communication Services, OpenAI Realtime | Phone call routing and real-time speech |
| **Tunnel** | Cloudflare quick-tunnel | Public endpoint exposure |
| **Container** | Docker multi-stage | Production deployment |

## Request Flow

### Web Chat

The chat interface lets the user pick a model and a skill mode before sending a message.

![Model and skill picker in the chat UI](/screenshots/web-chat-modelpicker.png)

1. Browser opens a WebSocket to `/api/chat/ws`
2. User sends a message (or slash command)
3. `ChatHandler` routes to `CommandDispatcher` or the Agent
4. Agent creates a Copilot SDK session, streams response deltas
5. Deltas are forwarded over WebSocket in real-time
6. Tool calls execute and their results are sent as structured messages
7. Session is recorded to the session store

### Bot Framework

1. Azure Bot Service delivers an activity to `POST /api/messages`
2. `BotEndpoint` validates the request and dispatches to `PolyclawBot`
3. The bot sends an immediate typing indicator (to avoid the 15-second timeout)
4. Processing runs in a background task via `MessageProcessor`
5. Agent generates a response, which is delivered via proactive messaging
6. Rich cards and media attachments are sent through the `CardQueue`

### Voice Call

1. `POST /api/voice/call` initiates an outbound call via ACS
2. ACS callback events arrive at `POST /acs`
3. On connect, media streaming starts via WebSocket at `/realtime-acs`
4. `RealtimeMiddleTier` bridges ACS audio to Azure OpenAI Realtime API
5. Speech-to-text and text-to-speech happen in real-time

## Data Flow

All persistent state is stored as JSON files under `~/.polyclaw/` (configurable via `POLYCLAW_DATA_DIR`):

```
~/.polyclaw/
  SOUL.md                # Agent personality
  profile.json           # Agent profile and stats
  mcp_servers.json       # MCP server configuration
  scheduler.json         # Scheduled tasks
  proactive.json         # Proactive messaging state
  sessions/              # Chat session archives
  media/                 # Incoming/outgoing files
    incoming/
    outgoing/
    pending/
    sent/
    error/
  memory/                # Memory consolidation
    daily/
    topics/
  skills/                # User and plugin skills
  plugins/               # User-uploaded plugins
```

## Component Diagram

The system is organized into these runtime modules:

- **`agent/`** -- Copilot SDK wrapper, BYOK provider, tools, prompt builder
- **`config/`** -- Settings singleton, environment loading
- **`media/`** -- MIME classification, attachment handling
- **`messaging/`** -- Bot, cards, commands, proactive delivery
- **`realtime/`** -- Voice routes, ACS middleware, auth
- **`registries/`** -- Plugin and skill registries
- **`server/`** -- aiohttp app, routes, middleware, chat handler
- **`services/`** -- Tunnel, deployer, Bicep deployer, Key Vault, Azure CLI wrapper
- **`state/`** -- Session, memory, profile, MCP config, proactive state

Dive deeper:

- [Agent Core](/architecture/agent-core/)
- [Server & Middleware](/architecture/server/)
- [State Management](/architecture/state/)
- [Services Layer](/architecture/services/)
