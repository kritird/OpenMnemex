# 🧰 06 — Script Contracts

> [!NOTE]
> 🧠 **Skills reason; scripts decide.** Anything that must be exact — decay math, id→path resolution,
> index regeneration, invariant checks, locking — lives in a deterministic script, never in skill prose.
> Every script emits `STATUS=OK|FAIL` + JSON so a skill can parse the result.

The skills reason; these scripts decide deterministically. Anything that must be exact lives here, not
in skill prose. **This document specifies the contracts — signatures, inputs, outputs, invariants. The
scripts in `scripts/` are implemented against these contracts as of `v0.1.0`** (`mnx_common`,
`mnx_config`, `mnx_decay`, `mnx_resolve`, `mnx_compact`, `mnx_stamp`, `mnx_stage`, `mnx_index`,
`mnx_lock`, `mnx_doctor`, `mnx_status`, and the `mnx_binding` entry point the session hook and every
skill depend on to locate the graph). All scripts
are Python 3.9+, standard library + `PyYAML` only, runnable as
`python3 ${CLAUDE_PLUGIN_ROOT}/scripts/<name>.py …`, and emit machine-readable `STATUS=OK|FAIL` plus
JSON on stdout so a skill can parse the result.

Conventions: timestamps are UTC ISO-8601; ids are stable slugs; a *cluster* is a leaf node-folder.

---

## 🧱 `mnx_common.py` — shared primitives

The single source of truth for time, parsing, and id rules. Every other script imports it; nothing
else writes a timestamp or mints an id.

```
now_utc() -> str
    # ISO-8601 UTC, second precision. The ONLY timestamp source.

parse_node(path) -> Node          # {id, type, title, summary, aliases, domain, status,
                                  #  confidence, volatility, trigger, edges[], references[],
                                  #  provenance, created, updated, verified, ...}
parse_index(path) -> Index        # {description, children[], hot[], warm[], cold[]}  (rows carry stale_after)
read_chunk(path, section) -> str  # ranged read of one labeled section (head|hot|warm|cold|body)

slugify(title) -> str             # candidate id; caller ensures uniqueness
is_valid_id(s) -> bool            # slug rules: [a-z0-9-]+, stable, no spaces

clamp_dt(t_from, t_to) -> float   # max(0, seconds) — used everywhere Δt appears
```

**Invariants:** `now_utc` is the only clock; `clamp_dt` never returns negative; `parse_*` reject
malformed front-matter rather than guessing.

---

## 🔗 `mnx_binding.py` — graph binding resolution, sync + persistence  *(IMPLEMENTED)*

Connects an author in any project to the graph, which is either a **git remote** or a **local folder**.
Self-contained (does not import the other helpers); stdlib + `PyYAML` only.
Full spec: [`10-binding-and-graph-sync.md`](10-binding-and-graph-sync.md).

```
resolve(start_dir=cwd) -> Binding | None
    # precedence: <project>/.mnemex.md  >  env  >  ~/.claude/mnemex/config.md
    # within a source, graph_path (local) beats graph_remote (+ warning)
    # Binding.kind() -> git-remote | git-local | plain-local ; Binding.graph_root() -> dir
sync(binding) -> {action: cloned|resynced|offline|local|error, graph_root, ...}
    # remote: materialize at remote HEAD in ~/.claude/mnemex/graphs/<slug> (reset --hard; offline=>read-only).
    # local:  verify the folder exists (used in place; no clone/reset).
persist(binding, message) -> {action: committed|nothing-to-commit|audit-recorded|error, push?...}
    # git-remote: commit + push (bounded retry) ; git-local: commit ; plain-local: append .mnemex/history.log
    # on push fail the merge is COMMITTED but unpushed -> caller must retry-push, NOT re-merge (double-apply).
push(binding, retries=3) -> {action: pushed|conflict|failed, recovery?...}   # remote-only commit replay+push
    # on conflict/failed returns a structured `recovery` block: {retry_command:"/mnemex:mnx-promote
    #   --retry-push", clone_path, branch, ahead, manual_fallback:[git -C … fetch/rebase/push], guidance}.
unpushed_state(binding) -> {ahead:int, unpushed:bool, branch?, kind}
    # ahead>0 ⇔ a prior promote committed but did not push; promote refuses a fresh merge while true.
probe_remote(remote, timeout=20) -> {reachable: bool, category?: auth|not-found|network, remediation?, fallback?, ...}
    # read-only `git ls-remote` with prompts DISABLED (no hang on missing creds); BEFORE binding exists.
```

