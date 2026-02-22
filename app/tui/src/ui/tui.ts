/**
 * Main chat TUI -- the original single-screen terminal interface.
 *
 * Layout (top-to-bottom):
 *   Header  -- mascot + block logo + admin URL
 *   Info    -- progress bar during boot / status dots after boot
 *   Chat    -- scrollable message area (build output + chat messages)
 *   Input   -- text input for slash commands and chat
 *
 * Lifecycle:
 *   1. Deploy (build image / ACA deploy) -> output streamed to chat
 *   2. Wait for server readiness
 *   3. WebSocket chat connection + status polling
 */

import {
  createCliRenderer,
  type CliRenderer,
  BoxRenderable,
  TextRenderable,
  TextNodeRenderable,
  InputRenderable,
  InputRenderableEvents,
  ScrollBoxRenderable,
} from "@opentui/core";

import { Colors, LogoColors, ShadowColor, GradientColors } from "../utils/theme.js";
import { resetTerminal } from "../utils/terminal.js";
import { getContainerStatuses, type ContainerHealth } from "../utils/containers.js";
import { LOGO_TEXT, LOGO_DIVIDER, SPINNER_FRAMES, STARTUP_PHASES, STATUS_ITEMS, BAR_FILL, BAR_LIGHT, BAR_WIDTH, SLASH_COMMANDS, MAX_AC_VISIBLE } from "../config/constants.js";
import type { DeployTarget } from "../deploy/target.js";
import type { DeployResult, StatusResponse, ModelEntry, SessionPickerEntry } from "../config/types.js";
// AcaDeployTarget loaded on-demand via target-picker
import { createMascotLogoLines } from "./mascot.js";

// -----------------------------------------------------------------------
// Helpers (module-private)
// -----------------------------------------------------------------------

function setContent(renderable: unknown, text: string): void {
  try { (renderable as { content: string }).content = text; } catch { /* ignore */ }
}

function clearInput(input: unknown): void {
  try { (input as { clear(): void }).clear(); return; } catch { /* ignore */ }
  try { (input as { editBuffer: { clear(): void } }).editBuffer.clear(); return; } catch { /* ignore */ }
  try { (input as { buffer: { clear(): void } }).buffer.clear(); return; } catch { /* ignore */ }
  try { (input as { value: string }).value = ""; } catch { /* ignore */ }
}

// -----------------------------------------------------------------------
// Public entry
// -----------------------------------------------------------------------

