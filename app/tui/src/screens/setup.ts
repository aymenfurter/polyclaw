/**
 * Setup screen -- Azure auth, tunnel, configuration, infrastructure.
 */

import {
  BoxRenderable,
  TextRenderable,
  InputRenderable,
  SelectRenderable,
  ScrollBoxRenderable,
} from "@opentui/core";
import { Screen } from "./screen.js";
import { Colors } from "../utils/theme.js";

export class SetupScreen extends Screen {
  capturesInput = true;

  private authText!: TextRenderable;
  private configText!: TextRenderable;
  private infraText!: TextRenderable;
  private resultText!: TextRenderable;

  private botRgInput!: InputRenderable;
  private botLocationInput!: InputRenderable;
  private botNameInput!: InputRenderable;
  private tgTokenInput!: InputRenderable;
  private tgWhitelistInput!: InputRenderable;

  private actionSelect!: SelectRenderable;

  async build(): Promise<void> {
    this.container = new ScrollBoxRenderable(this.renderer, {
      backgroundColor: Colors.bg,
      flexDirection: "column",
      width: "100%",
      flexGrow: 1,
      rowGap: 1,
      padding: 1,
    });

    // -- Auth status --
    const authBox = this.createSection(" Authentication ");
    this.authText = this.createText("Loading...");
    authBox.add(this.authText);
    this.container.add(authBox);

    // -- Actions --
    const actionsBox = this.createSection(" Actions ");
    this.actionSelect = new SelectRenderable(this.renderer, {
      options: [
        { name: "Azure Login", description: "Log in to Azure" },
        { name: "Azure Logout", description: "Log out from Azure" },
        { name: "Deploy Foundry", description: "Deploy AI models via Bicep" },
        { name: "Start Tunnel", description: "Start dev tunnel" },
        { name: "Run Smoke Test", description: "Test Copilot connectivity" },
        { name: "Save Configuration", description: "Save bot and channel config" },
        { name: "Deploy Infrastructure", description: "Provision Azure resources" },
        { name: "Decommission Infrastructure", description: "Remove Azure resources" },
        { name: "Run Preflight Checks", description: "Verify all prerequisites" },
      ],
      textColor: Colors.text,
      selectedTextColor: Colors.accent,
      width: "100%",
      height: 12,
    });
    actionsBox.add(this.actionSelect);
    this.container.add(actionsBox);

    this.actionSelect.on("itemSelected", () => {
      this.handleAction(this.actionSelect.getSelectedIndex());
    });

    // -- Bot configuration form --
    const configBox = this.createSection(" Bot Configuration ");
    configBox.add(this.createLabel("Resource Group:"));
    this.botRgInput = this.createInput("polyclaw-rg", "polyclaw-rg");
    configBox.add(this.botRgInput);
    configBox.add(this.createLabel("Location:"));
    this.botLocationInput = this.createInput("eastus", "eastus");
    configBox.add(this.botLocationInput);
    configBox.add(this.createLabel("Bot Display Name:"));
    this.botNameInput = this.createInput("polyclaw", "polyclaw");
    configBox.add(this.botNameInput);
    configBox.add(this.createLabel("Telegram Token (optional):"));
    this.tgTokenInput = this.createInput("Bot token from @BotFather");
    configBox.add(this.tgTokenInput);
    configBox.add(this.createLabel("Telegram Whitelist (optional):"));
    this.tgWhitelistInput = this.createInput("comma-separated usernames");
    configBox.add(this.tgWhitelistInput);
    this.container.add(configBox);

    // -- Config status --
    this.configText = this.createText("");
    this.container.add(this.configText);

    // -- Infra status --
    const infraBox = this.createSection(" Infrastructure Status ");
    this.infraText = this.createText("Loading...");
    infraBox.add(this.infraText);
    this.container.add(infraBox);

    // -- Result area --
    this.resultText = this.createText("");
    this.container.add(this.resultText);
  }

  refresh(): void {
    this.loadAuthStatus();
    this.loadBotConfig();
    this.loadInfraStatus();
    this.loadChannelConfig();
  }

  // -----------------------------------------------------------------------
  // Factory helpers
  // -----------------------------------------------------------------------