`graph_slug(binding) -> str` / `Binding.staging_root() -> dir` expose the per-graph slug and the local
side-store folder (`~/.claude/mnemex/staging/<slug>/`) used by `mnx_stage` (capture atoms) and
`mnx_stamp` (the stamp spill); `graph_slug`/`staging_root` also appear in `resolve`/`status` JSON.

CLI: `resolve | sync | status | unpushed-state | persist --message "…" | push | graph-root | staging-path | probe-remote --remote <url>`.
Each prints one JSON object. `status` folds in
`unpushed`/`ahead` for a materialized remote clone, so callers see a stranded promote without a second
call. `probe-remote` runs before any binding exists, so it does not call `resolve()`.
Exit codes: `0` ok (incl. offline-degraded), `2` unresolved (run `/mnemex:mnx-init`), `1` error.
(This script does not emit the `STATUS=OK|FAIL` line below; its JSON `action`/exit code is the contract.)

**Invariants:** resolution is most-specific-wins and side-effect-free; `sync` leaves a remote clone at the
remote HEAD or untouched (never partial) and never resets a local folder; persistence is determined solely
by `kind`; a plain-local graph always gets an append-only audit record; user config is read from
`~/.claude/`, never `${CLAUDE_PLUGIN_ROOT}`; the binding never carries graph-behavior parameters.

---

## 📉 `mnx_decay.py` — the relevance math

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

## 🧭 `mnx_resolve.py` — id ↔ path resolution and the reverse map

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

## 🗂️ `mnx_index.py` — index regeneration (derived from truth)

Generates navigation from nodes. Never the other way around.

```
regenerate_index(cluster, materialized_state) -> writes index.md (+ index.NNN.md when chained)
    # rebuild HOT/WARM/COLD sections; denormalize summary+aliases+stale_after from nodes;
    # carry strength/last_update; enforce hot section ≤ hot_k;
    # CHAIN: when cold rows exceed index_chunk_rows, spill into ordered index.001.md… continuation
    #        chunks (B-tree leaf); the head records the count; stale chunks are pruned.
index_node_ids(cluster) -> set[id]      # union of ids across the head AND continuation chunks
denorm_check(cluster) -> list[drift]    # node.summary/aliases/stale_after vs index copies (head + chain)
shard_index(cluster, by='domain') -> plan   # split a generated index past node_budget (no node moves);
    # action 'chain' (not 'escalate') when a single sub-key overflows — chaining is the fallback.
```

**Invariants:** after `regenerate_index`, `index.summary==node.summary`,
`index.aliases==node.aliases`, and `index.stale_after==resolve_horizon(node)` for every node; hot section length ≤ `hot_k` (head); the union of the head
+ continuation node-sets equals the folder's node-set; continuation files (`index.NNN.md`) are derived
navigation, never nodes (excluded from `iter_node_files`).

---

## ♻️ `mnx_compact.py` — registry replay + checkpoint (the LSM merge)

```
read_highwater(cluster) -> mark
deltas_after(cluster, mark) -> list[{id, ts, role, weight}]
fold(materialized_state, deltas, cfg, now) -> new_materialized_state   # pure
    # strength fold skips role in {flag, revalidated} (weight-0 freshness events never boost);
    # a `revalidated` delta advances the node's `verified` (monotonic max), then stale_after is
    # recomputed via mnx_config.resolve_horizon. `verified` defaults to `updated` if absent (migration).
advance_highwater(cluster, mark)            # checkpoint; does NOT delete registry lines
overdue(team, cfg, now) -> {due: bool, days_overdue: int, config_drift: bool}
```

**Invariants:** `advance_highwater` only moves the mark forward and never truncates below an
unconfirmed mark (no lost stamps); `fold` is pure and order-independent over same-id deltas applied in
ts order; a `revalidated` delta moves `verified` forward only (never touches strength/tier).

---

## 📝 `mnx_stamp.py` — durable usage stamping (the read-side write)

