---
name: mnx-read
description: Retrieve knowledge from a Mnemex Context Graph knowledge graph. Invoke this PROACTIVELY at the start of any substantive task that touches a domain the graph may cover — before designing, implementing, debugging, or answering a domain/architecture question — not only when the user names mnemex. Trigger it whenever the user asks something that should draw on accumulated domain knowledge or team patterns, references "the knowledge graph", "what we know about", "our patterns for", or pivots mid-session to a new domain area; when unsure whether prior knowledge exists, read first rather than answer cold. Routes structurally through tiered indexes, reads in chunks to stay within context budget, expands only needed nodes, and stamps usage. Pure with respect to knowledge (never rewrites nodes or indexes).
---

# mnx-read — tiered, budget-aware retrieval

Retrieve from the Mnemex graph **without context bloat**. You route by reading small index heads, open
only what you commit to, and record what you actually used. You never mutate knowledge — your only write
is appending usage stamps to a registry.

Background: `docs/rationale-and-concepts.md`, `docs/architecture.md`,
`docs/staging-and-promotion.md` (the staged overlay). Helpers you call:
`scripts/mnx_binding.py` (locate the graph), `scripts/mnx_read.py` (org/team routing + overdue
check, tier scan + staged overlay + stale flags, id→body expansion — the mechanical frontier,
shared with the MCP `read_*` tools), `scripts/mnx_decay.py` (true current score when labels are
stale), `scripts/mnx_stamp.py` (durable, auto-flushed usage stamps).

## Preflight — locate the graph (always first)
Run {{CALL:bind_status}}.
- If `resolved` is false → **STOP**: tell the user *"No Mnemex graph configured. Run `{{PROC:init}}`."*
- **Echo the resolved graph** so the user knows which graph the answer is drawn from: show the
  `resolution` line, e.g. *"Reading from **payments-knowledge** (source: project .mnemex.md)."* If
  `default_fallback` is true, flag it: *"⚠️ No project binding here — reading from your personal graph
  **personal-notes**."* (LIMITATIONS.md #2 — make the resolved graph and its source visible.)
- If `clone_present` is false (a remote graph not yet materialized this session) → run `mnx_binding.py sync` once.
- Use the returned **`graph_root`** as the graph location for every read below. **Never operate on the
  current working directory** — the author's project repo is not the graph.

## Procedure

