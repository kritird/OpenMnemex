// Small DOM + formatting helpers shared by the views. No framework, by design.

/** el("button.btn", {onclick}, ...children) — hyperscript-style element builder. */
export function el(spec, attrs, ...children) {
  const [tag, ...classes] = spec.split(".");
  const node = document.createElement(tag || "div");
  if (classes.length) node.className = classes.join(" ");
  for (const [key, value] of Object.entries(attrs || {})) {
    if (value == null) continue;
    if (key.startsWith("on") && typeof value === "function") {
      node.addEventListener(key.slice(2), value);
    } else if (key === "dataset") {
      Object.assign(node.dataset, value);
    } else if (key in node && key !== "list") {
      node[key] = value;
    } else {
      node.setAttribute(key, value);
    }
  }
  node.append(...children.flat(Infinity).filter((c) => c != null && c !== false));
  return node;
}

/** Replace #view's content. */
export function mount(...children) {
  const view = document.getElementById("view");
  view.replaceChildren(...children);
  return view;
}

/** "3 min ago" from an ISO timestamp; empty string when unknown. */
export function timeAgo(iso) {
  if (!iso) return "";
  const then = Date.parse(iso);
  if (Number.isNaN(then)) return "";
  const s = Math.max(0, (Date.now() - then) / 1000);
  if (s < 60) return "just now";
  const units = [[86400 * 365, "y"], [86400 * 30, "mo"], [86400, "d"], [3600, "h"], [60, "min"]];
  for (const [secs, label] of units) {
    if (s >= secs) return `${Math.floor(s / secs)}${label} ago`;
  }
  return "just now";
}

/** Error toast for an ApiError (or anything with message/action). Auto-dismisses. */
export function toastError(err) {
  const host = document.getElementById("toasts");
  const toast = el("div.toast", {},
    el("div", {}, String(err.message || err)),
    err.action ? el("div.action", {}, err.action) : null);
  host.append(toast);
  setTimeout(() => toast.remove(), 8000);
  toast.addEventListener("click", () => toast.remove());
}
