// Header search (V1.3): debounced /search, dropdown of hits. Picking a hit lands on
// the canvas with the node selected (?sel=) — the atom view is one more click from
// there. Deliberately absent from the chooser: search is per-graph.

import { api } from "./api.js";
import { el } from "./ui.js";
import { navigate, graphUrl } from "./router.js";

const DEBOUNCE_MS = 250;

function hitRow(hit, onPick) {
  return el("button.search-hit", { onclick: () => onPick(hit) },
    el("span.hit-id", {}, hit.id),
    hit.tier ? el("span.hit-tier", {}, hit.tier) : null,
    el("span.hit-text", {},
      hit.summary || hit.snippet || (hit.match === "content" ? "body match" : "")));
}

export function searchBox(slug, scope) {
  const input = el("input.search-input", {
    type: "search",
    placeholder: "Search this graph…",
    autocomplete: "off",
    spellcheck: false,
  });
  const pop = el("div.search-pop", { hidden: true });
  const box = el("div.header-search", {}, input, pop);

  let timer = null;
  let lastQ = "";
  let hits = [];

  const close = () => { pop.hidden = true; pop.replaceChildren(); hits = []; };
  const pick = (hit) => {
    close();
    input.value = "";
    navigate(graphUrl(slug, scope, hit.id));
  };

  const run = async (q) => {
    if (q !== input.value.trim()) return;   // superseded while the request ran
    let out;
    try {
      out = await api.search(slug, q, 12);
    } catch (err) {
      pop.replaceChildren(el("div.search-empty", {}, String(err.message || err)));
      pop.hidden = false;
      return;
    }
    if (q !== input.value.trim()) return;
    hits = out.hits || [];
    pop.replaceChildren(...(hits.length
      ? hits.map((h) => hitRow(h, pick))
      : [el("div.search-empty", {}, "No matches.")]));
    pop.hidden = false;
  };

  input.addEventListener("input", () => {
    clearTimeout(timer);
    const q = input.value.trim();
    lastQ = q;
    if (!q) { close(); return; }
    timer = setTimeout(() => run(q), DEBOUNCE_MS);
  });
  input.addEventListener("keydown", (ev) => {
    if (ev.key === "Escape") { input.value = ""; close(); input.blur(); }
    else if (ev.key === "Enter" && hits.length) pick(hits[0]);
  });
  // close on click-away; a hit's own click fires first (mousedown ordering)
  document.addEventListener("click", (ev) => {
    if (!box.contains(ev.target)) close();
  });

  return box;
}
