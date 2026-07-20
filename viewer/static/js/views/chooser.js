// Welcome / graph chooser (plan §6 screen 1): graph cards, "Open a folder…",
// "Rescan this machine", and the first-run empty state offering the ONE write the
// viewer will ever do — create a brand-new empty graph via the shared mnx_init
// scaffolder (server refuses non-empty targets; see POST /api/graphs/create).

import { api } from "../api.js";
import { el, mount, timeAgo, toastError } from "../ui.js";
import { navigate, graphUrl, configUrl, connectionsUrl } from "../router.js";

// A div, not a button: the card opens the graph, but it also hosts its own
// "configuration" action (nested buttons are invalid HTML), per Kriti 2026-07-20 —
// config belongs with the graph here, not behind a tiny header icon.
function graphCard(card) {
  return el("div.graph-card", {
    role: "button", tabindex: 0,
    onclick: () => navigate(graphUrl(card.slug)),
    onkeydown: (ev) => { if (ev.key === "Enter") navigate(graphUrl(card.slug)); },
  },
    el("div.name", {},
      el("span", {}, card.name),
      card.staged > 0 ? el("span.staged-badge", {}, `${card.staged} staged`) : null),
    el("div.path", { title: card.path }, card.path),
    el("div.meta", {},
      el("span", {}, el("b", {}, String(card.nodes)), " nodes"),
      el("span", {}, el("b", {}, String(card.clusters)), " clusters"),
      card.last_activity
        ? el("span", {}, "active ", timeAgo(card.last_activity))
        : null,
      el("span", { style: "flex:1" }),
      el("button.btn.card-config", {
        title: "This graph's settings, explained (read-only)",
        onclick: (ev) => { ev.stopPropagation(); navigate(configUrl(card.slug)); },
      }, "configuration")));
}

function field(labelText, inputAttrs, hint) {
  const input = el("input", inputAttrs);
  const wrap = el("div", {}, el("label", {}, labelText), input,
    hint ? el("div.hint", {}, hint) : null);
  return [wrap, input];
}

// A path field with a "Browse…" button. Browsers never hand a web page the absolute
// path a native Finder/file picker selects (even from localhost), so browsing goes
// through the server's read-only /api/fs/dirs instead.
function pathField(labelText, inputAttrs, hint) {
  const input = el("input", inputAttrs);
  const wrap = el("div", {},
    el("label", {}, labelText),
    el("div.path-row", {},
      input,
      el("button.btn", { type: "button", onclick: () => folderBrowserDialog(input) },
        "Browse…")),
    hint ? el("div.hint", {}, hint) : null);
  return [wrap, input];
}

function folderBrowserDialog(pathInput) {
  const listBox = el("div.fs-list", {});
  const pathLabel = el("div.fs-path", {});
  const useBtn = el("button.btn.btn-primary", { type: "button" }, "Use this folder");
  let current = null;

  async function load(path) {
    listBox.replaceChildren(el("div.insp-loading", {}, "Loading…"));
    let payload;
    try {
      payload = await api.fsDirs(path);
    } catch (err) {
      if (path) { load(null); return; }   // bad seed path → fall back to $HOME
      listBox.replaceChildren(el("div.fs-error", {},
        String(err.message || err)));
      return;
    }
    current = payload.path;
    pathLabel.textContent = payload.path;
    pathLabel.title = payload.path;
    const rows = [];
    if (payload.parent) {
      rows.push(el("button.fs-row.fs-up", { type: "button", onclick: () => load(payload.parent) },
        el("span.fs-name", {}, ".. (up)")));
    }
    for (const d of payload.dirs) {
      rows.push(el("button.fs-row", { type: "button", onclick: () => load(d.path), title: d.path },
        el("span.fs-name", {}, d.name),
        d.is_graph ? el("span.fs-graph-badge", {}, "graph") : null));
    }
    if (payload.denied) {
      rows.push(el("div.fs-error", {}, "Some folders were unreadable (permission denied)."));
    }
    if (!payload.dirs.length && !payload.denied) {
      rows.push(el("div.fs-empty", {}, "No subfolders."));
    }
    listBox.replaceChildren(...rows);
  }

  const dialog = el("dialog.mnx-dialog.fs-dialog", {},
    el("h2", {}, "Choose a folder"),
    pathLabel,
    listBox,
    el("div.dialog-actions", {},
      el("button.btn", { type: "button", onclick: () => load(null) }, "Home"),
      el("span", { style: "flex:1" }),
      el("button.btn", { type: "button", onclick: () => dialog.close() }, "Cancel"),
      useBtn));
  useBtn.addEventListener("click", () => {
    if (current) {
      pathInput.value = current;
      pathInput.dispatchEvent(new Event("input", { bubbles: true }));
    }
    dialog.close();
  });
  dialog.addEventListener("close", () => dialog.remove());
  document.body.append(dialog);
  dialog.showModal();
  // Seed from a typed absolute path when there is one; otherwise start at $HOME.
  load(pathInput.value.trim().startsWith("/") ? pathInput.value.trim() : null);
}

