---
name: mnx-status
description: Show an at-a-glance status of the user's Mnemex knowledge graph — what graph is bound, its kind (git-remote / git-local / plain-local), node and hot/warm/cold tier counts per team, pending (un-pushed) usage stamps, last gc per team, and a structural health summary. Use this when the user asks "what's in my graph", "is mnemex set up / healthy", "what's bound here", "mnemex status", "how big is my knowledge graph", or wants to browse what knowledge exists before reading or writing. Read-only — never syncs, commits, or repairs.
---

# mnx-status — at-a-glance graph status

A friendly status/browse surface so the user can answer *"is my memory set up, what's in it, and is it
healthy?"* in one move — without running `mnx-doctor` (a validator/repair tool, not a status) or reading
indexes by hand. This skill is **strictly read-only**: it never clones, syncs, commits, or repairs.

Helper you call: `scripts/mnx_status.py` (status). It aggregates `mnx_binding` (the binding),
`mnx_stamp` (pending stamps), `mnx_common` (node/tier counts), and `mnx_doctor` (health counts).

## Procedure

Run `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/mnx_status.py" status` and read the single JSON object.

### Not configured
If `resolved` is false → tell the user there is no Mnemex graph configured for this project and point
them at `/mnemex:mnx-init`. Stop.

### Bound but not materialized
If `available` is false (`clone_present` false) → the graph is bound but not synced into this session
yet. Report what it is bound to (`binding.graph_remote` / `graph_path` + `kind`) and suggest running
`/mnemex:mnx-read` once (or starting a fresh session) to materialize it, then re-checking status.

### Configured and available
Summarize concisely (don't dump raw JSON):

- **Binding** — `graph_remote` or `graph_path`, the `kind`, and where it came from (`source`:
  project `.mnemex.md` / env / user default).
- **Contents** — `totals` (teams / clusters / nodes) and the hot/warm/cold tier spread; optionally list
  team names and their `cluster_names` so the user can see what domains exist.
- **Pending usage stamps** — `pending_stamps` (reads recorded but not yet pushed). For `plain-local` /
  `git-local` graphs this is `0` by design (`stamp_durability: on-disk`).
- **Staged captures** — `staging`: `count`, `budget_level` (`ok`/`soft`/`hard`), `urgent`, and the
  per-atom `atoms` list (each `provisional_id` · `score` · `type` · `summary` · `staged_at`). When
  `count > 0`, list the atoms (newest first) so the user can *see* what is pending promotion — and tell
  them they can drop one with `/mnemex:mnx-capture --drop <provisional_id>` or clear all with
  `/mnemex:mnx-capture --discard-all` (this skill only reports; discard is a capture action).
- **Maintenance** — each team's `last_gc` and any `gc_overdue_days`.
- **Health** — `health.errors` / `health.warnings` from the doctor's invariant suite.

### Recommend the next step (only when warranted)
- `health.errors > 0` → recommend `/mnemex:mnx-doctor --fix`.
- any team `gc_overdue_days > 0` (or `last_gc` null) → recommend `/mnemex:mnx-promote` (consolidation
  is its back half; there is no standalone gc).
- `staging.budget_level == "hard"` → tell the user capture is now refusing new atoms; they can drain
  staging with `/mnemex:mnx-promote` **or** make room by discarding with `/mnemex:mnx-capture --drop
  <id>` / `--discard-all`.
- `pending_stamps` is high and the session is ending → note they will flush on Stop/SessionEnd.

## Never
- Never sync, clone, commit, push, or repair from this skill — it only reports.
- Never rewrite a node, index, or registry.
