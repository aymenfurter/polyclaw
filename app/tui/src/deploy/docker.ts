/**
 * Local Docker deployment target.
 *
 * Uses `docker compose` to run the two-container split (admin + runtime)
 * defined in docker-compose.yml.  Credential isolation:
 *   - Admin: HOME = /admin-home (polyclaw-admin-home volume)
 *   - Runtime: HOME = /runtime-home (ephemeral) with scoped SP
 *   - Shared: /data (polyclaw-data volume)
 *
 * The container lifecycle is tied to the CLI process -- `docker compose
 * down` is called on exit.
 */

import { resolve } from "path";
import type { DeployResult, LogStream } from "../config/types.js";
import type { DeployTarget } from "./target.js";
import { exec, execStream } from "./process.js";

/** Repository root -- two levels up from `app/tui/src/deploy/`. */
const PROJECT_ROOT = resolve(import.meta.dir, "../../../..");

/** Well-known container name from docker-compose.yml. */
const ADMIN_CONTAINER = "polyclaw-admin";

// ---------------------------------------------------------------------------
// Standalone functions (also used by the headless bot-only mode)
// ---------------------------------------------------------------------------

/**
 * Build the Docker image via `docker compose build`.
 *
 * When `onLine` is provided, stdout/stderr are piped and forwarded
 * line-by-line. Without it, output is inherited directly.
 */
export async function buildImage(
  onLine?: (line: string) => void,
): Promise<boolean> {
  return execStream(
    ["docker", "compose", "build"],
    onLine,
    PROJECT_ROOT,
  );
}

/**
 * Build a ``linux/amd64`` image tagged for ACA deployment.
 *
 * Uses ``docker build --platform linux/amd64`` to produce an image that
 * runs on ACA's x86-based infrastructure, even when building on an ARM
 * Mac.  The image is kept locally and pushed to ACR later by the admin
 * deployer (fast ``docker tag`` + ``docker push``).
 */
export async function buildAcaImage(
  tag: string,
  onLine?: (line: string) => void,
): Promise<boolean> {
  return execStream(
    [
      "docker", "build",
      "--platform", "linux/amd64",
      "-t", `polyclaw:${tag}`,
      ".",
    ],
    onLine,
    PROJECT_ROOT,
  );
}

/**
 * Stop and remove any existing compose stack.
 *
 * Runs `docker compose down` to clean up both admin and runtime containers.
 */
export async function killExisting(_adminPort?: number, _botPort?: number): Promise<void> {
  try {
    await exec(["docker", "compose", "down", "--remove-orphans"], PROJECT_ROOT);
  } catch {
    // Stack may not be running -- ignore
  }
}

/**
 * Start the two-container stack via `docker compose up -d`.
 *
 * Returns the admin container name as the instance identifier. The
 * admin container is the primary entry point; the runtime container
 * is started as a dependency.
 */
export async function startContainer(
  _adminPort: number,
  _botPort: number,
  _mode: string,
): Promise<string> {
  await killExisting();

  const { exitCode, stderr } = await exec(
    ["docker", "compose", "up", "-d"],
    PROJECT_ROOT,
  );
  if (exitCode !== 0) {
    throw new Error(`docker compose up failed (exit ${exitCode}): ${stderr}`);
  }
  return ADMIN_CONTAINER;
}

/**
 * Stop the compose stack.
 *
 * Accepts either a container name or any string -- always tears down
 * the full stack so both admin and runtime stop together.
 */
export async function stopContainer(_containerId: string): Promise<void> {
  try {
    await exec(["docker", "compose", "down"], PROJECT_ROOT);
  } catch {
    // May already be stopped
  }
}

/** Read the admin secret from the shared data volume.
 *
 * Prefers `docker exec` on the already-running admin container
 * (no extra image pull needed).  Falls back to `docker run alpine`
 * for backwards compatibility.
 */
