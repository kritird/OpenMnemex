// Main view (plan §6 screen 2): left folder tree · center canvas · right panel.
// Node click → inspector (+ ?sel= deep link), selected node's cluster is marked in
// the tree, legend + status bar in the pane. The tree's staging row is a passive
// count — staged atoms are the hollow purple dots in their folder's view.
// V1.4 adds a canvas toolbar: Queue (revalidation panel), Health (doctor findings
// pinned on canvas), Time (scrubber projecting the graph's future via ?at=).

import { api } from "../api.js";
import { el, mount, toastError } from "../ui.js";
import { navigate, graphUrl, configUrl } from "../router.js";
import { createCanvas } from "../canvas.js";
import { renderInspector } from "./inspector.js";
import { renderQueuePanel } from "./queue.js";
import { renderHealthPanel } from "./health.js";

let activeCanvas = null;

function destroyActiveCanvas() {
  if (activeCanvas) {
    activeCanvas.destroy();
    activeCanvas = null;
  }
}

// Always-visible collapsible legend (checkpoint feedback: the ?-button version was
// too hidden). Collapsed by default (Kriti, 2026-07-20); the choice persists per browser.
const LEGEND_KEY = "mnx-legend";

function legendPanel() {
  const row = (swatchCls, text) =>
    el("div.legend-row", {}, el(`span.sw.${swatchCls}`), el("span", {}, text));
  const body = el("div.legend-body", {},
    el("div.legend-row", {},
      el("span.sw.sw-hot"), el("span.sw.sw-cooling"), el("span.sw.sw-cold"),
      el("span", {}, "size + teal depth = hotness (how heavily used)")),
    row("sw-due", "amber dashed ring = due for reverification soon"),
    row("sw-stale", "red dashed ring = stale, overdue"),
    row("sw-timeless", "center dot = timeless (never stales)"),
    row("sw-pattern", "rounded square = pattern (circle = domain)"),
    row("sw-staged", "hollow purple = staged, not yet promoted"),
    row("sw-ghost", "dashed gray = red-link, not written yet"),
    row("sw-stub", "faded = outside the current scope"));

  let open = false;
  try { open = localStorage.getItem(LEGEND_KEY) === "open"; } catch (err) { /* keep default */ }
  const chevron = el("span.legend-chevron", {}, open ? "▾" : "▸");
  body.hidden = !open;

  const header = el("button.legend-header", {
    type: "button",
    title: "Node colouring scheme",
    onclick: () => {
      open = !open;
      body.hidden = !open;
      chevron.textContent = open ? "▾" : "▸";
      try { localStorage.setItem(LEGEND_KEY, open ? "open" : "closed"); } catch (err) { /* ok */ }
    },
  }, el("span", {}, "Legend"), chevron);

  return el("div.legend", {}, header, body);
}

function treeRow({ cls, label, count, onclick, decoration }) {
  return el(`button.tree-row.${cls}`, { onclick, title: label },
    decoration || el("span.twisty", {}),
    el("span.label", {}, label),
    count != null ? el("span.count", {}, String(count)) : null);
}

