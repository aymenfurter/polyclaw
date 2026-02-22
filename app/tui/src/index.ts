/**
 * Polyclaw TUI -- entry point.
 *
 * Admin mode: launches the interactive TUI (disclaimer -> target picker
 *             -> deploy lifecycle & chat).
 *
 * Bot mode:   headless -- Docker build, run, block until Ctrl-C.
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
  console.log("Usage: polyclaw-cli [admin|bot]");
  console.log("");
  console.log("  admin  - TUI with status dashboard and chat (default)");
  console.log("  bot    - Bot Framework server only (headless)");
  console.log("");
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

  if (!["admin", "bot"].includes(mode)) {
    console.error(`Unknown mode: ${mode}`);
    usage();
    process.exit(1);
  }

  const adminPort = parseInt(process.env.ADMIN_PORT || "8080", 10);
  const botPort = parseInt(process.env.BOT_PORT || "3978", 10);

  // ---- Admin TUI mode ---------------------------------------------------
  if (mode === "admin") {
    await showDisclaimer();

    const target = await pickDeployTarget(adminPort, botPort);
    await launchTUI(adminPort, botPort, target);
    return;
  }

  // ---- Bot-only mode (headless) -----------------------------------------
  console.log("Building polyclaw v3...");
  console.log("");

  const buildOk = await buildImage();
  if (!buildOk) {
    console.error("Build failed.");
    process.exit(1);
  }

  console.log("Starting polyclaw (admin + runtime)...");
  let instanceId: string;
  try {
    instanceId = await startContainer(adminPort, botPort, "bot");
  } catch (err: unknown) {
    const msg = err instanceof Error ? err.message : String(err);
    console.error("Failed to start containers:", msg);
    process.exit(1);
  }

  // Admin container listens on 9090 (docker-compose.yml)
  const composeAdminPort = 9090;

  let secret = await getAdminSecret();
  if (secret.startsWith("@kv:")) {
    secret = await resolveKvSecret(secret);
  }

  const adminUrl = secret
    ? `http://localhost:${composeAdminPort}/?secret=${secret}`
    : `http://localhost:${composeAdminPort}`;

  console.log(`Runtime on port 8080 | Admin on port ${composeAdminPort}`);
  console.log(`Admin: ${adminUrl}`);
  console.log("");

  const shutdown = async () => {
    console.log("\nStopping...");
    await stopContainer(instanceId);
    process.exit(0);
  };
  process.on("SIGINT", shutdown);
  process.on("SIGTERM", shutdown);

  console.log("Waiting for server...");
  const ready = await waitForReady(`http://localhost:${composeAdminPort}`);
  if (!ready) {
    console.error("Server did not become ready.");
    await stopContainer(instanceId);
    process.exit(1);
  }
  console.log("Server is ready. Press Ctrl+C to stop.");
  await new Promise(() => {});
}

main().catch((err: unknown) => {
  const msg = err instanceof Error ? err.message : String(err);
  console.error("Fatal:", msg);
  process.exit(1);
});
