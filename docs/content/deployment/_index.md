---
title: "Deployment"
weight: 80
---

Polyclaw supports two deployment targets, both managed through the TUI or CLI tooling.

## Deployment Targets

| Target | Description |
|---|---|
| [Local Docker](/deployment/docker/) | Builds the image locally and runs a container on your machine. The container lifecycle is tied to the TUI process. |
| [Azure Container Apps](/deployment/azure/) | **Experimental.** Pushes the image to Azure Container Registry and deploys a persistent Container App with optional Bot Service, ACS, and Key Vault integration. |
| [Runtime Isolation](/deployment/runtime-isolation/) | Separated admin and agent runtime architecture with least-privilege managed identity. |

Both targets are selected through the TUI deployment picker when you run `./scripts/run-tui.sh`. The TUI handles the build, push, deploy, and health-check steps automatically.

## User Interfaces

Once deployed, Polyclaw exposes three interfaces for interaction:

| Interface | Description |
|---|---|
| **TUI** | The Terminal UI (`./scripts/run-tui.sh`) is the primary control plane. It handles deployment, configuration, chat, plugin management, scheduling, and live logs. When using Azure Container Apps, the TUI can reconnect to a running deployment and keep it alive independently. |
| **Web Dashboard** | A React-based admin UI served by the container. Provides chat, session management, plugin marketplace, MCP configuration, environment deployment, and voice calling. |
| **Bot Service (Telegram)** | Azure Bot Service delivers messages from Telegram (with more channels planned). Supports proactive messaging, rich cards, and media attachments. |


