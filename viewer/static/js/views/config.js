// Read-only config screen (plan §6 screen 8, V1.4): the graph's effective knobs
// with the engine's own plain-language help strings, grouped, with overridden
// values marked against their defaults. The viewer never edits config — changes
// happen in mnemex.config.md or through an agent (mnx-config).

import { api } from "../api.js";
import { el, mount, toastError } from "../ui.js";
import { navigate, graphUrl } from "../router.js";

function fmtValue(v) {
  if (v == null) return "—";
  if (typeof v === "object") return JSON.stringify(v);
  return String(v);
}

function knobRow(item) {
  return el("div.cfg-row", {},
    el("div.cfg-head", {},
      el("span.cfg-key", {}, item.key),
      el("span.cfg-value", {}, fmtValue(item.value)),
      item.overridden
        ? el("span.cfg-overridden", { title: `default: ${fmtValue(item.default)}` },
            "customized")
        : null,
      item.advanced ? el("span.cfg-advanced", {}, "advanced") : null),
    item.help ? el("p.cfg-help", {}, item.help) : null,
    item.overridden
      ? el("p.cfg-default", {}, `default: ${fmtValue(item.default)}`)
      : null);
}

export async function renderConfig({ slug, scope }) {
  mount(el("div.loading", {}, "Loading configuration…"));

  let payload;
  try {
    payload = await api.config(slug);
  } catch (err) {
    toastError(err);
    navigate(graphUrl(slug, scope), { replace: true });
    return;
  }

  // preserve the engine's group order (Decay, Tiers, Freshness, …)
  const groups = new Map();
  for (const item of payload.items || []) {
    const g = item.group || "Other";
    if (!groups.has(g)) groups.set(g, []);
    groups.get(g).push(item);
  }

  const sections = [...groups.entries()].map(([group, items]) =>
    el("section.cfg-group", {},
      el("h2", {}, group),
      ...items.map(knobRow)));

  mount(el("div.page-view.config-view", {},
    el("nav.breadcrumb", {},
      el("a.crumb", {
        href: graphUrl(slug, scope),
        onclick: (ev) => { ev.preventDefault(); navigate(graphUrl(slug, scope)); },
      }, "← back to graph")),
    el("h1", {}, "Configuration"),
    el("p.subtitle", {},
      payload.exists === false
        ? "No config file yet — every knob is at its default. "
        : "Effective values for this graph. ",
      el("span.cfg-file", { title: payload.config_file }, payload.config_file || "")),
    ...sections,
    payload.view_only_note ? el("p.view-only-note", {}, payload.view_only_note) : null));
}
