/**
 * Tab-based TUI application.
 *
 * Phase 1 -- Startup: build Docker image (live log output), start
 *            container, wait for health.
 * Phase 2 -- Main:    tab-based navigation across all screens.
 */

import {
  createCliRenderer,
  type CliRenderer,
  BoxRenderable,
  TextRenderable,
  TabSelectRenderable,
  ScrollBoxRenderable,
} from "@opentui/core";
import { ApiClient } from "../api/client.js";
import { Colors } from "../utils/theme.js";
import { getContainerStatuses, type ContainerHealth } from "../utils/containers.js";
import { TAB_LABELS } from "../config/constants.js";
import type { Screen } from "../screens/screen.js";
import { DashboardScreen } from "../screens/dashboard.js";
import { SetupScreen } from "../screens/setup.js";
import { ChatScreen } from "../screens/chat.js";
import { SessionsScreen } from "../screens/sessions.js";
import { SkillsScreen } from "../screens/skills.js";
import { PluginsScreen } from "../screens/plugins.js";
import { McpScreen } from "../screens/mcp.js";
import { SchedulerScreen } from "../screens/scheduler.js";
import { ProactiveScreen } from "../screens/proactive.js";
import { ProfileScreen } from "../screens/profile.js";
import { WorkspaceScreen } from "../screens/workspace.js";
import {
  buildImage,
  startContainer,
  stopContainer,
  getAdminSecret,
  resolveKvSecret,
  waitForReady,
} from "../deploy/docker.js";

// -----------------------------------------------------------------------
// Config
// -----------------------------------------------------------------------

export interface AppConfig {
  projectRoot: string;
  port: number;
  secret: string;
  url: string;
}

// -----------------------------------------------------------------------
// App
// -----------------------------------------------------------------------

export class App {
  private renderer!: CliRenderer;
  private cfg: AppConfig;
  private api!: ApiClient;
  private containerId = "";
  private statusInterval: ReturnType<typeof setInterval> | null = null;

  // Phase 1 renderables
  private rootBox!: BoxRenderable;
  private startupStatus!: TextRenderable;
  private startupLog!: TextRenderable;
  private startupConfigText!: TextRenderable;
  private startupTitle!: TextRenderable;
  private startupBox!: BoxRenderable;

  // Phase 2 renderables
  private titleBar!: TextRenderable;
  private tabBar!: TabSelectRenderable;
  private contentArea!: BoxRenderable;
  private screens: Screen[] = [];
  private activeScreen: Screen | null = null;

  // Log buffer
  private logLines: string[] = [];
  private readonly MAX_LOG_LINES = 200;

  constructor(cfg: AppConfig) {
    this.cfg = cfg;
  }

  // -----------------------------------------------------------------------
  // Lifecycle
  // -----------------------------------------------------------------------

  async start(): Promise<void> {
    this.renderer = await createCliRenderer({
      exitOnCtrlC: true,
      useAlternateScreen: true,
      useMouse: true,
      backgroundColor: Colors.bg,
    });

    this.rootBox = new BoxRenderable(this.renderer, {
      flexDirection: "column",
      width: "100%",
      height: "100%",
    });
    this.renderer.root.add(this.rootBox);

    this.renderer.keyInput.on("keypress", (key: { name: string }) => {
      if (key.name === "q" && !this.activeScreen?.capturesInput) {
        this.shutdown();
      }
    });

    this.buildStartupUI();
    this.renderer.start();
    await this.runStartupSequence();
  }

  private async shutdown(): Promise<void> {
    if (this.statusInterval) clearInterval(this.statusInterval);
    if (this.containerId) {
      this.setStatus("Stopping container...");
      await stopContainer(this.containerId);
    }
    this.renderer.destroy();
    process.exit(0);
  }

  // -----------------------------------------------------------------------
  // Phase 1: Startup UI
  // -----------------------------------------------------------------------

