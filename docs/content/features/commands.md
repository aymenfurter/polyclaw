---
title: "Slash Commands"
weight: 7
---

# Slash Commands

![Slash commands in the TUI](/screenshots/tui-slashcommands.png)

Polyclaw supports an extensive set of slash commands, shared between Bot Framework channels and the WebSocket chat interface.

## Session Commands

| Command | Description |
|---|---|
| `/new` | Start a new conversation session |
| `/session` | Show current session info |
| `/sessions` | List all archived sessions |
| `/sessions clear` | Clear all sessions |
| `/session delete <id>` | Delete a specific session |
| `/clear` | Clear all memory files |

## Skill Commands

| Command | Description |
|---|---|
| `/skills` | List all available skills |
| `/addskill <name>` | Install a skill from the catalog |
| `/removeskill <name>` | Uninstall an installed skill |

## Plugin Commands

| Command | Description |
|---|---|
| `/plugins` | List all plugins |
| `/plugin enable <id>` | Enable a plugin |
| `/plugin disable <id>` | Disable a plugin |

## MCP Commands

| Command | Description |
|---|---|
| `/mcp` | List all MCP servers |
| `/mcp enable <name>` | Enable an MCP server |
| `/mcp disable <name>` | Disable an MCP server |
| `/mcp add <name> <url>` | Add an MCP server |
| `/mcp remove <name>` | Remove an MCP server |

## Schedule Commands

| Command | Description |
|---|---|
| `/schedules` | List scheduled tasks |
| `/schedule add <min> <hour> <dom> <month> <dow> <prompt>` | Add a scheduled task |
| `/schedule remove <id>` | Remove a task |

## Model Commands

| Command | Description |
|---|---|
| `/models` | List available models |
| `/model <name>` | Switch to a different model |

## Profile Commands

| Command | Description |
|---|---|
| `/profile` | Show agent profile |
| `/config` | Show current configuration |
| `/config <KEY> <VALUE>` | Update a configuration value |

## Communication Commands

| Command | Description |
|---|---|
| `/channels` | List connected channels |
| `/call` | Initiate a voice call to configured target |
| `/phone <number>` | Set default phone number |

## System Commands

| Command | Description |
|---|---|
| `/status` | Show system status |
| `/help` | Show command help |
| `/preflight` | Run preflight checks |
| `/change` | Browse recent sessions |
| `/lockdown on/off` | Enable/disable lockdown mode |

## Command Processing

Commands are processed by `CommandDispatcher` in `app/runtime/messaging/commands.py`. The dispatcher:

1. Checks for exact command matches first
2. Falls back to prefix matching for parameterized commands
3. Returns a structured response or delegates to the agent
4. Works identically across Bot Framework and WebSocket channels
