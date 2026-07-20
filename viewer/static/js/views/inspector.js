// Inspector panel (plan §6 screen 2, pulled forward from V1.3 after the V1.2 design
// checkpoint: "clicking an atom must show its details"). Regular and boundary-stub
// nodes fetch the full /node/{id} payload; ghosts and staged captures have no atom
// file behind them (the API 404s them by design), so their detail renders inline from
// the nodes payload the canvas already holds. The full rendered-markdown atom SCREEN
// (breadcrumb, clickable links) is still V1.3 — the body here is a raw preview.

import { api } from "../api.js";
import { el } from "../ui.js";
import { navigate, atomUrl } from "../router.js";

function chip(brief, onHop) {
  const label = brief.title || brief.name || brief.id;
  return el("button.chip", {
    title: brief.summary ? `${brief.id} — ${brief.summary}` : brief.id,
    onclick: onHop ? () => onHop(brief.id || brief.ghost_id) : null,
  },
    brief.type ? el("span.chip-type", {}, brief.type) : null,
    el("span.chip-label", {}, label));
}

export function chipRow(heading, briefs, onHop) {
  if (!briefs || !briefs.length) return null;
  return el("div.insp-section", {},
    el("h3", {}, heading),
    el("div.chips", {}, briefs.map((b) => chip(b, onHop))));
}

function badge(text, cls) {
  return el(`span.badge${cls ? "." + cls : ""}`, {}, text);
}

export function badges(entry) {
  const out = [];
  if (entry.staged) out.push(badge("staged", "badge-staged"));
  if (entry.tier && !entry.staged) out.push(badge(entry.tier));
  if (entry.node_type) out.push(badge(entry.node_type));
  if (entry.freshness_state === "stale") out.push(badge("stale", "badge-red"));
  else if (entry.freshness_state === "due_soon") out.push(badge("due soon", "badge-amber"));
  else if (entry.freshness_state === "timeless") out.push(badge("timeless"));
  if (entry.tombstoned) out.push(badge("tombstoned", "badge-red"));
  if (entry.superseded_by) out.push(badge("superseded", "badge-amber"));
  return out.length ? el("div.badges", {}, out) : null;
}

export function kv(rows) {
  const filled = rows.filter(([, v]) => v != null && v !== "");
  if (!filled.length) return null;
  return el("dl.kv", {}, filled.map(([k, v]) => [
    el("dt", {}, k), el("dd", {}, String(v)),
  ]));
}

function fmtStrength(v) {
  return typeof v === "number" ? v.toFixed(2) : v;
}

function header(title, onClose) {
  return el("div.insp-header", {},
    el("h2", { title }, title),
    el("button.icon-btn", { title: "Close", onclick: onClose }, "×"));
}

function ghostDetail(ghost, onHop, onClose) {
  return [
    header(ghost.title || ghost.name, onClose),
    el("div.badges", {}, badge("red-link", "badge-red")),
    el("p.insp-summary", {},
      "Not written yet — atoms link to this name, but no atom exists behind it."),
    chipRow("Wanted by", (ghost.wanted_by || []).map((id) => ({ id })), onHop),
  ];
}

function stagedDetail(entry, onClose) {
  return [
    header(entry.title || entry.id, onClose),
    el("div.badges", {}, badge("staged", "badge-staged"), badge(entry.node_type || "domain")),
    entry.summary ? el("p.insp-summary", {}, entry.summary) : null,
    kv([["id", entry.id], ["volatility", entry.volatility],
        ["capture score", entry.score]]),
    el("p.insp-note", {},
      "A staged capture — not yet promoted into the graph. Promotion happens through "
      + "your agent or the CLI; the viewer only shows."),
  ];
}

function nodeDetail(detail, { slug, scope }, onHop, onClose) {
  const entry = detail.node;
  const atom = detail.atom || {};
  const mesh = detail.mesh || {};
  const history = detail.history || {};
  return [
    header(entry.title || entry.id, onClose),
    badges(entry),
    entry.summary ? el("p.insp-summary", {}, entry.summary) : null,
    el("div.insp-path", { title: entry.path }, entry.path),
    kv([
      ["id", entry.id],
      ["team", entry.team],
      ["strength now", fmtStrength(entry.strength_now)],
      ["hotness", entry.hotness_bucket],
      ["volatility", entry.volatility],
      ["verified", entry.verified],
      ["stale at", entry.stale_at],
    ]),
    chipRow("Links out", mesh.out, onHop),
    chipRow("Linked from", mesh.in, onHop),
    chipRow("Red links", (mesh.red_links || []).map((r) => ({
      id: r.ghost_id, ghost_id: r.ghost_id, title: r.name, type: "red-link",
    })), onHop),
    chipRow("Superseded by", history.superseded_by_chain, onHop),
    chipRow("Supersedes", history.supersedes, onHop),
    atom.body ? el("div.insp-section", {},
      el("h3", {}, "Atom"),
      el("pre.atom-body", {}, atom.body)) : null,
    el("button.btn.open-atom", {
      onclick: () => navigate(atomUrl(slug, entry.id, scope)),
    }, "Open full atom →"),
  ];
}

/**
 * Render node detail into `host`. `kind` = {ghost, staged, stub} flags from the
 * canvas; `overlay` = the nodes payload (source for ghost/staged inline detail).
 */
export async function renderInspector(host, { slug, scope, nodeId, kind, overlay, at, onHop, onClose }) {
  host.hidden = false;
  host.replaceChildren(el("div.insp-loading", {}, "Loading…"));

  if (kind && kind.ghost) {
    const ghost = (overlay.ghosts || []).find((g) => g.id === nodeId);
    host.replaceChildren(...(ghost
      ? ghostDetail(ghost, onHop, onClose)
      : [header(nodeId, onClose), el("p.insp-summary", {}, "Ghost entry not found.")])
      .filter(Boolean));
    return;
  }
  if (kind && kind.staged) {
    const entry = (overlay.staged || []).find((s) => s.id === nodeId)
      || (overlay.nodes || []).find((n) => n.id === nodeId);
    host.replaceChildren(...(entry
      ? stagedDetail(entry, onClose)
      : [header(nodeId, onClose), el("p.insp-summary", {}, "Staged entry not found.")])
      .filter(Boolean));
    return;
  }

  try {
    const detail = await api.node(slug, nodeId, { at });
    host.replaceChildren(...nodeDetail(detail, { slug, scope }, onHop, onClose).filter(Boolean));
  } catch (err) {
    host.replaceChildren(...[
      header(nodeId, onClose),
      el("p.insp-summary", {}, String(err.message || err)),
      err.action ? el("p.insp-note", {}, err.action) : null,
    ].filter(Boolean));
  }
}