function buildTree(tree, slug, scope) {
  const rows = [];
  const totalNodes = tree.teams.reduce(
    (sum, t) => sum + t.clusters.reduce((s, c) => s + c.nodes, 0), 0);

  rows.push(treeRow({
    cls: "org-row",
    label: tree.org,
    count: totalNodes,
    onclick: () => navigate(graphUrl(slug)),
  }));
  if (!scope) rows[0].classList.add("selected");

  for (const team of tree.teams) {
    rows.push(el("div.section-gap"));
    // team names ARE the top-level folder names (mnx_common.team_of), so the team
    // row scopes to that folder; "(root)" clusters live at the graph root itself.
    const teamPath = team.team !== "(root)" ? team.team : "";
    const teamRow = treeRow({
      cls: "team-row",
      label: team.team,
      count: team.clusters.reduce((s, c) => s + c.nodes, 0),
      onclick: teamPath ? () => navigate(graphUrl(slug, teamPath))
                        : () => navigate(graphUrl(slug)),
    });
    if (scope && teamPath && scope === teamPath) teamRow.classList.add("selected");
    rows.push(teamRow);
    for (const cluster of team.clusters) {
      const row = treeRow({
        cls: "cluster-row",
        label: cluster.name,
        count: cluster.nodes,
        onclick: () => navigate(graphUrl(slug, cluster.path)),
      });
      row.dataset.path = cluster.path;   // tree↔canvas sync marks the selected node's cluster
      if (cluster.description) row.title = `${cluster.name} — ${cluster.description}`;
      if (scope === cluster.path) row.classList.add("selected");
      rows.push(row);
    }
  }

  // Passive indicator only (Kriti, 2026-07-20): a click-through list can't work —
  // the count is graph-wide but staged atoms live in their folders' scopes. The
  // hollow purple dots on the canvas ARE the staged atoms; click those.
  if (tree.staging && tree.staging.count > 0) {
    rows.push(el("div.section-gap"));
    rows.push(el("div.tree-row.staging-row", {
      title: "staged captures, not yet promoted — shown as hollow purple dots in "
        + "their folder's view",
    },
      el("span.dot"),
      el("span.label", {}, "staging"),
      el("span.count", {}, String(tree.staging.count))));
  }

  // Visible, labeled config entry at the bottom of the tree (Kriti 2026-07-20:
  // the header gear was too small to be discoverable).
  rows.push(el("div.section-gap"));
  rows.push(el("button.tree-row.config-row", {
    title: "This graph's settings, explained (read-only)",
    onclick: () => navigate(configUrl(slug, scope)),
  },
    el("span.twisty", {}, "⚙"),
    el("span.label", {}, "configuration")));

  return el("nav.tree", {}, rows);
}