  private createSection(title: string): BoxRenderable {
    return new BoxRenderable(this.renderer, {
      border: true,
      borderColor: Colors.border,
      title,
      backgroundColor: Colors.surface,
      width: "100%",
      padding: 1,
      flexDirection: "column",
      rowGap: 1,
    });
  }

  private createText(content: string): TextRenderable {
    return new TextRenderable(this.renderer, { content, fg: Colors.muted, width: "100%" });
  }

  private createLabel(text: string): TextRenderable {
    return new TextRenderable(this.renderer, { content: text, fg: Colors.muted, width: "100%", height: 1 });
  }

  private createInput(placeholder: string, defaultVal = ""): InputRenderable {
    const inp = new InputRenderable(this.renderer, { placeholder, textColor: Colors.text, width: "100%" });
    if (defaultVal) inp.value = defaultVal;
    return inp;
  }

  // -----------------------------------------------------------------------
  // Data loading
  // -----------------------------------------------------------------------

  private async loadAuthStatus(): Promise<void> {
    try {
      const s = await this.api.getSetupStatus();
      const azOk = s.azure?.logged_in ?? false;
      const tunnelOk = s.tunnel?.active ?? false;
      const dot = (ok: boolean) => ok ? "\x1b[32m●\x1b[0m" : "\x1b[31m●\x1b[0m";
      this.authText.content = [
        `  ${dot(azOk)} Azure    ${azOk ? `${s.azure?.user ?? ""} (${s.azure?.subscription ?? ""})` : "Not logged in  --  run 'Azure Login' below"}`,
        `  ${dot(tunnelOk)} Tunnel   ${tunnelOk ? (s.tunnel?.url ?? "Active") : "Not active  --  run 'Start Tunnel' below"}`,
      ].join("\n");
    } catch (err: unknown) {
      const msg = err instanceof Error ? err.message : String(err);
      this.authText.content = `\x1b[31m  Error loading status: ${msg}\x1b[0m`;
    }
  }

  private async loadBotConfig(): Promise<void> {
    try {
      const cfg = await this.api.getBotConfig();
      this.botRgInput.value = cfg.resource_group || "polyclaw-rg";
      this.botLocationInput.value = cfg.location || "eastus";
      this.botNameInput.value = cfg.display_name || "polyclaw";
    } catch { /* use defaults */ }
  }

  private async loadInfraStatus(): Promise<void> {
    try {
      const r = await this.api.getInfraStatus();
      const prov = r.provisioned as Record<string, Record<string, unknown>> | undefined;
      const lines: string[] = [];
      if (prov?.tunnel) lines.push(`  Tunnel:   ${prov.tunnel.active ? prov.tunnel.url : "not running"}`);
      if (prov?.bot) lines.push(`  Bot:      ${prov.bot.deployed ? `${prov.bot.name} (${prov.bot.resource_group})` : "not deployed"}`);
      const channels = prov?.channels as Record<string, Record<string, unknown>> | undefined;
      if (channels?.telegram) lines.push(`  Telegram: ${channels.telegram.live ? "live" : "not provisioned"}`);
      this.infraText.content = lines.length > 0 ? lines.join("\n") : "  No infrastructure deployed yet.";
    } catch {
      this.infraText.content = "  Could not load.";
    }
  }

  private async loadChannelConfig(): Promise<void> {
    try {
      const cfg = await this.api.getChannelsConfig();
      if (cfg.telegram?.token) {
        this.configText.content = "  Telegram: \x1b[32mConfigured\x1b[0m";
        this.tgWhitelistInput.value = cfg.telegram.whitelist || "";
      } else {
        this.configText.content = "  Telegram: \x1b[90mNot configured\x1b[0m";
      }
    } catch { /* ignore */ }
  }

  // -----------------------------------------------------------------------
  // Actions
  // -----------------------------------------------------------------------

  private async handleAction(index: number): Promise<void> {
    const actions: (() => Promise<void>)[] = [
      () => this.doAzureLogin(),
      () => this.doAzureLogout(),
      () => this.doDeployFoundry(),
      () => this.doStartTunnel(),
      () => this.doSmokeTest(),
      () => this.doSaveConfiguration(),
      () => this.doDeployInfra(),
      () => this.doDecommissionInfra(),
      () => this.doPreflightChecks(),
    ];
    if (actions[index]) await actions[index]();
  }

