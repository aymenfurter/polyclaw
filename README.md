<p align="center">  
  <img src="assets/logo.png" alt="Polyclaw" width="120" />
</p>

<h1 align="center">Polyclaw</h1>

<p align="center">
  <strong>Your personal AI copilot that lives where you do -- browser, terminal, messaging apps, or a phone call.</strong>
</p>

<p align="center">
  <a href="https://github.com/aymenfurter/polyclaw/actions/workflows/ci.yml"><img src="https://github.com/aymenfurter/polyclaw/actions/workflows/ci.yml/badge.svg" alt="CI" /></a>
  <a href="https://www.python.org/downloads/"><img src="https://img.shields.io/badge/python-3.11+-3776AB.svg?logo=python&logoColor=white" alt="Python 3.11+" /></a>
  <a href="https://nodejs.org/"><img src="https://img.shields.io/badge/node.js-20+-339933.svg?logo=nodedotjs&logoColor=white" alt="Node.js 20+" /></a>
  <a href="https://github.com/features/copilot"><img src="https://img.shields.io/badge/GitHub%20Copilot%20SDK-8957e5.svg?logo=github&logoColor=white" alt="GitHub Copilot SDK" /></a>
  <a href="Dockerfile"><img src="https://img.shields.io/badge/docker-ready-2496ED.svg?logo=docker&logoColor=white" alt="Docker" /></a>
  <a href="https://aymenfurter.github.io/polyclaw/"><img src="https://img.shields.io/badge/docs-online-blue.svg?logo=readthedocs&logoColor=white" alt="Documentation" /></a>
</p>

---

