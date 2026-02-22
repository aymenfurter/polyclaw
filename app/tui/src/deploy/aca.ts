/**
 * Azure Container Apps deployment target.
 *
 * Architecture: **local admin (permanent) + runtime deployed to ACA**.
 *
 * The TUI starts the admin container locally via ``docker compose up
 * admin``.  It calls ``POST /api/setup/aca/deploy`` which tags and pushes
 * the locally-built image to ACR, provisions ACA infrastructure, and
 * deploys the **runtime container only** to ACA with external ingress
 * restricted to the deployer's IP.
 *
 * The local admin stays running permanently and proxies ``/api/*``
 * requests to the ACA runtime via ``RUNTIME_URL``.  The admin is
 * **never** deployed to ACA.
 *
 * External communication (bots, channels) flows through the local
 * tunnel -> local admin -> ACA runtime.
 *
 * Prerequisites: ``az`` CLI installed and logged in, Docker running.
 */

import { resolve } from "path";
import type { DeployResult, LogStream } from "../config/types.js";
import type { DeployTarget } from "./target.js";
import { exec } from "./process.js";
import {
  buildImage,
  buildAcaImage,
  stopContainer,
  getAdminSecret,
  resolveKvSecret,
  waitForReady,
} from "./docker.js";

const PROJECT_ROOT = resolve(import.meta.dir, "../../../..");

// ---------------------------------------------------------------------------
// Preflight checks
// ---------------------------------------------------------------------------

export async function checkAzCliInstalled(): Promise<boolean> {
  try {
    const { exitCode } = await exec(["az", "version"]);
    return exitCode === 0;
  } catch {
    return false;
  }
}

export async function checkAzLoggedIn(): Promise<{ loggedIn: boolean; account?: string }> {
  try {
    const { stdout, exitCode } = await exec(["az", "account", "show", "--output", "json"]);
    if (exitCode !== 0) return { loggedIn: false };
    const data = JSON.parse(stdout);
    return {
      loggedIn: true,
      account: `${data.user?.name || "?"} (${data.name || data.id || "?"})`,
    };
  } catch {
    return { loggedIn: false };
  }
}

/**
 * Check whether the admin container is running and already has an ACA
 * deployment.  Returns a lightweight info object or ``null``.
 */
export async function getExistingDeployment(): Promise<{ appName: string; fqdn: string } | null> {
  try {
    let secret = await getAdminSecret();
    if (secret.startsWith("@kv:")) secret = await resolveKvSecret(secret);
    const headers: Record<string, string> = {};
    if (secret) headers["Authorization"] = `Bearer ${secret}`;
    const res = await fetch("http://localhost:9090/api/setup/aca/status", {
      headers,
      signal: AbortSignal.timeout(3000),
    });
    if (!res.ok) return null;
    const data = (await res.json()) as { deployed?: boolean; runtime_fqdn?: string };
    if (data.deployed && data.runtime_fqdn) {
      return { appName: "polyclaw-runtime", fqdn: data.runtime_fqdn };
    }
  } catch {
    /* admin not running or ACA not deployed */
  }
  return null;
}

// ---------------------------------------------------------------------------
// Remove deployment
// ---------------------------------------------------------------------------

export async function removeDeployment(onLine?: (line: string) => void): Promise<boolean> {
  const log = onLine || (() => {});

  // Try the admin's destroy endpoint first
  try {
    log("Requesting ACA deployment teardown from admin...");
    let secret = await getAdminSecret();
    if (secret.startsWith("@kv:")) secret = await resolveKvSecret(secret);
    const authHeaders: Record<string, string> = { "Content-Type": "application/json" };
    if (secret) authHeaders["Authorization"] = `Bearer ${secret}`;
    const res = await fetch("http://localhost:9090/api/setup/aca/destroy", {
      method: "POST",
      headers: authHeaders,
      body: "{}",
      signal: AbortSignal.timeout(120_000),
    });
    const data = (await res.json()) as {
      status: string;
      steps?: Array<{ step: string; status: string }>;
    };
    for (const step of data.steps || []) {
      log(`  ${step.step}: ${step.status}`);
    }
    log(data.status === "ok" ? "ACA deployment removed." : "Warning: some teardown steps may have failed.");
  } catch {
    log("Could not reach admin. Skipping ACA teardown.");
  }

  // Stop the local admin container
  log("Stopping admin container...");
  await stopContainer("polyclaw-admin");
  log("Done.");
  return true;
}