The only write a read performs. A *stamp* is one append-only registry line `{id} {ts} {role} {weight}`.
For a **git-remote** graph the clone is hard-reset to remote HEAD every session start, so a stamp
written straight to the registry dies unless committed **and** pushed; `mnx_stamp` instead spills
remote stamps to a session-durable file *outside* the clone and flushes them to the registry + remote
in one batch (driven by the Stop/SessionEnd hooks), so a read never has to commit+push per stamp. Local
kinds are never reset, so their stamps go straight to the registry. Imports `mnx_binding` + `mnx_common`.

```
append(cluster, id, role, weight=1.0, ts=None) -> {action: spilled|appended|error, ...}
    # git-remote  -> append {cluster_rel, line} to ~/.claude/mnemex/staging/<graph-slug>/stamps.jsonl
    #                (co-located with capture staging atoms; same slug as mnx_stage — see docs/11)
    # git/plain-local -> append the line straight to <cluster>/registry.md (already durable)
flush(message) -> {action: flushed|deferred|noop, persist, pending, clusters, push?, ...}
    # remote only: replay the spill into each cluster's registry, then mnx_binding.persist (commit+push).
    # clears the spill ONLY on a confirmed push (push==ok or nothing-to-commit); else 'deferred', spill kept.
    # local kinds -> noop (stamps already on disk).
status() -> {kind, pending: int, spill?}   # pending = un-flushed stamps; 0 for local kinds
```

CLI: `append --cluster <path> --id <id> --role <contributed|consulted> [--weight w] | flush [--message …] | status`.

**Invariants:** the spill is the source of truth for un-pushed stamps; the registry is rebuilt from
`HEAD + spill` at flush (`flush` drops exact spill-line duplicates first, so a retried flush within a
session cannot double-count); the spill is cleared only after a confirmed push, so an offline flush is
deferred and replayed next session (the session-start reset discards the orphaned local commit); `ts`
is captured at `append`, so deferring the flush never distorts decay; `traversed` nodes are never
stamped; appending takes no lock (append-only).

---

## 📥 `mnx_stage.py` — the capture staging tier (local, per-author)

Owns the staging substrate between session and graph: `mnx-capture` stages atoms here; `mnx-promote`
reconciles + merges them in and clears it. One folder per graph under
`~/.claude/mnemex/staging/<graph-slug>/atoms/` (outside the clone; never pushed). Imports `mnx_binding`
+ `mnx_common`. Full model: [`11-staging-and-promotion.md`](11-staging-and-promotion.md).

```
provisional_id(atom) -> 'stg-<sha1[:12]>'   # content hash; idempotent capture; NEVER a real id
add(atom) -> {action: staged|refused, provisional_id, budget, status}
    # validate (type domain|pattern, score now|later, pattern⇒trigger, summary required); write
    # atoms/<pid>.md with self-sufficient provenance; REFUSE a new atom past the HARD budget (backpressure).
status() -> {count, urgent, oldest_age_days, total_bytes, budget:{level: ok|soft|hard, …}, thresholds}
list_atoms() -> {count, atoms:[{provisional_id, type, summary, score, urgent, staged_at, …}]}
overlay(domains=None) -> {count, atoms:[{…, body, state:'staged/unpromoted'}]}   # newest-first
clear() -> {removed}          # ALL atoms (terminal step of a successful promote, OR a user --discard-all)
clear_one(pid) -> {action}    # drop ONE staged atom (user --drop <id>); the local 'un-stage'
```

CLI: `add [--json | flags] | list | status | size-check | overlay [--domain a;b] | clear | clear-one --id <pid>`.
`list` + `clear-one`/`clear` are **user-reachable**: `/mnemex:mnx-status` lists staging (review);
`/mnemex:mnx-capture --drop <id>` / `--discard-all` prune it (the local un-stage + hard-cap escape valve).

**Invariants:** provisional ids are content-derived and **never** enter the graph or a read stamp;
staged atoms are **never** usage-stamped; `add` is idempotent by content hash and refuses past the hard
cap (a re-stage of existing content is always allowed); budgets read from plugin defaults + the **user**
config, never the graph's `mnemex.config.md`; strictly local — never clones/syncs/commits/touches the clone.

---

## 📊 `mnx_status.py` — at-a-glance status surface (read-only)

