// Thin client over the V1.1 read-only API (scripts/mnx_serve.py). Every failure —
// transport or the server's {ok:false, error:{code,message,action}} contract — is
// normalized to a thrown ApiError so views handle exactly one shape.

export class ApiError extends Error {
  constructor(code, message, action) {
    super(message);
    this.code = code;
    this.action = action || "";
  }
}

async function request(path, options = {}) {
  let res;
  try {
    res = await fetch(path, options);
  } catch (err) {
    throw new ApiError("network", "Cannot reach the viewer server.",
      "is `openmnemex serve` still running in the terminal?");
  }
  let body = null;
  try {
    body = await res.json();
  } catch (err) { /* non-JSON body; fall through to status handling */ }
  if (!res.ok || (body && body.ok === false)) {
    const e = (body && body.error) || {};
    throw new ApiError(e.code || `http-${res.status}`,
      e.message || `${res.status} ${res.statusText}`, e.action);
  }
  return body;
}

function post(path, payload) {
  return request(path, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: JSON.stringify(payload || {}),
  });
}

function qs(params) {
  const pairs = Object.entries(params || {}).filter(([, v]) => v != null && v !== "");
  if (!pairs.length) return "";
  return "?" + new URLSearchParams(Object.fromEntries(pairs)).toString();
}

const g = (slug) => `/api/graph/${encodeURIComponent(slug)}`;

export const api = {
  graphs: () => request("/api/graphs"),
  rescan: (path) => post("/api/graphs/rescan", path ? { path } : {}),
  createGraph: ({ path, org, team }) => post("/api/graphs/create", { path, org, team }),
  tree: (slug) => request(`${g(slug)}/tree`),
  nodes: (slug, { scope, at, include } = {}) =>
    request(`${g(slug)}/nodes${qs({ scope, at, include })}`),
  node: (slug, id, { at } = {}) =>
    request(`${g(slug)}/node/${encodeURIComponent(id)}${qs({ at })}`),
  search: (slug, q, limit) => request(`${g(slug)}/search${qs({ q, limit })}`),
  health: (slug) => request(`${g(slug)}/health`),
  queue: (slug, { at } = {}) => request(`${g(slug)}/queue${qs({ at })}`),
  config: (slug) => request(`${g(slug)}/config`),
  agents: () => request("/api/agents"),
  connectAgent: (agent) => post("/api/agents/connect", { agent }),
  fsDirs: (path) => request(`/api/fs/dirs${qs({ path })}`),
};
