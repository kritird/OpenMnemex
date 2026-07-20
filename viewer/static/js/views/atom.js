// Full atom view (plan §6 screen 3, V1.3): rendered markdown, clickable
// [[wiki-links]], breadcrumb, front-matter table, "reveal in canvas".
//
// The body is rendered with vendored marked and ALWAYS passed through DOMPurify —
// atom bodies can contain ingested third-party markdown (real-repo ingestion), so
// raw HTML must never reach the DOM unsanitized (see vendor/VENDOR.md).

import { api } from "../api.js";
import { el, mount, toastError } from "../ui.js";
import { navigate, graphUrl, atomUrl } from "../router.js";
import { badges, kv, chipRow } from "./inspector.js";

// mirrors the engine: [[name]] / [[name|Display]], key = lowercased alnum words
// (mnx_common.parse_wikilinks) — used only to LOOK UP the server-resolved mention,
// never to resolve links ourselves.
const WIKI_RE = /\[\[([^\[\]]+?)\]\]/g;

function normKey(name) {
  return name.toLowerCase().replace(/[^a-z0-9]+/g, " ").trim();
}

function mentionMap(frontMatter) {
  const map = new Map();
  for (const m of frontMatter.mentions || []) {
    if (m && m.name && m.resolved_id) map.set(normKey(m.name), m.resolved_id);
  }
  return map;
}

/** Replace [[wiki-links]] in text nodes (outside code/pre/a) with live links. */
function linkifyWikiLinks(root, mentions, onHop) {
  const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT, {
    acceptNode: (n) => (n.parentElement && n.parentElement.closest("pre, code, a")
      ? NodeFilter.FILTER_REJECT : NodeFilter.FILTER_ACCEPT),
  });
  const texts = [];
  for (let n = walker.nextNode(); n; n = walker.nextNode()) {
    if (n.nodeValue.includes("[[")) texts.push(n);
  }
  for (const textNode of texts) {
    const text = textNode.nodeValue;
    const frag = document.createDocumentFragment();
    let last = 0;
    WIKI_RE.lastIndex = 0;
    for (let m = WIKI_RE.exec(text); m; m = WIKI_RE.exec(text)) {
      frag.append(text.slice(last, m.index));
      const parts = m[1].split("|").map((p) => p.trim());
      const name = parts[0];
      const display = parts[1] || name;
      const resolved = name ? mentions.get(normKey(name)) : null;
      if (resolved) {
        frag.append(el("a.wiki-link", {
          href: "#",
          title: resolved,
          onclick: (ev) => { ev.preventDefault(); onHop(resolved); },
        }, display));
      } else {
        // red-link: the engine found no atom behind this name (ghost in the mesh)
        frag.append(el("span.wiki-ghost", { title: "red-link — not written yet" }, display));
      }
      last = m.index + m[0].length;
    }
    frag.append(text.slice(last));
    textNode.replaceWith(frag);
  }
}

function renderBody(body, frontMatter, onHop) {
  const host = el("div.md-body");
  if (!body || !body.trim()) {
    host.append(el("p.atom-empty", {}, "This atom has no body text."));
    return host;
  }
  host.innerHTML = DOMPurify.sanitize(marked.parse(body, { gfm: true }));
  linkifyWikiLinks(host, mentionMap(frontMatter), onHop);
  for (const a of host.querySelectorAll("a[href]")) {
    if (/^https?:/i.test(a.getAttribute("href"))) {
      a.target = "_blank";
      a.rel = "noopener noreferrer";
    }
  }
  return host;
}

function crumb(parts) {
  const items = [];
  parts.forEach((p, i) => {
    if (i) items.push(el("span.crumb-sep", {}, "/"));
    items.push(p.href
      ? el("a.crumb", {
          href: p.href,
          onclick: (ev) => { ev.preventDefault(); navigate(p.href); },
        }, p.label)
      : el("span.crumb.current", { title: p.label }, p.label));
  });
  return el("nav.breadcrumb", {}, items);
}

function frontMatterTable(fm) {
  // mesh/mentions render as chips above; show the remaining scalar front matter
  const skip = new Set(["edges", "references", "mentions", "title", "summary"]);
  const rows = Object.entries(fm)
    .filter(([k, v]) => !skip.has(k) && (typeof v !== "object" || v === null))
    .map(([k, v]) => [k, v]);
  return kv(rows);
}

export async function renderAtom({ slug, id, scope }) {
  mount(el("div.loading", {}, "Loading atom…"));

  let detail;
  try {
    detail = await api.node(slug, id);
  } catch (err) {
    toastError(err);
    // ghosts/staged captures have no atom file (the API 404s them by design) —
    // land the user back on the canvas with the node selected instead of a dead end
    navigate(graphUrl(slug, scope, id), { replace: true });
    return;
  }

  const entry = detail.node;
  const fm = (detail.atom && detail.atom.front_matter) || {};
  const mesh = detail.mesh || {};
  const history = detail.history || {};
  const onHop = (nid) =>
    (nid.startsWith("ghost:") || nid.startsWith("stg-")
      ? navigate(graphUrl(slug, scope, nid))
      : navigate(atomUrl(slug, nid, scope)));

  document.title = `${entry.title || id} · ${slug} · OpenMnemex`;

  mount(el("div.atom-view", {},
    crumb([
      { label: slug, href: graphUrl(slug, scope) },
      entry.team && entry.team !== "(root)"
        ? { label: entry.team, href: graphUrl(slug, entry.team) } : null,
      entry.cluster && entry.cluster !== entry.team
        ? { label: entry.cluster.split("/").pop(), href: graphUrl(slug, entry.cluster) }
        : null,
      { label: entry.id },
    ].filter(Boolean)),
    el("div.atom-head", {},
      el("h1", {}, entry.title || entry.id),
      el("button.btn", {
        title: "Show this node selected on the canvas",
        onclick: () => navigate(graphUrl(slug, scope || entry.cluster, entry.id)),
      }, "Reveal in canvas")),
    badges(entry),
    entry.summary ? el("p.atom-summary", {}, entry.summary) : null,
    renderBody((detail.atom || {}).body, fm, onHop),
    chipRow("Links out", mesh.out, onHop),
    chipRow("Linked from", mesh.in, onHop),
    chipRow("Red links", (mesh.red_links || []).map((r) => ({
      id: r.ghost_id, title: r.name, type: "red-link",
    })), onHop),
    chipRow("Superseded by", history.superseded_by_chain, onHop),
    chipRow("Supersedes", history.supersedes, onHop),
    el("div.atom-fm", {},
      el("h3", {}, "Front matter"),
      frontMatterTable(fm),
      el("div.insp-path", { title: entry.path }, entry.path))));
}
