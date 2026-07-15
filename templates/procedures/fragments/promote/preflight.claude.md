## Preflight
1. **Locate + sync:** `mnx_binding.py status`. If `resolved` is false → **STOP**, point at
   `/mnemex:mnx-init`. **Echo the resolved graph before merging** — this is the irreversible write, so
   confirm the target: show the `resolution` line, e.g. *"Promoting into **payments-knowledge** (source:
   project .mnemex.md)."* If `default_fallback` is true, flag it prominently and **confirm with the user
   before merging** (*"⚠️ No project binding here — this will merge staged atoms into your personal graph
   **personal-notes**. Continue?"*) so a mis-resolved promote can't silently land in the wrong graph
   (LIMITATIONS.md #2). If `clone_present` is false → `mnx_binding.py sync` once. Operate on
   `graph_root`; note `kind`. If staging is empty (`mnx_stage.py status` → `count == 0`), promote is
   just "consolidate the graph" — say so and proceed (or stop if nothing is overdue either).
2. **Unpushed-promote guard (avoid double-apply):** if `status` reports `unpushed: true` (`ahead > 0`),
   a previous promote **committed the merge but did not push**. **Do NOT start a fresh merge** — that
   would re-apply staging on top of the existing commit. Go straight to the **Retry-push recovery**
   below (treat it as if `--retry-push` were given). A fresh promote is only safe when `ahead == 0`.
3. **Team lock:** `mnx_lock.acquire`. If a pass is already in progress, stop and tell the user.
   Recover any stranded `pass.plan.json` first.

