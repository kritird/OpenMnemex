// Cytoscape canvas wrapper. Full §4 visual encoding (hotness size/fill, freshness
// rings, timeless anchor dot, staged/ghost/stub kinds), hover tooltip with the exact
// engine numbers, and selection focus (selected node's mesh emphasized, rest dimmed).
//
// All colors are read from the CSS theme tokens at style-build time, and rebuilt on
// the mnx-theme-changed event, so the canvas follows light/dark like everything else.
// cytoscape + fcose arrive as deferred classic scripts (globals), see index.html.

function cssVar(name) {
  return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}

function buildStyle() {
  const node = cssVar("--canvas-node");
  const edge = cssVar("--canvas-edge");
  const purple = cssVar("--purple");
  const faint = cssVar("--text-faint");
  const muted = cssVar("--text-muted");
  const bg = cssVar("--bg");
  const amber = cssVar("--amber");
  const red = cssVar("--red");
  const hot = cssVar("--node-hot");
  const cooling = cssVar("--node-cooling");
  const cold = cssVar("--node-cold");
  return [
    // Labels are OFF at rest: at 1k nodes they smear into unreadable noise (V1.2
    // spike finding — min-zoomed-font-size culling proved unreliable in 3.34), so
    // titles appear on hover/selection only. Richer labeling is V1.3 polish.
    //
    // Encoding per plan §4 — the two axes stay orthogonal on screen:
    //   hotness   → size (data(size), radius ∝ sqrt(strength_now)) + fill depth
    //   freshness → ring (none = fresh · amber dashed = due soon · red dashed = STALE)
    //   timeless  → light anchor dot at center (radial gradient), never a ring
    //   shapes    → circle = domain, rounded square = pattern
    // hot+stale (big teal node, red dashed ring) falls out unmissably, by design.
    { selector: "node", style: {
      width: "data(size)", height: "data(size)",
      "background-color": node,
      "border-width": 0,
    } },
    { selector: "node.hb-hot", style: { "background-color": hot } },
    { selector: "node.hb-cooling", style: { "background-color": cooling } },
    { selector: "node.hb-cold", style: { "background-color": cold } },
    { selector: "node.type-pattern", style: {
      shape: "round-rectangle", "corner-radius": 4,
    } },
    { selector: "node.fr-due", style: {
      "border-width": 2, "border-color": amber, "border-style": "dashed",
    } },
    { selector: "node.fr-stale", style: {
      "border-width": 2.5, "border-color": red, "border-style": "dashed",
    } },
    { selector: "node.fr-timeless", style: {
      "background-fill": "radial-gradient",
      "background-gradient-stop-colors": `${bg} ${bg} ${hot} ${hot}`,
      "background-gradient-stop-positions": "0% 18% 34% 100%",
    } },
    { selector: "node.fr-timeless.hb-cooling", style: {
      "background-gradient-stop-colors": `${bg} ${bg} ${cooling} ${cooling}`,
    } },
    { selector: "node.fr-timeless.hb-cold", style: {
      "background-gradient-stop-colors": `${bg} ${bg} ${cold} ${cold}`,
    } },
    { selector: "node.hovered, node:selected", style: {
      label: "data(title)",
      color: muted,
      "font-size": 11,
      "text-valign": "bottom",
      "text-margin-y": 4,
      "font-family": "ui-monospace, Menlo, monospace",
      "text-background-color": cssVar("--bg-panel"),
      "text-background-opacity": 0.85,
      "text-background-padding": 2,
      "z-index": 10,
    } },
    { selector: "node.staged", style: {
      "background-opacity": 0,               // hollow purple = staged (§4)
      "border-width": 2, "border-color": purple,
    } },
    { selector: "node.ghost", style: {
      width: 10, height: 10,
      "background-opacity": 0,               // ghost red-link: dashed gray, no fill
      "border-width": 1.5, "border-color": faint, "border-style": "dashed",
    } },
    { selector: "node.stub", style: {
      opacity: 0.45,                          // boundary stub: outside the scope
    } },
    { selector: "edge", style: {
      width: 1, "line-color": edge, "curve-style": "haystack",
    } },
    { selector: "edge.reference", style: { "line-style": "dashed" } },
    { selector: "edge.red-link", style: { "line-style": "dotted" } },
    { selector: "node:selected", style: {
      "background-color": purple, "background-opacity": 1,
      "border-width": 3, "border-color": purple, "border-opacity": 0.35,
    } },
    // Health overlay (V1.4): doctor-flagged nodes get a soft red underlay pin —
    // a different channel from the freshness rings (border), so hot+stale+flagged
    // still reads. Applied/cleared by the graph view's Health toggle.
    { selector: "node.health-pin", style: {
      "underlay-color": red, "underlay-opacity": 0.25, "underlay-padding": 6,
    } },
    // Selection focus (§4): the selected node's mesh pops, everything else recedes.
    // Last in the list so .dimmed outranks every encoding rule (incl. stub opacity).
    { selector: "edge.mesh-hl", style: {
      width: 2, "line-color": purple, "line-opacity": 0.8,
    } },
    { selector: ".dimmed", style: { opacity: 0.12, "text-opacity": 0 } },
  ];
}

