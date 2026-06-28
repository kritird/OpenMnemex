---
name: mnx-doctor
description: Validate and repair a Mnemex Context Graph knowledge graph. Use this whenever the user wants to check the integrity/health of the knowledge graph, fix drift, after a manual edit to nodes, or as a gate before committing graph changes — and it runs automatically at the end of every mnx-write and mnx-gc apply. Checks all integrity invariants (edge targets exist, index matches the folder, denormalized copies are fresh, reverse-map consistent, no dangling edges, hot bound, cross-links complete, config drift) and can self-heal DERIVED files without touching node knowledge.
---

# mnx-doctor — validator and self-healer

The safety net that makes an LLM-authored graph trustworthy. Nodes are truth; everything else is
derived and must agree with them. You check the invariants and, with `--fix`, regenerate the derived
files from the nodes. You never edit node knowledge.

Full invariant list with severities: `docs/08-invariants-and-failure-modes.md` (Part A). Helpers:
`mnx_doctor.check`, `mnx_doctor.fix`, `mnx_resolve`, `mnx_index`, `mnx_config`.

## Check (read-only)
Run the full suite and report findings grouped by severity:
- **Referential:** every edge target exists; no edge points at a tombstoned node (unless repointed);
  reverse map covers all tiers + tombstones; cross-links complete with accurate paths; soft cross-team
  references flagged if dangling (info only — they carry the no-integrity disclaimer).
- **Schema:** front-matter valid; ids are valid stable slugs; `pattern` nodes have a non-null
  `trigger`; timestamps UTC ISO-8601.
- **Derived freshness:** index node-set equals the folder node-set; `index.summary == node.summary`
  and `index.aliases == node.aliases` for every node; materialized strength/last_update present.
- **Tier/budget:** each cluster's hot section ≤ `hot_k`; no cluster over `node_budget`; orphans
  (zero inbound) flagged.
- **Telemetry/state:** high-water marks monotonic; config matches `.mnemex` (or re-normalization
  pending); no stranded `pass.plan.json` without a lock.

Report each finding as `{invariant, severity (E/W/I), node-or-edge, detail}`.

## Fix (`--fix`, derived files only)
Regenerate, from the nodes: every `index.md` (rebuild HOT/WARM/COLD, re-denormalize summary/aliases),
the reverse-edge map, and each team `cross-links.md`. This resolves all **warning**-level drift.
`fix` is idempotent — running it twice produces no further change.

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
