/**
 * Headless ACA setup mode -- full day-one provisioning with ACA runtime.
 *
 * Orchestrates:
 *   1. Docker build (compose + linux/amd64 for ACA)
 *   2. Start admin container locally
 *   3. Azure CLI check + subscription selection
 *   4. Foundry deploy (Bicep)
 *   5. ACA deploy (ACR push, ACA environment, container app)
 *   6. Wait for runtime readiness via admin proxy
 *   7. Chat probe via WebSocket
 *
 * Designed for CI and E2E tests -- all output goes to stdout/stderr.
 *
 * Environment:
 *   POLYCLAW_SETUP_RG              Resource group (default: polyclaw-e2e-aca-rg)
 *   POLYCLAW_SETUP_LOCATION        Azure region (default: eastus)
 *   POLYCLAW_SETUP_BASE_NAME       Cognitive Services base name (auto if empty)
 *   POLYCLAW_SETUP_SUBSCRIPTION_ID Target subscription ID (first if empty)
 *   POLYCLAW_SETUP_ACA_IMAGE_TAG   ACA image tag (default: aca)
 */

import {
  buildImage,
  buildAcaImage,
  getAdminSecret,
  waitForReady,
  writeAzureOverride,
  stopContainer,
} from "../deploy/docker.js";
import { exec } from "../deploy/process.js";
import { resolve } from "path";

const PROJECT_ROOT = resolve(import.meta.dir, "../../../..");

// ---------------------------------------------------------------------------
// Config from environment
// ---------------------------------------------------------------------------

const RG = process.env.POLYCLAW_SETUP_RG || "polyclaw-e2e-aca-rg";
const LOCATION = process.env.POLYCLAW_SETUP_LOCATION || "eastus";
const BASE_NAME = process.env.POLYCLAW_SETUP_BASE_NAME || "";
const SUBSCRIPTION_ID = process.env.POLYCLAW_SETUP_SUBSCRIPTION_ID || "";
const DEPLOY_KV = process.env.POLYCLAW_SETUP_DEPLOY_KV !== "0";
const IMAGE_TAG = process.env.POLYCLAW_SETUP_ACA_IMAGE_TAG || "aca";
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

async function stepBuildImages(): Promise<void> {
  log("Building Docker image (compose) ...");
  writeAzureOverride();

  const buildOk = await buildImage((line) => {
    if (process.env.VERBOSE) console.log(line);
  });
  if (!buildOk) fail("Docker compose build failed");

  log(`Building linux/amd64 image for ACA (tag=${IMAGE_TAG}) ...`);
  const acaOk = await buildAcaImage(IMAGE_TAG, (line) => {
    if (process.env.VERBOSE) console.log(line);
  });
  if (!acaOk) fail("Docker build (linux/amd64) failed");
}

async function stepStartAdminOnly(): Promise<void> {
  log("Stopping any existing stack ...");
  try {
    await exec(["docker", "compose", "down", "--remove-orphans"], PROJECT_ROOT);
  } catch { /* may not be running */ }

  writeAzureOverride();

  log("Starting admin container only ...");
  const { exitCode, stderr } = await exec(
    ["docker", "compose", "up", "-d", "admin"],
    PROJECT_ROOT,
  );
  if (exitCode !== 0) fail(`docker compose up admin failed (exit ${exitCode}): ${stderr}`);

  log("Waiting for admin health ...");
  const ready = await waitForReady(BASE_URL, 120_000);
  if (!ready) fail("Admin did not become healthy within 120s");
  log("Admin is healthy");
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

async function stepDeployAca(): Promise<Record<string, unknown>> {
  log(`Deploying ACA: rg=${RG} location=${LOCATION} image_tag=${IMAGE_TAG}`);

  const { status, data } = await api<Record<string, unknown>>("/api/setup/aca/deploy", {
    method: "POST",
    body: {
      resource_group: RG,
      location: LOCATION,
      runtime_port: 8080,
      admin_port: 9090,
      image_tag: IMAGE_TAG,
    },
    timeoutMs: 2_700_000, // 45 min
  });

  if (status !== 200 || data?.status !== "ok") {
    fail(`ACA deploy failed (${status}): ${JSON.stringify(data)}`);
  }

  // Log each step
  const steps = (data.steps || []) as Array<{ step: string; status: string; detail?: string }>;
  for (const step of steps) {
    const icon = step.status === "ok" ? "+" : step.status === "skipped" ? "-" : "!";
    log(`  [${icon}] ${step.step}${step.detail ? `: ${step.detail}` : ""}`);
  }

  log(`ACA runtime FQDN: ${data.runtime_fqdn}`);
  return data;
}

async function stepWaitForAcaRuntime(): Promise<void> {
  log("Waiting for ACA runtime to become ready via admin proxy ...");
  const deadline = Date.now() + 300_000; // 5 min -- ACA cold start can be slow

  while (Date.now() < deadline) {
    try {
      const res = await fetch(`${BASE_URL}/health`, { signal: AbortSignal.timeout(5_000) });
      if (res.ok) {
        log("Admin health OK (runtime proxied)");
        break;
      }
    } catch { /* not ready */ }
    await sleep(5_000);
  }

  // ACA cold start -- give extra time
  await sleep(10_000);
  log("ACA runtime health check passed");
}

async function stepChatProbe(): Promise<string> {
  log("Sending chat probe via WebSocket ...");
  const secret = await getAdminSecret();
  const wsUrl = secret
    ? `ws://localhost:${COMPOSE_ADMIN_PORT}/api/chat/ws?token=${secret}`
    : `ws://localhost:${COMPOSE_ADMIN_PORT}/api/chat/ws`;

  const deadline = Date.now() + 300_000; // 5 min -- ACA can be slower
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
      log(`Chat probe failed: ${lastError} -- retrying in 10s`);
    }
    await sleep(10_000);
  }
  fail(`Chat probe did not succeed within 300s. Last error: ${lastError}`);
}

