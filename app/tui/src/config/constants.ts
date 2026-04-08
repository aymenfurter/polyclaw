/**
 * Application-wide constants.
 *
 * Consolidates magic strings, logo art, command lists, and animation
 * data so they are defined once and referenced everywhere.
 */

import type { SlashCommand } from "./types.js";

// ---------------------------------------------------------------------------
// Block text logo: POLYCLAW
// ---------------------------------------------------------------------------

export const LOGO_TEXT = [
  "████   ███  █     █   █  ████ █      ███  █   █",
  "█   █ █   █ █     █   █ █     █     █   █ █   █",
  "████  █   █ █      █ █  █     █     █████ █ █ █",
  "█     █   █ █       █   █     █     █   █ █ █ █",
  "█      ███  █████   █    ████ █████ █   █  █ █ ",
] as const;

/** Decorative RPG-style divider rendered below the logo. */
export const LOGO_DIVIDER =
  "  ◆━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━◆  ";

// ---------------------------------------------------------------------------
// Mascot pixel art
// ---------------------------------------------------------------------------

/**
 * 14x10 pixel grid for the cat-in-crab-costume mascot.
 *
 * Pixel codes:
 *   0 = transparent
 *   1 = shell (red)
 *   3 = face (cream)
 *   4 = eyes (near-black)
 *   5 = inner ear (pink)
 *   6 = antenna (orange)
 *   7 = mouth (dark red)
 *   8 = accent / blush (pink)
 */
export const MASCOT_GRID = [
  "00060000060000",
  "00006000060000",
  "00011111111000",
  "00151111115100",
  "01113333331110",
  "01134433443110",
  "01183333338110",
  "00133388333100",
  "00013333331000",
  "00001111110000",
] as const;

export const MASCOT_PALETTE: Record<number, string> = {
  1: "#D03030",   // shell (red)
  3: "#F5E6D2",   // face (cream)
  4: "#1E1414",   // eyes (near-black)
  5: "#FFA0A0",   // inner ear (pink)
  6: "#FF6B20",   // antenna (orange)
  7: "#783737",   // mouth (dark red)
  8: "#FF8296",   // accent / blush (pink)
};

// ---------------------------------------------------------------------------
// Spinner animation
// ---------------------------------------------------------------------------

export const SPINNER_FRAMES = [
  "\u280B", "\u2819", "\u2839", "\u2838",
  "\u283C", "\u2834", "\u2826", "\u2827",
  "\u2807", "\u280F",
] as const;

// ---------------------------------------------------------------------------
// Startup phases (progress bar)
// ---------------------------------------------------------------------------

export const STARTUP_PHASES = [
  { key: "build",  label: "Build" },
  { key: "start",  label: "Container" },
  { key: "server", label: "Server" },
  { key: "azure",  label: "Azure" },
  { key: "tunnel", label: "Tunnel" },
  { key: "bot",    label: "Bot" },
] as const;

export const STATUS_ITEMS = [
  { key: "azure",  label: "Azure" },
  { key: "tunnel", label: "Tunnel" },
  { key: "bot",    label: "Bot" },
] as const;

// Progress bar characters
export const BAR_FILL  = "\u2588"; // █
export const BAR_LIGHT = "\u2500"; // ─
export const BAR_WIDTH = 48;

// ---------------------------------------------------------------------------
// Autocomplete max visible items
// ---------------------------------------------------------------------------

export const MAX_AC_VISIBLE = 10;

// ---------------------------------------------------------------------------
// Slash commands
// ---------------------------------------------------------------------------

export const SLASH_COMMANDS: SlashCommand[] = [
  { cmd: "/new",         desc: "Start a new session" },
  { cmd: "/model",       desc: "Switch AI model" },
  { cmd: "/models",      desc: "List available models" },
  { cmd: "/status",      desc: "System status" },
  { cmd: "/session",     desc: "Current session info" },
  { cmd: "/sessions",    desc: "List recent sessions" },
  { cmd: "/config",      desc: "View/set runtime config" },
  { cmd: "/clear",       desc: "Clear all memory" },
  { cmd: "/help",        desc: "Show all commands" },
  { cmd: "/skills",      desc: "List installed skills" },
  { cmd: "/addskill",    desc: "Install a skill" },
  { cmd: "/removeskill", desc: "Remove a skill" },
  { cmd: "/plugins",     desc: "List plugins" },
  { cmd: "/plugin",      desc: "Enable/disable a plugin" },
  { cmd: "/mcp",         desc: "Manage MCP servers" },
  { cmd: "/schedules",   desc: "List scheduled tasks" },
  { cmd: "/schedule",    desc: "Create/remove tasks" },
  { cmd: "/profile",     desc: "Agent profile" },
  { cmd: "/channels",    desc: "Channel config" },
  { cmd: "/preflight",   desc: "Run security checks" },
  { cmd: "/phone",       desc: "Set voice target number" },
  { cmd: "/call",        desc: "Call configured number" },
  { cmd: "/change",      desc: "Switch to a recent session" },
  { cmd: "/quit",        desc: "Shut down and exit" },
  { cmd: "/exit",        desc: "Shut down and exit" },
];

// ---------------------------------------------------------------------------
// Disclaimer persistence flag
// ---------------------------------------------------------------------------

export const DISCLAIMER_FLAG = `${process.env.HOME || "/tmp"}/.polyclaw_disclaimer_accepted`;

// Tab labels for the main TUI (component-based mode)
export const TAB_LABELS = [
  "Dashboard",
  "Setup",
  "Chat",
  "Sessions",
  "Skills",
  "Plugins",
  "MCP",
  "Schedules",
  "Proactive",
  "Profile",
  "Workspace",
] as const;