// ---------------------------------------------------------------------------
// ACA Deploy Target
// ---------------------------------------------------------------------------

export class AcaDeployTarget implements DeployTarget {
  readonly name = "Azure Container Apps";
  readonly lifecycleTied = false;

  private _secret = "";
  private _imageTag = "aca";

  constructor(private _reconnect = false) {}

  /** Build auth headers for admin API calls. */
  private _authHeaders(extra?: Record<string, string>): Record<string, string> {
    const h: Record<string, string> = { ...extra };
    if (this._secret) h["Authorization"] = `Bearer ${this._secret}`;
    return h;
  }

  /**
   * Deploy flow:
   * 1. Build image + start admin container locally
   * 2. Wait for admin to be healthy
   * 3. If reconnecting, check for existing ACA deployment
   * 4. Call POST /api/setup/aca/deploy on the local admin
   * 5. Admin handles: docker push to ACR, ACA infra, runtime container
   * 6. Return local admin URL (admin stays running permanently)
   */
  async deploy(
    adminPort: number,
    botPort: number,
    mode: string,
    onLine?: (line: string) => void,
  ): Promise<DeployResult> {
    const log = onLine || (() => {});
    const localUrl = "http://localhost:9090";

    // -- Step 1: Start local admin container --------------------------------
    if (this._reconnect) {
      log("Checking if admin container is already running...");
      const healthy = await waitForReady(localUrl, 5_000);
      if (!healthy) {
        log("Admin not running. Building images and starting...");
        // Build the native image for the local admin container
        const localOk = await buildImage(onLine);
        if (!localOk) throw new Error("Docker compose build failed");
        // Build the amd64 image for ACA runtime (pushed to ACR later)
        log("Building linux/amd64 image for ACA...");
        const acaOk = await buildAcaImage(this._imageTag, onLine);
        if (!acaOk) throw new Error("Docker build (linux/amd64) failed");
        await this._startAdminOnly(adminPort, botPort, mode);
      }
    } else {
      log("Building Docker image...");
      const localOk = await buildImage(onLine);
      if (!localOk) throw new Error("Docker compose build failed");

      log("Building linux/amd64 image for ACA...");
      const acaOk = await buildAcaImage(this._imageTag, onLine);
      if (!acaOk) throw new Error("Docker build (linux/amd64) failed");

      log("Starting local admin container...");
      await this._startAdminOnly(adminPort, botPort, mode);
    }

    // -- Step 2: Wait for local admin to be healthy -----------------------
    log("Waiting for local admin to be healthy...");
    const healthy = await waitForReady(localUrl, 120_000);
    if (!healthy) throw new Error("Admin container failed to start. Check: docker logs polyclaw-admin");
    log("Local admin is healthy.");

    // -- Fetch admin secret for authenticated API calls -------------------
    this._secret = await getAdminSecret();
    if (this._secret.startsWith("@kv:")) {
      log("Resolving admin secret from Key Vault...");
      this._secret = await resolveKvSecret(this._secret);
    }
    if (!this._secret) {
      log("WARNING: Could not read admin secret. API calls may fail.");
    }

    // -- Step 3: Check for existing ACA deployment (reconnect) ------------
    if (this._reconnect) {
      const existing = await this._checkExistingAca(localUrl, log);
      if (existing) {
        return { baseUrl: existing, instanceId: "polyclaw-admin", reconnected: true };
      }
      log("No existing ACA deployment. Deploying fresh...");
    }

    // -- Step 4: Trigger ACA deployment via local admin API ---------------
    log("Triggering ACA deployment (pushes pre-built image to ACR)...");
    log("This deploys the runtime to ACA. May take 30-40 minutes.");

    const deployRes = await fetch(`${localUrl}/api/setup/aca/deploy`, {
      method: "POST",
      headers: this._authHeaders({ "Content-Type": "application/json" }),
      body: JSON.stringify({
        resource_group: "polyclaw-rg",
        location: "eastus",
        runtime_port: 8080,
        admin_port: 9090,
        image_tag: this._imageTag,
      }),
      signal: AbortSignal.timeout(2_700_000), // 45 min
    });

    const result = (await deployRes.json()) as {
      status: string;
      message: string;
      runtime_fqdn?: string;
      steps?: Array<{ step: string; status: string; detail?: string }>;
      deploy_id?: string;
    };

    // Log each step
    for (const step of result.steps || []) {
      const icon = step.status === "ok" ? "+" : step.status === "skipped" ? "-" : "!";
      log(`  [${icon}] ${step.step}${step.detail ? `: ${step.detail}` : ""}`);
    }

    if (result.status !== "ok" || !result.runtime_fqdn) {
      throw new Error(`ACA deployment failed: ${result.message}`);
    }

    log(`ACA runtime (external, IP-whitelisted): https://${result.runtime_fqdn}`);
    log("Local admin stays running -- proxying to ACA runtime via RUNTIME_URL.");
    return { baseUrl: localUrl, instanceId: "polyclaw-admin", reconnected: false };
  }

