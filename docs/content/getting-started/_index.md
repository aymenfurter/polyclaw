---
title: "Getting Started"
weight: 10
---

Polyclaw ships with an interactive **Terminal UI (TUI)** that handles the entire setup lifecycle. The TUI guides you through building, deploying, and configuring Polyclaw -- whether you are running locally with Docker or deploying to Azure Container Apps.

## How It Works

The single entry point is the `run-tui.sh` script in the `scripts/` directory:

```bash
./scripts/run-tui.sh
```

On launch, the TUI presents a **deployment target picker** where you choose between:

| Target | Description |
|---|---|
| **Local Docker** | Builds the Docker image and runs a container on your machine. The container lifecycle is tied to the TUI process. |
| **Azure Container Apps** (Experimental) | Pushes the image to Azure Container Registry and deploys a persistent Container App in your Azure subscription. The app keeps running after the TUI exits. |

After selecting a target, the TUI handles the build, deploy, health check, and then drops you into a full-featured dashboard with live logs, chat, plugin management, scheduling, and more.

## Project Structure

```
polyclaw/
  scripts/
    run-tui.sh         # Entry point -- launches the TUI
  app/
    tui/               # TUI source (OpenTUI + Bun)
    frontend/          # React + Vite admin dashboard
    runtime/           # Python backend
      agent/           # Copilot SDK agent core
      config/          # Settings and environment
      messaging/       # Bot Framework integration
      realtime/        # Voice call handling
      registries/      # Plugin and skill registries
      server/          # aiohttp web server
      services/        # Tunnel, deployer, Key Vault
      state/           # JSON-file-backed state stores
  plugins/             # Built-in MCP plugins
  skills/              # Built-in skill definitions
```

Continue to the [Quickstart](/getting-started/quickstart/) to get Polyclaw running in minutes.