function dialogForm({ title, fields, submitLabel, onSubmit }) {
  const errorBox = el("div.error", { hidden: true });
  const submit = el("button.btn.btn-primary", { type: "submit" }, submitLabel);
  const dialog = el("dialog.mnx-dialog", {},
    el("form", {
      method: "dialog",
      onsubmit: async (ev) => {
        ev.preventDefault();
        errorBox.hidden = true;
        submit.disabled = true;
        try {
          await onSubmit();
          dialog.close();
        } catch (err) {
          errorBox.textContent = err.action ? `${err.message}\n→ ${err.action}` : String(err.message || err);
          errorBox.hidden = false;
        } finally {
          submit.disabled = false;
        }
      },
    },
      el("h2", {}, title),
      ...fields,
      errorBox,
      el("div.dialog-actions", {},
        el("button.btn", { type: "button", onclick: () => dialog.close() }, "Cancel"),
        submit)));
  dialog.addEventListener("close", () => dialog.remove());
  document.body.append(dialog);
  dialog.showModal();
  return dialog;
}

function openFolderDialog() {
  const [pathRow, pathInput] = pathField("Folder path",
    { placeholder: "~/knowledge/my-graph", required: true, autofocus: true },
    "The folder holding mnemex.config.md (or any folder inside the graph).");
  dialogForm({
    title: "Open a folder",
    fields: [pathRow],
    submitLabel: "Open",
    onSubmit: async () => {
      const res = await api.rescan(pathInput.value.trim());
      navigate(graphUrl(res.graph.slug));
    },
  });
}

function createGraphDialog() {
  const [pathRow, pathInput] = pathField("New folder path",
    { placeholder: "~/knowledge/my-graph", required: true, autofocus: true },
    "Must be a new or empty folder — the viewer never touches existing data. "
    + "Browse to a parent folder, then add the new folder's name to the path.");
  const [orgField, orgInput] = field("Organization name (optional)",
    { placeholder: "my-org" });
  const [teamField, teamInput] = field("First team (optional)",
    { placeholder: "core" });
  dialogForm({
    title: "Create your first graph",
    fields: [pathRow, orgField, teamField],
    submitLabel: "Create graph",
    onSubmit: async () => {
      const res = await api.createGraph({
        path: pathInput.value.trim(),
        org: orgInput.value.trim() || undefined,
        team: teamInput.value.trim() || undefined,
      });
      navigate(graphUrl(res.graph.slug));
    },
  });
}

async function rescanMachine(button) {
  const original = button.textContent;
  button.disabled = true;
  button.textContent = "Scanning…";
  try {
    await api.rescan();
    renderChooser();
  } catch (err) {
    toastError(err);
    button.disabled = false;
    button.textContent = original;
  }
}

export async function renderChooser() {
  mount(el("div.loading", {}, "Loading graphs…"));
  let payload;
  try {
    payload = await api.graphs();
  } catch (err) {
    toastError(err);
    mount(el("div.loading", {}, "Could not load graphs."));
    return;
  }

  const rescanBtn = el("button.btn", {}, "Rescan this machine");
  rescanBtn.addEventListener("click", () => rescanMachine(rescanBtn));

  const body = payload.empty
    ? el("div.empty-state", {},
        el("img.logo", { src: "/static/logo.svg", alt: "" }),
        el("h2", {}, "No graphs on this machine yet"),
        el("p", {}, payload.empty_state
          ? payload.empty_state.message
          : "Create your first graph, open a folder, or rescan."),
        el("button.btn.btn-primary", { onclick: createGraphDialog },
          "Create your first graph"))
    : el("div.card-grid", {}, payload.graphs.map(graphCard));

  mount(el("div.chooser", {},
    el("div.chooser-inner", {},
      el("h1", {}, "Graphs"),
      el("p.subtitle", {},
        payload.empty
          ? "A graph is a folder of markdown atoms — plain files, in git."
          : `${payload.count} graph${payload.count === 1 ? "" : "s"} on this machine. `
            + "The Console is read-only over knowledge: browsing never changes a file."),
      el("div.actions", {},
        el("button.btn", { onclick: openFolderDialog }, "Open a folder…"),
        rescanBtn,
        payload.empty
          ? null
          : el("button.btn.btn-ghost", { onclick: createGraphDialog }, "New graph…"),
        el("button.btn.btn-ghost", { onclick: () => navigate(connectionsUrl()) },
          "Add agents")),
      body)));
}
