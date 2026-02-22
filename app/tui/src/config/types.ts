/**
 * Shared type definitions used across the TUI.
 */

// ---------------------------------------------------------------------------
// Server status (returned by /api/setup/status)
// ---------------------------------------------------------------------------

export interface StatusResponse {
  azure?: { logged_in?: boolean; user?: string; subscription?: string };
  copilot?: { authenticated?: boolean; details?: string };
  prerequisites_configured?: boolean;
  telegram_configured?: boolean;
  tunnel?: { active?: boolean; url?: string };
  bot_configured?: boolean;
  voice_call_configured?: boolean;
  model?: string;
}

// ---------------------------------------------------------------------------
// Deployment
// ---------------------------------------------------------------------------

export interface DeployResult {
  /** Base URL for the admin server. */
  baseUrl: string;
  /** Opaque identifier for the running instance. */
  instanceId: string;
  /** Whether this is a reconnection to an existing deployment. */
  reconnected: boolean;
}

export interface LogStream {
  stop: () => void;
}

// ---------------------------------------------------------------------------
// Slash commands (autocomplete data)
// ---------------------------------------------------------------------------

export interface SlashCommand {
  cmd: string;
  desc: string;
}

// ---------------------------------------------------------------------------
// Models
// ---------------------------------------------------------------------------

export interface ModelEntry {
  id: string;
  name: string;
  billing_multiplier?: number;
  reasoning_efforts?: string[] | null;
  policy?: string;
}

// ---------------------------------------------------------------------------
// Session picker
// ---------------------------------------------------------------------------

export interface SessionPickerEntry {
  id: string;
  label: string;
  detail: string;
}

// ---------------------------------------------------------------------------
// ACA config
// ---------------------------------------------------------------------------

export interface AcaConfig {
  deployId: string;
  deployTag: string;
  resourceGroup: string;
  location: string;
  acrName: string;
  acrLoginServer: string;
  environmentName: string;
  appName: string;
  /** FQDN of the remote ACA runtime container. */
  fqdn: string;
  storageAccountName: string;
  /** NFS share name (used by ACA runtime). */
  storageShareName: string;
  /** SMB share name (used by local admin Docker container). */
  smbShareName?: string;
  vnetName: string;
  subnetName: string;
  adminPort: number;
  botPort: number;
  adminSecret: string;
  /** Azure Storage account key for mounting SMB share locally. */
  storageKey?: string;
  /** Managed identity resource ID for the runtime container. */
  miId?: string;
  /** Managed identity client ID for the runtime container. */
  miClientId?: string;
  lastDeployed: string;
}