  // -----------------------------------------------------------------------
  // Private helpers
  // -----------------------------------------------------------------------

  /** Start only the admin service from docker-compose.yml. */
  private async _startAdminOnly(
    _adminPort: number,
    _botPort: number,
    _mode: string,
  ): Promise<void> {
    // Stop any existing stack first
    try {
      await exec(["docker", "compose", "down", "--remove-orphans"], PROJECT_ROOT);
    } catch { /* may not be running */ }

    const { exitCode, stderr } = await exec(
      ["docker", "compose", "up", "-d", "admin"],
      PROJECT_ROOT,
    );
    if (exitCode !== 0) {
      throw new Error(`docker compose up admin failed (exit ${exitCode}): ${stderr}`);
    }
  }

  /** Check if ACA deployment exists and runtime is reachable. */
  private async _checkExistingAca(
    localUrl: string,
    log: (line: string) => void,
  ): Promise<string | null> {
    try {
      const res = await fetch(`${localUrl}/api/setup/aca/status`, {
        headers: this._authHeaders(),
        signal: AbortSignal.timeout(10_000),
      });
      if (!res.ok) return null;
      const data = (await res.json()) as {
        deployed?: boolean;
        runtime_fqdn?: string;
      };
      if (data.deployed && data.runtime_fqdn) {
        log(`Found existing ACA deployment: runtime at ${data.runtime_fqdn}`);
        log("Reconnecting -- local admin stays running.");
        return localUrl;
      }
    } catch { /* not deployed */ }
    return null;
  }

  streamLogs(_instanceId: string, _onLine: (line: string) => void): LogStream {
    // For ACA, we can't stream Docker logs. Return a no-op stream.
    return {
      stop: () => {},
    };
  }

  async waitForReady(baseUrl: string, timeoutMs?: number): Promise<boolean> {
    return waitForReady(baseUrl, timeoutMs);
  }

  async disconnect(_instanceId: string): Promise<void> {
    // ACA deployments are not tied to the TUI lifecycle.
    // Nothing to stop.
  }

  async getAdminSecret(_instanceId?: string): Promise<string> {
    return this._secret || getAdminSecret();
  }

  async resolveKvSecret(secret: string, instanceId?: string): Promise<string> {
    return resolveKvSecret(secret, instanceId);
  }
}
