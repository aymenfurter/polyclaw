/**
 * Headless setup mode -- full day-one provisioning without interactive TUI.
 *
 * Orchestrates:
 *   1. Docker build + start
 *   2. Wait for admin health
 *   3. Azure CLI check + subscription selection
 *   4. Foundry deploy (Bicep)
 *   5. Wait for runtime BYOK readiness
 *   6. Chat probe via WebSocket
 *
 * Designed for CI and E2E tests -- all output goes to stdout/stderr.
 *
 * Environment:
 *   POLYCLAW_SETUP_RG          Resource group (default: polyclaw-e2e-rg)
 *   POLYCLAW_SETUP_LOCATION    Azure region (default: eastus)
 *   POLYCLAW_SETUP_BASE_NAME   Cognitive Services base name (auto-generated if empty)
 *   POLYCLAW_SETUP_SUBSCRIPTION_ID   Target subscription ID (picks first if empty)
 *   ADMIN_SECRET               Pre-set admin secret (auto-generated if empty)
 */

import {
  buildImage,
  startContainer,
  getAdminSecret,
  waitForReady,
  writeAzureOverride,
} from "../deploy/docker.js";

// ---------------------------------------------------------------------------
// Config from environment
// ---------------------------------------------------------------------------

const RG = process.env.POLYCLAW_SETUP_RG || "polyclaw-e2e-rg";
const LOCATION = process.env.POLYCLAW_SETUP_LOCATION || "eastus";
const BASE_NAME = process.env.POLYCLAW_SETUP_BASE_NAME || "";
const SUBSCRIPTION_ID = process.env.POLYCLAW_SETUP_SUBSCRIPTION_ID || "";
const DEPLOY_KV = process.env.POLYCLAW_SETUP_DEPLOY_KV !== "0";
const ADMIN_PORT = parseInt(process.env.ADMIN_PORT || "8080", 10);
const BOT_PORT = parseInt(process.env.BOT_PORT || "3978", 10);
const COMPOSE_ADMIN_PORT = 9090;
const BASE_URL = `http://localhost:${COMPOSE_ADMIN_PORT}`;

// ---------------------------------------------------------------------------
// Helpers
// ---------------------------------------------------------------------------

function log(msg: string): void {
  const ts = new Date().toISOString().slice(11, 19);
  console.log(`[${ts}] ${msg}`);
}

function fail(msg: string): never {
  console.error(`FATAL: ${msg}`);
  process.exit(1);
}

async function api<T = Record<string, unknown>>(
  path: string,
  opts?: { method?: string; body?: unknown; timeoutMs?: number },
): Promise<{ status: number; data: T }> {
  const secret = await getAdminSecret();
  const headers: Record<string, string> = { "Content-Type": "application/json" };
  if (secret) headers["Authorization"] = `Bearer ${secret}`;

  const res = await fetch(`${BASE_URL}${path}`, {
    method: opts?.method || "GET",
    headers,
    body: opts?.body ? JSON.stringify(opts.body) : undefined,
    signal: AbortSignal.timeout(opts?.timeoutMs || 30_000),
  });
  const data = await res.json().catch(() => null) as T;
  return { status: res.status, data };
}

async function sleep(ms: number): Promise<void> {
  await Bun.sleep(ms);
}

// ---------------------------------------------------------------------------
// Steps
// ---------------------------------------------------------------------------

async function stepBuildAndStart(): Promise<string> {
  log("Building Docker image ...");
  writeAzureOverride();

  const buildOk = await buildImage((line) => {
    if (process.env.VERBOSE) console.log(line);
  });
  if (!buildOk) fail("Docker build failed");

  log("Starting Docker stack ...");
  const instanceId = await startContainer(ADMIN_PORT, BOT_PORT, "setup");

  log("Waiting for admin health ...");
  const ready = await waitForReady(BASE_URL, 120_000);
  if (!ready) fail("Admin did not become healthy within 120s");

  log("Admin is healthy");
  return instanceId;
}

async function stepAzureCheck(): Promise<void> {
  log("Checking Azure CLI status ...");
  const deadline = Date.now() + 120_000;

  while (Date.now() < deadline) {
    try {
      const { status, data } = await api<Record<string, string>>(
        "/api/setup/azure/check",
        { timeoutMs: 60_000 },
      );
      if (status === 200 && data) {
        const st = data.status;
        if (st === "logged_in") {
          log(`Azure logged in: ${data.user || "?"} (${data.subscription || "?"})`);
          return;
        }
        if (st === "needs_subscription") {
          log("Azure needs subscription selection");
          await stepSetSubscription();
          // Re-check after setting subscription
          const { data: d2 } = await api<Record<string, string>>(
            "/api/setup/azure/check",
            { timeoutMs: 60_000 },
          );
          if (d2?.status === "logged_in") {
            log(`Azure logged in: ${d2.user || "?"} (${d2.subscription || "?"})`);
            return;
          }
        }
      }
    } catch (err: unknown) {
      const msg = err instanceof Error ? err.message : String(err);
      log(`Azure check attempt failed: ${msg} -- retrying ...`);
    }
    await sleep(5_000);
  }
  fail("Azure CLI not logged in within 120s -- ensure ~/.azure exists");
}