  private setResult(msg: string): void {
    this.resultText.content = msg;
  }

  private async doAzureLogin(): Promise<void> {
    this.setResult("  Starting Azure login...");
    try {
      const r = await this.api.azureLogin();
      if (r.status === "already_logged_in") {
        this.setResult(`  \x1b[32mAlready logged in as ${r.user}\x1b[0m`);
      } else if (r.code) {
        this.setResult(`  Open ${r.url} and enter code: \x1b[1m${r.code}\x1b[0m\n  Waiting for completion...`);
        await this.pollAzure();
      } else {
        this.setResult(`  ${r.message || "Login started -- check terminal"}`);
      }
      this.loadAuthStatus();
    } catch (err: unknown) {
      const msg = err instanceof Error ? err.message : String(err);
      this.setResult(`  \x1b[31mError: ${msg}\x1b[0m`);
    }
  }

  private async pollAzure(): Promise<void> {
    for (let i = 0; i < 60; i++) {
      await new Promise((r) => setTimeout(r, 3000));
      try {
        const c = await this.api.azureCheck();
        if (c.status === "logged_in") {
          this.setResult("  \x1b[32mAzure login successful!\x1b[0m");
          this.loadAuthStatus();
          return;
        }
      } catch { /* keep trying */ }
    }
    this.setResult("  \x1b[33mLogin timed out. Try again.\x1b[0m");
  }

  private async doAzureLogout(): Promise<void> {
    try {
      await this.api.azureLogout();
      this.setResult("  Logged out from Azure.");
      this.loadAuthStatus();
    } catch (err: unknown) {
      const msg = err instanceof Error ? err.message : String(err);
      this.setResult(`  \x1b[31m${msg}\x1b[0m`);
    }
  }

  private async doDeployFoundry(): Promise<void> {
    this.setResult("  Deploying Foundry infrastructure via Bicep...");
    try {
      const r = await this.api.fetchRaw("/api/setup/foundry/deploy", {
        method: "POST",
        body: JSON.stringify({ resource_group: "polyclaw-rg", location: "eastus" }),
        signal: AbortSignal.timeout(600_000),
      });
      const body = await r.json();
      if (body.status === "ok") {
        this.setResult(`  \x1b[32mFoundry deployed!\x1b[0m\n  Endpoint: ${body.foundry_endpoint}\n  Models: ${(body.deployed_models || []).join(", ")}`);
      } else {
        this.setResult(`  \x1b[31mDeployment failed: ${body.error || "unknown error"}\x1b[0m`);
      }
      this.loadAuthStatus();
    } catch (err: unknown) {
      const msg = err instanceof Error ? err.message : String(err);
      this.setResult(`  \x1b[31mError: ${msg}\x1b[0m`);
    }
  }

  private async doStartTunnel(): Promise<void> {
    this.setResult("  Starting tunnel...");
    try {
      const r = await this.api.startTunnel();
      if (r.status === "ok") {
        this.setResult(`  \x1b[32mTunnel started: ${r.url}\x1b[0m${r.endpoint_updated ? "\n  Bot endpoint updated" : ""}`);
      } else {
        this.setResult(`  \x1b[31m${r.message}\x1b[0m`);
      }
      this.loadAuthStatus();
    } catch (err: unknown) {
      const msg = err instanceof Error ? err.message : String(err);
      this.setResult(`  \x1b[31m${msg}\x1b[0m`);
    }
  }

  private async doSmokeTest(): Promise<void> {
    this.setResult("  Running smoke test...");
    try {
      const r = await this.api.smokeTest() as Record<string, unknown>;
      const lines: string[] = [];
      lines.push(r.status === "ok" ? "  \x1b[32mSmoke test passed\x1b[0m" : "  \x1b[31mSmoke test failed\x1b[0m");
      const steps = r.steps as Array<{ ok: boolean; step: string; detail?: string }> | undefined;
      if (steps) {
        for (const s of steps) {
          lines.push(`    ${s.ok ? "\x1b[32m✓\x1b[0m" : "\x1b[31m✗\x1b[0m"} ${s.step}: ${s.detail || ""}`);
        }
      }
      this.setResult(lines.join("\n"));
    } catch (err: unknown) {
      const msg = err instanceof Error ? err.message : String(err);
      this.setResult(`  \x1b[31m${msg}\x1b[0m`);
    }
  }

