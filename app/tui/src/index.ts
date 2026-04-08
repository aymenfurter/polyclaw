/**
 * Polyclaw TUI -- entry point.
 *
 * Admin mode:  launches the interactive TUI (disclaimer -> target picker
 *              -> deploy lifecycle & chat).
 *
 * Bot mode:    headless -- Docker build, run, block until Ctrl-C.
 *
 * Start mode:  headless -- build, start, print admin URL, block.
 *              Designed for scripts and CI: no TUI, no disclaimer, no
 *              interactive prompts.
 *
 * Run mode:    headless -- build, start, send a single prompt via the
 *              chat API, print the response, and exit.  Designed for
 *              scripted single-shot interactions.
 *
 * Health mode: check if the stack is already running and healthy.
 */

import {
  buildImage,
  startContainer,
  getAdminSecret,
  resolveKvSecret,
  waitForReady,
  stopContainer,
} from "./deploy/docker.js";
import { launchTUI } from "./ui/tui.js";
import { showDisclaimer } from "./ui/disclaimer.js";
import { pickDeployTarget } from "./ui/target-picker.js";

// -----------------------------------------------------------------------
// Help
// -----------------------------------------------------------------------

function usage(): void {
  console.log("Usage: polyclaw-cli <command> [options]");
  console.log("");
  console.log("Commands:");
  console.log("  admin           Interactive TUI with dashboard and chat (default)");
  console.log("  bot             Bot Framework server only (headless)");
  console.log("  start           Build, start, and print admin URL (scriptable)");
  console.log("  run <prompt>    Start stack, send prompt, print response, exit");
  console.log("  setup           Headless full setup: build, deploy Foundry, verify chat");
  console.log("  decommission    Tear down Azure resources provisioned by setup");
  console.log("  aca-setup       Headless ACA setup: build, Foundry + ACA deploy, verify chat");
  console.log("  aca-decommission  Tear down ACA + Foundry resources");
  console.log("  aca-restart     Restart the ACA runtime container");
  console.log("  aca-setup       Headless ACA setup: build, Foundry + ACA deploy, verify chat");
  console.log("  aca-decommission  Tear down ACA + Foundry resources");
  console.log("  aca-restart     Restart the ACA runtime container");
  console.log("  health          Check if the stack is running and healthy");
  console.log("  stop            Stop the running stack");
  console.log("");
  console.log("Environment:");
  console.log("  ADMIN_PORT      Admin server port (default: 8080)");
  console.log("  BOT_PORT        Bot Framework port (default: 3978)");
  console.log("  POLYCLAW_SETUP_RG              Resource group for setup (default: polyclaw-e2e-rg)");
  console.log("  POLYCLAW_SETUP_LOCATION        Azure region (default: eastus)");
  console.log("  POLYCLAW_SETUP_BASE_NAME       Cognitive Services base name (auto if empty)");
  console.log("  POLYCLAW_SETUP_SUBSCRIPTION_ID Target subscription ID (first if empty)");
  console.log("");
}

const VALID_MODES = ["admin", "bot", "start", "run", "setup", "decommission", "aca-setup", "aca-decommission", "aca-restart", "health", "stop"];

// -----------------------------------------------------------------------
// CLI helpers
// -----------------------------------------------------------------------

/** Build + start the compose stack, returning the instance ID. */
async function ensureStack(
  adminPort: number,
  botPort: number,
  onLine?: (line: string) => void,
): Promise<string> {
  const buildOk = await buildImage(onLine);
  if (!buildOk) {
    console.error("Docker build failed.");
    process.exit(1);
  }

  try {
    return await startContainer(adminPort, botPort, "bot");
  } catch (err: unknown) {
    const msg = err instanceof Error ? err.message : String(err);
    console.error("Failed to start containers:", msg);
    process.exit(1);
  }
}

