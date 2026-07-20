// Health panel (plan §6 screen 5, V1.4): doctor findings for the graph. Findings
// that anchor to nodes are ALSO pinned on the canvas (red underlay — the graph view
// applies the classes); findings with no node anchor only appear here. Clicking a
// node chip focuses that node on the canvas. Read-only: fixing happens through your
// agent (mnx-doctor --fix) or by editing the files.

import { el } from "../ui.js";

const SEVERITY = {
  E: { label: "error", cls: "sev-e" },
  W: { label: "warning", cls: "sev-w" },
  I: { label: "info", cls: "sev-i" },
};

function panelHeader(title, onClose) {
  return el("div.insp-header", {},
    el("h2", {}, title),
    el("button.icon-btn", { title: "Close", onclick: onClose }, "×"));
}

function findingRow(f, onPick) {
  const sev = SEVERITY[f.severity] || { label: f.severity, cls: "sev-i" };
  return el("div.health-row", {},
    el("div.health-row-head", {},
      el(`span.sev-chip.${sev.cls}`, {}, sev.label),
      el("span.health-inv", {}, `invariant ${f.invariant}`)),
    el("div.health-detail", {}, f.detail || f.node_or_edge || ""),
    (f.node_ids || []).length
      ? el("div.chips", {}, f.node_ids.map((id) =>
          el("button.chip", { title: `focus ${id} on the canvas`, onclick: () => onPick(id) },
            el("span.chip-label", {}, id))))
      : null);
}

/** Render doctor findings into the right-panel host. `payload` = /health response
 *  (fetched by the graph view, which also owns the canvas pinning). */
export function renderHealthPanel(host, { payload, onPick, onClose }) {
  host.hidden = false;
  const c = payload.counts || {};
  const findings = payload.findings || [];
  // "clean" from the server means no ERRORS — warnings/info can still be present.
  const summary = !findings.length
    ? "Doctor is clean — no findings."
    : (payload.clean ? "No errors. " : "")
      + ["E", "W", "I"].filter((k) => c[k])
        .map((k) => `${c[k]} ${SEVERITY[k].label}${c[k] === 1 ? "" : "s"}`)
        .join(" · ");

  host.replaceChildren(...[
    panelHeader("Health", onClose),
    el("p.insp-summary", {}, summary),
    findings.length ? el("p.insp-note", {},
      "Flagged atoms glow red on the canvas. Fixing stays with your agent "
      + "(mnx-doctor --fix) or the files — the viewer only shows.") : null,
    el("div.health-list", {}, findings.map((f) => findingRow(f, onPick))),
  ].filter(Boolean));
}
