---
description: Show Mnemex at a glance — what graph is bound, its kind, node/tier counts per team, pending usage stamps, last gc, and a health summary. Read-only; never syncs or repairs.
argument-hint: ""
---

Use the **mnx-status** skill to give the user a one-glance status of their Mnemex setup.

Run `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/mnx_status.py" status` and present the result concisely:

- If `resolved` is false → tell the user no graph is configured and point at `/mnemex:mnx-init`.
- Otherwise summarize: the bound graph (`graph_remote`/`graph_path` + `kind` + `source`), whether it is
  materialized (`clone_present`), totals (teams / clusters / nodes and hot/warm/cold), `pending_stamps`
  (un-pushed reads), `staging` (local un-promoted captures: `count`, `budget_level`, and the per-atom
  list — when non-empty, show each atom's `provisional_id` + `score` + `summary` so it can be reviewed),
  per-team `last_gc` (last consolidation) and any `gc_overdue_days`, and `health` (errors/warnings).
- If `available` is false, say the graph is bound but not synced this session and suggest running
  `/mnemex:mnx-read` (or starting a fresh session) to materialize it.
- If `health.errors > 0` recommend `/mnemex:mnx-doctor`; if any team is consolidation-overdue, recommend
  `/mnemex:mnx-promote` (consolidation is its back half). If `staging.budget_level` is `hard`, note that
  capture is refusing new atoms and the user can either `/mnemex:mnx-promote` or discard with
  `/mnemex:mnx-capture --drop <id>` / `--discard-all`.

This is purely informational — do not sync, commit, or repair anything.