export async function getAdminSecret(): Promise<string> {
  // Try the running admin container first -- fast and reliable.
  try {
    const { stdout, exitCode } = await exec([
      "docker", "exec", ADMIN_CONTAINER, "cat", "/data/.env",
    ]);
    if (exitCode === 0) {
      const match = stdout.match(/^ADMIN_SECRET=(.+)$/m);
      if (match) return match[1].replace(/"/g, "").trim();
    }
  } catch { /* container may not be running yet */ }

  // Fallback: ephemeral alpine container with volume mount.
  try {
    const { stdout, exitCode } = await exec([
      "docker", "run", "--rm",
      "-v", "polyclaw-data:/data",
      "alpine", "cat", "/data/.env",
    ]);
    if (exitCode !== 0) return "";
    const match = stdout.match(/^ADMIN_SECRET=(.+)$/m);
    return match ? match[1].replace(/"/g, "").trim() : "";
  } catch {
    return "";
  }
}

/**
 * Resolve a `@kv:...` secret reference.
 *
 * Uses `docker exec` on the admin container (which has KV credentials).
 * Falls back to `docker run` with a fresh image.
 */
export async function resolveKvSecret(
  secret: string,
  _containerId?: string,
): Promise<string> {
  if (!secret.startsWith("@kv:")) return secret;

  const script = [
    "import os, sys",
    "os.environ['POLYCLAW_DATA_DIR'] = '/data'",
    "from dotenv import load_dotenv",
    "load_dotenv('/data/.env', override=True)",
    "from polyclaw.services.keyvault import kv, is_kv_ref",
    "v = os.getenv('ADMIN_SECRET', '')",
    "if is_kv_ref(v):",
    "    print(kv.resolve_value(v), end='')",
    "else:",
    "    print(v, end='')",
  ].join("\n");

  // Try the admin container first (it has KV credentials)
  try {
    const { stdout, exitCode } = await exec([
      "docker", "exec", ADMIN_CONTAINER, "python", "-c", script,
    ]);
    if (exitCode === 0 && stdout) return stdout;
  } catch { /* fall through */ }

  // Fallback: ephemeral container with both volumes
  try {
    const { stdout, exitCode } = await exec([
      "docker", "run", "--rm",
      "-v", "polyclaw-admin-home:/admin-home",
      "-v", "polyclaw-data:/data",
      "-e", "HOME=/admin-home",
      "polyclaw", "python", "-c", script,
    ]);
    if (exitCode === 0 && stdout) return stdout;
  } catch { /* ignore */ }

  return "";
}

/**
 * Stream logs from both admin and runtime containers.
 *
 * Uses `docker compose logs -f` which interleaves output from all
 * services with service-name prefixes.
 */
export function streamContainerLogs(
  _containerId: string,
  onLine: (line: string) => void,
): LogStream {
  const proc = Bun.spawn(
    ["docker", "compose", "logs", "-f", "--tail", "200", "--no-color"],
    { cwd: PROJECT_ROOT, stdout: "pipe", stderr: "pipe" },
  );

  let stopped = false;

  const drain = async (stream: ReadableStream<Uint8Array> | null) => {
    if (!stream) return;
    const reader = stream.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    try {
      while (!stopped) {
        const { done, value } = await reader.read();
        if (done) break;
        buffer += decoder.decode(value, { stream: true });
        const lines = buffer.split("\n");
        buffer = lines.pop() || "";
        for (const line of lines) {
          if (line.trim()) onLine(line);
        }
      }
      if (buffer.trim()) onLine(buffer);
    } catch {
      // Process was killed
    }
  };

  drain(proc.stdout as ReadableStream<Uint8Array>);
  drain(proc.stderr as ReadableStream<Uint8Array>);

  return {
    stop() {
      stopped = true;
      try { proc.kill(); } catch { /* ignore */ }
    },
  };
}

/** Poll the health endpoint until it responds 200. */
export async function waitForReady(
  baseUrl: string,
  timeoutMs = 300_000,
): Promise<boolean> {
  const start = Date.now();
  while (Date.now() - start < timeoutMs) {
    try {
      const res = await fetch(`${baseUrl}/health`, {
        signal: AbortSignal.timeout(2000),
      });
      if (res.ok) return true;
    } catch {
      // Server not ready yet
    }
    await Bun.sleep(1500);
  }
  return false;
}

// ---------------------------------------------------------------------------
// DeployTarget implementation
// ---------------------------------------------------------------------------

export class DockerDeployTarget implements DeployTarget {
  readonly name = "Local Docker (Compose)";
  readonly lifecycleTied = true;

  async deploy(
    adminPort: number,
    botPort: number,
    mode: string,
    onLine?: (line: string) => void,
  ): Promise<DeployResult> {
    const buildOk = await buildImage(onLine);
    if (!buildOk) throw new Error("Docker build failed");

    const instanceId = await startContainer(adminPort, botPort, mode);
    // Admin listens on 9090 (docker-compose.yml), runtime on 8080
    return {
      baseUrl: `http://localhost:9090`,
      instanceId,
      reconnected: false,
    };
  }

  streamLogs(instanceId: string, onLine: (line: string) => void): LogStream {
    return streamContainerLogs(instanceId, onLine);
  }

  async waitForReady(baseUrl: string, timeoutMs?: number): Promise<boolean> {
    return waitForReady(baseUrl, timeoutMs);
  }

  async disconnect(instanceId: string): Promise<void> {
    await stopContainer(instanceId);
  }

  async getAdminSecret(_instanceId?: string): Promise<string> {
    return getAdminSecret();
  }

  async resolveKvSecret(secret: string, instanceId?: string): Promise<string> {
    return resolveKvSecret(secret, instanceId);
  }
}
