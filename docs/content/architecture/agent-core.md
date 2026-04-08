---
title: "Agent Core"
weight: 1
---

# Agent Core

The agent core is the heart of Polyclaw. It wraps the GitHub Copilot SDK to provide streaming AI conversations with tool execution. When `FOUNDRY_ENDPOINT` is configured, the agent operates in BYOK (Bring Your Own Key) mode, routing LLM inference through your Azure AI Services resource with Entra ID bearer tokens.

## CopilotAgent

Located in `app/runtime/agent/agent.py`, the `CopilotAgent` class manages the lifecycle of the Copilot SDK client.

### Lifecycle

```python
agent = CopilotAgent()
await agent.start()           # Initialize Copilot client
session = agent.new_session()  # Create a conversation session
response = await agent.send(session, "Hello")  # Send a message
await agent.stop()             # Clean shutdown
```

### Session Configuration

Each session is configured with:

| Parameter | Description |
|---|---|
| `model` | LLM model identifier (default: `gpt-4.1`) |
| `streaming` | Enable token-by-token streaming |
| `tools` | List of callable tool definitions |
| `system_message` | System prompt assembled by the prompt builder |
| `mcp_servers` | MCP server configurations to attach |
| `skill_dirs` | Skill directories to load |
| `provider` | BYOK provider config (when `FOUNDRY_ENDPOINT` is set) |

### Timeouts and Retries

- **Session timeout**: 60 seconds
- **Response timeout**: 120 seconds
- **Start retries**: up to 3 attempts with backoff

### Sandbox Mode

When sandbox mode is active, a `SandboxToolInterceptor` hooks into tool execution:

- **Pre-hook**: Intercepts shell/exec tool calls and redirects them to an Azure Container Apps dynamic session
- **Post-hook**: Syncs results back from the sandbox environment

## Tools

Defined in `app/runtime/agent/tools.py`, custom tools extend the agent's capabilities beyond the LLM:

| Tool | Description |
|---|---|
| `schedule_task` | Create a cron or one-shot scheduled task |
| `cancel_task` | Remove a scheduled task by ID |
| `list_scheduled_tasks` | Enumerate all scheduled tasks |
| `make_voice_call` | Initiate an outbound phone call via ACS |
| `search_memories_tool` | Search consolidated memories (when Foundry IQ is enabled) |
| `send_adaptive_card` | Send a rich adaptive card to the channel |
| `send_hero_card` | Send a hero card with image and buttons |
| `send_thumbnail_card` | Send a thumbnail card |
| `send_card_carousel` | Send a carousel of multiple cards |

Tools are defined using the `@define_tool` decorator from the Copilot SDK.

## Prompt Builder

Located in `app/runtime/agent/prompt.py`, `build_system_prompt()` assembles the system message from multiple sources:

1. **`system_prompt.md`** -- Core behavioral instructions
2. **`SOUL.md`** -- Agent personality and communication style
3. **Agent profile** -- Name, emoji, location, emotional state, preferences
4. **`bootstrap_prompt.md`** -- Shown only during initial setup
5. **`sandbox_prompt.md`** -- Appended when sandbox mode is active
6. **`mcp_guidance.md`** -- Instructions for interacting with MCP servers
7. **MCP server descriptions** -- Dynamically generated from active MCP configs

### Template System

Prompt templates are Markdown files stored in `app/runtime/templates/`. They support variable interpolation for agent profile fields and dynamic MCP server listings.

## BYOK Mode

When `FOUNDRY_ENDPOINT` is set in the configuration, the agent activates BYOK mode. Located in `app/runtime/agent/byok.py`, this module:

1. Acquires an Entra ID bearer token via `az account get-access-token --resource https://cognitiveservices.azure.com`
2. Configures the Copilot SDK session with a custom provider (`type: azure`, `base_url: <endpoint>`, `bearer_token: <token>`)
3. Overrides the session model and provider for every conversation

The runtime service principal (or managed identity) must have the `Cognitive Services OpenAI User` role on the AI Services resource. The fix-roles endpoint (`POST /api/identity/fix-roles`) assigns this role automatically if missing.

## Multi-Model Support

Polyclaw supports any model deployed on the configured Azure AI Services resource (BYOK mode) or available through the Copilot SDK. The default model is configured via `COPILOT_MODEL` (default: `gpt-4.1`). Users can switch models at runtime:

- Via slash command: `/model <model-name>`
- Via the web dashboard model selector
- Via API: `GET /api/models` lists available models

The memory system uses a separate model (`MEMORY_MODEL`, default: `gpt-4.1`) for consolidation tasks.
