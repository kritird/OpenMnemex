---
description: Ingest the current build session (artifact + human review points) into the Mnemex knowledge graph — extract domain facts and patterns, reconcile against existing nodes, show a change plan for approval, then apply atomically.
argument-hint: "[--cluster <path>] [--dry-run]"
---

Use the **mnx-write** skill to capture the knowledge produced in THIS session into the Mnemex graph.

Options: $ARGUMENTS

Read the artifact and the human clarifications/review points from the current conversation. Run the four
phases: Extract (tag domain vs pattern; turn review corrections into patterns with a `trigger`),
Reconcile (classify new/update/merge/contradiction against cluster indexes; lazy cold reconciliation per
config), Plan (present a surgical change plan and STOP for human approval — also stop here on
`--dry-run`), Apply (take the team lock, write nodes + edges, regenerate affected indexes, delta-update
cross-links, run mnx-doctor, one git commit, release lock).
