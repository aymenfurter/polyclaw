/**
 * Docker container status helpers.
 *
 * Queries `docker inspect` for the admin and runtime containers to
 * surface health and lifecycle state in the TUI.
 */

import { exec } from "../deploy/process.js";

// ---------------------------------------------------------------------------
// Types
// ---------------------------------------------------------------------------

export type ContainerHealth = "running" | "starting" | "stopped" | "not_found" | "error";

export interface ContainerStatus {
  name: string;
  health: ContainerHealth;
  /** Container uptime, e.g. "Up 3 minutes". Empty when not running. */
  uptime: string;
  /** Port mappings, e.g. "9090->9090/tcp". */
  ports: string;
}

export interface ContainerStatuses {
  admin: ContainerStatus;
  runtime: ContainerStatus;
}

// Well-known container names from docker-compose.yml
const ADMIN_CONTAINER = "polyclaw-admin";
const RUNTIME_CONTAINER = "polyclaw-runtime";

// ---------------------------------------------------------------------------
// Public API
// ---------------------------------------------------------------------------

/**
 * Query Docker for the status of both the admin and runtime containers.
 *
 * Uses `docker inspect` which is fast and does not require the daemon
 * to enumerate all containers. Falls back gracefully when Docker is
 * unavailable or the container does not exist.
 */
export async function getContainerStatuses(): Promise<ContainerStatuses> {
  const [admin, runtime] = await Promise.all([
    inspectContainer(ADMIN_CONTAINER),
    inspectContainer(RUNTIME_CONTAINER),
  ]);
  return { admin, runtime };
}

/**
 * Query Docker for a single container by name.
 *
 * Also accepts a container ID (e.g. from the startup sequence) which
 * may not match the well-known names above.
 */
export async function getContainerStatus(nameOrId: string): Promise<ContainerStatus> {
  return inspectContainer(nameOrId);
}

// ---------------------------------------------------------------------------
// Internal
// ---------------------------------------------------------------------------

async function inspectContainer(name: string): Promise<ContainerStatus> {
  const base: ContainerStatus = { name, health: "not_found", uptime: "", ports: "" };
  try {
    const { stdout, exitCode } = await exec([
      "docker", "inspect",
      "--format", "{{.State.Status}}|{{.State.StartedAt}}|{{.State.Running}}",
      name,
    ]);
    if (exitCode !== 0) return base;

    const parts = stdout.split("|");
    const stateStatus = (parts[0] || "").trim().toLowerCase();
    const running = (parts[2] || "").trim() === "true";

    if (running) {
      base.health = "running";
      base.uptime = formatUptime(parts[1] || "");
    } else if (stateStatus === "created" || stateStatus === "restarting") {
      base.health = "starting";
    } else {
      base.health = "stopped";
    }

    // Grab port mappings
    const portsResult = await exec([
      "docker", "port", name,
    ]);
    if (portsResult.exitCode === 0 && portsResult.stdout) {
      base.ports = portsResult.stdout
        .split("\n")
        .map((l) => l.trim())
        .filter(Boolean)
        .join(", ");
    }
  } catch {
    base.health = "error";
  }
  return base;
}

function formatUptime(startedAt: string): string {
  if (!startedAt || startedAt === "0001-01-01T00:00:00Z") return "";
  try {
    const started = new Date(startedAt);
    const now = new Date();
    const diffMs = now.getTime() - started.getTime();
    if (diffMs < 0) return "";

    const secs = Math.floor(diffMs / 1000);
    if (secs < 60) return `${secs}s`;
    const mins = Math.floor(secs / 60);
    if (mins < 60) return `${mins}m`;
    const hours = Math.floor(mins / 60);
    const remMins = mins % 60;
    if (hours < 24) return remMins > 0 ? `${hours}h ${remMins}m` : `${hours}h`;
    const days = Math.floor(hours / 24);
    const remHours = hours % 24;
    return remHours > 0 ? `${days}d ${remHours}h` : `${days}d`;
  } catch {
    return "";
  }
}