Backs `/mnemex:mnx-status`. Aggregates read-only signals into one JSON object so the user can answer
"what's bound / what's in my graph / is it healthy" in one move (distinct from `mnx_doctor`, which
validates/repairs). Imports `mnx_binding`, `mnx_common`, `mnx_stamp`, and best-effort `mnx_compact` /
`mnx_config` / `mnx_doctor`. Every section is guarded, so a partial or broken graph still yields a status.

```
status() -> {resolved, binding, clone_present, available, pending_stamps, stamp_durability,
             staging:{count, budget_level, urgent, oldest_age_days, atoms:[{provisional_id, score, …}]},
             teams:[{team, clusters, nodes, hot, warm, cold, cluster_names, last_gc, gc_overdue_days}],
             totals:{teams, clusters, nodes, hot, warm, cold}, health:{ok, errors, warnings}}
    # resolved=false -> {message: run /mnemex:mnx-init}.  available=false -> bound but not materialized.
    # tier counts come from each cluster index; node counts from node files; last_gc from .mnemex/last_compaction.
    # staging is LOCAL (independent of the clone), so it is reported even when available=false.
```

CLI: `status`. **Invariants:** strictly read-only — never clones, syncs, commits, or repairs; never
raises (each section is independently guarded); "not configured" is a valid result, not a failure.

---

## 🔒 `mnx_lock.py` — team lock + crash recovery

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

## 🎚️ `mnx_config.py` — config load, derivation, version stamping

```
load(repo) -> Config                      # parse mnemex.config.md front-matter; apply defaults
derive(cfg) -> Config                     # compute λ_domain, λ_pattern, freshness horizons, etc.
version(cfg) -> int
stamp(team, cfg)                          # write config_version + λ to .mnemex/
changed_since_last_compaction(team, cfg) -> bool
renormalize(scope, old_lam, new_lam, now) -> plan
    # recompute stored strengths so score_new(now)==score_old(now) for every node (continuity)
resolve_horizon(node, cfg) -> str|None    # stale_after = verified + horizon(volatility, type, cfg);
    # None for volatility:timeless or dead/superseded (Doc 14 §3). Pure; no clock read beyond `verified`.
```

**Invariants:** `derive`/`resolve_horizon` are deterministic; `renormalize` preserves every node's
*current* live score across a parameter change (no flash-cold); a config change is detectable before
scores are trusted.

---

## 🩺 `mnx_doctor.py` — invariant checks + self-heal

```
check(scope) -> Report          # list of {invariant, severity, node/edge, detail}
    # adds inv 14 (W): node body > node_body_max_chars (split into nodes + an edge; never truncate);
    # index node-set (inv 8) and denorm (inv 9, incl. stale_after 9c) span the chained index (head + index.NNN.md);
    # freshness (Doc 14): inv 9b verified monotonic + created≤verified/updated; 9d timeless never a death mark.
fix(scope) -> Report            # regenerate derived files (index, reverse map, cross-links) from nodes
check_staging() -> Report       # OPTIONAL inv 17: local staging tier — provisional ids well-formed +
                                # unique, each id matches its content hash (untampered), provenance
                                # present, within the hard budget. Read-only; outside the graph.
```

CLI: `check [scope] | fix [scope] | check-staging`.

The full invariant list it enforces is in
[`08-invariants-and-failure-modes.md`](08-invariants-and-failure-modes.md). `fix` only ever rebuilds
**derived** artifacts — nodes are truth and are never auto-edited by the doctor.

**Invariants:** `check` is read-only; `fix` is idempotent (running twice yields no further change);
`fix` never alters node knowledge, only navigation/telemetry-derived files.

---

## 🕸️ Mesh & derived-file scripts

### 📇 `mnx_phonebook.py` — team link-resolution catalog + org directory (DERIVED)

```
regenerate(team) -> writes team-<x>/phonebook.md   # id·aliases·summary·cluster_path·tier·status
entries(team) -> [row]                              # every active node in the team
resolve(name, team) -> {resolved:id|None, cluster_path, tier, match, candidates[], red_link}
    # exact-first: id → alias → summary → ranked token-overlap candidates; no exact ⇒ red_link
regenerate_org(graph_root) -> writes root index.md  # COARSE teams→domains; never lists nodes
```
CLI: `regenerate <team> | regenerate-org <root> | entries <team> | resolve <name> <team>`.
**Invariants:** team-scoped (resolution scope = edge scope); derived (never hand-merged); the referred-to
node is never written — edges live on the source + cross-links.