// radius ∝ sqrt(strength_now) (§4): strength 0→10px, 1→28px; no strength → 12px.
function nodeSize(entry) {
  const s = entry.strength_now;
  if (typeof s !== "number" || Number.isNaN(s)) return 12;
  return Math.round(10 + 18 * Math.sqrt(Math.max(0, Math.min(1, s))));
}

function encodingClasses(entry) {
  const cls = [];
  if (entry.hotness_bucket) cls.push(`hb-${entry.hotness_bucket}`);
  if (entry.node_type === "pattern") cls.push("type-pattern");
  if (entry.freshness_state === "due_soon") cls.push("fr-due");
  else if (entry.freshness_state === "stale") cls.push("fr-stale");
  else if (entry.freshness_state === "timeless") cls.push("fr-timeless");
  return cls;
}

function toElements(payload) {
  const elements = [];
  const known = new Set();
  const addNode = (entry, cls) => {
    if (known.has(entry.id)) return;
    known.add(entry.id);
    elements.push({
      group: "nodes",
      // the raw payload entry rides along for the hover tooltip (exact numbers, §4)
      data: { id: entry.id, title: entry.title || entry.id, size: nodeSize(entry), entry },
      classes: [...encodingClasses(entry), cls].filter(Boolean).join(" "),
    });
  };
  for (const n of payload.nodes || []) addNode(n, n.staged ? "staged" : "");
  for (const n of payload.staged || []) addNode(n, "staged");
  for (const n of payload.stubs || []) addNode(n, "stub");
  for (const n of payload.ghosts || []) addNode(n, "ghost");
  (payload.edges || []).forEach((e, i) => {
    if (!known.has(e.from) || !known.has(e.to)) return;   // never crash on a bad edge
    elements.push({
      group: "edges",
      data: { id: `e${i}`, source: e.from, target: e.to, type: e.type || "" },
      classes: e.kind === "edge" ? "" : e.kind,
    });
  });
  return elements;
}

// Hover tooltip content — every number verbatim from the server payload; the only
// client-side arithmetic is the calendar difference to the stale date.
function tooltipLines(entry, at) {
  if (entry.ghost) return [["red-link", "not written yet"]];
  if (entry.stub) return [["stub", "outside the current scope"]];
  const lines = [];
  lines.push(["type", `${entry.node_type || "domain"} · ${entry.volatility || "default"}`]);
  if (typeof entry.strength_now === "number") {
    lines.push(["strength", entry.strength_now.toFixed(2)
      + (entry.hotness_bucket ? ` (${entry.hotness_bucket})` : "")]);
  }
  if (entry.half_life_days) lines.push(["half-life", `${entry.half_life_days} d`]);
  if (entry.verified) lines.push(["verified", entry.verified.slice(0, 10)]);
  if (entry.freshness_state === "timeless") {
    lines.push(["freshness", "timeless — never stales"]);
  } else if (entry.stale_at) {
    const ref = at ? Date.parse(at) : Date.now();
    const days = Math.round((Date.parse(entry.stale_at) - ref) / 86400000);
    lines.push(["stale", days < 0 ? `${-days} d overdue` : `in ${days} d`]);
  }
  if (entry.staged) lines.push(["tier", "staged — not yet promoted"]);
  return lines;
}

