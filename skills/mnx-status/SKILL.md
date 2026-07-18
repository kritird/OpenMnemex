---
name: mnx-status
description: Show an at-a-glance status of the user's Mnemex knowledge graph — what graph is bound, its kind (git-remote / git-local / plain-local), node and hot/warm/cold tier counts per team, pending (un-pushed) usage stamps, last gc per team, a structural health summary, and every OTHER graph Mnemex knows about. Use this when the user asks "what's in my graph", "is mnemex set up / healthy", "what's bound here", "mnemex status", "how big is my knowledge graph", "what graphs do I have", "what other graphs exist", or wants to browse what knowledge exists before reading or writing. Read-only — never syncs, commits, or repairs.
---

# mnx-status — at-a-glance graph status

A friendly status/browse surface so the user can answer *"is my memory set up, what's in it, and is it
healthy?"* in one move — without running `mnx-doctor` (a validator/repair tool, not a status) or reading
indexes by hand. This skill is **strictly read-only**: it never clones, syncs, commits, or repairs.

Helper you call: `scripts/mnx_status.py` (status). It aggregates `mnx_binding` (the binding),
`mnx_stamp` (pending stamps), `mnx_common` (node/tier counts), and `mnx_doctor` (health counts).

## Procedure

Run `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/mnx_status.py" status --session <sid>` (pass the session
id from session-start if you have one — see mnx-init step 1; honors a mid-session graph switch) and
read the single JSON object.

### Not configured
If `resolved` is false → check `known_graphs` first: if it has entries, tell the user they have no
graph bound *here* but list the graphs they've used elsewhere (name, kind, `present`) so they can bind
to one (`/mnemex:mnx-init` → connect to an existing graph) instead of assuming they must create a new
one. If `known_graphs` is empty, just point them at `/mnemex:mnx-init`. Stop either way.

### Bound but not materialized
If `available` is false (`clone_present` false) → the graph is bound but not synced into this session
yet. Report what it is bound to (`binding.graph_remote` / `graph_path` + `kind`) and suggest running
`/mnemex:mnx-read` once (or starting a fresh session) to materialize it, then re-checking status.

### Configured and available
Summarize concisely (don't dump raw JSON):

- **Binding** — lead with the `resolution` line (human `display_name` + source, e.g. *"payments-knowledge
  (source: project .mnemex.md)"*), then `graph_remote`/`graph_path` and `kind`. If `default_fallback` is
  true, flag it: no project `.mnemex.md` matched, so operations here fall through to the user's personal
  graph.
- **Contents** — `totals` (teams / clusters / nodes) and the hot/warm/cold tier spread; optionally list
  team names and their `cluster_names` so the user can see what domains exist.
- **Pending usage stamps** — `pending_stamps` (reads recorded but not yet pushed). For `plain-local` /
  `git-local` graphs this is `0` by design (`stamp_durability: on-disk`).
- **Staged captures** — `staging`: `count`, `budget_level` (`ok`/`soft`/`hard`), `urgent`, and the
  per-atom `atoms` list (each `provisional_id` · `score` · `type` · `summary` · `staged_at`). When
  `count > 0`, list the atoms (newest first) so the user can *see* what is pending promotion — and tell
  them they can drop one with `/mnemex:mnx-capture --drop <provisional_id>` or clear all with
  `/mnemex:mnx-capture --discard-all` (this skill only reports; discard is a capture action).
- **Held contradictions** — `staging.held`: `count` (and `oldest_age_days` / `lingering_nag` when
  present) — atoms a prior promote could not reconcile against the graph, parked in the local held queue
  (W9). When `count > 0`, note them so the user knows a past contradiction is still awaiting resolution
  at the next `/mnemex:mnx-promote`; `lingering_nag` means one has aged past `held_max_age_days`.
- **Maintenance** — each team's `last_gc` and any `gc_overdue_days`.
- **Health** — `health.errors` / `health.warnings` from the doctor's invariant suite.
- **Known graphs** — `known_graphs` lists every OTHER graph Mnemex has registered (name, kind,
  location, `present`), not just this one. Only mention it if the user asks, or if one entry looks
  like it might be what they actually meant to bind here — this list is for discovery, not noise on
  every status check. To use a different one for just this session: `mnx_binding.py use-graph <slug>
  --session <sid>` (revert with `clear-graph-override --session <sid>`) — session-scoped only; point
  them at `/mnemex:mnx-init` instead if they want the switch to stick.
- **Override active** — if `override_notice` is present, always relay it verbatim: it means this
  session is currently reading/writing a DIFFERENT graph than this project/user would normally
  resolve. Never suppress it, even if the user didn't ask about graphs.

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
