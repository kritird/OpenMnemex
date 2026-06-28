---
name: mnx-read
description: Retrieve knowledge from a Mnemex Protocol knowledge graph. Use this whenever the user asks a question that should be answered from accumulated domain knowledge or patterns stored in a Mnemex repo, references "the knowledge graph", "what we know about", "our patterns for", or wants prior domain context loaded before a task — even if they don't say "mnemex". Routes structurally through tiered indexes, reads in chunks to stay within context budget, expands only needed nodes, and stamps usage. Pure with respect to knowledge (never rewrites nodes or indexes).
---

# mnx-read — tiered, budget-aware retrieval

Retrieve from the Mnemex graph in the current repo **without context bloat**. You route by reading
small index heads, open only what you commit to, and record what you actually used. You never mutate
knowledge — your only write is appending usage stamps to a registry.

Background: `docs/01-rationale-and-concepts.md`, `docs/02-architecture.md`. Helpers you call:
`scripts/mnx_compact.py` (overdue check, registry-tail fold), `scripts/mnx_resolve.py` (id→path),
`scripts/mnx_decay.py` (true current score when labels are stale).

## Procedure

### 1. Overdue / config-drift check (warn only)
Run `mnx_compact.py overdue`. If compaction is overdue or `λ`/`config_version` has drifted, tell the
user in one line: *"Knowledge maintenance is N days overdue — run `/mnemex-protocol:mnx-gc`."* You may
append one `__maintenance-due__` marker line to the nearest registry. **Do not compact. Do not run gc.**

### 2. Route (chunk-1 reads only)
- Read the org `index.md` head → choose the team(s) whose description matches the request.
- Read each team `index.md` head → choose the domain cluster(s).
Use ranged reads; do not load whole index files. Routing is structural — match on the one-line
descriptions and the node `summary`/`aliases`, not on guesswork.

### 3. Scan tiers in order, stop early
For each chosen cluster:
- Read **Hot** (chunk 1). Often enough — stop if it answers the request.
- Read **Warm** (chunk 2) only if Hot is insufficient.
- Read **Cold** (chunk 3+) only on a deliberate deep search, or when the request clearly concerns a
  dormant concept (alias/domain overlap with a cold entry).
If you need a node's *true current* score (tier labels are only as fresh as the last gc), fold the
registry tail with `mnx_decay.score` rather than trusting the stale label.

### 4. Expand only on commit
Resolve candidate ids to paths with `mnx_resolve.resolve` (local index for intra-cluster, team
`cross-links.md` for cross-cluster). Load **only** the node bodies you decide to use. When following
edges, stay within a frontier budget (a small max number of nodes / tokens per hop) — this is beam
search, not "load every neighbor". Loading a domain node, also pull its `governed-by` pattern nodes so
you get the *what* and the *how* together.

### 5. Emit the usage manifest (the gate)
At the **end** of the task, output a manifest — one entry per node **whose body you loaded**:

```
USAGE MANIFEST
- {id: iso8583-field124, role: contributed, why: "supplied the Field 124 routing semantics in the answer"}
- {id: visanet-routing,  role: consulted,   why: "checked routing topology to confirm the path"}
- {id: legacy-de124-fmt, role: traversed,   why: "opened while routing; not used"}
```

Rules:
- **Every body-load needs a disposition.** You may not silently omit a node whose body you opened.
- **`role` test:** if you cannot write a true one-line `why`, the role is `traversed`.
- `contributed` = materially shaped the output. `consulted` = informed reasoning, not in the output.
  `traversed` = routed through, not relied on.

### 6. Stamp (append-only)
For each `contributed`/`consulted` node, append `{id, ts (UTC), role, weight}` to **that node's
home-cluster registry** (a cross-cluster use stamps the foreign cluster, not the one you started in).
Use `mnx_common.now_utc` for the timestamp. Appending needs no lock. `traversed` nodes are not stamped.

## Never
- Never rewrite a node body or front-matter.
- Never rewrite or re-tier an index.
- Never run compaction or gc from a read.
- Never invent an id or a timestamp by hand — use the helpers.
