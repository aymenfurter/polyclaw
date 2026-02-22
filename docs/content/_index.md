---
title: "Polyclaw Documentation"
---

## Quick Links

| Resource | Description |
|---|---|
| [Getting Started](/getting-started/) | Installation, prerequisites, and first run |
| [Architecture](/architecture/) | System design, components, and data flow |
| [Configuration](/configuration/) | Environment variables and secrets management |
| [Security, Governance & RAI](/responsible-ai/) | Responsible AI principles, security controls, governance |
| [Runtime Isolation](/deployment/runtime-isolation/) | Separated admin and agent runtime architecture |
| [Plugins & MCP](/plugins/) | Model Context Protocol servers and plugin system |
| [Skills](/skills/) | Skill authoring, marketplace, and built-in skills |
| [API Reference](/api/) | REST endpoints and WebSocket protocol |
| [Deployment](/deployment/) | Docker, Azure, and CLI tools |

## What is polyclaw?

![polyclaw web chat interface](/screenshots/web-newchat-try-asking.png)

polyclaw is an autonomous AI copilot built on the **GitHub Copilot SDK**. It messages you on Telegram, uses skills to get things done, and can even call your phone for a real-time voice conversation. Through the Copilot SDK it can write and execute code to solve problems on the spot. It comes with its own web browser that skills can drive to navigate websites and perform tasks. Under the hood the agent is powered by a cron-based scheduler, a plugin ecosystem, a self-extending skill system, and MCP servers.

### Key Capabilities

- **Streaming AI responses** with tool execution and multi-model support
- **Multi-channel bot** with adaptive cards, media handling, and proactive messaging
- **Realtime voice** using Azure Communication Services and OpenAI Realtime API
- **Persistent workspace** -- files, databases, and scripts the agent creates survive across sessions, like a personal drive for your agent
- **Built-in web browser** that skills can use to navigate sites and automate web-based tasks
- **Code generation and execution** -- the agent writes and runs code to solve issues via sandboxed sessions
- **Model Context Protocol servers** for browser automation, Azure management, GitHub integration
- **Autonomous scheduling** with cron expressions and one-shot tasks
- **Memory system** with idle-triggered consolidation and daily briefings
- **Key Vault integration** for secure secret management
- **Cloudflare tunnel** for zero-config public endpoint exposure
- **Flexible deployment** -- from hybrid (agent on a Raspberry Pi with cloud components) to full cloud on Azure Container Apps; deploy wherever it fits your setup

### Hardening

- **Guardrails & HITL** -- defense-in-depth tool interception with allow/deny/HITL/PITL/AITL/filter strategies and per-model autonomy levels
- **Agent Identity** -- least-privilege managed identity with credential isolation between admin and runtime containers
- **Tool Activity** -- enterprise audit dashboard with automated scoring and Prompt Shield results
- **Runtime Isolation** -- separated admin and agent runtime architecture with independent credentials
- **Monitoring** -- OpenTelemetry integration with Azure Monitor (Application Insights, Log Analytics)

![Session management](/screenshots/web-sessionlist.png)
