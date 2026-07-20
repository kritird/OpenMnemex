// Path routing, deep-linkable: "/" → graph chooser, "/g/{slug}" → main view,
// "/g/{slug}/atom/{id}" → full atom view (V1.3), "/g/{slug}/config" → read-only
// config screen and "/connections" → agent connection status (both V1.4). Scope
// (tree selection) travels as ?scope=<relative path>, a selected node as ?sel=<id>,
// so a pasted URL restores the exact view. The server returns index.html for "/",
// "/g/*" and "/connections".

export function parseRoute(loc = location) {
  const parts = loc.pathname.split("/").filter(Boolean);
  if (parts[0] === "connections") return { view: "connections" };
  if (parts[0] === "g" && parts[1]) {
    const slug = decodeURIComponent(parts[1]);
    const params = new URLSearchParams(loc.search);
    if (parts[2] === "atom" && parts[3]) {
      return {
        view: "atom",
        slug,
        id: decodeURIComponent(parts[3]),
        scope: params.get("scope") || "",
      };
    }
    if (parts[2] === "config") {
      return { view: "config", slug, scope: params.get("scope") || "" };
    }
    return {
      view: "graph",
      slug,
      scope: params.get("scope") || "",
      sel: params.get("sel") || "",
    };
  }
  return { view: "chooser" };
}

export function graphUrl(slug, scope, sel) {
  const base = `/g/${encodeURIComponent(slug)}`;
  const params = new URLSearchParams();
  if (scope) params.set("scope", scope);
  if (sel) params.set("sel", sel);
  const q = params.toString();
  return q ? `${base}?${q}` : base;
}

export function atomUrl(slug, id, scope) {
  const base = `/g/${encodeURIComponent(slug)}/atom/${encodeURIComponent(id)}`;
  return scope ? `${base}?scope=${encodeURIComponent(scope)}` : base;
}

export function configUrl(slug, scope) {
  const base = `/g/${encodeURIComponent(slug)}/config`;
  return scope ? `${base}?scope=${encodeURIComponent(scope)}` : base;
}

export const connectionsUrl = () => "/connections";

let renderFn = null;

export function navigate(url, { replace = false } = {}) {
  if (replace) history.replaceState(null, "", url);
  else history.pushState(null, "", url);
  if (renderFn) renderFn(parseRoute());
}

export function startRouter(render) {
  renderFn = render;
  addEventListener("popstate", () => renderFn(parseRoute()));
  renderFn(parseRoute());
}