### 1. Frontier — overdue check + route, in one call
Run:
{{CALL:read_frontier}}
This returns `overdue` (the graph-wide consolidation-due signal), `org_head.description`, and each
team's `description` + its cluster list (`{cluster, path, description}`) — chunk-1-equivalent: no tier
rows, just what routing needs.
- **Overdue, warn only:** if `overdue.due` is true (or a team's `config_drift` is true), tell the user
  in one line: *"Knowledge maintenance is N days overdue — run `{{PROC:promote}}`."* (Consolidation
  is the back half of promote now; there is no standalone gc.) **Do not compact. Do not consolidate.
  Do not run promote.**
- **Route (judgment, not the script's job):** choose the team(s) whose `description` matches the
  request, then the cluster(s) within it whose `description` matches — structural matching on the
  one-line descriptions and node `summary`/`aliases`, not guesswork.

### 2. Scan tiers in order, stop early
For each chosen cluster (`path` from step 1), run:
{{CALL:read_cluster}}
This returns the cluster's `tiers.hot` table (id·summary·aliases·strength·stale_after, each row
already carrying a computed `stale` boolean), a `stale` cue list, and the staged-capture `overlay`
for that cluster's domain — all three in one call.
- **Stop early.** Hot is often enough — stop if it answers the request. Only if insufficient,
  re-run with `--tiers hot,warm`, and only on a deliberate deep search (or when the request clearly
  concerns a dormant concept — alias/domain overlap with a cold entry) widen to
  `--tiers hot,warm,cold`.
- If you need a node's *true current* score (tier labels are only as fresh as the last gc), fold the
  registry tail with `mnx_decay.score` rather than trusting the stale label.

**Overlay rules** (decision #10) for the `overlay.atoms` in the response — staged atoms captured
this/earlier session but not yet promoted:
- **Newest-wins.** Returned newest-first; a staged atom is more recent than the graph node it
  concerns, so prefer it when both speak to the same point.
- **Mark provenance.** Anything you use from the overlay carries `state: "staged/unpromoted"` —
  say so in the answer (it has not been reconciled or peer-reviewed into the shared graph yet).
- **Flag contradictions, never resolve them.** If a staged atom contradicts a graph node, surface
  *both* and flag the conflict; do **not** body-merge them and do **not** pick a winner silently —
  reconciliation is promote's job.
- **Never stamp, never give a real id.** Staged atoms carry provisional `stg-…` ids and are **not**
  usage-stamped (only real graph nodes are). Do not add them to the usage manifest below.
- Routing stays correct between consolidations via the registry tail-fold (step 2, `mnx_decay.score`)
  — the overlay is additive, not a substitute for reading the graph tiers.

**Freshness rule** (Freshness & Revalidation) for any row where `stale` is `true` in the response —
freshness is a **separate axis from heat**: a node can be `hot` yet `stale`. For every such node you
bring into the answer:
- **Emit a refresh cue** in your answer, once per session per atom: *"⏳ `<id>` was last verified <N>d
  ago (horizon passed <stale_after>) — I'll re-derive it from source before relying on it."* Then
  actually re-check it against its source as part of the task.
- `stale: false` (a `—` `stale_after`) means the atom is **timeless** (or dead) — never cue it.
- This is a **signal only**. Do not rewrite the node here. Acting on the outcome is step 5 (still-true)
  or a follow-up `mnx-capture` (changed) / promote (obsolete). See the three outcomes in Freshness &
  Revalidation §6.

### 3. Expand only on commit
{{CALL:read_nodes}}
Resolves each id to its node body and, for every domain node, its `governed-by` pattern
companion(s) — so you get the *what* and the *how* together in one call. Pass **only** the ids you
decide to use — this is beam search, not "load every neighbor": choosing which ids to pass (staying
within a small frontier of nodes/tokens per hop) is your judgment, not the script's. `--max-bytes`
caps the total but never starves the first id, so a lopsided budget still returns something. The
tool refuses `stg-…` ids outright (those bodies already came from step 2's overlay).

### 4. Emit the usage manifest (the gate)
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

### 5. Stamp (append-only, auto-flushed)
For each `contributed`/`consulted` node, record one usage stamp against that node's **home-cluster**
registry (a cross-cluster use stamps the foreign cluster, not the one you started in):

{{CALL:record_usage_role}}

If you re-checked a **stale** atom (step 2) and confirmed it is **still correct**, also append one
`revalidated` stamp for it (weight `0`) — this advances its freshness clock at the next consolidation
without touching heat:

{{CALL:record_usage_revalidated}}

If the fact had **changed**, do not append `revalidated` — stage the update with `{{PROC:capture}}`
instead (promote will bump both `updated` and `verified`). If you could not verify it in-session, append
nothing; it stays stale and the cue returns next session.

`traversed` nodes are not stamped. The helper timestamps the stamp (`mnx_common.now_utc`) and chooses
the durable store by graph kind:
- **git-remote** → written to a session-durable spill *outside* the clone, so it survives the next
  session-start reset and any offline window. Stamps are flushed to the registry and pushed
  automatically in one batch by the Stop/SessionEnd hook — you do **not** persist per read.
- **git-local / plain-local** → appended straight to the registry on disk (already durable).

You never commit or push usage stamps yourself.

## Never
- Never rewrite a node body or front-matter (the `revalidated` stamp is an append to the registry, not a
  node write — advancing `verified` is consolidation's job, not the read's).
- Never rewrite or re-tier an index.
- Never run compaction, consolidation, or promote from a read.
- Never body-merge or stamp a staged overlay atom; never resolve a staged-vs-graph contradiction here.
- Never invent an id or a timestamp by hand — use the helpers.