  private buildStartupUI(): void {
    this.startupTitle = new TextRenderable(this.renderer, {
      content: " polyclaw v3  |  Starting up...",
      fg: Colors.accent,
      width: "100%",
      height: 1,
    });
    this.rootBox.add(this.startupTitle);

    this.startupBox = new BoxRenderable(this.renderer, {
      flexDirection: "column",
      width: "100%",
      flexGrow: 1,
      padding: 1,
      rowGap: 1,
    });
    this.rootBox.add(this.startupBox);

    this.startupStatus = new TextRenderable(this.renderer, {
      content: " \x1b[33m●\x1b[0m Building Docker image...",
      fg: Colors.text,
      width: "100%",
      height: 1,
    });
    this.startupBox.add(this.startupStatus);

    // Build log
    const logBox = new BoxRenderable(this.renderer, {
      border: true,
      borderColor: Colors.border,
      title: " Build Output ",
      backgroundColor: Colors.surface,
      width: "100%",
      flexGrow: 1,
      flexDirection: "column",
    });

    const logScroll = new ScrollBoxRenderable(this.renderer, {
      backgroundColor: Colors.surface,
      width: "100%",
      flexGrow: 1,
      flexDirection: "column",
      stickyScroll: true,
      scrollX: false,
      scrollY: true,
      padding: 1,
    });

    this.startupLog = new TextRenderable(this.renderer, {
      content: "",
      fg: Colors.muted,
      width: "100%",
    });
    logScroll.add(this.startupLog);
    logBox.add(logScroll);
    this.startupBox.add(logBox);

    // Config panel
    const configBox = new BoxRenderable(this.renderer, {
      border: true,
      borderColor: Colors.border,
      title: " Configuration ",
      backgroundColor: Colors.surface,
      width: "100%",
      height: 7,
      padding: 1,
      flexDirection: "column",
    });

    // Compose admin port for display
    const displayPort = 9090;
    const serverUrl = this.cfg.url || `http://localhost:${displayPort}`;
    this.startupConfigText = new TextRenderable(this.renderer, {
      content: [
        `  Admin:       http://localhost:${displayPort}`,
        `  Runtime:     http://localhost:8080`,
        `  Server URL:  ${serverUrl}`,
        `  Secret:      ${this.cfg.secret ? "(provided)" : "(auto-detect)"}`,
        `  Project:     ${this.cfg.projectRoot}`,
      ].join("\n"),
      fg: Colors.text,
      width: "100%",
    });
    configBox.add(this.startupConfigText);
    this.startupBox.add(configBox);

    // Bottom hint
    this.rootBox.add(new TextRenderable(this.renderer, {
      content: " q: quit",
      fg: Colors.muted,
      width: "100%",
      height: 1,
    }));
  }

  private appendLog(line: string): void {
    this.logLines.push(line);
    if (this.logLines.length > this.MAX_LOG_LINES) {
      this.logLines = this.logLines.slice(-this.MAX_LOG_LINES);
    }
    this.startupLog.content = this.logLines.join("\n");
  }

  private setStatus(msg: string): void {
    this.startupStatus.content = ` ${msg}`;
  }

  // -----------------------------------------------------------------------
  // Docker lifecycle
  // -----------------------------------------------------------------------

