---
title: "Security, Governance & Responsible AI"
weight: 25
---

Polyclaw is in **early preview**. Treat it as experimental software and read this page carefully.

---

## Understand the Risks

Polyclaw is an autonomous agent. The agent runtime is architecturally separated from the admin plane and operates under its **own Azure managed identity** with least-privilege RBAC -- it does not share your personal Azure credentials. However, it can still execute code, deploy infrastructure, send messages, and make phone calls within the scope of its assigned roles. GitHub authentication remains a prerequisite for the Copilot SDK.

**What can go wrong:**

- **Unintended actions.** The agent decides what tools to call based on its prompt and conversation context. A misunderstood instruction can lead to unwanted commits, messages sent to the wrong person, or files overwritten.
- **Credential exposure.** Although the runtime identity is scoped with least-privilege RBAC, prompt injection attacks or badly written skills could still misuse the credentials available to the runtime. The [guardrails](/features/guardrails/) framework mitigates this with content safety filtering and HITL approval.
- **Cost overruns.** The agent can spin up Azure resources, make API calls, and schedule recurring tasks. Without monitoring, a runaway loop could generate unexpected cloud bills.
- **Code execution.** The agent can execute arbitrary code in the runtime container or in a [sandbox](/features/sandbox/). [Guardrails](/features/guardrails/) can require human approval before code execution occurs.
- **Data leakage.** Conversations, files, and tool outputs pass through the Copilot SDK and any configured channels. Sensitive data in your workspace could be included in agent context unintentionally.
- **Availability of external services.** The agent depends on the GitHub Copilot SDK, Azure services, and third-party APIs. Outages in any of these can cause failures or degraded behavior.

This is not a theoretical list. These are real failure modes of autonomous agents. You should be comfortable with these risks before deploying Polyclaw in any environment that matters.

---

## What We Have Built So Far

The project includes several controls today, but none of them have been formally audited. They represent a best-effort starting point.

### Authentication

| Layer | Mechanism |
|---|---|
| Admin API | Bearer token (`ADMIN_SECRET`) required on all `/api/*` routes. See [Security & Auth](/configuration/security/). |
| Bot channels | JWT validation via `botbuilder-core` SDK. |
| Voice callbacks | RS256 JWT validation; query-param callback token as secondary check. |
| Telegram | User ID whitelist (`TELEGRAM_WHITELIST`). Non-whitelisted messages are dropped. |
| Tunnel | `TUNNEL_RESTRICTED` limits the Cloudflare tunnel to bot and voice endpoints only. |
| Frontend | Login gate -- the SPA renders no data until `ADMIN_SECRET` is verified. |

### Agent Identity

The agent runtime operates under its own Azure identity rather than your personal credentials. See [Agent Identity](/features/agent-identity/) for full details.

| Strategy | Deployment Target | Identity Type |
|----------|-------------------|---------------|
| Service Principal | Docker / Docker Compose | `polyclaw-runtime` SP with client secret |
| Managed Identity | Azure Container Apps | `polyclaw-runtime-mi` user-assigned MI |

The runtime identity is assigned least-privilege RBAC roles (Bot Service Contributor, Reader, Key Vault access, Session Executor). No elevated roles (Owner, Contributor, User Access Administrator, Role Based Access Control Administrator) are assigned. The security preflight checker verifies this.

### Separated Admin and Agent Runtime

The application is split into two containers to enforce credential isolation:

| Container | Purpose | GitHub Token | Admin Secret | Azure Identity |
|-----------|---------|-------------|-------------|----------------|
| **Admin** | UI, configuration, deployment | Yes | Yes | Your personal CLI session |
| **Runtime** | Agent execution, tool invocation | No | No | Service principal or managed identity |

Each container has its own HOME directory. The runtime container never sees the GitHub token, admin secret, or personal Azure credentials.

### Guardrails

A defense-in-depth framework intercepts every tool invocation before execution. Guardrails require careful configuration to match your use case and risk appetite. See [Guardrails & HITL](/features/guardrails/).