/** Resolve the admin secret and build the full admin URL. */
async function resolveAdminUrl(port: number): Promise<{ secret: string; url: string }> {
  let secret = await getAdminSecret();
  if (secret.startsWith("@kv:")) {
    secret = await resolveKvSecret(secret);
  }
  const url = secret
    ? `http://localhost:${port}/?secret=${secret}`
    : `http://localhost:${port}`;
  return { secret, url };
}

/** Wait for the stack to become healthy or exit with an error. */
async function waitOrDie(baseUrl: string, instanceId: string): Promise<void> {
  const ready = await waitForReady(baseUrl);
  if (!ready) {
    console.error("Server did not become ready.");
    await stopContainer(instanceId);
    process.exit(1);
  }
}

/** Wire Ctrl-C / SIGTERM to gracefully stop the stack. */
function wireShutdown(instanceId: string): void {
  const shutdown = async () => {
    console.log("\nStopping...");
    await stopContainer(instanceId);
    process.exit(0);
  };
  process.on("SIGINT", shutdown);
  process.on("SIGTERM", shutdown);
}

// -----------------------------------------------------------------------
// Main
// -----------------------------------------------------------------------

async function main(): Promise<void> {
  const mode = process.argv[2] || "admin";

  if (mode === "-h" || mode === "--help") {
    usage();
    process.exit(0);
  }

  if (!VALID_MODES.includes(mode)) {
    console.error(`Unknown command: ${mode}`);
    usage();
    process.exit(1);
  }

  const adminPort = parseInt(process.env.ADMIN_PORT || "8080", 10);
  const botPort = parseInt(process.env.BOT_PORT || "3978", 10);
  const composeAdminPort = 9090;

  // ---- Admin TUI mode ---------------------------------------------------
  if (mode === "admin") {
    await showDisclaimer();

    const target = await pickDeployTarget(adminPort, botPort);
    await launchTUI(adminPort, botPort, target);
    return;
  }

  // ---- Headless setup mode -----------------------------------------------
  if (mode === "setup") {
    const { runHeadlessSetup } = await import("./headless/setup.js");
    await runHeadlessSetup();
    return;
  }

  // ---- Headless decommission mode ----------------------------------------
  if (mode === "decommission") {
    const { runHeadlessDecommission } = await import("./headless/setup.js");
    await runHeadlessDecommission();
    return;
  }

  // ---- ACA headless modes -------------------------------------------------
  if (mode === "aca-setup") {
    const { runAcaHeadlessSetup } = await import("./headless/aca_setup.js");
    await runAcaHeadlessSetup();
    return;
  }

  if (mode === "aca-decommission") {
    const { runAcaHeadlessDecommission } = await import("./headless/aca_setup.js");
    await runAcaHeadlessDecommission();
    return;
  }

  if (mode === "aca-restart") {
    const { runAcaHeadlessRestart } = await import("./headless/aca_setup.js");
    await runAcaHeadlessRestart();
    return;
  }

  // ---- Health check (no build, no start) --------------------------------
  if (mode === "health") {
    try {
      const res = await fetch(`http://localhost:${composeAdminPort}/health`, {
        signal: AbortSignal.timeout(5_000),
      });
      if (res.ok) {
        const body = await res.json();
        console.log(JSON.stringify(body, null, 2));
        process.exit(0);
      } else {
        console.error(`Health check failed: ${res.status} ${res.statusText}`);
        process.exit(1);
      }
    } catch {
      console.error("Stack is not running or not reachable.");
      process.exit(1);
    }
  }

  // ---- Stop -------------------------------------------------------------
  if (mode === "stop") {
    console.log("Stopping stack...");
    await stopContainer("polyclaw-admin");
    console.log("Stopped.");
    process.exit(0);
  }

  // ---- Start mode (scriptable, headless) --------------------------------
  if (mode === "start") {
    console.log("Building and starting polyclaw...");
    const instanceId = await ensureStack(adminPort, botPort);
    const { url } = await resolveAdminUrl(composeAdminPort);

    console.log(`Runtime: http://localhost:8080`);
    console.log(`Admin:   ${url}`);

    wireShutdown(instanceId);

    console.log("Waiting for server...");
    await waitOrDie(`http://localhost:${composeAdminPort}`, instanceId);
    console.log("Server is ready. Press Ctrl+C to stop.");
    await new Promise(() => {});
    return;
  }

  // ---- Run mode (single prompt, headless) -------------------------------
  if (mode === "run") {
    const prompt = process.argv.slice(3).join(" ").trim();
    if (!prompt) {
      console.error("Usage: polyclaw-cli run <prompt>");
      process.exit(1);
    }

    const baseUrl = `http://localhost:${composeAdminPort}`;

    // Check if the stack is already running -- skip build/start if so.
    let instanceId = "";
    let alreadyRunning = false;
    try {
      const res = await fetch(`${baseUrl}/health`, { signal: AbortSignal.timeout(3_000) });
      alreadyRunning = res.ok;
    } catch { /* not running */ }

    if (alreadyRunning) {
      instanceId = "polyclaw-admin";
    } else {
      console.log("Building and starting polyclaw...");
      instanceId = await ensureStack(adminPort, botPort, (line) => {
        if (process.env.VERBOSE) console.log(line);
      });

      console.log("Waiting for server...");
      await waitOrDie(baseUrl, instanceId);
    }

    const { secret } = await resolveAdminUrl(composeAdminPort);

    // Send the prompt via the chat WebSocket
    let response = "";
    try {
      const wsUrl = secret
        ? `ws://localhost:${composeAdminPort}/api/chat/ws?token=${secret}`
        : `ws://localhost:${composeAdminPort}/api/chat/ws`;
      const ws = new WebSocket(wsUrl);

      response = await new Promise<string>((resolve, reject) => {
        const timeout = setTimeout(() => {
          ws.close();
          reject(new Error("Chat response timed out after 120s"));
        }, 120_000);

        const chunks: string[] = [];

        ws.onopen = () => {
          ws.send(JSON.stringify({
            action: "send",
            text: prompt,
          }));
        };

        ws.onmessage = (event) => {
          try {
            const data = JSON.parse(String(event.data));
            if (data.type === "delta" && data.content) {
              chunks.push(data.content);
            } else if (data.type === "done" || data.type === "end") {
              clearTimeout(timeout);
              ws.close();
              resolve(chunks.join(""));
            } else if (data.type === "error") {
              clearTimeout(timeout);
              ws.close();
              reject(new Error(data.content || data.message || "Chat error"));
            }
          } catch {
            // Non-JSON message, ignore
          }
        };

        ws.onerror = (err) => {
          clearTimeout(timeout);
          reject(new Error(`WebSocket error: ${err}`));
        };

        ws.onclose = () => {
          clearTimeout(timeout);
          if (chunks.length > 0) {
            resolve(chunks.join(""));
          }
        };
      });
    } catch (err: unknown) {
      const msg = err instanceof Error ? err.message : String(err);
      console.error(`Chat failed: ${msg}`);
      if (!alreadyRunning) await stopContainer(instanceId);
      process.exit(1);
    }

    console.log(response);
    if (!alreadyRunning) await stopContainer(instanceId);
    process.exit(0);
  }

  // ---- Bot-only mode (headless) -----------------------------------------
  console.log("Building polyclaw...");
  console.log("");

  const instanceId = await ensureStack(adminPort, botPort);
  const { url: adminUrl } = await resolveAdminUrl(composeAdminPort);

  console.log(`Runtime on port 8080 | Admin on port ${composeAdminPort}`);
  console.log(`Admin: ${adminUrl}`);
  console.log("");

  wireShutdown(instanceId);

  console.log("Waiting for server...");
  await waitOrDie(`http://localhost:${composeAdminPort}`, instanceId);
  console.log("Server is ready. Press Ctrl+C to stop.");
  await new Promise(() => {});
}

main().catch((err: unknown) => {
  const msg = err instanceof Error ? err.message : String(err);
  console.error("Fatal:", msg);
  process.exit(1);
});
