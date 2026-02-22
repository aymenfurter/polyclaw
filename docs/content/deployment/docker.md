---
title: "Docker"
weight: 1
---

# Local Docker Deployment

When you select **Local Docker** in the TUI target picker, the TUI builds the Docker image, starts both admin and runtime containers via `docker compose`, and connects automatically. The container lifecycle is tied to the TUI process -- both containers stop when you exit.

## How It Works

1. **Launch the TUI** with `./scripts/run-tui.sh` (see [Quickstart](/getting-started/quickstart/))
2. **Select "Local Docker"** from the target picker
3. The TUI builds the image and starts both containers via `docker compose up -d`
4. Once the admin health check passes, you land in the TUI dashboard

![TUI deployment target selection](/screenshots/tui-deployoptions.png)

The TUI handles the full build-deploy-healthcheck cycle and streams build output in real time.

## What Gets Built

The Dockerfile uses a two-stage build:

| Stage | Base Image | What It Does |
|---|---|---|
| **Frontend** | `node:22-slim` | Runs `npm ci` and `npm run build` to produce the Vite/React dashboard |
| **Runtime** | `python:3.12-slim` | Installs the Python runtime, Node.js 22, and all system tools |

### Bundled Tools

The image includes everything the agent needs to operate:

- **GitHub Copilot CLI** (`@github/copilot`) -- the agent engine
- **GitHub CLI** (`gh`) -- authentication
- **Azure CLI** (`az`) -- infrastructure provisioning and bot registration
- **Cloudflare tunnel** (`cloudflared`) -- automatic public endpoint for webhooks
- **Playwright MCP + Chromium** -- headless browser for web-based skills
- **Python runtime** -- the Polyclaw server, agent, and all backend services
- **React dashboard** -- embedded frontend static assets

### Ports

| Port | Container | Service |
|---|---|---|
| `9090` | admin | Admin server and web dashboard (configurable via `ADMIN_PORT`) |
| `3978` | runtime | Bot Framework webhook endpoint |

## Persistent Data

The TUI creates two Docker named volumes that persist across restarts:

| Volume | Mount | Container | Contents |
|---|---|---|---|
| `polyclaw-data` | `/data` | both | Agent config, `.env`, skills, plugins, memory, scheduler state |
| `polyclaw-admin-home` | `/admin-home` | admin only | GitHub and Azure CLI authentication state |

Because these are named Docker volumes, your data survives even when the containers are stopped and recreated on the next TUI launch.

## Container Entrypoint

Each container runs the same entrypoint script, which branches on `POLYCLAW_MODE`:

1. Sets `HOME` based on container mode: `/admin-home` (admin container) or `/runtime-home` (runtime container)
2. Cleans stale Copilot CLI runtime cache (keeps only the matching version)
3. Loads environment variables from the shared persisted `.env` file
4. Resolves any `@kv:` Key Vault secret references (if configured)
5. Authenticates the runtime container's Azure identity (service principal or managed identity)
6. Starts the server: `polyclaw-admin --admin-only` (admin) or `polyclaw-admin --runtime-only` (runtime)

## What Happens on Exit

When you exit the TUI (Ctrl+C or `/quit`), both containers are stopped via `docker compose down`. The named volumes are preserved, so the next launch picks up where you left off -- same configuration, same auth state, same data.

## Integrations Deployed Automatically

These services start automatically inside the container without any manual configuration:

| Service | Description |
|---|---|
| **Cloudflare tunnel** | Exposes a public HTTPS endpoint for Bot Framework webhooks |
| **Playwright browser** | Headless Chromium for web-based skills and MCP servers |
| **Bot Service** | Azure Bot registration using the tunnel URL (if Azure CLI is authenticated) |

All other integrations (voice, Key Vault, sandbox, additional MCP servers) are optional and configured through the [Setup Wizard](/getting-started/setup-wizard/) or the [Web Dashboard](/deployment/#user-interfaces) after deployment.