  private async runStartupSequence(): Promise<void> {
    // Compose admin listens on 9090 (docker-compose.yml), not cfg.port
    const composeAdminPort = 9090;
    let serverUrl = this.cfg.url || `http://localhost:${composeAdminPort}`;

    // 1) Build
    this.setStatus("\x1b[33m●\x1b[0m Building Docker images...");
    this.appendLog("$ docker compose build");

    const buildOk = await buildImage((line) => this.appendLog(line));
    if (!buildOk) {
      this.setStatus("\x1b[31m●\x1b[0m Docker build FAILED. Check output above. Press q to quit.");
      return;
    }
    this.appendLog("\nBuild complete.");

    // 2) Read admin secret
    this.setStatus("\x1b[33m●\x1b[0m Reading admin secret...");
    let secret = this.cfg.secret;
    if (!secret) secret = await getAdminSecret();

    // 3) Start compose stack (admin + runtime)
    this.setStatus("\x1b[33m●\x1b[0m Starting compose stack...");
    this.appendLog("Starting admin + runtime containers...");
    try {
      this.containerId = await startContainer(this.cfg.port, 3978, "admin");
      this.appendLog(`Compose stack started (${this.containerId})`);
    } catch (err: unknown) {
      this.setStatus("\x1b[31m●\x1b[0m Failed to start containers. Press q to quit.");
      this.appendLog(`Error: ${err instanceof Error ? err.message : err}`);
      return;
    }

    this.startupConfigText.content = [
      `  Admin:       http://localhost:${composeAdminPort}`,
      `  Runtime:     http://localhost:8080`,
      `  Server URL:  ${serverUrl}`,
    ].join("\n");

    // 4) Wait for health
    this.setStatus("\x1b[33m●\x1b[0m Waiting for server to become ready...");
    this.appendLog("Waiting for health endpoint...");

    const healthy = await waitForReady(serverUrl, 90_000);
    if (!healthy) {
      this.setStatus("\x1b[33m●\x1b[0m Server did not respond in time. Launching TUI anyway...");
      this.appendLog("Warning: /health did not respond within 90s");
    } else {
      this.appendLog("Server is healthy.");
    }

    // Re-read secret if auto-generated
    if (!secret) {
      this.appendLog("Reading admin secret from volume...");
      for (let attempt = 0; attempt < 5 && !secret; attempt++) {
        secret = await getAdminSecret();
        if (!secret) await Bun.sleep(2000);
      }
      if (secret) {
        this.appendLog(`Admin secret: ${secret.slice(0, 4)}****`);
      } else {
        this.appendLog("Warning: Could not read admin secret. API calls may fail (401).");
      }
    }

    // Resolve KV references
    if (secret.startsWith("@kv:")) {
      secret = await resolveKvSecret(secret, this.containerId);
    }

    this.startupConfigText.content = [
      `  Admin:       http://localhost:${composeAdminPort}`,
      `  Runtime:     http://localhost:8080`,
      `  Web UI:      ${secret ? `${serverUrl}/?secret=${secret}` : serverUrl}`,
      `  Container:   ${this.containerId.slice(0, 12)}`,
    ].join("\n");

    this.setStatus("\x1b[32m●\x1b[0m Ready. Launching TUI...");
    await Bun.sleep(600);

    // 5) Create API client & transition
    this.api = new ApiClient({ baseUrl: serverUrl, adminSecret: secret });

    const cleanup = async () => { if (this.containerId) await stopContainer(this.containerId); };
    process.on("SIGINT", async () => { await cleanup(); process.exit(0); });
    process.on("SIGTERM", async () => { await cleanup(); process.exit(0); });

    await this.transitionToMainUI();
  }

  // -----------------------------------------------------------------------
  // Phase 2: Main tabbed UI
  // -----------------------------------------------------------------------

  private async transitionToMainUI(): Promise<void> {
    // Remove startup elements
    this.rootBox.remove(this.startupTitle.id);
    this.rootBox.remove(this.startupBox.id);
    const children = (this.rootBox as unknown as { children: { id: string }[] }).children;
    if (children?.length > 0) {
      this.rootBox.remove(children[children.length - 1].id);
    }

    // Title bar
    this.titleBar = new TextRenderable(this.renderer, {
      content: " polyclaw v3  |  Autonomous AI Copilot",
      fg: Colors.accent,
      width: "100%",
      height: 1,
    });
    this.rootBox.add(this.titleBar);

    // Tab bar
    this.tabBar = new TabSelectRenderable(this.renderer, {
      options: TAB_LABELS.map((label) => ({ name: label, description: "" })),
      textColor: Colors.muted,
      selectedTextColor: Colors.accent,
      width: "100%",
      height: 1,
    });
    this.rootBox.add(this.tabBar);

    // Content area
    this.contentArea = new BoxRenderable(this.renderer, {
      backgroundColor: Colors.bg,
      flexGrow: 1,
      width: "100%",
      flexDirection: "column",
    });
    this.rootBox.add(this.contentArea);

    // Status bar
    this.rootBox.add(new TextRenderable(this.renderer, {
      content: " Tab/Shift+Tab: switch sections | q: quit",
      fg: Colors.muted,
      width: "100%",
      height: 1,
    }));

    // Web UI link
    this.rootBox.add(new TextRenderable(this.renderer, {
      content: ` Web UI: ${this.api.webUiUrl}`,
      fg: Colors.accent,
      width: "100%",
      height: 1,
    }));

    // Initialize all screens
    this.screens = [
      new DashboardScreen(this.renderer, this.api),
      new SetupScreen(this.renderer, this.api),
      new ChatScreen(this.renderer, this.api),
      new SessionsScreen(this.renderer, this.api),
      new SkillsScreen(this.renderer, this.api),
      new PluginsScreen(this.renderer, this.api),
      new McpScreen(this.renderer, this.api),
      new SchedulerScreen(this.renderer, this.api),
      new ProactiveScreen(this.renderer, this.api),
      new ProfileScreen(this.renderer, this.api),
      new WorkspaceScreen(this.renderer, this.api),
    ];

    for (const screen of this.screens) {
      await screen.build();
    }

    // Tab navigation
    this.tabBar.on("selectionChanged", () => {
      this.switchToScreen(this.tabBar.getSelectedIndex());
    });

    this.renderer.keyInput.on("keypress", (key: { name: string; shift?: boolean }) => {
      if (key.name === "tab" && !key.shift) {
        const next = (this.tabBar.getSelectedIndex() + 1) % TAB_LABELS.length;
        this.tabBar.setSelectedIndex(next);
        this.switchToScreen(next);
      } else if (key.name === "tab" && key.shift) {
        const prev = (this.tabBar.getSelectedIndex() - 1 + TAB_LABELS.length) % TAB_LABELS.length;
        this.tabBar.setSelectedIndex(prev);
        this.switchToScreen(prev);
      }
    });

    this.switchToScreen(0);
    this.refreshStatus();
    this.statusInterval = setInterval(() => this.refreshStatus(), 30_000);
  }