> **Warning:** Polyclaw is an autonomous agent. It can execute code, deploy infrastructure, send messages to real people, and make phone calls. The agent runtime is architecturally separated from the admin plane and operates under its **own Azure managed identity** with least-privilege RBAC -- it does **not** share your personal Azure credentials. GitHub authentication is still a prerequisite (the Copilot SDK is the agent's reasoning engine). Understand the [risks](https://aymenfurter.github.io/polyclaw/responsible-ai/) before running it.

Polyclaw is an autonomous AI copilot built on the **GitHub Copilot SDK**. It gives you the full power of GitHub Copilot -- untethered from the IDE. It writes code, interacts with your repos via the GitHub CLI, authors its own skills at runtime, reaches out to you proactively when something matters, schedules tasks for the future, and can even call you on the phone for urgent matters.

## Why Polyclaw?

**Self-extending.** Ask it to learn something new and it writes, saves, and immediately starts using the skill -- no redeployment needed.

**Proactive.** When something important happens -- a scheduled check fails, a reminder fires, or a condition you defined is met -- it messages you on whatever channel you have connected.

**Scheduled.** Cron jobs and one-shot tasks let Polyclaw plan ahead. Daily briefings, recurring web scrapes, future reminders -- all handled autonomously.

**Voice calls.** For truly urgent matters, it calls you on the phone via Azure Communication Services and OpenAI Realtime for a live conversation with your agent.

**Extensible.** Add MCP servers, drop in plugin packs, or write skill files in Markdown. Everything is configurable from the dashboard. Ships with built-in plugins for **Microsoft Work IQ** (daily rollover, end-of-day reviews, weekly and monthly retrospectives powered by Microsoft 365 productivity data) and **Microsoft Foundry Agents** (provision Foundry resources, deploy models, and spin up ad-hoc agents with code interpreter and data analysis via the Foundry v2 Responses API).

**Guardrails & HITL.** A defense-in-depth framework intercepts every tool invocation and applies a configurable mitigation strategy -- allow, deny, human-in-the-loop (chat or phone call), AI-in-the-loop (a second model reviews the action), or content filtering via Azure AI Prompt Shields. Preset policies (permissive, balanced, restrictive) and per-tool rules give you fine-grained control over what the agent can do.

**Agent Identity.** The agent runtime runs under its own Azure managed identity (or service principal in Docker) with least-privilege RBAC. It never shares your personal CLI session. The admin plane and agent runtime are separate containers with independent credential scopes, enforcing strict isolation between configuration management and agent execution.

**Tool Activity.** An enterprise audit dashboard logs every tool invocation with automated risk scoring, Prompt Shield results, session breakdowns, manual flagging, and CSV export. Risk scoring runs automatically on every tool call as an observability layer.

**Monitoring.** One-click provisioning of Application Insights and Log Analytics. OpenTelemetry traces, metrics, and logs flow from the runtime to Azure Monitor with configurable sampling and optional live metrics.

**Memory system.** Conversations are automatically consolidated into long-term memory after idle periods. Daily topic notes and memory logs build a persistent knowledge base across sessions. Enable **Foundry IQ** as an optional retrieval layer to index memories into Azure AI Search for richer, semantically grounded recall.

**Persistent workspace.** Its own home directory survives across sessions -- files, databases, scripts, and a built-in Playwright browser for autonomous web navigation.

## Architecture

<p align="center">
  <img src="docs/static/screenshots/architecture.png" alt="Architecture" width="700" />
</p>

## Intro
<p align="center">
  
https://github.com/user-attachments/assets/c218bd9d-b313-40d7-8e9f-6081a62b3de2

</p>

## Web Dashboard

<p align="center">
  <img src="assets/screenshot-webui.png" alt="Web dashboard" width="700" />
</p>

## Terminal UI

<p align="center">
  <img src="assets/screenshot-tui.png" alt="Terminal UI" width="700" />
</p>

## Messaging

<p align="center">
  <img src="assets/screenshot-telegram.png" alt="Telegram messaging" width="300" />
</p>

## Getting Started

```bash
git clone https://github.com/aymenfurter/polyclaw.git
cd polyclaw
./scripts/run-tui.sh
```

The TUI walks you through setup, configuration, and deployment. Run locally or deploy to Azure Container Apps (experimental).

For full setup instructions, configuration reference, and feature guides, see the **[Documentation](https://aymenfurter.github.io/polyclaw/)**.

## Prerequisites

- Docker
- A GitHub account with a Copilot subscription
- An Azure subscription (needed for voice, bot channels, and Foundry integration)
- [Azure CLI](https://learn.microsoft.com/en-us/cli/azure/install-azure-cli) (if deploying to Azure)

## Security, Governance & Responsible AI

Polyclaw is in **early preview**. Treat it as experimental software and read this section carefully.

### Understand the Risks

Polyclaw is an autonomous agent. The agent runtime is architecturally separated from the admin plane and operates under its **own Azure managed identity** with least-privilege RBAC -- it does **not** share your personal Azure credentials. However, it can still execute code, deploy infrastructure, send messages, and make phone calls within the scope of its assigned roles. GitHub authentication remains a prerequisite for using the Copilot SDK.

**What can go wrong:** unintended actions from misunderstood instructions, credential exposure via prompt injection or badly written skills, cost overruns from runaway loops provisioning Azure resources, arbitrary code execution without human review, and data leakage through conversations and tool outputs passing through configured channels.

### What We Have Built So Far

None of these controls have been formally audited. They represent a best-effort starting point.

| Layer | Mechanism |
|---|---|
| Admin API | Bearer token (`ADMIN_SECRET`) on all `/api/*` routes |
| Bot channels | JWT validation via `botbuilder-core` SDK |
| Voice callbacks | RS256 JWT validation; query-param callback token as secondary check |
| Telegram | User ID whitelist (`TELEGRAM_WHITELIST`) |
| Tunnel | `TUNNEL_RESTRICTED` limits exposure to bot/voice endpoints only |
| Secrets | Azure Key Vault via `@kv:` prefix; `ADMIN_SECRET` auto-generated if not set |
| Isolation | [Sandbox execution](https://aymenfurter.github.io/polyclaw/features/sandbox/) redirects code to isolated sessions without host access |
| Lockdown | `LOCKDOWN_MODE` rejects all admin API requests immediately |
| Transparency | Tool calls visible in chat UI, human-readable `SOUL.md`, version-controlled prompt templates, full session archives |
| Preflight | [Setup Wizard](https://aymenfurter.github.io/polyclaw/getting-started/setup-wizard/) validates JWT, tunnel, endpoints, and channel security before deployment |
| [Guardrails](https://aymenfurter.github.io/polyclaw/features/guardrails/) | Defense-in-depth tool interception with configurable mitigation strategies (allow/deny/HITL/PITL/AITL/filter) |
| [Content Safety](https://aymenfurter.github.io/polyclaw/features/guardrails/) | Azure AI Prompt Shields detect and block prompt injection attacks before tool execution |
| [Agent Identity](https://aymenfurter.github.io/polyclaw/features/agent-identity/) | Least-privilege managed identity for the agent runtime with RBAC scoping and credential isolation |
| [Tool Activity](https://aymenfurter.github.io/polyclaw/features/tool-activity/) | Append-only audit log of every tool invocation with automated scoring and manual flagging |
| [Monitoring](https://aymenfurter.github.io/polyclaw/features/monitoring/) | OpenTelemetry integration with Azure Monitor for traces, metrics, and logs |
| Runtime separation | Admin and agent runtime containers with separate HOME directories, credential isolation, and route separation |

### What Is Missing

- **Multi-runtime management (1:N).** The admin plane currently manages a single agent runtime. The goal is to support managing multiple agent runtimes from a single admin plane -- deploying, monitoring, and configuring N independent agent runtimes from one control surface.
- **Multi-tenant isolation.** Designed for single-operator use only.

### Recommendations

1. Deploy with separated admin and agent runtime containers to enforce credential isolation.
2. Set a strong `ADMIN_SECRET` and store it in a key vault.
3. Enable `TUNNEL_RESTRICTED` and `TELEGRAM_WHITELIST`.
4. Enable sandbox execution for code-running workloads.
5. Run the security preflight checker to verify identity, RBAC, and secret isolation.
6. Enable guardrails with at least the balanced preset. Use HITL for high-risk tools.
7. Monitor tool activity and logs. Do not leave the agent running unattended for extended periods.
8. Review `SOUL.md` and system prompt templates to make sure agent instructions match your expectations.

For the full assessment, see the [Security, Governance & Responsible AI](https://aymenfurter.github.io/polyclaw/responsible-ai/) documentation.

This project uses the [GitHub Copilot SDK](https://github.com/features/copilot), subject to the [GitHub Terms of Service](https://docs.github.com/en/site-policy/github-terms/github-terms-of-service), [Copilot Product Specific Terms](https://docs.github.com/en/site-policy/github-terms/github-copilot-product-specific-terms), and [Pre-release License Terms](https://docs.github.com/en/site-policy/github-terms/github-pre-release-license-terms). Not endorsed by or affiliated with GitHub, Inc.

## License

[MIT](LICENSE)
