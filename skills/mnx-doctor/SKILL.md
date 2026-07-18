---
name: mnx-doctor
description: Validate and repair a Mnemex Context Graph knowledge graph. Use this whenever the user wants to check the integrity/health of the knowledge graph, fix drift, after a manual edit to nodes, or as a gate before committing graph changes — and it runs automatically at the end of every mnx-promote apply (including its folded consolidate). Checks all integrity invariants (edge targets exist, index matches the folder, denormalized copies are fresh, reverse-map consistent, no dangling edges, hot bound, cross-links complete, config drift) and can self-heal DERIVED files without touching node knowledge.
---

# mnx-doctor — validator and self-healer

The safety net that makes an LLM-authored graph trustworthy. Nodes are truth; everything else is
derived and must agree with them. You check the invariants and, with `--fix`, regenerate the derived
files from the nodes. You never edit node knowledge.

Full invariant list with severities: `docs/invariants-and-failure-modes.md` (Part A). Helpers:
`mnx_binding` (locate + persist), `mnx_doctor.check`, `mnx_doctor.fix`, `mnx_resolve`, `mnx_index`,
`mnx_config`.

## Preflight — locate the graph (always first)
Run `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/mnx_binding.py" status --session <sid>` (the session id
from session-start, if you have one — see mnx-init step 1; honors a mid-session graph switch). If
`resolved` is false → **STOP** and point at `/mnemex:mnx-init`; if `clone_present` is false, run
`mnx_binding.py sync` once. Check/fix
operate on the returned **`graph_root`**, never the working directory. (When `mnx-doctor` runs *inside*
mnx-promote, it has already resolved it.) Add `--staging` to also run the staged-integrity check
(`mnx_doctor.py check-staging`) over the local capture tier.

## Check (read-only)
Run `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/mnx_doctor.py" check <graph_root>`. The **script** is the
source of truth for which checks run and their severities — you do not enumerate or re-derive the
invariants by hand; you run it and report what it returns. It covers the full suite — referential
integrity, schema, derived-state freshness, freshness horizons, tier/budget, telemetry/state, and
mesh/derivability — and returns one finding per problem as
`{invariant, severity (E/W/I), node-or-edge, detail}`. Report the findings grouped by severity.
(Fuller per-invariant reference, optional: Part A of `docs/invariants-and-failure-modes.md`.)

## Fix (`--fix`, derived files only)
Regenerate, from the nodes: every `index.md` (rebuild HOT/WARM/COLD, re-denormalize summary/aliases),
the reverse-edge map, and each team `cross-links.md`. This resolves all **warning**-level drift.
`fix` is idempotent — running it twice produces no further change. When run **standalone** (not inside
write/gc), persist the regenerated files with
`python3 "${CLAUDE_PLUGIN_ROOT}/scripts/mnx_binding.py" persist --message "mnx-doctor: heal derived files"`
so a remote graph's repair is pushed rather than discarded at the next session resync.

**Error-level invariants that involve node truth** (missing edge targets, invalid front-matter, a
changed id, an edge to a node that no longer exists) indicate node-level corruption, not derived drift.
**Report these for human/skill attention; do not auto-edit node knowledge to paper over them.**

## Duplication advisories (the soft limit)
Surface possible cross-cluster/cross-team duplicates (info level: *"possible duplicate of `<id>` in
`<other-cluster>` by alias overlap"*) so the human convergence ritual has a worklist. The protocol does
not auto-merge across clusters and must not claim global deduplication.

## Never
- Never edit a node's knowledge to satisfy an invariant — only regenerate derived files.
- Never auto-merge across teams.
- Never report integrity guarantees for soft cross-team references.