  private switchToScreen(index: number): void {
    if (this.activeScreen) this.activeScreen.unmount(this.contentArea);
    this.activeScreen = this.screens[index];
    this.activeScreen.mount(this.contentArea);
    this.activeScreen.refresh();
  }

  private async refreshStatus(): Promise<void> {
    // Container health is independent -- always poll even if API fails
    try {
      const cs = await getContainerStatuses();
      const cColor = (h: ContainerHealth) => {
        if (h === "running") return "\x1b[32m";
        if (h === "starting") return "\x1b[33m";
        return "\x1b[31m";
      };
      const cLabel = (h: ContainerHealth) => {
        if (h === "running") return "OK";
        if (h === "starting") return "..";
        if (h === "not_found") return "--";
        return "!!";
      };
      const ctrPieces = [
        `Admin: ${cColor(cs.admin.health)}${cLabel(cs.admin.health)}\x1b[0m`,
        `Runtime: ${cColor(cs.runtime.health)}${cLabel(cs.runtime.health)}\x1b[0m`,
      ];
      // Update title bar with at least container info on API failure
      this.titleBar.content = ` \x1b[33m●\x1b[0m polyclaw v3  |  ${ctrPieces.join("  ")}`;
    } catch { /* Docker unavailable */ }

    try {
      const status = await this.api.getSetupStatus();
      const azOk = status.azure?.logged_in ?? false;
      const ghOk = status.copilot?.authenticated ?? false;
      const tunnelOk = status.tunnel?.active ?? false;
      const pieces = [
        `Azure: ${azOk ? "\x1b[32mOK\x1b[0m" : "\x1b[31m--\x1b[0m"}`,
        `GitHub: ${ghOk ? "\x1b[32mOK\x1b[0m" : "\x1b[31m--\x1b[0m"}`,
        `Tunnel: ${tunnelOk ? "\x1b[32mOK\x1b[0m" : "\x1b[90m--\x1b[0m"}`,
      ];

      // Append container health
      try {
        const cs = await getContainerStatuses();
        const cColor = (h: ContainerHealth) => {
          if (h === "running") return "\x1b[32m";
          if (h === "starting") return "\x1b[33m";
          return "\x1b[31m";
        };
        const cLabel = (h: ContainerHealth) => {
          if (h === "running") return "OK";
          if (h === "starting") return "..";
          if (h === "not_found") return "--";
          return "!!";
        };
        pieces.push(`Admin: ${cColor(cs.admin.health)}${cLabel(cs.admin.health)}\x1b[0m`);
        pieces.push(`Runtime: ${cColor(cs.runtime.health)}${cLabel(cs.runtime.health)}\x1b[0m`);
      } catch { /* Docker unavailable */ }

      const dot = azOk && ghOk ? "\x1b[32m●\x1b[0m" : "\x1b[33m●\x1b[0m";
      this.titleBar.content = ` ${dot} polyclaw v3  |  ${pieces.join("  ")}`;
    } catch {
      // Leave whatever container info was written above
    }
  }
}
