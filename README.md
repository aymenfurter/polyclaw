<p align="center">
  <img src="assets/logo.png" alt="Octoclaw" width="120" />
</p>

<h1 align="center">Octoclaw</h1>

<p align="center">
  <strong>Your personal AI copilot that lives where you do -- browser, terminal, messaging apps, or a phone call.</strong>
</p>

<p align="center">
  <a href="https://github.com/aymenfurter/octoclaw/actions/workflows/ci.yml"><img src="https://github.com/aymenfurter/octoclaw/actions/workflows/ci.yml/badge.svg" alt="CI" /></a>
  <a href="https://www.python.org/downloads/"><img src="https://img.shields.io/badge/python-3.11+-3776AB.svg?logo=python&logoColor=white" alt="Python 3.11+" /></a>
  <a href="https://nodejs.org/"><img src="https://img.shields.io/badge/node.js-20+-339933.svg?logo=nodedotjs&logoColor=white" alt="Node.js 20+" /></a>
  <a href="https://github.com/features/copilot"><img src="https://img.shields.io/badge/GitHub%20Copilot%20SDK-8957e5.svg?logo=github&logoColor=white" alt="GitHub Copilot SDK" /></a>
  <a href="Dockerfile"><img src="https://img.shields.io/badge/docker-ready-2496ED.svg?logo=docker&logoColor=white" alt="Docker" /></a>
  <a href="https://aymenfurter.github.io/octoclaw/"><img src="https://img.shields.io/badge/docs-online-blue.svg?logo=readthedocs&logoColor=white" alt="Documentation" /></a>
</p>

---

> **Warning:** Octoclaw is an autonomous agent that runs as you. It authenticates with your GitHub token, Azure credentials, and API keys. It can execute code, deploy infrastructure, send messages to real people, and make phone calls -- all under your identity. Understand the [risks](#risks) before running it.

Octoclaw is an autonomous AI copilot built on the **GitHub Copilot SDK**. It gives you the full power of GitHub Copilot -- untethered from the IDE. It writes code, interacts with your repos via the GitHub CLI, authors its own skills at runtime, reaches out to you proactively when something matters, schedules tasks for the future, and can even call you on the phone for urgent matters.

## Why Octoclaw?

**Self-extending.** Ask it to learn something new and it writes, saves, and immediately starts using the skill -- no redeployment needed.

**Proactive.** When something important happens -- a scheduled check fails, a reminder fires, or a condition you defined is met -- it messages you on whatever channel you have connected.

**Scheduled.** Cron jobs and one-shot tasks let Octoclaw plan ahead. Daily briefings, recurring web scrapes, future reminders -- all handled autonomously.

**Voice calls.** For truly urgent matters, it calls you on the phone via Azure Communication Services and OpenAI Realtime for a live conversation with your agent.

**Extensible.** Add MCP servers, drop in plugin packs, or write skill files in Markdown. Everything is configurable from the dashboard.

**Persistent workspace.** Its own home directory survives across sessions -- files, databases, scripts, and a built-in Playwright browser for autonomous web navigation.

## Architecture

<p align="center">
  <img src="docs/static/screenshots/architecture.png" alt="Architecture" width="700" />
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
git clone https://github.com/aymenfurter/octoclaw.git
cd octoclaw
./scripts/run-tui.sh
```

The TUI walks you through setup, configuration, and deployment. Run locally or deploy to Azure Container Apps.

For full setup instructions, configuration reference, and feature guides, see the **[Documentation](https://aymenfurter.github.io/octoclaw/)**.

## Prerequisites

- Docker
- A GitHub account with a Copilot subscription
- An Azure subscription (needed for voice, bot channels, and Foundry integration)
- [Azure CLI](https://learn.microsoft.com/en-us/cli/azure/install-azure-cli) (if deploying to Azure)

## Risks

> **Warning:** Octoclaw is an independent, community-built tool -- not an official GitHub product. It operates under your identity with a high degree of autonomy, meaning it can take actions on your behalf (GitHub, Azure, phone calls, code execution). Misconfiguration or unattended use can have real consequences. This project is intended for developers and technical users who understand the risks of running an autonomous agent with access to their accounts and infrastructure.

Octoclaw runs as **you**. It authenticates with your GitHub token, your Azure credentials, your API keys. When it pushes code, opens a PR, or deploys infrastructure -- that's your identity on every commit and every API call. There is no sandbox between the agent and your accounts unless you explicitly set one up.

It can execute arbitrary code on its host. It has a browser. It can make outbound network requests, write files, and call external services. If you give it access to Azure, it can provision real resources that cost real money. If you connect it to a messaging channel, it can send messages to real people. If you give it a phone number, it can make real phone calls.

The Copilot SDK usage consumes your GitHub Copilot allowance. Scheduled tasks and proactive loops can burn through it fast. The admin UI exposes agent controls over HTTP -- if you don't set a strong `ADMIN_SECRET` and enable `LOCKDOWN_MODE`, anyone who finds your endpoint owns your agent.

This project uses the [GitHub Copilot SDK](https://github.com/features/copilot), subject to the [GitHub Terms of Service](https://docs.github.com/en/site-policy/github-terms/github-terms-of-service), [Copilot Product Specific Terms](https://docs.github.com/en/site-policy/github-terms/github-copilot-product-specific-terms), and [Pre-release License Terms](https://docs.github.com/en/site-policy/github-terms/github-pre-release-license-terms). Not endorsed by or affiliated with GitHub, Inc. or Microsoft Corporation.

## License

[MIT](LICENSE)