### ♻️ `mnx_regen.py` — the `mnx-regen` git merge driver (the keystone)

```
merge_driver(%O,%A,%B,%P) -> exit 0|1   # regenerate the derived file at %P from truth into %A
regen_content(path) -> str|None         # regenerate index*/phonebook/cross-links/tier files; None if not derived
install(repo) / is_installed(repo)      # register/verify merge.mnx-regen.driver (per-clone git config)
```
CLI: `merge %O %A %B %P | regen <path> | install [repo] | is-installed [repo]`. Paired with
`templates/gitattributes.template` (`registry.md merge=union`; derived files `merge=mnx-regen`).
**Invariants:** only TRUTH (nodes + registry) is authoritative; a conflict on any derived file is resolved
by regeneration, never a 3-way merge. (No `STATUS` line on `merge` — git reads the exit code.)

### 🔎 `mnx_simindex.py` — fuzzy link/dup candidate filter (NON-AUTHORITATIVE)

```
query(text, scope, threshold=0.4, k=5) -> {candidates:[{id, similarity, cluster, summary}]}
pairs(scope, threshold=0.5) -> {candidate_pairs:[{a, b, similarity, …}]}   # cross-cluster S2 worklist
```
Pure-python MinHash + LSH over `summary+aliases` (word tokens + 3-char shingles → typo tolerance).
CLI: `query --text … --scope … [--threshold] | pairs --scope … [--threshold]`.
**Invariants:** consulted ONLY at promote (never the read path); proposes, never writes.

### Additions to existing scripts

- **`mnx_decay.struct_g(weighted_in_degree, cfg)`**: saturating map of liveness-weighted in-degree
  → structural strength ∈ [0, strength_max]; the deterministic dual of `strength_max` (no *structural*
  immortality). Config `struct_scale` (default 2.0).
- **`mnx_resolve.weighted_in_degree(id, reverse_map, weight_by_id)`** + **`structural_strength_map(scope,
  score_by_id, cfg)`**: one-pass liveness weighting (referrer weight = its usage score). CLI `struct <scope>`.
- **`mnx_compact.retier(scope)`**: re-tier-only local pass — fold registry tail, regenerate index +
  phonebook + org; advances HWM; **does not push, does not stamp last_compaction**. CLI `retier <scope>`.
- **`mnx_compact.rotate(cluster, drop=False)`**: retire registry lines at/below the HWM into
  `.mnemex/registry-archive/<key>.md` (or drop). CLI `rotate <cluster> [--drop]`.
- **`mnx_lock.acquire_cluster(cluster)` / `release_cluster` / `held_cluster`**: per-cluster exclusive
  locks under a per-team guard; `acquire(team)` is team-EXCLUSIVE (conflicts with any cluster lock).
  CLI `acquire-cluster | release-cluster`.
- **`mnx_stage`** held-contradictions queue: `hold(pid, reason, contradicts)`, `release_held`,
  `drop_held`, `held_status`, `clear_merged(pids)` (per-atom terminal disposition vs all-or-nothing
  `clear`). `status()` now carries a `held` block. CLI `hold | held-list | release-held | drop-held |
  clear-merged`. Config `held_max_age_days` (default 14).
- **`mnx_index.regenerate_index`**: when `tier_files: true`, writes a slim ROUTER `index.md` (Hot +
  counts + freshness) plus `warm.md` / `cold.md` (+ `cold.NNN.md`) / `dead.md`. `mnx_common.parse_index`
  merges sibling tier files transparently, so all consumers see the full tier set either way.
- **`mnx_doctor`**: invariants **17** (derivability), **18** (phonebook completeness + path accuracy),
  **19** (unresolved mentions / red links), **20** (org-directory completeness), and a
  merge-driver-registered check; `fix()` regenerates the phonebook + org directory and registers the
  merge driver.

---

## 🔌 Exit/IO contract (all scripts)

- stdout final line: `STATUS=OK` or `STATUS=FAIL`.
- stdout JSON payload (one object) for the skill to parse.
- non-zero process exit on `FAIL`.
- all mutating scripts are **no-ops without the team lock** (they verify the lock handle).
