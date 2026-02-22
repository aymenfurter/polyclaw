---
title: "Services Layer"
weight: 4
---

# Services Layer

The services layer provides infrastructure management, secret handling, and external integrations.

## Tunnel Service

**Module**: `app/runtime/services/tunnel.py`

Manages a Cloudflare quick-tunnel subprocess to expose local endpoints publicitly.

| Feature | Description |
|---|---|
| Auto-start | Launched during server startup |
| URL detection | Parses tunnel URL from subprocess output |
| Health monitoring | Detects tunnel disconnections |
| Restricted mode | `TUNNEL_RESTRICTED=true` limits access to bot/voice endpoints |

The tunnel URL is used as the Bot Framework messaging endpoint and ACS callback URL.

## Key Vault Service

**Module**: `app/runtime/services/keyvault.py`

Integrates with Azure Key Vault for secret management.

| Feature | Description |
|---|---|
| `@kv:` resolution | Secrets prefixed with `@kv:secret-name` are resolved at startup |
| Write-back | `Settings.write_env()` stores secrets in Key Vault and writes `@kv:` references |
| Firewall allowlisting | Automatically adds current IP to Key Vault firewall rules |
| Credential chain | Uses `AzureCliCredential` or `DefaultAzureCredential` |

## Provisioner

**Module**: `app/runtime/services/provisioner.py`

Orchestrates the infrastructure lifecycle for a configured bot:

| Method | Description |
|---|---|
| `provision()` | Register Entra ID app and provision a scoped runtime identity |
| `recreate_endpoint()` | Update the bot messaging endpoint when the tunnel URL changes |
| `decommission()` | Delete the bot registration and revoke the runtime identity |
| `status()` | Return current provisioning state from the deploy store |

Channel configuration (Telegram) is applied as part of `provision()` when a token is present in the infra config. Teams is enabled automatically via the bot resource itself.

## Bot Deployer

**Module**: `app/runtime/services/deployer.py`

`BotDeployer` manages the full Azure Bot resource lifecycle:

| Method | Description |
|---|---|
| `deploy()` | Create resource group, register Entra ID app, generate credentials, and register the Azure Bot |
| `register_app()` | Register the Entra ID app and bot resource without creating infrastructure |
| `recreate()` | Update the bot messaging endpoint and re-register credentials |
| `delete()` | Remove the Azure Bot resource and optionally the resource group |

## ACA Deployer

**Module**: `app/runtime/services/aca_deployer.py`

`AcaDeployer` deploys the full Polyclaw stack to Azure Container Apps:

| Feature | Description |
|---|---|
| Image build & push | Builds `polyclaw:latest` and pushes to a provisioned Azure Container Registry |
| ACA environment | Creates the Container Apps environment with workload profile |
| Runtime app | Deploys the runtime container app with CPU/memory limits and ingress |
| Managed identity | Creates and assigns `polyclaw-runtime-mi` with Bot Contributor, Reader, and Session Executor roles |
| IP allowlisting | Adds the deployer's public IP to the Key Vault and ACR firewall |
| Deploy state | Records each deployment in `DeployStateStore` for idempotent re-runs |

Key operations: `deploy(req)`, `destroy(deploy_id)`, `status()`, `restart()`.

## Runtime Identity Provisioner

**Module**: `app/runtime/services/runtime_identity.py`

`RuntimeIdentityProvisioner` provisions and revokes the scoped identity the agent runtime uses to interact with Azure:

| Strategy | Description |
|---|---|
| Service principal | Used in Docker Compose deployments; credentials written to `/data/.env` as `RUNTIME_SP_*` keys |
| Managed identity | Used in ACA deployments; `polyclaw-runtime-mi` attached to the container app |

RBAC roles granted (scoped to the resource group):

| Role | Purpose |
|---|---|
| Azure Bot Service Contributor | Create/update/delete the Bot Service registration |
| Reader | Enumerate resources in the resource group |
| Key Vault Secrets Officer | Read/write bot credentials stored in Key Vault |
| Azure ContainerApps Session Executor | Invoke ACA Dynamic Sessions for code execution |

Key operations: `provision(resource_group)`, `revoke()`, `provision_managed_identity()`, `revoke_managed_identity()`, `status()`.

## Azure CLI Wrapper

**Module**: `app/runtime/services/azure.py`

Wraps `az` CLI commands for:

- Bot creation and deletion
- Channel management (Teams, Telegram)
- Resource group operations
- Subscription queries

## OpenTelemetry Service

**Module**: `app/runtime/services/otel.py`

Bootstraps Azure Monitor distributed tracing via the OpenTelemetry SDK:

| Feature | Description |
|---|---|
| `configure_otel()` | Initialises the Azure Monitor distro with a connection string and sampling ratio |
| Agent spans | `agent_span()` / `invoke_agent_span()` context managers wrap agent invocations |
| Event recording | `record_event()` emits custom span events; `set_span_attribute()` annotates the active span |
| Graceful init | Monitoring is optional -- a missing connection string or import error never blocks startup |
| Noisy logger suppression | Azure SDK HTTP and identity loggers are quieted to `WARNING` |

## Prompt Shield Service

**Module**: `app/runtime/services/prompt_shield.py`

`PromptShieldService` calls the Azure AI Content Safety Prompt Shields API to detect prompt injection attacks in tool arguments before execution:

| Feature | Description |
|---|---|
| Authentication | `DefaultAzureCredential` with `https://cognitiveservices.azure.com/.default` scope; API keys are never used |
| Result type | `ShieldResult(attack_detected, mode, detail)` frozen dataclass |
| Opt-in | Service is a no-op when no Content Safety endpoint is configured |

## Security Preflight Checker

**Module**: `app/runtime/services/security_preflight.py`

`SecurityPreflightChecker` runs verifiable runtime security checks and produces a structured `PreflightResult`:

| Check category | What is verified |
|---|---|
| Azure login | Active `az login` session exists |
| Identity configured | `RUNTIME_SP_*` or managed identity env vars are present |
| Identity valid | Service principal or MI is resolvable and not expired |
| RBAC roles | Runtime identity holds expected roles and no elevated roles (Owner, Contributor, etc.) |
| RBAC scope | Role assignments are scoped to the correct resource group, not subscription-wide |
| Secret isolation | Admin CLI credentials are not accessible from the runtime container |
| Bot credentials | `MicrosoftAppId` / `MicrosoftAppPassword` are set and non-empty |
| Key Vault reachability | Key Vault endpoint responds to a token-authenticated probe |

Every check executes a real command or environment inspection -- no static claims.

## Other Services

| Module | Purpose |
|---|---|
| `github.py` | GitHub API integration |
| `foundry_iq.py` | Azure AI Foundry IQ indexing and search |
| `resource_tracker.py` | Azure resource tracking and cost awareness |
| `misconfig_checker.py` | Configuration auditing and validation |
