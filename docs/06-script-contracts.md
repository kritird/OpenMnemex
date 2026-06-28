# 06 — Script Contracts

The skills reason; these scripts decide deterministically. Anything that must be exact lives here, not
in skill prose. **This document specifies the contracts — signatures, inputs, outputs, invariants. The
scripts in `scripts/` are published as stubs (docstrings + `NotImplementedError`) at `v0.1.0` and are
not yet implemented.** All scripts are Python 3.9+, standard library + `PyYAML` only, runnable as
`python ${CLAUDE_PLUGIN_ROOT}/scripts/<name>.py …`, and emit machine-readable `STATUS=OK|FAIL` plus
JSON on stdout so a skill can parse the result.

Conventions: timestamps are UTC ISO-8601; ids are stable slugs; a *cluster* is a leaf node-folder.

---

## `mnx_common.py` — shared primitives

The single source of truth for time, parsing, and id rules. Every other script imports it; nothing
else writes a timestamp or mints an id.

```
now_utc() -> str
    # ISO-8601 UTC, second precision. The ONLY timestamp source.

parse_node(path) -> Node          # {id, type, title, summary, aliases, domain, status,
                                  #  confidence, trigger, edges[], references[], provenance, ...}
parse_index(path) -> Index        # {description, children[], hot[], warm[], cold[]}
read_chunk(path, section) -> str  # ranged read of one labeled section (head|hot|warm|cold|body)

slugify(title) -> str             # candidate id; caller ensures uniqueness
is_valid_id(s) -> bool            # slug rules: [a-z0-9-]+, stable, no spaces

clamp_dt(t_from, t_to) -> float   # max(0, seconds) — used everywhere Δt appears
```

**Invariants:** `now_utc` is the only clock; `clamp_dt` never returns negative; `parse_*` reject
malformed front-matter rather than guessing.

---

## `mnx_decay.py` — the relevance math

Pure functions. No I/O. Deterministic given inputs.

```
lam(half_life_days) -> float                       # ln(2)/H
half_life_for(node_type, cfg) -> float             # domain: H ; pattern: H·(1+bonus)
score(strength, last_update, now, lam) -> float    # strength · exp(−lam · clamp_dt(last_update, now))
boost(role, cfg) -> float                          # contributed|consulted|traversed → weight
recall_bonus(prev_score, cfg) -> float             # larger when prev_score low (spaced repetition)
apply_use(strength, last_update, now, role, node_type, cfg) -> (new_strength, now)
    # new = min(strength_max, score(...) + boost(role)·recall_bonus(...))   # SATURATING
tier_of(score, rank_in_cluster, cfg) -> 'hot'|'warm'|'cold'
    # hot iff rank_in_cluster < hot_k ; else warm iff score>=warm_band ; else cold
retention(score, structural_strength, cfg) -> float
```

**Invariants:** `score` monotonically non-increasing in `Δt`; `apply_use` never exceeds
`strength_max` (no immortal nodes); `recall_bonus` strictly larger for lower `prev_score`.

---

## `mnx_resolve.py` — id ↔ path resolution and the reverse map

The shared resolver used by read, write, and gc. They must all agree.

```
resolve(id, scope) -> path | None
    # local index for intra-cluster; team cross-links.md for cross-cluster; None if absent.
build_reverse_map(scope) -> dict[id -> list[referrer_id]]
    # from node front-matter edges + cross-links.md. COLD AND TOMBSTONED INCLUDED.
in_degree(id, reverse_map, cross_links) -> (local:int, cross:int)
referrers(id, reverse_map, cross_links) -> list[{id, path}]   # for transactional severing
sole_referrer_of(id, reverse_map) -> list[id]                  # live nodes whose only inbound is `id`
```

**Invariants:** the reverse map covers **all** tiers and tombstones (a cold or dead node is still
visible to integrity checks — this is what makes logical tiering safe); soft cross-team `references`
are **excluded** from in-degree.

---

## `mnx_index.py` — index regeneration (derived from truth)

Generates navigation from nodes. Never the other way around.

```
regenerate_index(cluster, materialized_state) -> writes index.md
    # rebuild HOT/WARM/COLD sections; denormalize summary+aliases from nodes;
    # carry strength/last_update; enforce hot section ≤ hot_k.
denorm_check(cluster) -> list[drift]    # node.summary/aliases vs index copies
shard_index(cluster, by='domain') -> plan   # split a generated index past node_budget (no node moves)
```

**Invariants:** after `regenerate_index`, `index.summary==node.summary` and
`index.aliases==node.aliases` for every node; hot section length ≤ `hot_k`; the index node-set equals
the folder's node-set.

---

## `mnx_compact.py` — registry replay + checkpoint (the LSM merge)

```
read_highwater(cluster) -> mark
deltas_after(cluster, mark) -> list[{id, ts, role, weight}]
fold(materialized_state, deltas, cfg, now) -> new_materialized_state   # pure
advance_highwater(cluster, mark)            # checkpoint; does NOT delete registry lines
overdue(team, cfg, now) -> {due: bool, days_overdue: int, config_drift: bool}
```

**Invariants:** `advance_highwater` only moves the mark forward and never truncates below an
unconfirmed mark (no lost stamps); `fold` is pure and order-independent over same-id deltas applied in
ts order.

---

## `mnx_lock.py` — team lock + crash recovery

```
acquire(team) -> handle | raises Busy
release(handle)
plan_path(team) -> path
write_plan(team, plan); read_plan(team) -> plan | None
in_progress(team) -> bool                 # plan present
recover(team) -> {dirty: bool, action: 'rollback'|'replay'|'none'}
```

**Invariants:** at most one holder per team; a crash leaves a readable plan + (possibly) a dirty tree
that `recover` can roll back via `git checkout` to the last good commit.

---

## `mnx_config.py` — config load, derivation, version stamping

```
load(repo) -> Config                      # parse mnemex.config.md front-matter; apply defaults
derive(cfg) -> Config                     # compute λ_domain, λ_pattern, etc.
version(cfg) -> int
stamp(team, cfg)                          # write config_version + λ to .mnemex/
changed_since_last_compaction(team, cfg) -> bool
renormalize(scope, old_lam, new_lam, now) -> plan
    # recompute stored strengths so score_new(now)==score_old(now) for every node (continuity)
```

**Invariants:** `derive` is deterministic; `renormalize` preserves every node's *current* live score
across a parameter change (no flash-cold); a config change is detectable before scores are trusted.

---

## `mnx_doctor.py` — invariant checks + self-heal

```
check(scope) -> Report      # list of {invariant, severity, node/edge, detail}
fix(scope) -> Report        # regenerate derived files (index, reverse map, cross-links) from nodes
```

The full invariant list it enforces is in
[`08-invariants-and-failure-modes.md`](08-invariants-and-failure-modes.md). `fix` only ever rebuilds
**derived** artifacts — nodes are truth and are never auto-edited by the doctor.

**Invariants:** `check` is read-only; `fix` is idempotent (running twice yields no further change);
`fix` never alters node knowledge, only navigation/telemetry-derived files.

---

## Exit/IO contract (all scripts)

- stdout final line: `STATUS=OK` or `STATUS=FAIL`.
- stdout JSON payload (one object) for the skill to parse.
- non-zero process exit on `FAIL`.
- all mutating scripts are **no-ops without the team lock** (they verify the lock handle).