export function createCanvas(container, { onNodeTap, onBackgroundTap } = {}) {
  const cy = cytoscape({
    container,
    elements: [],
    style: buildStyle(),
    boxSelectionEnabled: false,
  });
  window.__mnxCy = cy;   // debug/test handle — the fidelity pass reads it (plan §8)
  let payloadAt = null;

  // Tooltip is a plain positioned <div> over the canvas — cytoscape has no HTML
  // labels, and canvas-drawn text can't do the small key/value card we need.
  const tip = document.createElement("div");
  tip.className = "canvas-tip";
  tip.hidden = true;
  container.appendChild(tip);
  const hideTip = () => { tip.hidden = true; };
  const showTip = (node) => {
    const entry = node.data("entry");
    if (!entry) return;
    tip.replaceChildren();
    const title = document.createElement("div");
    title.className = "tip-title";
    title.textContent = node.data("title");
    tip.appendChild(title);
    for (const [k, v] of tooltipLines(entry, payloadAt)) {
      const row = document.createElement("div");
      row.className = "tip-row";
      const kEl = document.createElement("span");
      kEl.className = "tip-k";
      kEl.textContent = k;
      const vEl = document.createElement("span");
      vEl.textContent = v;
      row.append(kEl, vEl);
      tip.appendChild(row);
    }
    tip.hidden = false;
    const pos = node.renderedPosition();
    const r = (node.renderedWidth() || 12) / 2;
    const box = container.getBoundingClientRect();
    let x = pos.x + r + 12;
    let y = pos.y - 10;
    // flip left / clamp so the card never leaves the canvas
    if (x + tip.offsetWidth > box.width - 8) x = pos.x - r - 12 - tip.offsetWidth;
    y = Math.max(8, Math.min(y, box.height - tip.offsetHeight - 8));
    tip.style.left = `${Math.max(8, x)}px`;
    tip.style.top = `${y}px`;
  };

  // Selection focus: dim everything, then lift the selected node + its mesh back up.
  const applyFocus = () => {
    const sel = cy.$("node:selected");
    cy.elements().removeClass("dimmed mesh-hl");
    if (!sel.length) return;
    const hood = sel.closedNeighborhood();
    cy.elements().not(hood).addClass("dimmed");
    sel.connectedEdges().addClass("mesh-hl");
  };
  cy.on("select unselect", "node", applyFocus);

  cy.on("mouseover", "node", (ev) => { ev.target.addClass("hovered"); showTip(ev.target); });
  cy.on("mouseout", "node", (ev) => { ev.target.removeClass("hovered"); hideTip(); });
  cy.on("viewport", hideTip);
  cy.on("tap", hideTip);
  if (onNodeTap) {
    cy.on("tap", "node", (ev) => onNodeTap(ev.target.id(), {
      ghost: ev.target.hasClass("ghost"),
      staged: ev.target.hasClass("staged"),
      stub: ev.target.hasClass("stub"),
    }));
  }
  if (onBackgroundTap) {
    cy.on("tap", (ev) => { if (ev.target === cy) onBackgroundTap(); });
  }

  const onTheme = () => cy.style(buildStyle());
  addEventListener("mnx-theme-changed", onTheme);

  return {
    cy,

    /** Replace content with a /nodes payload; resolves {nodes, edges, layoutMs}. */
    load(payload) {
      payloadAt = payload.at || null;   // tooltip's "stale in N d" is relative to this
      const elements = toElements(payload);
      cy.elements().remove();
      cy.add(elements);
      const counts = { nodes: cy.nodes().length, edges: cy.edges().length };
      if (!counts.nodes) return Promise.resolve({ ...counts, layoutMs: 0 });
      const t0 = performance.now();
      return new Promise((resolve) => {
        // Spike-tuned (2026-07-19, 1017-node fixture): "default" quality costs ~4 s
        // at 1k nodes even with numIter capped (the cost is not in the iterations),
        // while "draft" (spectral only) lands in ~200 ms and still shows global
        // structure. So: pretty refinement for small scopes, spectral above the
        // threshold. packComponents + per-node dimensions were the other 8 s.
        const layout = cy.layout({
          name: "fcose",
          animate: false,
          quality: counts.nodes <= 400 ? "default" : "draft",
          randomize: true,
          packComponents: false,
          uniformNodeDimensions: true,
          padding: 40,
        });
        layout.one("layoutstop", () => {
          cy.fit(undefined, 40);
          resolve({ ...counts, layoutMs: Math.round(performance.now() - t0) });
        });
        layout.run();
      });
    },

    /**
     * Re-apply encodings from a fresh /nodes payload WITHOUT re-layout — positions
     * stay put so the time scrubber reads as the same graph aging, not a new one.
     * The node set is identical for a given scope (?at= only changes the computed
     * numbers), so this is a pure data/class update.
     */
    restyle(payload) {
      payloadAt = payload.at || null;
      const groups = [
        [payload.nodes, (n) => (n.staged ? "staged" : "")],
        [payload.staged, () => "staged"],
        [payload.stubs, () => "stub"],
        [payload.ghosts, () => "ghost"],
      ];
      cy.batch(() => {
        for (const [list, kindOf] of groups) {
          for (const entry of list || []) {
            const n = cy.$id(entry.id);
            if (!n.length) continue;
            n.data("entry", entry);
            n.data("size", nodeSize(entry));
            // rebuild encoding classes; keep the orthogonal overlay/focus classes
            const keep = ["dimmed", "health-pin"].filter((c) => n.hasClass(c));
            n.classes([...encodingClasses(entry), kindOf(entry), ...keep]
              .filter(Boolean).join(" "));
          }
        }
      });
    },

    destroy() {
      removeEventListener("mnx-theme-changed", onTheme);
      tip.remove();
      cy.destroy();
    },
  };
}
