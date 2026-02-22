---
title: "Setup Wizard"
weight: 3
---

# Setup Wizard

Once polyclaw finishes building and passes its health check, the TUI automatically opens the Setup screen in your default browser. From here you configure authentication, bot settings, and infrastructure.

## Setup Screen

![Setup screen](/screenshots/web-setupdone.png)

### Authentication

<div class="callout callout--warning" style="margin-top:12px">
<p class="callout__title">Understand what these logins mean</p>
<p><strong>Both Azure Login and GitHub Login are required during setup.</strong> You cannot skip either one at this stage.</p>

<p><strong>GitHub Login</strong> authenticates with GitHub Copilot. The Copilot SDK is the agent&rsquo;s reasoning engine&mdash;without it, polyclaw cannot function. This authentication must remain active for the lifetime of the agent. Your GitHub account determines which Copilot models and rate limits are available.</p>

<p><strong>Azure Login</strong> signs you in with the Azure CLI. During setup, your Azure identity is used to provision infrastructure (Bot Service, Container Registry, Key Vault, etc.). After setup, the agent runtime operates under its <strong>own Azure identity</strong>&mdash;a service principal (Docker) or user-assigned managed identity (Azure Container Apps) with least-privilege RBAC. See <a href="/features/agent-identity/">Agent Identity</a> for details.</p>

<p>The runtime identity is scoped to:</p>
<ul>
<li><strong>Bot Service Contributor</strong> on the resource group (create/update bot registrations)</li>
<li><strong>Reader</strong> on the resource group (enumerate resources)</li>
<li><strong>Key Vault access</strong> (read/write secrets)</li>
<li><strong>Session Executor</strong> (if sandbox is configured)</li>
</ul>
<p>No elevated roles (Owner, Contributor, User Access Administrator) are assigned to the runtime. The <a href="/features/guardrails/">security preflight checker</a> verifies this. Your personal Azure CLI session remains on the admin container and is not shared with the runtime.</p>
<p>To further limit exposure, enable <a href="/features/guardrails/">Guardrails</a> to require human approval before the agent executes high-risk tools. Enable <a href="/features/sandbox/">Sandbox Execution</a> to redirect code execution to isolated Azure Container Apps sessions.</p>
</div>

Status indicators for Azure, GitHub, and tunnel connectivity. Each can be initiated directly from this page:

- **Azure Login** -- opens device-code flow for Azure CLI authentication
- **Azure Logout** -- signs out of the current Azure CLI session
- **GitHub Login** -- authenticates with GitHub Copilot via device code
- **Set GitHub Token** -- manually configure a GitHub PAT
- **Start Tunnel** -- starts a Cloudflare tunnel to expose the bot endpoint publicly

<div class="callout callout--info" style="margin-top:16px">
<p class="callout__title">You can sign out of Azure after setup</p>
<p>Your personal Azure CLI session is used during setup for provisioning infrastructure and the runtime identity. Once the runtime identity is provisioned (service principal or managed identity), the agent authenticates independently. If you sign out of Azure on the admin container, core agent functionality (chat, skills, scheduling) continues to work. Operations that require your personal Azure CLI session (e.g., provisioning new infrastructure) will fail until you sign back in.</p>
</div>

### Bot Configuration

A form for configuring the Bot Framework deployment:

- **Resource Group** -- Azure resource group for bot resources (default: `polyclaw-rg`)
- **Location** -- Azure region (default: `eastus`)
- **Bot Display Name** -- display name for the Azure Bot resource
- **Telegram Token** -- Bot token from @BotFather (optional)
- **Telegram Whitelist** -- comma-separated list of allowed Telegram usernames

<div class="callout callout--danger" style="margin-top:12px">
<p class="callout__title">Set a Telegram whitelist</p>
<p>Without a whitelist, <strong>anyone</strong> who discovers your bot&rsquo;s Telegram handle can send it messages&mdash;and the agent will respond using the runtime identity&rsquo;s Azure credentials. That means a stranger could instruct your agent to take actions within the scope of its RBAC roles. Always set a whitelist with only the Telegram usernames you trust.</p>
</div>

### Infrastructure Actions

- **Save Configuration** -- persists bot and channel settings
- **Deploy Infrastructure** -- provisions Azure Bot Service, channels, and related resources
- **Deploy Content Safety** -- provisions Azure AI Content Safety for Prompt Shields integration (recommended)
- **Provision Agent Identity** -- creates the runtime service principal or managed identity with least-privilege RBAC
- **Decommission Infrastructure** -- tears down deployed Azure resources
- **Run Preflight Checks** -- validates bot credentials, JWT, tunnel, endpoint auth, channel security, identity, and RBAC
- **Run Security Preflight** -- comprehensive evidence-based validation of identity, RBAC roles, secret isolation, and credential separation
- **Run Smoke Test** -- end-to-end connectivity test for Copilot

![Preflight checks](/screenshots/web-infra-preflight.png)

## Identity Setup (Web Dashboard)

On first launch, the agent detects whether identity has been configured by checking the `SOUL.md` file and profile state. If not configured, a **bootstrap prompt** activates that walks through the setup conversationally.

### Steps

1. **Identity** -- the agent chooses a name, emoji, location, and personality traits for itself
2. **SOUL.md** -- a Markdown file at `~/.polyclaw/SOUL.md` defining the agent's personality, communication style, and behavioral guidelines. Used as part of the system prompt for every interaction.
3. **Channel setup** (optional) -- if Bot Framework credentials are configured, the wizard can start a tunnel, deploy a bot resource, and configure Teams or Telegram channels
4. **Completion** -- the bootstrap prompt deactivates and the agent switches to normal operation

![Agent profile configuration](/screenshots/web-agentprofile.png)

### Re-running Setup

To reset identity and re-enter the wizard:

1. Delete `~/.polyclaw/SOUL.md`
2. Clear the profile state via `/profile` commands or delete `~/.polyclaw/agent_profile.json`
3. Restart Polyclaw

## Customization Page

The web dashboard includes a **Customization** page that gives an overview of everything you can configure on your agent:

- Skills -- installed and self-created agent skills
- Plugins -- active MCP plugin connections
- MCP Servers -- registered Model Context Protocol servers
- Scheduling -- recurring and one-shot scheduled tasks

![Customization main page](/screenshots/web-customization-main-page.png)