function chatOnce(wsUrl: string): Promise<string> {
  return new Promise<string>((resolve, reject) => {
    const ws = new WebSocket(wsUrl);
    const timeout = setTimeout(() => {
      ws.close();
      reject(new Error("Chat response timed out after 90s"));
    }, 90_000);

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

async function stepDestroyAca(): Promise<void> {
  log("Destroying ACA deployment ...");
  try {
    const { status, data } = await api<Record<string, unknown>>("/api/setup/aca/destroy", {
      method: "POST",
      body: {},
      timeoutMs: 120_000,
    });
    log(`ACA destroy: ${status} ${JSON.stringify(data)}`);
  } catch (err: unknown) {
    const msg = err instanceof Error ? err.message : String(err);
    log(`ACA destroy failed (best-effort): ${msg}`);
  }
}

async function stepDecommissionFoundry(): Promise<void> {
  log(`Decommissioning Foundry: rg=${RG}`);
  try {
    const { status, data } = await api<Record<string, unknown>>("/api/setup/foundry/decommission", {
      method: "POST",
      body: { resource_group: RG },
      timeoutMs: 480_000,
    });
    log(`Decommission: ${status} ${JSON.stringify(data)}`);
  } catch (err: unknown) {
    const msg = err instanceof Error ? err.message : String(err);
    log(`Decommission failed (best-effort): ${msg}`);
  }
}

async function stepAcaRestart(): Promise<void> {
  log("Triggering ACA runtime restart ...");
  try {
    const { status, data } = await api<Record<string, unknown>>("/api/setup/container/restart", {
      method: "POST",
      body: {},
      timeoutMs: 60_000,
    });
    log(`ACA restart: ${status} ${JSON.stringify(data)}`);
  } catch (err: unknown) {
    const msg = err instanceof Error ? err.message : String(err);
    log(`ACA restart failed: ${msg}`);
    throw new Error(`ACA restart failed: ${msg}`);
  }
}

// ---------------------------------------------------------------------------
// Main exports
// ---------------------------------------------------------------------------

export async function runAcaHeadlessSetup(): Promise<void> {
  const startTime = Date.now();

  try {
    await stepBuildImages();
    await stepStartAdminOnly();
    await stepAzureCheck();
    await stepDeployFoundry();
    await stepDeployAca();
    await stepWaitForAcaRuntime();
    const probeText = await stepChatProbe();

    const elapsed = ((Date.now() - startTime) / 1000).toFixed(0);
    log("========================================");
    log(`ACA SETUP COMPLETE in ${elapsed}s`);
    log(`  Chat probe: ${probeText.slice(0, 100)}`);
    log("========================================");

    console.log(JSON.stringify({
      status: "ok",
      elapsed_seconds: parseInt(elapsed),
      probe_response: probeText.slice(0, 200),
      target: "aca",
    }));
  } catch (err) {
    const msg = err instanceof Error ? err.message : String(err);
    console.error(`\nFATAL: ACA Setup failed: ${msg}`);
    process.exit(1);
  }
}

export async function runAcaHeadlessDecommission(): Promise<void> {
  await stepDestroyAca();
  await stepDecommissionFoundry();
  // Stop local admin container
  log("Stopping admin container ...");
  await stopContainer("polyclaw-admin");
}

export async function runAcaHeadlessRestart(): Promise<void> {
  await stepAcaRestart();
}
