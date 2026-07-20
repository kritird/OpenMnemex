// Dark mode: follows the OS by default; the header toggle stores an explicit choice
// (plan §5). No stored choice → no data-theme attribute → theme.css media query rules.

const KEY = "mnx-theme";

function stored() {
  try {
    const t = localStorage.getItem(KEY);
    return t === "light" || t === "dark" ? t : null;
  } catch (err) {
    return null;
  }
}

export function effectiveTheme() {
  return stored()
    || (matchMedia("(prefers-color-scheme: dark)").matches ? "dark" : "light");
}

export function toggleTheme() {
  const next = effectiveTheme() === "dark" ? "light" : "dark";
  try { localStorage.setItem(KEY, next); } catch (err) { /* private mode */ }
  document.documentElement.dataset.theme = next;
  dispatchEvent(new CustomEvent("mnx-theme-changed", { detail: { theme: next } }));
}

export function initTheme() {
  // index.html's boot script already applied any stored choice pre-paint; here we
  // only track OS flips while running in follow-OS mode.
  matchMedia("(prefers-color-scheme: dark)").addEventListener("change", () => {
    if (!stored()) {
      dispatchEvent(new CustomEvent("mnx-theme-changed",
        { detail: { theme: effectiveTheme() } }));
    }
  });
}
