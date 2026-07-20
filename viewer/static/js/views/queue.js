// Revalidation queue panel (plan §6 screen 4, V1.4): active nodes ordered by
// stale_at — soonest horizon first — each with a mini-node dot in the same §4
// encoding as the canvas. Clicking a row FOCUSES the node on the canvas (select +
// center); it does not open the inspector — click the node itself for details.
// Timeless and dead atoms never appear (the server's /queue guarantees this).

import { api } from "../api.js";
import { el } from "../ui.js";

function miniDot(item) {
  const cls = ["sw"];
  cls.push(item.freshness_state === "stale" ? "sw-stale"
    : item.freshness_state === "due_soon" ? "sw-due" : "sw-hot");
  if (item.node_type === "pattern") cls.push("sw-sq");
  return el(`span.${cls.join(".")}`);
}

function dueText(item) {
  const d = Math.round(item.days_until_stale);
  if (d < 0) return `${-d} d overdue`;
  if (d === 0) return "due today";
  return `in ${d} d`;
}

function panelHeader(title, onClose) {
  return el("div.insp-header", {},
    el("h2", {}, title),
    el("button.icon-btn", { title: "Close", onclick: onClose }, "×"));
}

/**
 * Render the queue into the right-panel host. `at` = active projection timestamp
 * (or null for now) so the queue and the canvas always agree on "today".
 */
export async function renderQueuePanel(host, { slug, at, onPick, onClose }) {
  host.hidden = false;
  host.replaceChildren(panelHeader("Revalidation queue", onClose),
    el("div.insp-loading", {}, "Loading…"));

  let payload;
  try {
    payload = await api.queue(slug, { at });
  } catch (err) {
    host.replaceChildren(panelHeader("Revalidation queue", onClose),
      el("p.insp-summary", {}, String(err.message || err)));
    return;
  }

  const rows = payload.queue.map((item) => {
    const row = el("button.queue-row", {
      title: `${item.id} — revalidate via your agent: ${item.revalidate_command}`,
      onclick: () => {
        for (const r of host.querySelectorAll(".queue-row.active")) r.classList.remove("active");
        row.classList.add("active");
        onPick(item.id);
      },
    },
      miniDot(item),
      el("div.queue-main", {},
        el("div.queue-title", {}, item.title),
        el("div.queue-sub", {},
          el("span.queue-due"
            + (item.freshness_state === "stale" ? ".overdue"
              : item.freshness_state === "due_soon" ? ".due-soon" : ""),
            {}, dueText(item)),
          el("span.queue-cluster", {}, item.cluster))));
    return row;
  });

  host.replaceChildren(...[
    panelHeader("Revalidation queue", onClose),
    // `at` (the request arg) only exists while a projection is active — the server
    // echoes payload.at even for "now", so that can't be the signal.
    at ? el("p.insp-note", {}, `Projection — ordered as of ${at.slice(0, 10)}.`) : null,
    el("p.insp-summary", {},
      payload.count
        ? `${payload.count} atom${payload.count === 1 ? "" : "s"}, soonest-stale first. `
          + "Click to locate on the canvas."
        : "Nothing has a freshness horizon here — timeless atoms never need revalidation."),
    el("div.queue-list", {}, rows),
    payload.count ? el("p.insp-note", {},
      "Revalidation itself happens through your agent (mnx-revalidate) — the viewer only shows.") : null,
  ].filter(Boolean));
}
