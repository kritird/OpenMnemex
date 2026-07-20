// Agent connections screen (plan §6 screen 7; reworked per Kriti 2026-07-20): which
// coding agents on this machine are hooked up to Mnemex, with a one-click **Connect**
// for each detected-but-unconnected one — no command copy-pasting. The button POSTs
// /api/agents/connect, which drives the SAME shared installer the CLI uses
// (mnx_install.install, user scope), then the screen re-renders with fresh detection.
// Knowledge stays read-only; this writes only the agent's own MCP config, on click.

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

function connectButton(row) {
  const label = row.agent === "claude-code" ? "Connect via MCP" : "Connect";
  const btn = el("button.btn.btn-primary.connect-btn", {
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

function agentRow(row) {
  const state = STATE_LABELS[row.state] || { text: row.state, cls: "st-off" };
  const connected = state.cls === "st-ok";
  // Connect only when the agent itself is present (writing config for an agent that
  // isn't installed helps nobody) and it isn't already wired up. copilot never gets a
  // button — its MCP config is per-project (the server refuses it too).
  const canConnect = !connected && row.state !== "double-connected"
    && row.installed === true && row.agent !== "copilot";
  return el("div.agent-row", {},
    el("div.agent-head", {},
      el("span.agent-name", {}, row.agent),
      el(`span.state-chip.${state.cls}`, {}, state.text),
      row.installed === false
        ? el("span.agent-missing", {}, "not detected on this machine — install it first")
        : null,
      el("span", { style: "flex:1" }),
      canConnect ? connectButton(row) : null),
    row.connection
      ? el("div.agent-paths", {},
          `plugin: ${row.connection.plugin} · MCP: ${row.connection.mcp}`)
      : null,
    row.note ? el("p.agent-note", {}, row.note) : null);
}

function renderList(payload) {
  mount(el("div.page-view.connections-view", {},
    el("nav.breadcrumb", {},
      el("a.crumb", {
        href: "/",
        onclick: (ev) => { ev.preventDefault(); navigate("/"); },
      }, "← back to graphs")),
    el("h1", {}, "Agent connections"),
    el("p.subtitle", {},
      "Which coding agents on this machine can use Mnemex. Connect wires an agent up "
      + "machine-wide in one click — the same thing the CLI installer does."),
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