export async function renderGraph({ slug, scope, sel }) {
  destroyActiveCanvas();
  mount(el("div.loading", {}, "Loading graph…"));

  let tree;
  try {
    tree = await api.tree(slug);
  } catch (err) {
    toastError(err);
    navigate("/", { replace: true });
    return;
  }

  const canvasHost = el("div.canvas-host");
  const emptyHint = el("div.canvas-empty", { hidden: true }, "No nodes in this scope.");
  const status = el("div.canvas-status", {}, el("span", {}, "loading nodes…"));
  const rightPanel = el("aside.inspector", { hidden: true });
  const legend = legendPanel();
  const projBanner = el("div.projection-banner", { hidden: true });

  let payload = null;        // current /nodes payload (re-fetched on projection)
  let panelMode = null;      // "inspector" | "queue" | "health" | null
  let currentSel = null;     // selected node id (kept for projection refresh)
  let at = null;             // ISO timestamp of the active projection, null = now
  let healthPayload = null;  // /health response while the overlay is on

  // Tree↔canvas sync: the selected node's cluster gets a marker in the tree.
  const markTreeSelection = (nodeId) => {
    for (const r of document.querySelectorAll(".tree .cluster-row.contains-selection")) {
      r.classList.remove("contains-selection");
    }
    if (!nodeId || !payload) return;
    const entry = (payload.nodes || []).find((n) => n.id === nodeId)
      || (payload.stubs || []).find((n) => n.id === nodeId);
    if (!entry || !entry.cluster) return;
    const row = document.querySelector(
      `.tree .cluster-row[data-path="${CSS.escape(entry.cluster)}"]`);
    if (row) row.classList.add("contains-selection");
  };

  // ---- right panel (inspector / queue / health share the slot) --------------

  const clearSelection = () => {
    currentSel = null;
    if (activeCanvas) activeCanvas.cy.elements().unselect();
    markTreeSelection(null);
    history.replaceState(null, "", graphUrl(slug, scope));
  };

  const applyHealthPins = () => {
    if (!activeCanvas) return;
    activeCanvas.cy.nodes().removeClass("health-pin");
    if (!healthPayload) return;
    for (const id of Object.keys(healthPayload.by_node || {})) {
      activeCanvas.cy.$id(id).addClass("health-pin");
    }
  };

  const closePanel = () => {
    const was = panelMode;
    panelMode = null;
    rightPanel.hidden = true;
    rightPanel.replaceChildren();
    if (was === "inspector") clearSelection();
    if (was === "health") { healthPayload = null; applyHealthPins(); }
    syncToolbar();
  };

  // Focus without inspector: queue/health rows point AT the canvas (select +
  // center + dim the rest); details stay one node-click away.
  const focusNode = (nodeId) => {
    currentSel = nodeId;
    if (activeCanvas) {
      const target = activeCanvas.cy.$id(nodeId);
      activeCanvas.cy.elements().unselect();
      if (target.length) {
        target.select();
        activeCanvas.cy.animate({ center: { eles: target } }, { duration: 200 });
      }
    }
    markTreeSelection(nodeId);
    history.replaceState(null, "", graphUrl(slug, scope, nodeId));
  };

  const showNode = (nodeId, kind) => {
    currentSel = nodeId;
    // Hop targets may live outside the current scope; select on-canvas when present.
    if (activeCanvas) {
      const target = activeCanvas.cy.$id(nodeId);
      activeCanvas.cy.elements().unselect();
      if (target.length) {
        kind = kind || { ghost: target.hasClass("ghost"), staged: target.hasClass("staged"),
                         stub: target.hasClass("stub") };
        target.select();
        activeCanvas.cy.animate({ center: { eles: target } }, { duration: 200 });
      }
    }
    panelMode = "inspector";
    renderInspector(rightPanel, {
      slug, scope, nodeId, kind: kind || {}, overlay: payload, at,
      onHop: (id) => showNode(id, null),
      onClose: closePanel,
    });
    markTreeSelection(nodeId);
    // keep the URL shareable: the selected node travels as ?sel= (no re-render)
    history.replaceState(null, "", graphUrl(slug, scope, nodeId));
    syncToolbar();
  };

  const toggleQueue = () => {
    if (panelMode === "queue") { closePanel(); return; }
    panelMode = "queue";
    renderQueuePanel(rightPanel, { slug, at, onPick: focusNode, onClose: closePanel });
    syncToolbar();
  };

  // Health button = the overlay switch. The pins survive a detour into the
  // inspector (panel hijacked by a node click); clicking Health again first
  // brings the findings list back, closing it clears the pins.
  const toggleHealth = async () => {
    if (panelMode === "health") { closePanel(); return; }
    if (!healthPayload) {
      try {
        healthPayload = await api.health(slug);
      } catch (err) {
        toastError(err);
        return;
      }
      applyHealthPins();
    }
    panelMode = "health";
    renderHealthPanel(rightPanel, { payload: healthPayload, onPick: focusNode, onClose: closePanel });
    syncToolbar();
  };

  // ---- time scrubber (plan §6 screen 6): the server recomputes at ?at=, the ----
  // ---- canvas restyles in place — same positions, aged numbers. --------------
  const DAY_MS = 86400000;
  let projSeq = 0;
  let projTimer = null;

  const scrubDate = el("span.scrub-date", {}, "today");
  const range = el("input.scrub-range", {
    type: "range", min: "0", max: "365", step: "1", value: "0",
    title: "Drag to project up to a year ahead",
  });
  const presetBtns = [0, 7, 30, 90].map((days) =>
    el("button.btn.scrub-preset", { onclick: () => setProjection(days) },
      days === 0 ? "today" : `+${days} d`));
  const scrubber = el("div.scrubber", { hidden: true },
    el("span.scrub-label", {}, "project ahead"),
    ...presetBtns,
    range,
    scrubDate);

  const applyProjection = async (days) => {
    const seq = ++projSeq;
    const nextAt = days > 0 ? new Date(Date.now() + days * DAY_MS).toISOString() : null;
    let proj;
    try {
      proj = await api.nodes(slug, { scope, at: nextAt });
    } catch (err) {
      toastError(err);
      return;
    }
    if (seq !== projSeq) return;   // a newer scrub position superseded this fetch
    at = nextAt;
    payload = proj;
    if (activeCanvas) activeCanvas.restyle(proj);
    projBanner.hidden = !at;
    if (at) {
      projBanner.replaceChildren(
        el("span", {},
          `Projection — how this graph would look on ${at.slice(0, 10)} with no further `
          + "activity. Nothing is changed; this is a preview."),
        el("button.btn.scrub-preset", { onclick: () => setProjection(0) }, "back to today"));
    }
    // panels that show computed numbers follow the projection
    if (panelMode === "queue") {
      renderQueuePanel(rightPanel, { slug, at, onPick: focusNode, onClose: closePanel });
    } else if (panelMode === "inspector" && currentSel) {
      showNode(currentSel, null);
    }
    syncToolbar();
  };

  function setProjection(days) {
    range.value = String(days);
    const d = new Date(Date.now() + days * DAY_MS);
    scrubDate.textContent = days === 0 ? "today"
      : `+${days} d → ${d.toISOString().slice(0, 10)}`;
    for (const [i, btn] of presetBtns.entries()) {
      btn.classList.toggle("active", [0, 7, 30, 90][i] === days);
    }
    clearTimeout(projTimer);
    projTimer = setTimeout(() => applyProjection(days), 150);
  }
  range.addEventListener("input", () => setProjection(parseInt(range.value, 10) || 0));

  const toggleScrubber = () => {
    scrubber.hidden = !scrubber.hidden;
    syncToolbar();
  };

  // ---- toolbar --------------------------------------------------------------
  const queueBtn = el("button.tb-btn", {
    title: "Revalidation queue — what needs re-checking, soonest first",
    onclick: toggleQueue,
  }, "Queue");
  const healthBtn = el("button.tb-btn", {
    title: "Health — pin doctor findings on the canvas",
    onclick: () => { toggleHealth(); },
  }, "Health");
  const timeBtn = el("button.tb-btn", {
    title: "Time — project how the graph ages if nothing happens",
    onclick: toggleScrubber,
  }, "Time");
  // phone-only (CSS hides it above 640px): the sidebar returns as an overlay
  const treeBtn = el("button.tb-btn.tb-tree", {
    title: "Show the folder tree",
    onclick: () => rootEl.classList.toggle("tree-open"),
  }, "Tree");
  const toolbar = el("div.canvas-toolbar", {}, treeBtn, queueBtn, healthBtn, timeBtn);

  function syncToolbar() {
    queueBtn.classList.toggle("active", panelMode === "queue");
    healthBtn.classList.toggle("active", !!healthPayload);
    timeBtn.classList.toggle("active", !scrubber.hidden || !!at);
  }

  const pane = el("div.canvas-pane", {},
    tree.maintenance
      ? el("div.maintenance-banner", {},
          "Maintenance in progress — the graph is being reorganized; this view may be "
          + "briefly out of date.")
      : null,
    projBanner,
    canvasHost, emptyHint, toolbar, legend, scrubber, status);

  const rootEl = el("div.graph-view", {},
    el("aside.sidebar", {}, buildTree(tree, slug, scope)),
    pane,
    rightPanel);
  mount(rootEl);

  try {
    payload = await api.nodes(slug, { scope });
  } catch (err) {
    toastError(err);
    status.replaceChildren(el("span", {}, "failed to load nodes"));
    return;
  }

  activeCanvas = createCanvas(canvasHost, {
    onNodeTap: showNode,
    onBackgroundTap: () => {
      if (panelMode === "inspector") closePanel();
      else clearSelection();
    },
  });
  const { nodes, edges, layoutMs } = await activeCanvas.load(payload);
  emptyHint.hidden = nodes > 0;
  if (sel) showNode(sel, null);   // deep link / search result / "reveal in canvas"

  const scopeLabel = scope ? `scope: ${scope}` : "scope: whole graph";
  status.replaceChildren(...[
    el("span.scope-label", { title: scopeLabel }, scopeLabel),
    el("span", {}, `${nodes} nodes · ${edges} edges`),
    layoutMs ? el("span", {}, `layout ${layoutMs} ms`) : null,
    payload.warnings && payload.warnings.length
      ? el("span", { style: "color: var(--amber)" },
          `${payload.warnings.length} file warning${payload.warnings.length === 1 ? "" : "s"}`)
      : null,
  ].filter(Boolean));
}

export function graphViewCleanup() {
  destroyActiveCanvas();
}
