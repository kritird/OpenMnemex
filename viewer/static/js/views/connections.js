// Add agents screen (plan §6 screen 7; reworked per Kriti 2026-07-20, console framing
// same day): the Console is where users hook their coding agents up to Mnemex. Each
// detected-but-unconnected agent gets a one-click **Connect** that POSTs
// /api/agents/connect, which drives the SAME shared installer the CLI uses
// (mnx_install.install, user scope), then the screen re-renders with fresh detection.
// Claude Code is the special case: the PLUGIN route is recommended (richer — auto-hooks,
// full tier) and shown first with its in-Claude commands; MCP connect stays as the
// secondary option. Knowledge stays read-only; this writes only agent config, on click.

import { api } from "../api.js";
import { el, mount, toastError } from "../ui.js";
import { navigate } from "../router.js";

const STATE_LABELS = {
  "connected": { text: "connected", cls: "st-ok" },
  "connected-via-plugin": { text: "connected · plugin", cls: "st-ok" },
  "connected-via-mcp": { text: "connected · MCP", cls: "st-ok" },
  "double-connected": { text: "double-connected", cls: "st-warn" },
  "not-connected": { text: "not connected", cls: "st-off" },
  "unknown": { text: "unknown", cls: "st-off" },
};

const PLUGIN_COMMANDS = "/plugin marketplace add kritird/OpenMnemex\n"
  + "/plugin install mnemex@mnemex-marketplace";

function connectButton(row, { secondary = false } = {}) {
  const label = row.agent === "claude-code" ? "Connect via MCP instead" : "Connect";
  const cls = secondary ? "button.btn.btn-ghost.connect-btn"
    : "button.btn.btn-primary.connect-btn";
  const btn = el(cls, {
    onclick: async () => {
      btn.disabled = true;
      btn.textContent = "Connecting…";
      try {
        const payload = await api.connectAgent(row.agent);
        renderList(payload);   // fresh detection: the row flips to connected
      } catch (err) {
        toastError(err);
        btn.disabled = false;
        btn.textContent = label;
      }
    },
  }, label);
  return btn;
}

// The recommended path for Claude Code: the plugin, installed from INSIDE Claude Code
// (plugins can only be installed there — the Console can't click this for you). The
// one-click MCP connect stays available underneath as the secondary option.
function pluginRecommendation(row) {
  const code = el("pre.plugin-cmds", {}, PLUGIN_COMMANDS);
  return el("div.rec-panel", {},
    el("div.rec-head", {},
      el("span.rec-badge", {}, "Recommended"),
      el("span.rec-title", {}, "Install the plugin — inside Claude Code, run:")),
    code,
    el("div.rec-actions", {},
      el("button.btn.btn-ghost.copy-btn", {
        onclick: (ev) => {
          navigator.clipboard.writeText(PLUGIN_COMMANDS).then(() => {
            ev.target.textContent = "Copied";
            setTimeout(() => { ev.target.textContent = "Copy commands"; }, 1500);
          });
        },
      }, "Copy commands"),
      connectButton(row, { secondary: true })),
    el("p.rec-note", {},
      "The plugin is the richer path: 7 auto-capture hooks and the full skill tier. "
      + "MCP works too, from any client — just with fewer automatics."));
}

function agentRow(row) {
  const state = STATE_LABELS[row.state] || { text: row.state, cls: "st-off" };
  const connected = state.cls === "st-ok";
  // Connect only when the agent itself is present (writing config for an agent that
  // isn't installed helps nobody) and it isn't already wired up. copilot never gets a
  // button — its MCP config is per-project (the server refuses it too).
  const canConnect = !connected && row.state !== "double-connected"
    && row.installed === true && row.agent !== "copilot";
  // Claude Code, not yet connected: lead with the plugin recommendation instead of a
  // bare MCP button (the backend marks this with recommended === "plugin").
  const recommendPlugin = canConnect && row.recommended === "plugin";
  return el("div.agent-row", {},
    el("div.agent-head", {},
      el("span.agent-name", {}, row.agent),
      el(`span.state-chip.${state.cls}`, {}, state.text),
      row.installed === false
        ? el("span.agent-missing", {}, "not detected on this machine — install it first")
        : null,
      el("span", { style: "flex:1" }),
      canConnect && !recommendPlugin ? connectButton(row) : null),
    row.connection
      ? el("div.agent-paths", {},
          `plugin: ${row.connection.plugin} · MCP: ${row.connection.mcp}`)
      : null,
    recommendPlugin ? pluginRecommendation(row) : null,
    // The backend note repeats the plugin pitch in prose; the recommendation panel
    // already says it better, so skip the note when the panel is shown.
    row.note && !recommendPlugin ? el("p.agent-note", {}, row.note) : null);
}

function renderList(payload) {
  mount(el("div.page-view.connections-view", {},
    el("nav.breadcrumb", {},
      el("a.crumb", {
        href: "/",
        onclick: (ev) => { ev.preventDefault(); navigate("/"); },
      }, "← back to graphs")),
    el("h1", {}, "Add agents"),
    el("p.subtitle", {},
      "Hook your coding agents up to Mnemex from here. Connect wires an agent up "
      + "machine-wide in one click — the same thing the CLI installer does. For "
      + "Claude Code, the plugin route is the recommended one."),
    payload.connected_agent
      ? el("p.connect-done", {},
          `${payload.connected_agent} connected. Restart it (or its session) to pick up `
          + "the new MCP entry.")
      : null,
    el("div.agent-list", {}, (payload.agents || []).map(agentRow)),
    payload.note ? el("p.view-only-note", {}, payload.note) : null));
}

export async function renderConnections() {
  mount(el("div.loading", {}, "Checking agent connections…"));
  let payload;
  try {
    payload = await api.agents();
  } catch (err) {
    toastError(err);
    navigate("/", { replace: true });
    return;
  }
  renderList(payload);
}
