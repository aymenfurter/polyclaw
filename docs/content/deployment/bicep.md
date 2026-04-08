---
title: "Bicep Infrastructure"
weight: 4
---

# Bicep Infrastructure

Polyclaw provisions Azure infrastructure through a single Bicep template (`infra/main.bicep`) driven by `az deployment group create`. This replaces ad-hoc Azure CLI provisioning with a declarative, parameterised approach.

## How It Works

The **Deploy Infrastructure** button in the Setup Wizard (or the `POST /api/setup/foundry/deploy` API) triggers a Bicep deployment. The `BicepDeployer` service assembles parameters from internal config state and runs `az deployment group create` against the template.

Progress is streamed in real time via the `GET /api/setup/foundry/deploy/stream` SSE endpoint.

## What Gets Provisioned

Each resource block in the Bicep template is gated by a boolean flag. Callers enable only the subset they need.

| Resource | Flag | Default | Description |
|---|---|---|---|
| Azure AI Services (Foundry) | `deploy_foundry` | enabled | AI Services account with model deployments (gpt-4.1, gpt-5, gpt-5-mini) |
| Key Vault | `deploy_key_vault` | enabled | Centralized secret management with firewall rules |
| Azure AI Content Safety | `deploy_content_safety` | disabled | Prompt Shields and content moderation |
| Azure Container Apps Session Pool | `deploy_session_pool` | disabled | Sandboxed code execution |
| Azure Communication Services | `deploy_acs` | disabled | Inbound and outbound voice calls |
| Azure AI Search | `deploy_search` | disabled | Search index for Foundry IQ memory |
| Azure OpenAI (Embedding) | `deploy_embedding_aoai` | disabled | Embedding model for Foundry IQ |
| Application Insights | `deploy_monitoring` | disabled | Distributed tracing and log analytics |

## Model Deployments

The Foundry AI Services resource deploys models as configured in the request. The default set is:

| Model | Version | SKU | Capacity |
|---|---|---|---|
| gpt-4.1 | 2025-04-14 | GlobalStandard | 10 |
| gpt-5 | 2025-08-07 | GlobalStandard | 10 |
| gpt-5-mini | 2025-08-07 | GlobalStandard | 10 |

Custom model lists can be passed in the deploy request body.

## Deployment Parameters

| Parameter | Default | Description |
|---|---|---|
| `resource_group` | `polyclaw-rg` | Target resource group |
| `location` | `eastus` | Azure region |
| `base_name` | auto-generated | Base name for all resources (generates unique suffix) |

## API Endpoints

| Method | Path | Description |
|---|---|---|
| `GET` | `/api/setup/foundry/status` | Current deployment status |
| `POST` | `/api/setup/foundry/deploy` | Trigger a Bicep deployment |
| `GET` | `/api/setup/foundry/deploy/stream` | SSE stream for deployment progress |
| `POST` | `/api/setup/foundry/decommission` | Tear down deployed resources |

## Post-Deployment

After a successful Bicep deployment, the deployer writes the following to the `.env` file:

- `FOUNDRY_ENDPOINT` -- the AI Services endpoint URL
- `FOUNDRY_NAME` -- the resource display name
- `FOUNDRY_RESOURCE_GROUP` -- the resource group name
- `KEY_VAULT_URL` -- the Key Vault URL (if deployed)

The runtime container picks up these values on restart and activates BYOK mode when `FOUNDRY_ENDPOINT` is present.

## RBAC

The Bicep deployment itself runs under your personal Azure CLI session on the admin container. After deployment, the runtime service principal needs the `Cognitive Services OpenAI User` role on the AI Services resource to perform BYOK inference. Use the **Fix Roles** button on the Agent Identity page (or `POST /api/identity/fix-roles`) to assign missing roles automatically.

## Idempotent Re-runs

Deployments are tracked in `DeployStateStore`. Re-running a deployment updates existing resources rather than creating duplicates. The Bicep template uses Azure Resource Manager's built-in idempotency.

## Decommissioning

`POST /api/setup/foundry/decommission` tears down resources created by the Bicep template. The deployment record is removed from the local store.

## Template Location

The Bicep template is at `infra/main.bicep` in the repository root. The compiled ARM template (`infra/main.json`) is also committed for environments where `az bicep` is not available.
