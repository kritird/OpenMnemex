---
name: mnx-read
description: Retrieve knowledge from a Mnemex Context Graph knowledge graph. Invoke this PROACTIVELY at the start of any substantive task that touches a domain the graph may cover — before designing, implementing, debugging, or answering a domain/architecture question — not only when the user names mnemex. Trigger it whenever the user asks something that should draw on accumulated domain knowledge or team patterns, references "the knowledge graph", "what we know about", "our patterns for", or pivots mid-session to a new domain area; when unsure whether prior knowledge exists, read first rather than answer cold. Routes structurally through tiered indexes, reads in chunks to stay within context budget, expands only needed nodes, and stamps usage. Pure with respect to knowledge (never rewrites nodes or indexes).
---

# mnx-read — tiered, budget-aware retrieval

Retrieve from the Mnemex graph **without context bloat**. You route by reading small index heads, open
only what you commit to, and record what you actually used. You never mutate knowledge — your only write
is appending usage stamps to a registry.

Background: `docs/01-rationale-and-concepts.md`, `docs/02-architecture.md`,
`docs/11-staging-and-promotion.md` (the staged overlay). Helpers you call:
`scripts/mnx_binding.py` (locate the graph), `scripts/mnx_compact.py` (overdue check, registry-tail
fold), `scripts/mnx_resolve.py` (id→path), `scripts/mnx_decay.py` (true current score when labels are stale),
`scripts/mnx_stage.py` (capture-staging overlay — local, un-promoted atoms),
`scripts/mnx_stamp.py` (durable, auto-flushed usage stamps).

## Preflight — locate the graph (always first)
Run `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/mnx_binding.py" status`.
- If `resolved` is false → **STOP**: tell the user *"No Mnemex graph configured. Run `/mnemex:mnx-init`."*
- If `clone_present` is false (a remote graph not yet materialized this session) → run `mnx_binding.py sync` once.
- Use the returned **`graph_root`** as the graph location for every read below. **Never operate on the
  current working directory** — the author's project repo is not the graph.

## Procedure

### 1. Overdue / config-drift check (warn only)
Run `mnx_compact.py overdue`. If compaction is overdue or `λ`/`config_version` has drifted, tell the
user in one line: *"Knowledge maintenance is N days overdue — run `/mnemex:mnx-promote`."* (Consolidation
is the back half of promote now; there is no standalone gc.) You may append one `__maintenance-due__`
marker line to the nearest registry. **Do not compact. Do not consolidate. Do not run promote.**

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

### 3b. Overlay the capture staging tier (local, un-promoted)
For every routed cluster, also pull the **staged** atoms — knowledge captured this/earlier sessions but
not yet promoted into the graph:

```
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/mnx_stage.py" overlay --domain "<routed-domain(s), ;-separated>"
```

Overlay rules (decision #10):
- **Newest-wins.** Staged atoms are returned newest-first; a staged atom is more recent than the
  graph node it concerns, so prefer it when both speak to the same point.
- **Mark provenance.** Anything you use from the overlay is **`staged/unpromoted`** — say so in the
  answer (it has not been reconciled or peer-reviewed into the shared graph yet).
- **Flag contradictions, never resolve them.** If a staged atom contradicts a graph node, surface
  *both* and flag the conflict; do **not** body-merge them and do **not** pick a winner silently —
  reconciliation is promote's job.
- **Never stamp, never give a real id.** Staged atoms carry provisional `stg-…` ids and are **not**
  usage-stamped (only real graph nodes are). Do not add them to the usage manifest below.
- Routing stays correct between consolidations via the registry tail-fold (step 3) — the overlay is
  additive, not a substitute for reading the graph tiers.

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
- {id: ledger-routing,   role: consulted,   why: "checked routing topology to confirm the path"}
- {id: legacy-de124-fmt, role: traversed,   why: "opened while routing; not used"}
```

Rules:
- **Every body-load needs a disposition.** You may not silently omit a node whose body you opened.
- **`role` test:** if you cannot write a true one-line `why`, the role is `traversed`.
- `contributed` = materially shaped the output. `consulted` = informed reasoning, not in the output.
  `traversed` = routed through, not relied on.

### 6. Stamp (append-only, auto-flushed)
For each `contributed`/`consulted` node, record one usage stamp against that node's **home-cluster**
registry (a cross-cluster use stamps the foreign cluster, not the one you started in):

```
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/mnx_stamp.py" append \
  --cluster <home-cluster-path> --id <node-id> --role <contributed|consulted> [--weight w]
```

`traversed` nodes are not stamped. The helper timestamps the stamp (`mnx_common.now_utc`) and chooses
the durable store by graph kind:
- **git-remote** → written to a session-durable spill *outside* the clone, so it survives the next
  session-start reset and any offline window. Stamps are flushed to the registry and pushed
  automatically in one batch by the Stop/SessionEnd hook — you do **not** persist per read.
- **git-local / plain-local** → appended straight to the registry on disk (already durable).

You never commit or push usage stamps yourself.

## Never
- Never rewrite a node body or front-matter.
- Never rewrite or re-tier an index.
- Never run compaction, consolidation, or promote from a read.
- Never body-merge or stamp a staged overlay atom; never resolve a staged-vs-graph contradiction here.
- Never invent an id or a timestamp by hand — use the helpers.