  private async doSaveConfiguration(): Promise<void> {
    this.setResult("  Saving configuration...");
    try {
      const body: Record<string, unknown> = {
        bot: {
          resource_group: this.botRgInput.value || "polyclaw-rg",
          location: this.botLocationInput.value || "eastus",
          display_name: this.botNameInput.value || "polyclaw",
        },
        telegram: {} as Record<string, string>,
      };
      const tgToken = this.tgTokenInput.value?.trim();
      if (tgToken) {
        (body.telegram as Record<string, string>).token = tgToken;
        (body.telegram as Record<string, string>).whitelist = this.tgWhitelistInput.value?.trim() || "";
      }
      const r = await this.api.saveConfiguration(body);
      this.setResult(this.formatStepResult(r));
      this.refresh();
    } catch (err: unknown) {
      const msg = err instanceof Error ? err.message : String(err);
      this.setResult(`  \x1b[31m${msg}\x1b[0m`);
    }
  }

  private async doDeployInfra(): Promise<void> {
    this.setResult("  \x1b[33mDeploying infrastructure... This may take several minutes.\x1b[0m");
    try {
      const r = await this.api.deployInfra();
      this.setResult(this.formatStepResult(r));
      this.loadInfraStatus();
    } catch (err: unknown) {
      const msg = err instanceof Error ? err.message : String(err);
      this.setResult(`  \x1b[31m${msg}\x1b[0m`);
    }
  }

  private async doDecommissionInfra(): Promise<void> {
    this.setResult("  \x1b[33mDecommissioning infrastructure...\x1b[0m");
    try {
      const r = await this.api.decommissionInfra();
      this.setResult(this.formatStepResult(r));
      this.loadInfraStatus();
    } catch (err: unknown) {
      const msg = err instanceof Error ? err.message : String(err);
      this.setResult(`  \x1b[31m${msg}\x1b[0m`);
    }
  }

  private async doPreflightChecks(): Promise<void> {
    this.setResult("  Running preflight checks...");
    try {
      const r = await this.api.getPreflight();
      const checks = (r as Record<string, unknown>).checks as Array<{ ok: boolean; check: string; detail: string }> | undefined;
      const labels: Record<string, string> = {
        bot_credentials: "Bot Credentials",
        jwt_validation: "JWT Validation",
        tunnel: "Tunnel",
        tenant_id: "Tenant ID",
        endpoint_auth: "Endpoint Auth",
        telegram_security: "Telegram Security",
        acs_voice: "ACS / Voice",
        acs_callback_security: "ACS Callback Security",
      };
      const lines: string[] = [];
      for (const c of checks || []) {
        const icon = c.ok ? "\x1b[32m✓\x1b[0m" : "\x1b[31m✗\x1b[0m";
        lines.push(`    ${icon} ${labels[c.check] || c.check}: ${c.detail}`);
      }
      this.setResult(lines.length > 0 ? lines.join("\n") : "  No checks available.");
    } catch (err: unknown) {
      const msg = err instanceof Error ? err.message : String(err);
      this.setResult(`  \x1b[31m${msg}\x1b[0m`);
    }
  }

  private formatStepResult(r: Record<string, unknown>): string {
    const lines: string[] = [];
    const msg = r.message as string | undefined;
    lines.push(r.status === "ok"
      ? `  \x1b[32m${msg}\x1b[0m`
      : `  \x1b[31m${msg}\x1b[0m`);
    const steps = r.steps as Array<{ status: string; step: string; detail?: string }> | undefined;
    if (steps) {
      for (const s of steps) {
        const icon = s.status === "ok" ? "\x1b[32m✓\x1b[0m" : "\x1b[31m✗\x1b[0m";
        lines.push(`    ${icon} ${s.step}: ${s.detail || s.status}`);
      }
    }
    return lines.join("\n");
  }
}
