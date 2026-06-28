---
name: mnx-gc
description: Run the Mnemex Protocol maintenance pass over a knowledge graph — compact usage stamps, recompute decay and structural strength, re-tier nodes into hot/warm/cold, tombstone stale nodes, sever their edges, and regenerate navigation. Use this whenever the user asks to run maintenance/garbage-collection/compaction on the knowledge graph, prune stale knowledge, "clean up the graph", or when mnx-read reported maintenance is overdue. Strictly snapshot-then-apply, locked, atomic, and recoverable.
---

# mnx-gc — the maintenance pass

One pass does three coupled jobs — compaction, re-tiering, budget-split — plus death and edge hygiene.
The governing principle is **snapshot-then-apply**: decide everything against a frozen view, then apply
together. Read `docs/05-maintenance-pass-algorithm.md` in full before running; this is a summary.

Helpers: `mnx_lock`, `mnx_config`, `mnx_compact`, `mnx_decay`, `mnx_resolve`, `mnx_index`, `mnx_doctor`,
`mnx_common`.

## Pre-flight
1. Acquire the team lock (`mnx_lock.acquire`). One mutating op per team.
2. Crash recovery: if a `pass.plan.json` exists with a dirty tree, offer `git checkout .` to restore
   the last good commit before continuing.
3. Config drift: if `config_version`/`λ` changed since the last compaction, **re-normalize first**
   (`mnx_config.renormalize`) so every node's live score is continuous across the change — *before* any
   tier decision. Stamp the new version/λ.

## Phase A — MARK (read-only; may parallelize per cluster)
Mutate nothing. Build the decision set against a frozen snapshot.
1. **Compact (in memory):** for each cluster, replay registry deltas after the high-water mark
   (`mnx_compact.deltas_after`), decaying each node to now and folding boosts with saturation
   (`mnx_decay.apply_use`). This yields `score[X]`.
2. **Structural strength:** build the reverse-edge map over the snapshot **including cross-links and
   cold/dead nodes** (`mnx_resolve.build_reverse_map`); `struct[X] = g(local_in + cross_in)`. Soft
   cross-team `references` contribute nothing.
3. **Retention + target tier:** `retention[X] = combine(score, struct)`; `target_tier[X]` with hot =
   top-K (`hot_k`) by score (`mnx_decay.tier_of`).
4. **Death candidates (conjunction gate):** a cold node is a death candidate only if score is low
   **and** struct is weak **and** TTL expired. **Sole-referrer reluctance:** if the node is the only
   inbound of any still-active node, keep it warm instead (no orphan cascade).
5. **Budget:** if a cluster exceeds `node_budget`, plan to sweep cold out of the active index; if a
   single `domain:` sub-key still overflows, mark it for **human escalation** — do not invent
   structure.
6. Write `pass.plan.json` (every decision, addressed by id + path).

## Phase B — SWEEP (serial; under the lock; one transaction)
Apply the plan, truth-first then derived:
1. Relabel tiers in the index model. For each death: tombstone (status `dead`, clear body, set `died`,
   keep id + front-matter; or hard-delete if `--purge`). **Transactionally sever** every incident edge
   using the reverse map + cross-links (cold included) — rewrite each referrer to drop the edge or
   repoint to `superseded-by`. Never leave an edge pointing at a dead node.
2. Regenerate affected `index.md` sections from the now-final nodes (denormalizing summary/aliases);
   delta-update `cross-links.md`.
3. Advance high-water marks (`mnx_compact.advance_highwater` — checkpoint, never truncate); stamp
   `.mnemex/last_compaction` and `config_version`/λ.
4. Run `mnx-doctor` (must pass) → **one git commit** → remove the plan file → release the lock.

## Modes
- Default: produce the Phase-A plan, show a summary, and **ask for confirmation** before Phase B.
- `--apply`: run A→B end-to-end (for a scheduled, non-interactive invocation).
- `--dry-run`: stop after Phase A; show the plan; mutate nothing.

## Never
- Never decide against freshly-mutated state — snapshot first.
- Never delete on age alone — require the conjunction (low usage AND structurally weak).
- Never sever an edge non-transactionally or skip cold/dead nodes in the reverse map.
- Never truncate the registry — only advance the high-water mark.
- Never auto-invent folder structure on a budget overflow — escalate.
