---
name: mnx-consolidate
description: INTERNAL maintenance pass over a Mnemex graph — compact usage stamps, recompute decay and structural strength, re-tier hot/warm/cold, tombstone stale nodes, sever their edges, chain over-budget indexes, and regenerate navigation. This is the BACK HALF of /mnemex:mnx-promote (run over the post-merge graph inside promote's single plan/lock/transaction), NOT a user-facing command. There is no /mnemex:mnx-consolidate slash command — do not invoke this standalone; run /mnemex:mnx-promote, which calls it. Strictly snapshot-then-apply, locked, atomic, recoverable.
---

# mnx-consolidate — the maintenance pass (internal; promote's back half)

> **Not a user command.** Consolidate is invoked by `/mnemex:mnx-promote` after staged atoms have been
> merged, over the **post-merge** graph, and contributes its decisions to promote's **single** approval
> plan / lock / doctor / push. It does not flush stamps, reconcile staged atoms, commit, or push on its
> own — promote owns the transaction boundary. (Run directly only for deliberate forced maintenance.)

One pass does three coupled jobs — compaction, re-tiering, budget-handling — plus death and edge
hygiene. The governing principle is **snapshot-then-apply**: decide everything against a frozen view,
then apply together. The steps below are self-contained for execution; fuller rationale (optional):
`docs/maintenance-pass-algorithm.md`.

Helpers: `mnx_binding` (locate), `mnx_lock`, `mnx_config`, `mnx_compact`, `mnx_decay`, `mnx_resolve`,
`mnx_node` (the deterministic node writer — tombstone / revalidate), `mnx_index`, `mnx_doctor`, `mnx_common`.

## Pre-flight (when promote has not already established it)
0. **Locate the graph:** `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/mnx_binding.py" status --session <sid>`
   (the session id from session-start, if you have one — see mnx-init step 1; honors a mid-session graph
   switch). If `resolved` is false → **STOP**, point at `/mnemex:mnx-init`. If `clone_present` is false,
   `mnx_binding.py sync` once. Operate on `graph_root`, never the working directory; note `kind`.
1. **Team lock** (`mnx_lock.acquire`) — one mutating op per team. When promote already holds it, reuse it.
2. **Crash recovery:** a `pass.plan.json` with a dirty tree → offer `git checkout .` to restore the
   last good commit before continuing.
3. **Config drift:** if `config_version`/`λ` changed since the last compaction, **re-normalize first**
   (`mnx_config.renormalize`) so every node's live score is continuous across the change — before any
   tier decision. Stamp the new version/λ.

## Phase A — MARK (read-only; may parallelize per cluster)
Mutate nothing. Build the decision set against a frozen snapshot.
1. **Compact (in memory):** per cluster, replay registry deltas after the high-water mark
   (`mnx_compact.deltas_after`), decaying each node to now and folding boosts with saturation
   (`mnx_decay.apply_use`). Yields `score[X]`.
2. **Structural strength:** reverse-edge map over the snapshot **including cross-links and cold/dead
   nodes** (`mnx_resolve.build_reverse_map`); `struct[X] = g(local_in + cross_in)`. Soft cross-team
   `references` contribute nothing.
3. **Retention + target tier:** `retention[X] = combine(score, struct)`; `target_tier[X]` with hot =
   top-K (`hot_k`) by score (`mnx_decay.tier_of`).
3b. **Freshness (independent of heat; Freshness & Revalidation):** per cluster, gather the revalidation events in the
   registry tail — `mnx_compact.latest_revalidations(deltas)` → `{id: latest_ts}`. These are weight-0
   `revalidated` stamps; they never entered `score[X]` (the fold ignores them). Plan to advance each
   node's `verified` to that ts in Phase B.
4. **Death candidates (conjunction gate):** a cold node dies only if score is low **and** struct is
   weak **and** TTL expired. **Sole-referrer reluctance:** if it is the only inbound of any still-active
   node, keep it warm (no orphan cascade). **Timeless exemption:** a node with `volatility: timeless` is
   **never** a death candidate — it decayed in heat but is permanently true, so it may sit in cold yet
   must never be auto-tombstoned (it can leave only by an explicit human SUPERSEDE). Freshness & Revalidation §7.
5. **Budget:** if a cluster exceeds `node_budget`, plan to sweep cold out of the active index; split the
   index along the `domain:` sub-key (`mnx_index.shard_index`); if a single sub-key still overflows,
   **chain the index** into `index.NNN.md` continuation chunks (B-tree-leaf style; `regenerate_index`
   does this automatically) — human escalation only as the genuine last resort.
6. Write `pass.plan.json` (every decision, addressed by id + path).

## Phase B — SWEEP (serial; under the lock; one transaction)
Apply the plan, truth-first then derived:
1. Relabel tiers in the index model. For each death: tombstone through **`mnx_node.py tombstone --id <id>`**
   (status `dead`, `died` stamped, **body + id + front-matter kept** — a dead node is retained for audit and
   resurrection, never hollowed; the writer also refuses a `timeless` node, enforcing inv 9d by construction;
   hard-delete only if `--purge`). A human SUPERSEDE uses `mnx_node.py supersede`, which sets `superseded-by`
   on the retired node. **Transactionally sever** every incident edge using the reverse map + cross-links
   (cold included) — rewrite each referrer to drop the edge or repoint to `superseded-by`. Never leave an
   edge pointing at a dead node.
1b. **Advance `verified`** for each node in the revalidation plan (step A.3b) through
   **`mnx_node.py revalidate --id <id> --ts <revalidation-ts>`** — a monotonic node truth-write
   (`verified = max(current verified, ts)`, never regressing; backfills a missing `verified` from
   `updated`). Never hand-edit the timestamp. Leave `updated` and the index strength **untouched** (a
   confirmation is not a meaning-change and not a use).
2. Regenerate affected `index.md` sections from the now-final nodes with `mnx_index.regenerate_index`
   (denormalizing summary/aliases **and** the freshness `stale_after` column, recomputed from each node's
   `verified`+`volatility`; chains the cold tier when over `index_chunk_rows`); delta-update `cross-links.md`.
3. Advance high-water marks (`mnx_compact.advance_highwater` — checkpoint, never truncate); stamp
   `.mnemex/last_compaction` and `config_version`/λ.
4. **Hand back to promote.** Consolidate's tier/death/budget decisions are folded into promote's single
   approval plan; promote runs `mnx-doctor`, persists (commit + push by kind), removes the plan file,
   and releases the lock. When run **standalone** (forced maintenance), do those final steps here.

## Modes (standalone use only)
- Default: produce the Phase-A plan, show a summary, and **ask for confirmation** before Phase B.
- `--apply`: run A→B end-to-end. `--dry-run`: stop after Phase A; mutate nothing.

## Never
- Never decide against freshly-mutated state — snapshot first.
- Never delete on age alone — require the conjunction (low usage AND structurally weak).
- Never auto-tombstone a `volatility: timeless` node; never let a `revalidated` stamp touch strength/tier.
- Never sever an edge non-transactionally or skip cold/dead nodes in the reverse map.
- Never truncate the registry — only advance the high-water mark.
- Never auto-invent folder structure on a budget overflow — split by sub-key, then chain; escalate last.