export async function launchTUI(
  adminPort: number,
  _botPort: number,
  target: DeployTarget,
): Promise<void> {
  // Error logging (TUI garbles stderr)
  const errorLog = Bun.file("/tmp/polyclaw-cli-error.log");
  const logError = async (label: string, err: unknown) => {
    const msg = `[${new Date().toISOString()}] ${label}: ${err instanceof Error ? err.stack || err.message : String(err)}\n`;
    await Bun.write(errorLog, msg);
  };
  process.on("uncaughtException", (err) => { logError("uncaughtException", err); });
  process.on("unhandledRejection", (err) => { logError("unhandledRejection", err); });

  // -- Mutable state -------------------------------------------------------

  let baseUrl = `http://localhost:${adminPort}`;
  let secret = "";
  let containerId = "";
  let statusTimer: ReturnType<typeof setInterval> | null = null;
  let chatWs: WebSocket | null = null;
  let exiting = false;
  let thinkingTimer: ReturnType<typeof setInterval> | null = null;
  let thinkingFrame = 0;
  let browserOpened = false;

  // Autocomplete
  let acActive = false;
  let acIndex = 0;
  let acFiltered: typeof SLASH_COMMANDS = [];
  let acOverlay: BoxRenderable | null = null;
  let acItems: TextRenderable[] = [];

  // Model picker
  let pickerActive = false;
  let pickerIndex = 0;
  let pickerModels: ModelEntry[] = [];
  let pickerOverlay: BoxRenderable | null = null;
  let pickerItems: TextRenderable[] = [];

  // Session picker
  let sessionPickerActive = false;
  let sessionPickerIndex = 0;
  let sessionPickerEntries: SessionPickerEntry[] = [];
  let sessionPickerOverlay: BoxRenderable | null = null;
  let sessionPickerItems: TextRenderable[] = [];

  // -- Shutdown -------------------------------------------------------------

  const shutdown = async () => {
    if (exiting) return;
    exiting = true;
    if (statusTimer) clearInterval(statusTimer);
    if (thinkingTimer) clearInterval(thinkingTimer);
    stopShimmer();
    chatWs?.close();
    try { renderer.stop(); } catch { /* ignore */ }
    try { renderer.destroy(); } catch { /* ignore */ }
    if (containerId) await target.disconnect(containerId);
    resetTerminal();
    process.exit(0);
  };

  const safeExit = () => {
    try { renderer.stop(); } catch { /* ignore */ }
    try { renderer.destroy(); } catch { /* ignore */ }
    resetTerminal();
  };
  process.on("exit", safeExit);
  process.on("SIGTERM", () => { shutdown(); });
  process.on("SIGINT", () => { shutdown(); });

  // -- Renderer -------------------------------------------------------------

  const renderer: CliRenderer = await createCliRenderer({
    exitOnCtrlC: false,
    targetFps: 30,
    prependInputHandlers: [
      (sequence: string) => {
        try {
          if (sequence === "\x03") { setImmediate(() => shutdown()); return true; }

          // Autocomplete
          if (acActive) {
            if (sequence === "\x1b[A") { acIndex = Math.max(0, acIndex - 1); refreshAcDisplay(); return true; }
            if (sequence === "\x1b[B") { acIndex = Math.min(acFiltered.length - 1, acIndex + 1); refreshAcDisplay(); return true; }
            if (sequence === "\t") { acceptAutocomplete(); return true; }
            if (sequence === "\x1b" || sequence === "\x1b\x1b") { hideAutocomplete(); return true; }
          }

          // Model picker
          if (pickerActive) {
            if (sequence === "\x1b[A") { pickerIndex = Math.max(0, pickerIndex - 1); refreshPickerDisplay(); return true; }
            if (sequence === "\x1b[B") { pickerIndex = Math.min(pickerModels.length - 1, pickerIndex + 1); refreshPickerDisplay(); return true; }
            if (sequence === "\r" || sequence === "\n") {
              const selected = pickerModels[pickerIndex];
              if (selected && selected.id !== currentModelName) {
                if (chatWs && chatWs.readyState === WebSocket.OPEN) {
                  chatWs.send(JSON.stringify({ action: "send", message: `/model ${selected.id}` }));
                  addMessage("system", `Switching to ${selected.name || selected.id}...`, "#FFD700");
                }
              }
              closeModelPicker();
              return true;
            }
            if (sequence === "\x1b" || sequence === "\x1b\x1b") { closeModelPicker(); return true; }
            return true;
          }

          // Session picker
          if (sessionPickerActive) {
            if (sequence === "\x1b[A") { sessionPickerIndex = Math.max(0, sessionPickerIndex - 1); refreshSessionPickerDisplay(); return true; }
            if (sequence === "\x1b[B") { sessionPickerIndex = Math.min(sessionPickerEntries.length - 1, sessionPickerIndex + 1); refreshSessionPickerDisplay(); return true; }
            if (sequence === "\r" || sequence === "\n") {
              const entry = sessionPickerEntries[sessionPickerIndex];
              if (entry) {
                if (entry.id === "__new__") {
                  if (chatWs && chatWs.readyState === WebSocket.OPEN) {
                    chatWs.send(JSON.stringify({ action: "new_session" }));
                    addMessage("system", "New session started.", Colors.muted);
                  }
                } else {
                  if (chatWs && chatWs.readyState === WebSocket.OPEN) {
                    chatWs.send(JSON.stringify({ action: "resume_session", session_id: entry.id }));
                    addMessage("system", `Resuming session ${entry.id}...`, "#FFD700");
                  }
                }
              }
              closeSessionPicker();
              return true;
            }
            if (sequence === "\x1b" || sequence === "\x1b\x1b") { closeSessionPicker(); return true; }
            return true;
          }

          // Instant approval on single y/n keypress (no Enter needed)
          if (approvalQueue.length > 0 && !pickerActive && !sessionPickerActive) {
            const lower = sequence.toLowerCase();
            if (lower === "y" || lower === "n") {
              const pending = approvalQueue[0];
              const approved = lower === "y";
              if (chatWs && chatWs.readyState === WebSocket.OPEN) {
                chatWs.send(JSON.stringify({ action: "approve_tool", call_id: pending.call_id, response: approved ? "y" : "n" }));
              }
              addMessage("user", approved ? "Approved" : "Denied", approved ? Colors.green : Colors.red);
              clearInput(chatInput);
              return true;
            }
            // Block all other keys while approval is pending (except Ctrl+C handled above)
            return true;
          }

          // Schedule autocomplete update after key is processed
          if (!pickerActive && !sessionPickerActive) {
            // Tab toggles log panel when autocomplete is not active
            if (sequence === "\t") { toggleLogPanel(); return true; }
            setTimeout(updateAutocomplete, 0);
          }
          return false;
        } catch { return false; }
      },
    ],
  });

  renderer.setBackgroundColor(Colors.bg);

  // -- Root container -------------------------------------------------------

  const root = new BoxRenderable(renderer, { id: "root", flexDirection: "column", width: "100%", height: "100%" });
  renderer.root.add(root);

  // -- Header ---------------------------------------------------------------

  const headerBox = new BoxRenderable(renderer, {
    id: "header",
    height: 10,
    border: true,
    borderColor: "#B8860B",
    paddingLeft: 2,
    paddingRight: 2,
    paddingTop: 1,
    flexDirection: "column",
  });
  root.add(headerBox);

  const mascotLines = createMascotLogoLines(renderer, LOGO_TEXT, LogoColors);
  for (const line of mascotLines) headerBox.add(line);

  headerBox.add(new TextRenderable(renderer, { id: "logo-shadow", content: LOGO_DIVIDER, fg: ShadowColor }));
  const headerUrl = new TextRenderable(renderer, { id: "header-url", content: `  ${baseUrl}`, fg: Colors.muted });
  headerBox.add(headerUrl);

  // -- Info bar (progress / status dots) -----------------------------------

  const infoBox = new BoxRenderable(renderer, {
    id: "info-bar",
    height: 3,
    border: true,
    borderColor: "#5C4400",
    paddingLeft: 2,
    paddingRight: 2,
    flexDirection: "column",
    justifyContent: "center",
  });
  root.add(infoBox);

  // Progress bar state
  let bootComplete = false;
  let currentModelName = "gpt-4.1";
  let activityText = "";
  let activeTools: string[] = [];
  let gradientIdx = 0;

  const phaseState: Record<string, boolean> = {};
  for (const p of STARTUP_PHASES) phaseState[p.key] = false;

  // Gold shimmer colors for the progress bar (dark -> bright -> white -> bright -> dark)
  const SHIMMER_COLORS = [
    "#7A6520", "#8B6914", "#9B7424", "#AB8034", "#B8860B",
    "#CB9854", "#DAA520", "#E8B830", "#F0C840", "#F5D550",
    "#FFE870", "#FFED80", "#FFF2A0", "#FFFDE0", "#FFFFFF",
    "#FFFDE0", "#FFF2A0", "#FFED80", "#FFE870", "#F5D550",
    "#F0C840", "#E8B830", "#DAA520", "#CB9854", "#B8860B",
    "#AB8034", "#9B7424", "#8B6914", "#7A6520",
  ];
  const SHIMMER_WIDTH = SHIMMER_COLORS.length;
  let shimmerOffset = 0;
  let shimmerTimer: ReturnType<typeof setInterval> | null = null;

  // Pre-create the progress bar with fixed TextNodeRenderable children.
  // Layout: [filled_0] [filled_1] ... [filled_BAR_WIDTH-1] [empty] [suffix]
  const progressBar = new TextRenderable(renderer, { id: "progress-bar", content: "", fg: "#5C4400" });

  const barFilledNodes: TextNodeRenderable[] = [];
  for (let i = 0; i < BAR_WIDTH; i++) {
    const node = new TextNodeRenderable({ id: `pb-f-${i}`, fg: "#5C4400" });
    node.children = [BAR_LIGHT];
    progressBar.add(node);
    barFilledNodes.push(node);
  }
  const barSuffixNode = new TextNodeRenderable({ id: "pb-s", fg: Colors.muted });
  barSuffixNode.children = [" 0%"];
  progressBar.add(barSuffixNode);

  infoBox.add(progressBar);

  function refreshShimmerBar(): void {
    if (bootComplete) return;
    const okCount = STARTUP_PHASES.filter((s) => phaseState[s.key]).length;
    const total = STARTUP_PHASES.length;
    const filled = Math.round((okCount / total) * BAR_WIDTH);

    // Update each bar position
    for (let i = 0; i < BAR_WIDTH; i++) {
      if (i < filled) {
        const colorIdx = ((i - shimmerOffset) % SHIMMER_WIDTH + SHIMMER_WIDTH) % SHIMMER_WIDTH;
        barFilledNodes[i].children = [BAR_FILL];
        try { (barFilledNodes[i] as unknown as { fg: string }).fg = SHIMMER_COLORS[colorIdx]; } catch { /* ignore */ }
      } else {
        barFilledNodes[i].children = [BAR_LIGHT];
        try { (barFilledNodes[i] as unknown as { fg: string }).fg = "#5C4400"; } catch { /* ignore */ }
      }
    }

    // Update suffix
    const pending = STARTUP_PHASES.filter((s) => !phaseState[s.key]).map((s) => s.label);
    const pct = Math.round((okCount / total) * 100);
    let suffix = ` ${pct}%`;
    if (activityText) suffix += `  ${activityText}...`;
    else if (pending.length > 0) suffix += `  ${pending.slice(0, 3).join(", ")}${pending.length > 3 ? "..." : ""}`;
    else suffix += "  All systems go";
    barSuffixNode.children = [suffix];

    renderer.requestRender();
  }

  function startShimmer(): void {
    if (shimmerTimer) return;
    shimmerTimer = setInterval(() => {
      shimmerOffset = (shimmerOffset + 1) % SHIMMER_WIDTH;
      refreshShimmerBar();
    }, 60);
  }

  function stopShimmer(): void {
    if (shimmerTimer) { clearInterval(shimmerTimer); shimmerTimer = null; }
  }

  refreshShimmerBar();
  startShimmer();

  // Post-boot status dots
  const statusRow = new BoxRenderable(renderer, { id: "status-row", flexDirection: "row", width: "100%" });
  const statusDots: Record<string, TextRenderable> = {};
  for (const item of STATUS_ITEMS) {
    const dot = new TextRenderable(renderer, { id: `dot-${item.key}`, content: `● ${item.label}  `, fg: Colors.red });
    statusRow.add(dot);
    statusDots[item.key] = dot;
  }
  // Container health indicators (admin + runtime)
  const containerDots: Record<string, TextRenderable> = {};
  for (const ctr of [{ key: "ctr-admin", label: "Admin" }, { key: "ctr-runtime", label: "Runtime" }]) {
    const dot = new TextRenderable(renderer, { id: `dot-${ctr.key}`, content: `● ${ctr.label}  `, fg: Colors.dim });
    statusRow.add(dot);
    containerDots[ctr.key] = dot;
  }
  const targetIndicator = new TextRenderable(renderer, { id: "target-indicator", content: `│ ${target.name} `, fg: Colors.purple });
  statusRow.add(targetIndicator);
  const modelActivity = new TextRenderable(renderer, { id: "model-activity", content: `│ ${currentModelName}`, fg: "#FFD700" });
  statusRow.add(modelActivity);

  function switchToStatusDots(): void {
    if (bootComplete) return;
    bootComplete = true;
    stopShimmer();
    try { infoBox.remove("progress-bar"); progressBar.destroyRecursively(); } catch { /* ignore */ }
    for (const item of STATUS_ITEMS) {
      try { (statusDots[item.key] as unknown as { fg: string }).fg = phaseState[item.key] ? Colors.green : Colors.red; } catch { /* ignore */ }
    }
    infoBox.add(statusRow);
    renderer.requestRender();
  }

  function refreshProgressBar(): void {
    if (bootComplete) return;
    refreshShimmerBar();
  }

  function markPhase(key: string, done: boolean): void {
    phaseState[key] = done;
    refreshProgressBar();
  }

  function updateModelActivity(): void {
    let text = `│ ${currentModelName}`;
    if (activeTools.length > 0) {
      text += `  ${SPINNER_FRAMES[thinkingFrame]} ${activeTools[activeTools.length - 1]}...`;
    } else if (activityText) {
      text += `  ${SPINNER_FRAMES[thinkingFrame]} ${activityText}`;
    }
    setContent(modelActivity, text);
    try {
      (modelActivity as unknown as { fg: string }).fg = (activeTools.length > 0 || activityText)
        ? GradientColors[gradientIdx] : "#FFD700";
    } catch { /* ignore */ }
    renderer.requestRender();
  }

  function refreshInfoBar(): void {
    if (bootComplete) updateModelActivity();
    else refreshProgressBar();
  }

  function startThinking(): void {
    if (thinkingTimer) return;
    thinkingFrame = 0;
    gradientIdx = 0;
    activityText = "Thinking";
    refreshInfoBar();
    thinkingTimer = setInterval(() => {
      thinkingFrame = (thinkingFrame + 1) % SPINNER_FRAMES.length;
      gradientIdx = (gradientIdx + 1) % GradientColors.length;
      refreshInfoBar();
    }, 80);
  }

  function stopThinking(): void {
    if (thinkingTimer) { clearInterval(thinkingTimer); thinkingTimer = null; }
    activityText = "";
    activeTools = [];
    refreshInfoBar();
  }

  function updateStatusDots(s: StatusResponse): void {
    const states: Record<string, boolean> = {
      azure: s.azure?.logged_in ?? false,
      github: s.copilot?.authenticated ?? false,
      tunnel: s.tunnel?.active ?? false,
      bot: s.bot_configured ?? false,
    };
    for (const item of STATUS_ITEMS) {
      try { (statusDots[item.key] as unknown as { fg: string }).fg = states[item.key] ? Colors.green : Colors.red; } catch { /* ignore */ }
    }
    // Auto-open admin UI if auth needs attention
    if (!browserOpened && (!states.azure || !states.github)) {
      browserOpened = true;
      const adminUrl = secret ? `${baseUrl}/?secret=${secret}` : baseUrl;
      addMessage("system", `Opening admin UI for setup: ${adminUrl}`, Colors.muted);
      Bun.spawn(["open", adminUrl], { stdout: "ignore", stderr: "ignore" });
    }
    renderer.requestRender();
  }

  function containerHealthColor(h: ContainerHealth): string {
    if (h === "running") return Colors.green;
    if (h === "starting") return Colors.yellow;
    if (h === "stopped" || h === "error") return Colors.red;
    return Colors.dim;
  }

  async function refreshContainerDots(): Promise<void> {
    try {
      const cs = await getContainerStatuses();
      const adminColor = containerHealthColor(cs.admin.health);
      const runtimeColor = containerHealthColor(cs.runtime.health);
      try { (containerDots["ctr-admin"] as unknown as { fg: string }).fg = adminColor; } catch { /* ignore */ }
      try { (containerDots["ctr-runtime"] as unknown as { fg: string }).fg = runtimeColor; } catch { /* ignore */ }
      renderer.requestRender();
    } catch { /* Docker unavailable -- leave dots dim */ }
  }

  // -- Log panel (collapsible, above chat) --------------------------------

  let logExpanded = true;
  const logLines: string[] = [];

  const LOG_EXPANDED_HEIGHT = 20;

  const logPanel = new BoxRenderable(renderer, {
    id: "log-panel",
    height: LOG_EXPANDED_HEIGHT,
    border: true,
    borderColor: Colors.border,
    flexDirection: "column",
  });
  root.add(logPanel);

  const logPanelTitle = new TextRenderable(renderer, {
    id: "log-panel-title",
    content: "",
    fg: Colors.muted,
    height: 1,
  });
  const logTitleLabel = new TextNodeRenderable({ id: "log-title-label", fg: Colors.muted });
  logTitleLabel.children = [" Log  "];
  logPanelTitle.add(logTitleLabel);
  const logTitleHint = new TextNodeRenderable({ id: "log-title-hint", fg: Colors.dim });
  logTitleHint.children = ["(Tab to minimize)"];
  logPanelTitle.add(logTitleHint);
  logPanel.add(logPanelTitle);

  const logScroll = new ScrollBoxRenderable(renderer, {
    id: "log-scroll",
    flexGrow: 1,
    stickyScroll: true,
    stickyStart: "bottom",
    backgroundColor: Colors.surface,
    contentOptions: { paddingLeft: 1, paddingRight: 1 },
  });
  logPanel.add(logScroll);

  let logLineCounter = 0;

  /** Pick the message color based on log level. */
  function logLevelColor(level: string): string {
    const l = level.trim().toUpperCase();
    if (l === "ERROR" || l === "CRITICAL") return Colors.red;
    if (l === "WARNING" || l === "WARN") return Colors.yellow;
    return Colors.dim;
  }

  /**
   * Add a container log line to the log panel with colored segments:
   *   - Time portion is grey/muted
   *   - Message color depends on log level (red for ERROR, yellow for WARN, dim otherwise)
   */
  function addLogLine(raw: string): void {
    const id = `log-${++logLineCounter}`;

    // Try to parse a Python log line
    const m = raw.match(
      /^\d{4}-\d{2}-\d{2}\s+(\d{2}:\d{2}:\d{2}),?\d*\s+([\w.]+)\s+(DEBUG|INFO|WARNING|WARN|ERROR|CRITICAL)\s+(.*)/s,
    );

    if (!m) {
      // Non-log line — render as-is in dim
      logScroll.add(new TextRenderable(renderer, { id, content: raw, fg: Colors.dim }));
    } else {
      const time = m[1];
      const logger = m[2].split(".").pop() || m[2];
      const level = m[3];
      const msg = m[4];
      const msgColor = logLevelColor(level);

      const lineText = new TextRenderable(renderer, { id, content: "", fg: Colors.dim });

      const timeNode = new TextNodeRenderable({ id: `${id}-t`, fg: Colors.muted });
      timeNode.children = [`${time} `];
      lineText.add(timeNode);

      const bodyNode = new TextNodeRenderable({ id: `${id}-b`, fg: msgColor });
      bodyNode.children = [`${logger} ${level.padEnd(5)} ${msg}`];
      lineText.add(bodyNode);

      logScroll.add(lineText);
    }

    // Cap at 500 lines
    const children = logScroll.getChildren();
    while (children.length > 500) {
      const oldest = children.shift();
      if (oldest) { logScroll.remove(oldest.id); oldest.destroyRecursively(); }
    }

    logLines.push(raw);
    if (logLines.length > 500) logLines.splice(0, logLines.length - 500);

    renderer.requestRender();
  }

  function toggleLogPanel(): void {
    logExpanded = !logExpanded;
    if (logExpanded) {
      try { (logPanel as unknown as { height: number }).height = LOG_EXPANDED_HEIGHT; } catch { /* ignore */ }
      try { logPanel.add(logScroll); } catch { /* ignore — already added */ }
      logTitleLabel.children = [" Log  "];
      logTitleHint.children = ["(Tab to minimize)"];
    } else {
      try { (logPanel as unknown as { height: number }).height = 3; } catch { /* ignore */ }
      try { logPanel.remove("log-scroll"); } catch { /* ignore */ }
      const lineCount = logLines.length;
      logTitleLabel.children = [` Log (${lineCount} lines)  `];
      logTitleHint.children = ["(Tab to expand)"];
    }
    renderer.requestRender();
  }

  // -- Chat area ------------------------------------------------------------

  const chatArea = new ScrollBoxRenderable(renderer, {
    id: "chat-area",
    flexGrow: 1,
    stickyScroll: true,
    stickyStart: "bottom",
    border: true,
    borderColor: Colors.border,
    title: " Chat ",
    titleAlignment: "left",
    contentOptions: { paddingLeft: 1, paddingRight: 1 },
  });
  root.add(chatArea);

  // -- Input ----------------------------------------------------------------

  const inputBox = new BoxRenderable(renderer, {
    id: "input-box",
    height: 3,
    border: true,
    borderColor: Colors.accent,
    paddingLeft: 1,
    paddingRight: 1,
  });
  root.add(inputBox);

  const chatInput = new InputRenderable(renderer, {
    id: "chat-input",
    width: "100%",
    placeholder: "Waiting for server...  (Ctrl+C to quit)",
    focusedBackgroundColor: "#161B22",
    textColor: Colors.text,
    cursorColor: Colors.accent,
  });
  inputBox.add(chatInput);
  chatInput.focus();
  renderer.requestRender();

  // =====================================================================
  //  Autocomplete
  // =====================================================================

  function updateAutocomplete(): void {
    if (pickerActive || exiting) return;
    const val = chatInput.value;
    if (val.startsWith("/") && !val.includes(" ") && val.length > 0) {
      const query = val.toLowerCase();
      const filtered = SLASH_COMMANDS.filter((c) => c.cmd.startsWith(query));
      if (filtered.length > 0 && !(filtered.length === 1 && filtered[0].cmd === query)) {
        acFiltered = filtered;
        acIndex = Math.min(acIndex, filtered.length - 1);
        showAutocomplete();
        return;
      }
    }
    if (acActive) hideAutocomplete();
  }

  function showAutocomplete(): void {
    if (acOverlay) {
      try { root.remove("ac-overlay"); } catch { /* ignore */ }
      try { acOverlay.destroyRecursively(); } catch { /* ignore */ }
      acOverlay = null;
      acItems = [];
    }

    const visible = Math.min(acFiltered.length, MAX_AC_VISIBLE);
    const extra = acFiltered.length - visible;

    acOverlay = new BoxRenderable(renderer, {
      id: "ac-overlay",
      height: visible + (extra > 0 ? 1 : 0) + 2,
      border: true,
      borderColor: Colors.dim,
      paddingLeft: 1,
      paddingRight: 1,
      flexDirection: "column",
    });

    acItems = [];
    for (let i = 0; i < visible; i++) {
      const c = acFiltered[i];
      const selected = i === acIndex;
      const item = new TextRenderable(renderer, {
        id: `ac-item-${i}`,
        content: `${selected ? "\u25b6" : " "} ${c.cmd}  ${c.desc}`,
        fg: selected ? "#FFD700" : Colors.muted,
      });
      acOverlay.add(item);
      acItems.push(item);
    }

    if (extra > 0) {
      acOverlay.add(new TextRenderable(renderer, {
        id: "ac-more",
        content: `  ... ${extra} more (keep typing to filter)`,
        fg: Colors.dim,
      }));
    }

    try { root.remove("input-box"); } catch { /* ignore */ }
    root.add(acOverlay);
    root.add(inputBox);
    chatInput.focus();
    acActive = true;
    renderer.requestRender();
  }

  function hideAutocomplete(): void {
    acActive = false;
    acIndex = 0;
    if (acOverlay) {
      try { root.remove("ac-overlay"); } catch { /* ignore */ }
      try { acOverlay.destroyRecursively(); } catch { /* ignore */ }
      acOverlay = null;
      acItems = [];
      renderer.requestRender();
    }
  }

  function refreshAcDisplay(): void {
    for (let i = 0; i < acItems.length; i++) {
      const c = acFiltered[i];
      const selected = i === acIndex;
      try {
        setContent(acItems[i], `${selected ? "\u25b6" : " "} ${c.cmd}  ${c.desc}`);
        (acItems[i] as unknown as { fg: string }).fg = selected ? "#FFD700" : Colors.muted;
      } catch { /* ignore */ }
    }
    renderer.requestRender();
  }

  function acceptAutocomplete(): void {
    if (!acActive || acFiltered.length === 0) return;
    chatInput.value = acFiltered[acIndex].cmd;
    hideAutocomplete();
  }

  // =====================================================================
  //  Chat helpers
  // =====================================================================

  let msgCounter = 0;
  let currentReplyId: string | null = null;
  let currentReplyText = "";
  const pendingToolIds = new Map<string, string>();

  // -- HITL approval queue ------------------------------------------------
  interface PendingApproval { call_id: string; tool: string; arguments: string }
  const approvalQueue: PendingApproval[] = [];

  function addLine(text: string, color: string): string {
    const id = `msg-${++msgCounter}`;
    chatArea.add(new TextRenderable(renderer, { id, content: text, fg: color }));
    const children = chatArea.getChildren();
    while (children.length > 500) {
      const oldest = children.shift();
      if (oldest) { chatArea.remove(oldest.id); oldest.destroyRecursively(); }
    }
    renderer.requestRender();
    return id;
  }

  function addMessage(role: string, text: string, color: string): string {
    const prefixMap: Record<string, string> = { user: "You", assistant: "Bot", tool: "  [tool]", error: "  [error]", system: "  [system]" };
    return addLine(`${prefixMap[role] ?? role}: ${text}`, color);
  }

  function updateReply(text: string): void {
    if (!currentReplyId) return;
    const existing = chatArea.findDescendantById(currentReplyId);
    if (!existing) return;
    try {
      setContent(existing, `Bot: ${text}`);
      renderer.requestRender();
    } catch {
      chatArea.remove(currentReplyId);
      existing.destroyRecursively();
      chatArea.add(new TextRenderable(renderer, { id: currentReplyId, content: `Bot: ${text}`, fg: Colors.accent }));
      renderer.requestRender();
    }
  }

  // =====================================================================
  //  Model picker
  // =====================================================================

  function renderPickerItem(m: ModelEntry, index: number): string {
    const pointer = index === pickerIndex ? "\u25b6 " : "  ";
    const current = m.id === currentModelName ? " \u2713" : "";
    const cost = m.billing_multiplier && m.billing_multiplier !== 1.0 ? ` (${m.billing_multiplier}x)` : "";
    return `${pointer}${m.name || m.id}${current}${cost}`;
  }

  function refreshPickerDisplay(): void {
    for (let i = 0; i < pickerItems.length; i++) {
      try {
        setContent(pickerItems[i], renderPickerItem(pickerModels[i], i));
        (pickerItems[i] as unknown as { fg: string }).fg =
          i === pickerIndex ? "#FFD700" : pickerModels[i].id === currentModelName ? Colors.green : Colors.text;
      } catch { /* ignore */ }
    }
    renderer.requestRender();
  }

  function closeModelPicker(): void {
    pickerActive = false;
    if (pickerOverlay) {
      try { root.remove("model-picker-overlay"); } catch { /* ignore */ }
      try { pickerOverlay.destroyRecursively(); } catch { /* ignore */ }
      pickerOverlay = null;
      pickerItems = [];
    }
    chatInput.focus();
    renderer.requestRender();
  }

  async function openModelPicker(): Promise<void> {
    if (pickerActive) return;
    addMessage("system", "Loading models...", Colors.muted);
    try {
      const headers: Record<string, string> = {};
      if (secret) headers["Authorization"] = `Bearer ${secret}`;
      const res = await fetch(`${baseUrl}/api/models`, { headers, signal: AbortSignal.timeout(10000) });
      if (!res.ok) { addMessage("error", `Failed to fetch models (${res.status})`, Colors.red); return; }
      const data = await res.json() as { models?: ModelEntry[] };
      pickerModels = (data.models || []).filter((m) => !m.policy || m.policy === "enabled");
      if (pickerModels.length === 0) { addMessage("system", "No models available.", Colors.yellow); return; }
    } catch (err: unknown) {
      addMessage("error", `Failed to fetch models: ${err instanceof Error ? err.message : err}`, Colors.red);
      return;
    }

    pickerIndex = Math.max(0, pickerModels.findIndex((m) => m.id === currentModelName));
    pickerActive = true;
    const visibleCount = Math.min(pickerModels.length, 20);

    pickerOverlay = new BoxRenderable(renderer, {
      id: "model-picker-overlay",
      height: visibleCount + 4,
      border: true,
      borderColor: "#B8860B",
      paddingLeft: 2,
      paddingRight: 2,
      flexDirection: "column",
    });

    pickerOverlay.add(new TextRenderable(renderer, { id: "picker-title", content: "Select Model  (arrows to move, Enter to select, Esc to cancel)", fg: "#DAA520" }));
    pickerOverlay.add(new TextRenderable(renderer, { id: "picker-sep", content: "\u2500".repeat(60), fg: "#5C4400" }));

    pickerItems = [];
    for (let i = 0; i < pickerModels.length; i++) {
      const item = new TextRenderable(renderer, {
        id: `picker-item-${i}`,
        content: renderPickerItem(pickerModels[i], i),
        fg: i === pickerIndex ? "#FFD700" : pickerModels[i].id === currentModelName ? Colors.green : Colors.text,
      });
      pickerOverlay.add(item);
      pickerItems.push(item);
    }

    try { root.remove("input-box"); } catch { /* ignore */ }
    root.add(pickerOverlay);
    root.add(inputBox);
    renderer.requestRender();
  }

  // =====================================================================
  //  Session picker
  // =====================================================================

  function renderSessionPickerItem(entry: SessionPickerEntry, index: number): string {
    const pointer = index === sessionPickerIndex ? "\u25b6 " : "  ";
    return `${pointer}${entry.label}  ${entry.detail}`;
  }

  function refreshSessionPickerDisplay(): void {
    for (let i = 0; i < sessionPickerItems.length; i++) {
      try {
        setContent(sessionPickerItems[i], renderSessionPickerItem(sessionPickerEntries[i], i));
        (sessionPickerItems[i] as unknown as { fg: string }).fg =
          i === sessionPickerIndex ? "#FFD700" : sessionPickerEntries[i].id === "__new__" ? Colors.green : Colors.text;
      } catch { /* ignore */ }
    }
    renderer.requestRender();
  }

  function closeSessionPicker(): void {
    sessionPickerActive = false;
    if (sessionPickerOverlay) {
      try { root.remove("session-picker-overlay"); } catch { /* ignore */ }
      try { sessionPickerOverlay.destroyRecursively(); } catch { /* ignore */ }
      sessionPickerOverlay = null;
      sessionPickerItems = [];
    }
    chatInput.focus();
    renderer.requestRender();
  }

  async function openSessionPicker(): Promise<void> {
    if (sessionPickerActive || pickerActive) return;
    addMessage("system", "Loading sessions...", Colors.muted);
    try {
      const headers: Record<string, string> = {};
      if (secret) headers["Authorization"] = `Bearer ${secret}`;
      const res = await fetch(`${baseUrl}/api/sessions`, { headers, signal: AbortSignal.timeout(10000) });
      if (!res.ok) { addMessage("error", `Failed to fetch sessions (${res.status})`, Colors.red); return; }
      const sessions = (await res.json()) as Record<string, unknown>[];

      sessionPickerEntries = [{ id: "__new__", label: "+ New session", detail: "" }];
      for (const s of sessions.slice(0, 5)) {
        const started = s.started_at ? String(s.started_at).slice(0, 16).replace("T", " ") : "?";
        const model = String(s.model || "?");
        const count = s.message_count || 0;
        const fmsg = s.first_message as string | undefined;
        const preview = fmsg ? `"${fmsg.slice(0, 35)}${fmsg.length > 35 ? "..." : ""}"` : "(empty)";
        sessionPickerEntries.push({ id: String(s.id), label: `${started}  ${model}  (${count} msgs)`, detail: preview });
      }
      if (sessionPickerEntries.length === 1) { addMessage("system", "No sessions yet. Use /new to start one.", Colors.yellow); return; }
    } catch (err: unknown) {
      addMessage("error", `Failed to fetch sessions: ${err instanceof Error ? err.message : err}`, Colors.red);
      return;
    }

    sessionPickerActive = true;
    sessionPickerIndex = 0;

    sessionPickerOverlay = new BoxRenderable(renderer, {
      id: "session-picker-overlay",
      height: sessionPickerEntries.length + 4,
      border: true,
      borderColor: "#B8860B",
      paddingLeft: 2,
      paddingRight: 2,
      flexDirection: "column",
    });

    sessionPickerOverlay.add(new TextRenderable(renderer, { id: "session-picker-title", content: "Switch Session  (arrows to move, Enter to select, Esc to cancel)", fg: "#DAA520" }));
    sessionPickerOverlay.add(new TextRenderable(renderer, { id: "session-picker-sep", content: "\u2500".repeat(60), fg: "#5C4400" }));

    sessionPickerItems = [];
    for (let i = 0; i < sessionPickerEntries.length; i++) {
      const entry = sessionPickerEntries[i];
      const item = new TextRenderable(renderer, {
        id: `session-picker-item-${i}`,
        content: renderSessionPickerItem(entry, i),
        fg: i === sessionPickerIndex ? "#FFD700" : entry.id === "__new__" ? Colors.green : Colors.text,
      });
      sessionPickerOverlay.add(item);
      sessionPickerItems.push(item);
    }

    try { root.remove("input-box"); } catch { /* ignore */ }
    root.add(sessionPickerOverlay);
    root.add(inputBox);
    renderer.requestRender();
  }

  // =====================================================================
  //  Phase 1+2: Deploy
  // =====================================================================

  const deployLabel = target.lifecycleTied ? "Building polyclaw v3..." : `Deploying to ${target.name}...`;
  addLogLine(deployLabel);
  activityText = target.lifecycleTied ? "Building" : "Deploying";
  refreshProgressBar();

  let deployResult: DeployResult;
  try {
    deployResult = await target.deploy(adminPort, _botPort, "admin", (line) => addLogLine(line));
    baseUrl = deployResult.baseUrl;
    containerId = deployResult.instanceId;
    markPhase("build", true);
    markPhase("start", true);
    addLogLine(
      deployResult.reconnected
        ? `Reconnected to ${containerId}`
        : target.lifecycleTied
          ? `Compose stack started (admin + runtime)`
          : `Deployed to ${baseUrl}`,
    );
  } catch (err: unknown) {
    addLogLine(`Deploy failed: ${err instanceof Error ? err.message : err}`);
    addLogLine("Press Ctrl+C to exit.");
    await new Promise(() => {});
    return;
  }

  activityText = "";
  refreshProgressBar();
  addLogLine("");
  setContent(headerUrl, `  ${baseUrl}`);
  renderer.requestRender();

  // =====================================================================
  //  Phase 3: Stream logs & wait for server
  // =====================================================================

  addLogLine("Waiting for server...");
  activityText = "Waiting for server";
  refreshProgressBar();
  addLogLine("");

  let adminUrlFromLogs = "";
  /* logStream kept alive — stops when container is disconnected on shutdown */
  target.streamLogs(containerId, (line) => {
    addLogLine(line);
    const m = line.match(/Admin\s+UI:\s+(https?:\/\/\S+)/i);
    if (m) {
      adminUrlFromLogs = m[1].trim();
      const hadSecret = !!secret;
      try { const url = new URL(adminUrlFromLogs); secret = url.searchParams.get("secret") || ""; } catch { /* ignore */ }
      setContent(headerUrl, `  ${adminUrlFromLogs}`);
      renderer.requestRender();
      // If the secret was just discovered, reconnect the WebSocket so it
      // picks up the auth token instead of looping with 401.
      if (!hadSecret && secret && chatWs) {
        try { chatWs.close(); } catch { /* ignore */ }
      }
    }
    if (/Resolved.*secret.*Key Vault|azure.*logged.in/i.test(line)) markPhase("azure", true);
    if (/copilot.*authenticated|gh.*logged.in/i.test(line)) markPhase("github", true);
    if (/Tunnel started/i.test(line)) markPhase("tunnel", true);
    if (/Bot deployment completed|bot_deploy.*ok/i.test(line)) markPhase("bot", true);
  });

  const ready = await target.waitForReady(baseUrl);
  // Keep logStream running — continues feeding container output to the log panel

  if (!ready) {
    addLogLine("Server did not become ready.");
    addLogLine("Press Ctrl+C to exit.");
    if (target.lifecycleTied && containerId) await target.disconnect(containerId);
    await new Promise(() => {});
    return;
  }

  addLogLine("Server is ready!");
  markPhase("server", true);
  activityText = "";
  switchToStatusDots();

  // Minimize the log panel now that boot is complete
  if (logExpanded) toggleLogPanel();

  // Obtain the admin secret.  For non-lifecycle targets (ACA) the deploy
  // target already resolved it -- ask it directly before falling back to
  // parsing docker logs.
  if (!secret) {
    try {
      secret = await target.getAdminSecret(containerId);
    } catch { /* not available via target */ }
  }
  // Fallback: wait for the secret to appear in the log stream (the
  // entrypoint prints "Admin UI: http://...?secret=XXX").
  if (!secret) {
    for (let attempt = 0; attempt < 15 && !secret; attempt++) {
      await Bun.sleep(1000);
    }
    if (!secret) {
      addLine("Warning: Admin secret not found in logs. Status polling may fail (401).", Colors.yellow);
    }
  }

  // Update header URL to include the secret so `/setup` link works
  if (secret) {
    setContent(headerUrl, `  ${baseUrl}/?secret=${secret}`);
    renderer.requestRender();
  }

  // Update input placeholder
  const exitHint = target.lifecycleTied ? "Ctrl+C to quit" : "Ctrl+C to disconnect (container keeps running)";
  try {
    (chatInput as unknown as { placeholder: string }).placeholder = `Type a message and press Enter  (${exitHint})`;
  } catch { /* ignore */ }
  renderer.requestRender();

  // =====================================================================
  //  Phase 5: WebSocket chat
  // =====================================================================

  function connectChat(): void {
    if (exiting) return;
    const wsBase = baseUrl.replace(/^https:/, "wss:").replace(/^http:/, "ws:");
    const wsUrl = secret ? `${wsBase}/api/chat/ws?token=${secret}` : `${wsBase}/api/chat/ws`;
    chatWs = new WebSocket(wsUrl);

    chatWs.onopen = () => { addMessage("system", "Connected", Colors.muted); };

    chatWs.onmessage = (ev) => {
      let data: Record<string, unknown>;
      try { data = JSON.parse(String(ev.data)); } catch { return; }

      switch (data.type) {
        case "delta":
          if (data.content) {
            if (!currentReplyId) { stopThinking(); currentReplyText = ""; currentReplyId = addMessage("assistant", "", Colors.accent); }
            currentReplyText += data.content;
            updateReply(currentReplyText);
          }
          break;
        case "message":
          stopThinking();
          addMessage("assistant", String(data.content || "(no response)"), Colors.accent);
          currentReplyId = null; currentReplyText = "";
          break;
        case "done":
          stopThinking();
          currentReplyId = null; currentReplyText = "";
          break;
        case "event": {
          const evt = data.event;
          if (evt === "tool_start") {
            const tool = String(data.tool || "tool");
            const callId = String(data.call_id || "");
            activeTools.push(tool);
            if (callId) pendingToolIds.set(callId, tool);
            refreshInfoBar();
          } else if (evt === "tool_done") {
            const callId = String(data.call_id || "");
            const toolName = pendingToolIds.get(callId);
            if (toolName) { pendingToolIds.delete(callId); activeTools = activeTools.filter((t) => t !== toolName); }
            refreshInfoBar();
          } else if (evt === "approval_request") {
            const callId = String(data.call_id || "");
            const tool = String(data.tool || "unknown");
            const args = String(data.arguments || "");
            approvalQueue.push({ call_id: callId, tool, arguments: args });
            addMessage("system", "\n--- APPROVAL REQUIRED ---", Colors.yellow);
            addMessage("system", `Tool: ${tool}`, Colors.yellow);
            if (args) addMessage("system", `Args: ${args}`, Colors.muted);
            addMessage("system", "Press y to approve or n to deny", Colors.yellow);
            try {
              (chatInput as unknown as { placeholder: string }).placeholder = "[APPROVAL] press y = approve, n = deny";
            } catch { /* ignore */ }
          } else if (evt === "approval_resolved") {
            const callId = String(data.call_id || "");
            const approved = Boolean(data.approved);
            const idx = approvalQueue.findIndex((a) => a.call_id === callId);
            if (idx !== -1) approvalQueue.splice(idx, 1);
            addMessage("system", approved ? "Approved." : "Denied.", approved ? Colors.green : Colors.red);
            if (approvalQueue.length === 0) {
              try {
                const exitHint = target.lifecycleTied ? "Ctrl+C to quit" : "Ctrl+C to disconnect";
                (chatInput as unknown as { placeholder: string }).placeholder = `Type a message and press Enter  (${exitHint})`;
              } catch { /* ignore */ }
            }
          } else if (evt === "tool_denied") {
            addMessage("system", `Tool denied: ${String(data.tool || "")} -- ${String(data.reason || "")}`, Colors.red);
          }
          break;
        }
        case "system":
          addMessage("system", String(data.content || ""), Colors.muted);
          break;
        case "media":
          if (Array.isArray(data.files)) {
            for (const f of data.files as { kind?: string; name?: string }[]) {
              addMessage("system", `[${f.kind}] ${f.name}`, Colors.muted);
            }
          }
          break;
        case "cards":
          addMessage("system", `[${Array.isArray(data.cards) ? data.cards.length : 0} card(s)]`, Colors.muted);
          break;
        case "error":
          stopThinking();
          addMessage("error", String(data.content || "Unknown error"), Colors.red);
          currentReplyId = null; currentReplyText = "";
          break;
      }
    };

    chatWs.onclose = () => {
      if (exiting) return;
      stopThinking();
      addMessage("system", "Disconnected. Reconnecting...", Colors.muted);
      setTimeout(connectChat, 3000);
    };
    chatWs.onerror = () => {};
  }

  // -- Input handler -------------------------------------------------------

  chatInput.on(InputRenderableEvents.CHANGE, (value: unknown) => {
    if (acActive) hideAutocomplete();
    const text = String(value ?? "").trim();
    if (!text) return;

    if (!chatWs || chatWs.readyState !== WebSocket.OPEN) {
      addMessage("system", "Not connected", Colors.yellow);
      return;
    }

    // -- HITL approval interception ------------------------------------
    if (approvalQueue.length > 0) {
      const lower = text.toLowerCase();
      if (lower === "y" || lower === "yes" || lower === "n" || lower === "no") {
        const pending = approvalQueue[0];
        const approved = lower === "y" || lower === "yes";
        chatWs.send(JSON.stringify({ action: "approve_tool", call_id: pending.call_id, response: approved ? "y" : "n" }));
        addMessage("user", approved ? "Approved" : "Denied", approved ? Colors.green : Colors.red);
        clearInput(chatInput);
        return;
      }
    }

    if (text.toLowerCase() === "/quit" || text.toLowerCase() === "/exit") {
      addMessage("system", "Shutting down...", Colors.yellow);
      clearInput(chatInput);
      shutdown();
      return;
    }
    if (text.toLowerCase() === "/models") { clearInput(chatInput); openModelPicker(); return; }
    if (text.toLowerCase() === "/change") { clearInput(chatInput); openSessionPicker(); return; }

    chatWs.send(JSON.stringify({ action: "send", message: text }));
    addMessage("user", text, Colors.green);
    clearInput(chatInput);

    if (!text.startsWith("/")) { startThinking(); }
  });

  connectChat();

  // =====================================================================
  //  Phase 6: Status polling
  // =====================================================================

  async function refreshStatus(): Promise<void> {
    // Container health is independent of the API status endpoint.
    // Always update container dots even if the status fetch fails.
    if (bootComplete) refreshContainerDots();

    try {
      const headers: Record<string, string> = {};
      if (secret) headers["Authorization"] = `Bearer ${secret}`;
      const res = await fetch(`${baseUrl}/api/setup/status`, { headers, signal: AbortSignal.timeout(15000) });
      if (!res.ok) {
        await logError("refreshStatus", new Error(`status endpoint returned ${res.status}`));
        return;
      }
      const s = (await res.json()) as StatusResponse;
      if (s.model && s.model !== currentModelName) { currentModelName = s.model; refreshInfoBar(); }
      if (bootComplete) { updateStatusDots(s); }
      else {
        markPhase("azure", s.azure?.logged_in ?? false);
        markPhase("github", s.copilot?.authenticated ?? false);
        markPhase("tunnel", s.tunnel?.active ?? false);
        markPhase("bot", s.bot_configured ?? false);
      }
    } catch (err) { await logError("refreshStatus", err); }
  }

  refreshStatus();
  statusTimer = setInterval(refreshStatus, 5000);
}
