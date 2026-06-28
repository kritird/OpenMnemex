# 05 — The Maintenance Pass Algorithm

`mnx-gc` carries three coupled jobs — **compaction, re-tiering, budget-split** — plus death and edge
hygiene. Coupling makes ordering critical. The whole algorithm is built on one principle:

> **Snapshot-then-apply.** Compute *every* decision against a single frozen view of the graph, then
> apply them all. Never read state you have already mutated within the same pass.

This one rule resolves the ordering-corruption, the structural-strength-staleness, and most of the
concurrency hazards catalogued in [`08-invariants-and-failure-modes.md`](08-invariants-and-failure-modes.md).

Acronyms: **HWM** = High-Water Mark; **TTL** = Time-To-Live.

---

## Pre-flight

```
acquire team.lock                        # one mutating op per team (Doc 02 §9)
if pass.plan.json exists AND tree dirty:  # crash recovery (Doc 02 §10)
    offer `git checkout .`; on confirm, restore last good commit
if config_version/λ changed since .mnemex/config_version:
    RE-NORMALIZE: recompute every node's stored strength so that
        score_new(now) == score_old(now)   (continuity across the parameter change)
    stamp new config_version/λ
```

Re-normalization runs **before** any tier decision, so the pass never mixes old-λ strengths with
new-λ decay.

---

## Phase A — MARK  (read-only; parallelizable across clusters)

No file in the graph is mutated in Phase A. Sub-agents may run one cluster each, all reading the same
shared `cross-links.md`.

```
SNAPSHOT = freeze( all clusters in scope , team/cross-links.md )

# 1. Compaction (in-memory): fold the registry tail into strengths
for each cluster C in SNAPSHOT:
    deltas = registry(C).lines_after( HWM[C] )
    for each node X in C:
        s = materialized_strength(X) · exp(−λ(type(X)) · (now − last_update(X)))   # decay to now
        for d in deltas where d.id == X.id and d.role != 'flag':
            s = min(STRENGTH_MAX, decay(s, d.ts→now) + boost(d.role) · recall_bonus(X, s))
        score[X] = s

# 2. Structural strength (deterministic counterweight)
REVERSE = build_reverse_map(SNAPSHOT)        # who points AT X: intra-cluster + cross-links
for each node X:
    struct[X] = g( in_degree_local(X, REVERSE) + in_degree_cross(X, cross-links) )
    # soft cross-TEAM `references` contribute NOTHING here

# 3. Retention and target tier
for each node X:
    retention[X] = combine( score[X] , struct[X] )
    target_tier[X] = tier_of( score[X] , hot_k , warm_band )   # hot = top-K by score within C

# 4. Death candidates (CONJUNCTION gate + edge safety)
for each node X in cold tier:
    if score[X] low AND struct[X] weak AND (now > expires[X]):
        if X is the SOLE referrer of any still-active node D:
            demote-reluctant: keep X warm (its structural role to D protects it)   # no orphan cascade
        else:
            mark X for death

# 5. Budget
for each cluster C:
    if active_node_count(C) > node_budget:
        plan: sweep cold nodes out of the active index sections   # logical, not a move
        if still > node_budget on a single domain sub-key:
            ESCALATE_TO_HUMAN(C, sub-key)        # never auto-invent folder structure

write pass.plan.json  ← every decision above, addressed by id + path
```

Key guarantees from operating on `SNAPSHOT`:

- **Order independence.** `struct[X]` is measured once, for everyone, against the frozen graph. Node
  A's later demotion cannot retroactively change a `struct` value that node B's decision already used.
- **No orphan cascade.** The sole-referrer check in step 4 runs against the snapshot, so demoting/killing
  one node cannot silently orphan a live node mid-pass.

---

## Phase B — SWEEP  (serial; under the lock; one transaction)

Apply the plan exactly. Order within Phase B is fixed so derived files are rebuilt *after* the truth
they derive from is final.

```
for each decision in pass.plan.json:        # 1. truth-level mutations first
    relabel tier in the (in-memory) index model
    if death:
        tombstone X (status: dead, clear body, set died, keep id+front-matter)   # or hard-delete if --purge
        SEVER incident edges TRANSACTIONALLY:
            for each referrer R of X (from REVERSE + cross-links, COLD INCLUDED):
                rewrite R.edges to drop (R→X)   # or repoint to X.superseded-by
        # never leave an edge pointing at a tombstoned/removed node

# 2. derived navigation, rebuilt from the now-final nodes
regenerate affected index.md sections (HOT/WARM/COLD), denormalizing summary+aliases
delta-update team/cross-links.md from changed boundary edges

# 3. telemetry checkpoints
advance HWM[C] for every compacted cluster      # checkpoint, do NOT truncate (Doc 02 §2)
stamp .mnemex/last_compaction[team], config_version/λ

# 4. verify + commit
run mnx-doctor   → must pass
git add -A && git commit -m "mnx-gc: <summary of plan>"
remove pass.plan.json
release team.lock
```

The single commit makes the whole pass atomic from git's perspective: either the commit exists (pass
succeeded and validated) or it does not (recover from the plan on next run).

---

## Why each ordering choice matters (quick reference)

| Choice | Prevents |
|---|---|
| Re-normalize before tiering | Nodes flash-cold when half-life is edited (retroactive drift). |
| Mark is read-only; struct measured once | Order-dependent, non-deterministic tier outcomes. |
| Sole-referrer reluctance in mark | Orphan cascade — killing a node that is some live node's only inbound. |
| Truth mutations before derived rebuild | Index/cross-links rebuilt from stale node state. |
| Sever edges transactionally, cold included | Dangling edges to tombstoned nodes; cold nodes missed by a partial reverse map. |
| HWM advance, not truncate | Lost stamps from a read racing the compaction window. |
| Validator before commit | Committing a corrupt graph. |
| One commit + plan file | Half-applied pass after a mid-sweep timeout. |

---

## Parallel mark, serial sweep

Phase A is embarrassingly parallel (read-only per cluster). Phase B must be serial because tombstoning
a node in one cluster rewrites referrer nodes in sibling clusters (cross-cluster severing), and two
concurrent sweeps could race on a shared referrer. The team lock enforces this; reads continue
unblocked throughout (registry appends are commutative).
