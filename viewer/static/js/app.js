// Entry point: header, theme, router. Views render into #view (see router.js for
// the URL scheme). Plain ES modules, no build step — this file is loaded directly
// by index.html.

import { el } from "./ui.js";
import { initTheme, toggleTheme, effectiveTheme } from "./theme.js";
import { startRouter, parseRoute, navigate } from "./router.js";
import { renderChooser } from "./views/chooser.js";
import { renderGraph, graphViewCleanup } from "./views/graph.js";
import { renderAtom } from "./views/atom.js";
import { renderConfig } from "./views/config.js";
import { renderConnections } from "./views/connections.js";
import { searchBox } from "./search.js";

function themeIcon() {
  return effectiveTheme() === "dark" ? "☀" : "☾";
}

function renderHeader(route) {
  const toggle = el("button.icon-btn", {
    title: "Toggle light/dark (follows the OS until you choose)",
    onclick: () => toggleTheme(),
  }, themeIcon());
  addEventListener("mnx-theme-changed", () => { toggle.textContent = themeIcon(); });

  document.getElementById("header").replaceChildren(...[
    el("a.brand", {
      href: "/",
      onclick: (ev) => { ev.preventDefault(); navigate("/"); },
    },
      el("img", { src: "/static/logo.svg", alt: "OpenMnemex" }),
      el("span.wordmark", {},
        el("span.open", {}, "open"), el("span.mnemex", {}, "mnemex"))),
    route.slug ? el("span.crumb-sep", {}, "/") : null,
    route.slug ? el("span.graph-name", {}, route.slug) : null,
    el("span.spacer"),
    route.slug ? searchBox(route.slug, route.scope) : null,
    toggle,
  ].filter(Boolean));
}

function render(route) {
  graphViewCleanup();
  renderHeader(route);
  document.title = route.view === "connections" ? "Add agents · OpenMnemex Console"
    : route.slug ? `${route.slug} · OpenMnemex Console` : "OpenMnemex Console";
  if (route.view === "graph") renderGraph(route);
  else if (route.view === "atom") renderAtom(route);
  else if (route.view === "config") renderConfig(route);
  else if (route.view === "connections") renderConnections();
  else renderChooser();
}

initTheme();
startRouter(render);