| Strategy | Description |
|----------|-------------|
| Allow | Immediate permit |
| Deny | Immediate block, logged to [Tool Activity](/features/tool-activity/) |
| HITL | Human approval via chat or bot channel (300s timeout) |
| PITL (Experimental) | Phone call for voice approval (300s timeout) |
| AITL | Independent AI safety reviewer (30s timeout) |
| Filter | Azure AI Prompt Shields content analysis; also runs as a pre-check before HITL, AITL, and PITL |

Built-in presets (permissive, balanced, restrictive) define policy matrices based on tool risk level and agent context. Model-aware policies allow you to control autonomy levels per model -- stronger models that are less susceptible to prompt injection can operate with more autonomy, while less capable models are automatically assigned tighter guardrails.

### Content Safety

Azure AI Content Safety Prompt Shields provide prompt injection detection. Authentication uses Entra ID (no API keys). The service is fail-closed: any API error is treated as an attack detection.

### Tool Activity

An append-only audit log records every tool invocation with automated scoring, Prompt Shield results, execution duration, and session context. See [Tool Activity](/features/tool-activity/).

### Monitoring

OpenTelemetry traces, metrics, and logs flow from the runtime to Azure Monitor via Application Insights. See [Monitoring](/features/monitoring/).

### Secret Management

- Secrets can be stored in Azure Key Vault and referenced via `@kv:` prefix notation. See [Key Vault](/configuration/keyvault/).
- `ADMIN_SECRET` is auto-generated with `secrets.token_urlsafe(24)` if not explicitly set.
- A `SECRET_ENV_KEYS` frozenset enumerates which variables are treated as secrets.

### Isolation

- Code execution can be redirected to isolated sandbox sessions where the remote environment has no access to credentials or the host filesystem. See [Sandbox](/features/sandbox/).
- An experimental `LOCKDOWN_MODE` flag rejects all admin API requests, allowing you to freeze the agent immediately. See [Security & Auth](/configuration/security/).

### Transparency

- Every tool call is surfaced in the chat interface with the tool name, parameters, and result.
- The agent's behavioral guidelines live in a human-readable `SOUL.md` file. You can inspect and modify it.
- System prompts are assembled from version-controlled Markdown templates in `app/runtime/templates/`.
- All conversations are archived with full message and tool-call history.
- Structured logging with context tags (`[agent.start]`, `[chat.dispatch]`) covers all operations.

### Preflight and Disclaimers

- The [Setup Wizard](/getting-started/setup-wizard/) runs validation checks (JWT, tunnel, endpoints) before the agent starts.
- The security preflight checker validates identity, RBAC, and credential isolation with evidence-based reporting.
- The web frontend requires users to accept a disclaimer about the agent's autonomous nature before proceeding.

---

## What Is Missing

The following areas are known gaps that the project intends to address:

- **Multi-runtime management (1:N).** The admin plane currently manages a single agent runtime. The goal is to support managing multiple agent runtimes from a single admin plane.
- **Multi-tenant isolation.** Polyclaw is designed for single-operator use. Running it for multiple users would require significant changes.

---

## Recommendations for Early Adopters

1. **Deploy with separated admin and agent runtime containers** to enforce credential isolation between the admin plane and the agent runtime.
2. **Set a strong `ADMIN_SECRET`** and store it in a key vault rather than in plaintext.
3. **Enable `TUNNEL_RESTRICTED`** to limit what is exposed through the tunnel.
4. **Set `TELEGRAM_WHITELIST`** if you use the Telegram channel.
5. **Enable sandbox execution** for code-running workloads to isolate them from the host.
6. **Enable guardrails** with at least the balanced preset. Use HITL for high-risk tools.
7. **Run the security preflight checker** to verify identity, RBAC, and secret isolation.
8. **Monitor tool activity and logs** to review what the agent has been doing.
9. **Review `SOUL.md`** and system prompt templates to make sure the agent's instructions match your expectations.
10. **Do not leave the agent running unattended** for extended periods without checking in on its activity.