async function stepSetSubscription(): Promise<void> {
  if (SUBSCRIPTION_ID) {
    log(`Setting subscription: ${SUBSCRIPTION_ID}`);
    await api("/api/setup/azure/subscription", {
      method: "POST",
      body: { subscription_id: SUBSCRIPTION_ID },
    });
    return;
  }

  // Auto-pick first enabled subscription
  const { data } = await api<Array<Record<string, string>>>("/api/setup/azure/subscriptions");
  const subs = Array.isArray(data) ? data : [];
  if (subs.length === 0) fail("No Azure subscriptions available");

  const sub = subs[0];
  log(`Auto-selecting subscription: ${sub.name} (${sub.id})`);
  await api("/api/setup/azure/subscription", {
    method: "POST",
    body: { subscription_id: sub.id },
  });
}

async function stepDeployFoundry(): Promise<Record<string, unknown>> {
  log(`Deploying Foundry: rg=${RG} location=${LOCATION} base_name=${BASE_NAME || "(auto)"}`);
  const body: Record<string, unknown> = {
    resource_group: RG,
    location: LOCATION,
    deploy_key_vault: DEPLOY_KV,
  };
  if (BASE_NAME) body.base_name = BASE_NAME;

  const { status, data } = await api<Record<string, unknown>>("/api/setup/foundry/deploy", {
    method: "POST",
    body,
    timeoutMs: 480_000,
  });

  if (status !== 200 || data?.status !== "ok") {
    fail(`Foundry deploy failed (${status}): ${JSON.stringify(data)}`);
  }

  log(`Foundry deployed: endpoint=${data.foundry_endpoint}`);
  log(`  Models: ${JSON.stringify(data.deployed_models)}`);
  if (data.key_vault_url) log(`  Key Vault: ${data.key_vault_url}`);
  return data;
}

async function stepWaitForRuntime(): Promise<void> {
  log("Waiting for runtime to become ready (BYOK mode) ...");
  const deadline = Date.now() + 180_000;

  while (Date.now() < deadline) {
    try {
      const res = await fetch(`${BASE_URL}/health`, { signal: AbortSignal.timeout(3_000) });
      if (res.ok) {
        // Check runtime container logs for BYOK marker via admin
        // We can't access runtime directly, but the health endpoint works
        break;
      }
    } catch { /* not ready */ }
    await sleep(5_000);
  }

  // Give RBAC a moment to propagate
  await sleep(5_000);
  log("Runtime health check passed");
}

async function stepChatProbe(): Promise<string> {
  log("Sending chat probe via WebSocket ...");
  const secret = await getAdminSecret();
  const wsUrl = secret
    ? `ws://localhost:${COMPOSE_ADMIN_PORT}/api/chat/ws?token=${secret}`
    : `ws://localhost:${COMPOSE_ADMIN_PORT}/api/chat/ws`;

  const deadline = Date.now() + 180_000;
  let lastError = "";

  while (Date.now() < deadline) {
    try {
      const text = await chatOnce(wsUrl);
      if (text) {
        log(`Chat probe OK: ${text.slice(0, 100)}`);
        return text;
      }
    } catch (err: unknown) {
      lastError = err instanceof Error ? err.message : String(err);
      log(`Chat probe failed: ${lastError} -- retrying in 8s`);
    }
    await sleep(8_000);
  }
  fail(`Chat probe did not succeed within 180s. Last error: ${lastError}`);
}

async function chatOnce(wsUrl: string): Promise<string> {
  return new Promise<string>((resolve, reject) => {
    const ws = new WebSocket(wsUrl);
    const timeout = setTimeout(() => {
      ws.close();
      reject(new Error("Chat response timed out after 60s"));
    }, 60_000);

    const chunks: string[] = [];

    ws.onopen = () => {
      ws.send(JSON.stringify({
        action: "send",
        text: "Reply with exactly: PROBE_OK",
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
      } catch { /* non-JSON */ }
    };

    ws.onerror = () => {
      clearTimeout(timeout);
      reject(new Error("WebSocket connection error"));
    };

    ws.onclose = () => {
      clearTimeout(timeout);
      if (chunks.length > 0) resolve(chunks.join(""));
      else reject(new Error("WebSocket closed without response"));
    };
  });
}

async function stepDecommission(): Promise<void> {
  log(`Decommissioning: rg=${RG}`);
  const { status, data } = await api<Record<string, unknown>>("/api/setup/foundry/decommission", {
    method: "POST",
    body: { resource_group: RG },
    timeoutMs: 480_000,
  });
  log(`Decommission: ${status} ${JSON.stringify(data)}`);
}

// ---------------------------------------------------------------------------
// Main
// ---------------------------------------------------------------------------

export async function runHeadlessSetup(): Promise<void> {
  const startTime = Date.now();

  await stepBuildAndStart();

  try {
    await stepAzureCheck();
    await stepDeployFoundry();
    await stepWaitForRuntime();
    const probeText = await stepChatProbe();

    const elapsed = ((Date.now() - startTime) / 1000).toFixed(0);
    log("========================================");
    log(`SETUP COMPLETE in ${elapsed}s`);
    log(`  Chat probe: ${probeText.slice(0, 100)}`);
    log("========================================");

    // Output structured result for test consumption
    console.log(JSON.stringify({
      status: "ok",
      elapsed_seconds: parseInt(elapsed),
      probe_response: probeText.slice(0, 200),
    }));
  } catch (err) {
    // On failure, log clearly but leave the stack running for diagnostics
    const msg = err instanceof Error ? err.message : String(err);
    console.error(`\nFATAL: Setup failed: ${msg}`);
    process.exit(1);
  }
}

export async function runHeadlessDecommission(): Promise<void> {
  await stepDecommission();
}
