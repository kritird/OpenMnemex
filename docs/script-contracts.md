# 🧰 Script Contracts

> [!NOTE]
> 🧠 **Skills reason; scripts decide.** Anything that must be exact — decay math, id→path resolution,
> **node persistence** (minting the id, stamping the clock, front-matter shape — `mnx_node`), index
> regeneration, invariant checks, locking — lives in a deterministic script, never in skill prose.
> Every script emits `STATUS=OK|FAIL` + JSON so a skill can parse the result.

This document specifies the script contracts — signatures, inputs, outputs, invariants. The scripts in
`scripts/` implement them: `mnx_common`, `mnx_config`, `mnx_decay`, `mnx_resolve`, `mnx_compact`,
`mnx_stamp`, `mnx_stage`, `mnx_node`, `mnx_index`, `mnx_lock`, `mnx_doctor`, `mnx_status`, and the
`mnx_binding` entry point the session hook and every skill depend on to locate the graph. All scripts
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

parse_wikilinks(body) -> [{name, display?}]   # inline [[name]] / [[name|Display]] (Link Reconciliation). Pipe = DISPLAY
                                  #  (wiki-native), NOT a type. De-duped by normalized name, order kept.

clamp_dt(t_from, t_to) -> float   # max(0, seconds) — used everywhere Δt appears
```

**Invariants:** `now_utc` is the only clock; `clamp_dt` never returns negative; `parse_*` reject
malformed front-matter rather than guessing.

---

## 🔗 `mnx_binding.py` — graph binding resolution, sync + persistence

Connects an author in any project to the graph, which is either a **git remote** or a **local folder**.
Self-contained (does not import the other helpers); stdlib + `PyYAML` only.
Full spec: [`binding-and-graph-sync.md`](binding-and-graph-sync.md).

```
resolve(start_dir=cwd, session_id=None) -> Binding | None
    # precedence: session override (Phase 5b, only when session_id given + a live one exists)
    #   >  <project>/.mnemex.md  >  env  >  ~/.claude/mnemex/config.md
    # within the project/env/user chain, graph_path (local) beats graph_remote (+ warning)
    # Binding.kind() -> git-remote | git-local | plain-local ; Binding.graph_root() -> dir
    # Binding.source_kind() -> project | env | user | override
resolve_project_only(start_dir=cwd) -> Binding | None
    # resolve() minus the override check — the project/env/user chain alone. Used by
    # override_mismatch (below) and by set_session_override to find the graph a switch leaves.
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
write_user_default(path|remote, force=False, default_team?, author?) -> {ok, action: written|overwritten|exists, path, ...}
    # guided setup: write <mnemex home>/config.md (the user default). EXACTLY ONE of path/remote.
    # refuses to clobber an existing default without force; stores an ABSOLUTE path (resolves from any cwd).
register_graph(binding) -> {registered: bool, slug?, reason?, error?}
    # best-effort append to <mnemex home>/graphs.md if `binding.slug()` isn't already listed. NEVER
    # raises. Called by sync() (non-error actions only) and mnx_init.init_graph.
list_graphs() -> [{slug, kind, name, location, last_used, present}, ...]
    # the registry UNIONED with a scan of graphs_cache_root() for clones the registry missed. Bounded
    # to that one dir — no filesystem-wide search. `present`: clone exists (remote) / folder exists
    # (local). Works with no binding at all.
set_session_override(session_id, path=|remote=, ttl_hours=12.0) -> {ok, action: overridden|busy, slug?, expires?, ...}
    # Phase 5b: point THIS session at a different graph, outranking project/env/user until it
    # expires or the session ends. Refuses (action=busy) if the CURRENTLY effective graph has an
    # open promote lock / in-flight plan (mnx_lock.held/in_progress on any team dir) — finish or
    # abort first. Writes <mnemex home>/session-override/<session_id>.md.
clear_session_override(session_id) -> {ok, action: cleared}
    # drop the override file. Missing is a no-op, not an error.
override_mismatch(binding, start_dir=cwd) -> str | None
    # None unless `binding` is a session override that disagrees with resolve_project_only() here —
    # then a one-line "writing into Y, NOT X" marker to echo. The required anti-silent-misroute check.
```

**Guided setup (onboarding):** `mnx_init.suggest_default_graph(cwd) -> {path, org, team, rationale}` proposes
a local-folder default under `<mnemex home>/graphs/<project-name>` (pure; no writes). Pair it with
`init_graph` + `write_user_default` to go from nothing to a bound, doctor-clean graph. Exposed over MCP as
the read-only `init_suggest` tool and `init_graph(use_default=true)`; over the CLI as
`mnx_init.py suggest-default` and `mnx_binding.py write-user-default`.

`graph_slug(binding) -> str` / `Binding.staging_root() -> dir` expose the per-graph slug and the local
side-store folder (`~/.claude/mnemex/staging/<slug>/`) used by `mnx_stage` (capture atoms) and
`mnx_stamp` (the stamp spill); `graph_slug`/`staging_root` also appear in `resolve`/`status` JSON.

CLI: `resolve [--session <id>] | sync [--session <id>] | status [--session <id>] | unpushed-state |
persist --message "…" | push | graph-root | staging-path | probe-remote --remote <url> |
write-user-default --path <dir>|--remote <url> [--force] | list-graphs |
use-graph <slug> [--session <id>] | clear-graph-override [--session <id>]`.
Each prints one JSON object. `status` folds in
`unpushed`/`ahead` for a materialized remote clone, so callers see a stranded promote without a second
call. `resolve`/`sync`/`status` also fold in `override_notice` (see `override_mismatch` above) when a
session override is active and disagrees with the project/user graph. `probe-remote` runs before any
binding exists, so it does not call `resolve()`; `use-graph`/`clear-graph-override`/`list-graphs` run
before any binding exists too — a session override must be settable/listable with nothing else bound.
`--session` defaults to absent (no override considered) everywhere except `use-graph`/
`clear-graph-override`, which default it to `"default"` — matching `mnx_mcp.session_id()`'s fallback.
Exit codes: `0` ok (incl. offline-degraded), `2` unresolved (run `/mnemex:mnx-init`), `1` error.
(This script does not emit the `STATUS=OK|FAIL` line below; its JSON `action`/exit code is the contract.)

**Invariants:** resolution is most-specific-wins; `sync` leaves a remote clone at the
remote HEAD or untouched (never partial) and never resets a local folder; persistence is determined solely
by `kind`; a plain-local graph always gets an append-only audit record; user config is read from
`~/.claude/`, never `${CLAUDE_PLUGIN_ROOT}`; the binding never carries graph-behavior parameters.
`resolve()` is side-effect-free with ONE narrow exception: an expired session-override file is
best-effort deleted the moment it is read (a pure tidy of an already-inert file — it changes no
resolution outcome, since an expired override is ignored either way).

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

## 🪧 `mnx_node.py` — the deterministic node writer (truth-writes)

Persists node truth for promote (CREATE/MERGE/SUPERSEDE/RESURRECT) and consolidate (tombstone,
`verified`-advance). The reconcile *judgment* (which disposition, which node, what body) stays in the
skill/sub-agent; this script executes an **already-decided** disposition as a mechanical, invariant-
preserving write — the deterministic peer to `mnx_stage` (staged atoms), `mnx_mesh` (edges), `mnx_index`
(indexes). Called under the team lock (the skill holds it; the writer does not take the lock itself).

```
create(cluster, fields) -> {id, path, action:"created"}
    # mint a UNIQUE slug from fields.title (slugify + numeric suffix if taken graph-wide);
    # status=active; created=updated=verified=now_utc(); trigger REQUIRED iff type==pattern;
    # reject a provisional stg- id; write in the node.template front-matter shape.
merge(id, cluster, changes, meaning_change=False) -> {id, path, action:"merged"}
    # edit in place: keep id + created; apply changed fields/body; verified=now_utc();
    # updated=now_utc() ONLY when meaning_change (a use/confirm is not a meaning change).
supersede(old_id, cluster, new_fields) -> {new_id, old_id, action:"superseded"}
    # create() the replacement, then retire old: status=dead, superseded-by=new_id, died=now, body kept.
    # (Referrer repoint stays with the caller via mnx_mesh/mnx_resolve — this writer sets only the two nodes.)
resurrect(id, cluster) -> {id, path, action:"resurrected"}   # dead->active; verified=now; clear died + superseded-by
tombstone(id, cluster) -> {id, path, action:"tombstoned"}    # status=dead, died=now, KEEP body; refuses a timeless node
revalidate(id, cluster, ts) -> {id, path, verified, no_op}   # verified=max(current,ts), monotonic; backfills missing verified from updated
```

**Invariants:** timestamps come only from `now_utc()` (`revalidate` accepts an external confirmation ts
but never regresses `verified`); ids come only from `slugify` + a uniqueness suffix; a `stg-` provisional
id is rejected; `merge`/`tombstone`/`revalidate` preserve the body verbatim; a dead node keeps its body
(never hollowed); `verified` is monotonic and never precedes `created`; a `timeless` node is never
tombstoned (only superseded). These make the doctor's freshness invariants **9b** (created ≤ verified)
and **9d** (timeless never auto-tombstoned) hold **by construction**.

---

## ♻️ `mnx_compact.py` — registry replay + checkpoint (the LSM merge)

```
read_highwater(cluster) -> mark
deltas_after(cluster, mark) -> list[{id, ts, role, weight}]
fold(materialized_state, deltas, cfg, now) -> new_materialized_state   # pure
    # strength fold skips role in {flag, revalidated} (weight-0 freshness events never boost);
    # a `revalidated` delta advances the node's `verified` (monotonic max), then stale_after is
    # recomputed via mnx_config.resolve_horizon. `verified` defaults to `updated` when absent.
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
    #                (co-located with capture staging atoms; same slug as mnx_stage)
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
+ `mnx_common`. Full model: [`staging-and-promotion.md`](staging-and-promotion.md).

```
provisional_id(atom) -> 'stg-<sha1[:12]>'   # content hash; idempotent capture; NEVER a real id
add(atom) -> {action: staged|already-staged|refused, provisional_id, budget, status}
    # validate (type domain|pattern, score now|later, pattern⇒trigger, summary required); write
    # atoms/<pid>.md with self-sufficient provenance; REFUSE a new atom past the HARD budget (backpressure).
    # already-staged = the content hash was present before the call (idempotent re-stage, no-op) — lets
    # a bulk re-run report new-vs-known accurately (G9).
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

## 🏗️ `mnx_ingest.py` — the corpus front-end (walk · classify · chunk · hash · manifest · delta)

The deterministic front half of ingest (docs/corpus-ingestion.md). It acquires a source (local in
place, or a shallow clone to a read-only cache), walks it, classifies each file, chunks large files
along structure into candidate *units*, hashes them, and reads/writes the ingest manifest to compute a
re-run delta. **No judgment, no graph writes, never mutates the source.** stdlib only (imports `mnx_common`).

```
acquire(source, cache=None) -> {kind: local|remote, root, commit, cached}
    # local path → used in place; remote URL/*.git → shallow clone into cache (default MNEMEX_INGEST_CACHE).
probe(root, include=None, exclude=None, max_bytes=1048576)
    -> {units:[{id, path, kind, anchor, hash, bytes}], counts:{doc,interface,code-doc,config,skip},
        est_atoms, bytes_total, skipped_secrets}
    # classify (doc|interface|code-doc|config|skip) by ext+path; chunk docs by h2 headings and code by
    # EXPORTED symbol (private/underscore symbols are never emitted); a secret file is COUNTED
    # (skipped_secrets) and its bytes are NEVER opened.
delta(root, manifest, include=None, exclude=None) -> {added[], changed[], unchanged, orphans:[{path, node_ids}]}
    # file-granularity diff of walked content-hashes vs the manifest; a deleted file's node_ids surface as
    # orphan CANDIDATES (never auto-tombstoned — the human decides).
manifest_write(graph_root, source_slug, files, source_repo=None, last_commit=None) -> {path, files}
    # merges into <graph>/.mnemex/ingest/<slug>.json (protocol state, committed with the graph beside highwater).
source_slug(source) -> 'name-<sha1[:8]>'   # mirrors mnx_binding.graph_slug's scheme
```
CLI: `acquire --source <p|url> [--cache d] | probe --root d [--include g;g] [--exclude g] [--max-bytes N]
| delta --root d --manifest p | manifest-write --graph r --source-slug s --json`.
**Invariants:** never writes the graph and never mutates the source; a secret file's bytes are never read;
chunking splits along structure but never truncates a unit; `probe`/`delta` are pure over the source
(deterministic hashes); re-ingest of an unchanged tree yields an empty delta (DP4 idempotency substrate).

---

## 🧹 `mnx_glean.py` — the bounded "what did I miss?" recall primitive (Gleanings)

The mechanical half of *gleanings* (docs/corpus-ingestion.md §8): bound the recall loop and bookkeep it.
The **judgment** ("what durable fact/entity did I not stage yet?") always stays in the skill/LLM; this
script writes nothing and reads no graph. Two modes, one per consumer. stdlib only (imports `mnx_common`).

```
step(before, after, pass_no, max_passes=2) -> {pass, added, stop, reason}   # guardrail (episodic)
    # added = after-before; stop on no-progress (added<=0, precedence) OR at the pass cap (pass_no>=max).
coverage(units, staged, pass_no, max_passes=2) -> {total, covered, uncovered:[id…], pass, stop, reason}
    # checklist (ingest): a unit is COVERED when ≥1 staged atom carries its `anchor` (top-level or under
    # provenance); returns the still-uncovered unit ids (the re-ask worklist). stop on complete OR cap.
```
CLI: `step --before <n> --after <n> --pass <k> [--max 2] | coverage --units u.json --staged s.json --pass <k> [--max 2]`.
Config `max_glean_passes` (default 2) — user config for episodic, ingest config for corpus.
**Invariants:** pure/deterministic (same inputs → same output); writes nothing; never reads the graph;
the loop is bounded (stops at the cap even with gaps remaining) so a glean pass can never run away.

---

## 📊 `mnx_status.py` — at-a-glance status surface (read-only)

Backs `/mnemex:mnx-status`. Aggregates read-only signals into one JSON object so the user can answer
"what's bound / what's in my graph / is it healthy" in one move (distinct from `mnx_doctor`, which
validates/repairs). Imports `mnx_binding`, `mnx_common`, `mnx_stamp`, and best-effort `mnx_compact` /
`mnx_config` / `mnx_doctor`. Every section is guarded, so a partial or broken graph still yields a status.

```
status(session_id=None) -> {resolved, binding, known_graphs, override_notice?, clone_present, available,
             pending_stamps, stamp_durability,
             staging:{count, budget_level, urgent, oldest_age_days, atoms:[{provisional_id, score, …}],
                      held:{count, oldest_age_days?, lingering_nag?}},
             teams:[{team, clusters, nodes, hot, warm, cold, cluster_names, last_gc, gc_overdue_days}],
             totals:{teams, clusters, nodes, hot, warm, cold}, health:{ok, errors, warnings}}
    # resolved=false -> {message: run /mnemex:mnx-init, known_graphs}.  available=false -> bound but not materialized.
    # tier counts come from each cluster index; node counts from node files; last_gc from .mnemex/last_compaction.
    # staging is LOCAL (independent of the clone), so it is reported even when available=false.
    # known_graphs: mnx_binding.list_graphs(), best-effort — every graph known, not just this one
    #   (Phase 4). override_notice: mnx_binding.override_mismatch(binding) when a session override
    #   (Phase 5b, needs session_id) is active and disagrees with the project/user graph.
```

CLI: `status [--session <id>]`. **Invariants:** strictly read-only — never clones, syncs, commits, or
repairs; never raises (each section is independently guarded); "not configured" is a valid result, not
a failure.

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

## 🔁 `mnx_promote.py` — the plan-transaction orchestrator

The host submits ONE declarative plan (dispositions + splits + links + consolidate); this module executes
the whole transaction serially under the team lock, in fixed order (truth before derived) — see
docs/staging-and-promotion.md. `begin`/`context`/`apply` all take an optional `ingest_batch`: omitted (or
`None`) selects the unlabeled **session** batch (`_session`); given, selects that labeled **bulk** batch
instead (onboarding + ingest plan, the O1 lift) — same transaction, same lock, same doctor gate, just a
different staged batch. `begin`/`context`/`apply` must all pass the SAME `ingest_batch` for one promote run.

```
begin(binding=None, team=None, ingest_batch=None) -> {guard: none|busy|unpushed|ingest-batch|empty-batch, ...}
    # preflight (flush stamps, D7 unpushed guard -> retry_push, stranded-plan recovery) then acquire the
    # team lock. guard='none' -> {lock, batch, batch_count, phonebook}. guard='ingest-batch' (plain begin(),
    # only bulk atoms staged) names begin(ingest_batch=<id>) as the fix. guard='empty-batch' (a named
    # ingest_batch with nothing staged under it).
context(binding=None, team=None, pids=None, clusters=None, ingest_batch=None)
    -> {team, batch, near_matches, cluster_index, mesh_preview}
    # everything the reconcile judgment needs: the staged batch (optionally filtered by pids/clusters),
    # mnx_simindex near-match candidates per atom, routed cluster index rows, a mnx_mesh link-plan preview.
validate_plan(plan, batch_pids, graph_root) -> list[str]   # [] = valid; schema + FULL-COVERAGE (every
    # batch_pid disposed exactly once, no disposition references a pid outside the batch)
apply(plan, approved=True, binding=None, team=None, ingest_batch=None)
    -> {action: applied|rejected|committed-not-pushed, ...}
    # 1 validate 2 write pass.plan.json 3 mnx_node truth writes 4 mnx_mesh.apply_links 5 consolidate
    # (approved-death tombstones) 6 regen indexes/cross-links/phonebook + the TEAM ROUTER index (its
    # Children listing — matters whenever a disposition creates a brand-new cluster, e.g. an all-CREATE
    # empty-graph bulk seed) 7 doctor gate (E==0, else git-rollback + reject) 8 mnx_binding.persist (push
    # failure -> action=committed-not-pushed, plan stays for retry_push) 9 per-atom settle (hold
    # contradictions, clear-merged the rest), remove plan, release lock.
retry_push(binding=None, team=None) -> {action: pushed-and-settled|still-failing, ...}
    # push an already-committed merge, then the deferred settle from the persisted plan.
abort(binding=None, team=None) -> {action: aborted, had_plan}   # release lock, drop plan, staging untouched
```
CLI: `begin [--team t] [--ingest-batch id] | context [--team t] [--ingest-batch id] [--pids p1,p2]
[--clusters c1,c2] | apply [--team t] [--ingest-batch id] --json <plan.json | --json-file f> | retry-push
[--team t] | abort [--team t]`.
**Invariants:** a team lock handle is rederived from `graph_root+team` (not threaded across calls) so a
CLI retry_push/abort in a fresh process after a crash still finds it; `apply` never partially commits (the
doctor gate rolls back an uncommitted write; a push failure leaves the commit + plan intact for
`retry_push`, never re-runs the merge); staging is only settled on a CONFIRMED persist; `ingest_batch`
partitions bulk from session atoms end-to-end (DP8) — draining one never touches the other.

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
    # None for volatility:timeless or a dead (retired) node (Freshness & Revalidation §3). Pure; no clock read beyond `verified`.
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
    # freshness (Freshness & Revalidation): inv 9b verified monotonic + created≤verified/updated; 9d timeless never a death mark.
fix(scope) -> Report            # regenerate derived files (index, reverse map, cross-links) from nodes
check_staging() -> Report       # OPTIONAL inv 17: local staging tier — provisional ids well-formed +
                                # unique, each id matches its content hash (untampered), provenance
                                # present, within the hard budget. Read-only; outside the graph.
```

CLI: `check [scope] | fix [scope] | check-staging`.

The full invariant list it enforces is in
[`invariants-and-failure-modes.md`](invariants-and-failure-modes.md). `fix` only ever rebuilds
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
resolve_batch(names, team) -> {resolved:{name:id}, red:[name], candidates:{name:[…]}}   # Link Reconciliation L1
red_links(team) -> [{source_id, source_path, name, type}]   # outstanding [[names]] with resolved_id null
backfill(team, new_id, aliases) -> [{source_id, source_path, name, type}]   # red-links a new id resolves
```
CLI: `regenerate <team> | regenerate-org <root> | entries <team> | resolve <name> <team> | red-links <team>
| backfill <team> <new_id> [aliases;list]`.
**Invariants:** team-scoped (resolution scope = link scope); derived (never hand-merged); the referred-to
node is never written — links live on the source note (+ the generated cross-links mirror).

### 🕸️ `mnx_mesh.py` — Step 2b link reconciliation (the wiki mesh; Link Reconciliation)

```
plan_links(notes, team) -> {links[], red_links[], backlinks[], counts}   # PURE / read-only proposer
    # notes: [{id, body, aliases?, disposition?, mentions?}]. Resolves each note's inline [[wiki-links]]
    # against the team phonebook (+ an in-batch catalog for sibling pages created this cycle); keeps a
    # red-link for any [[name]] with no page yet; back-fills OLDER notes whose red-links a new/renamed
    # page now satisfies (mnx_phonebook.backfill). Untyped by default; no self-links; deterministic.
apply_links(plan, team) -> {nodes_edited, missing_sources, …}   # SWEEP: writes body links + mirrors
    # mirrors each source note's resolved links into front-matter `edges:`, records red-links in
    # `mentions:`. Idempotent (no duplicate edge on re-apply). Lock-gated in the promote flow.
```
CLI: `plan <team> <notes.json> | apply <team> <plan.json>`.
**Invariants:** `plan_links` writes nothing; `apply_links` never duplicates an edge; the new page is never
edited to fake an inbound link (back-links are written onto the OLDER source note); fuzzy similarity is
never turned into a link here (that is `mnx_simindex` → HITL); front-matter `edges:` is a generated mirror.

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
pairs(scope, threshold=0.5, with_atoms=None, intra=False) -> {candidate_pairs:[{a, b, similarity, a_cluster, b_cluster}]}
    # default (no flags): near-duplicate NODE pairs ACROSS clusters — the doctor's S2 worklist.
    # AS THE ER BLOCKER (docs/corpus-ingestion.md §9): `with_atoms` injects staged atoms (cluster=null) so
    # blocking covers staged↔graph and staged↔staged; `intra=True` drops the same-cluster skip so
    # intra-batch duplicates surface (DP5). With no flags, only the cross-cluster S2 behavior applies.
```
Pure-python MinHash + LSH over `summary+aliases` (word tokens + 3-char shingles → typo tolerance).
CLI: `query --text … --scope … [--threshold] | pairs --scope … [--threshold] [--with staged.json] [--intra]`.
**Invariants:** consulted ONLY at promote (never the read path); proposes, never writes.

### 🧬 `mnx_er.py` — entity resolution for bulk ingest (block → score → cluster → dispose). PURE PROPOSER

The Fellegi-Sunter ER stage (docs/corpus-ingestion.md §9): reuses `mnx_simindex.pairs` as the blocker,
scores each blocked pair, clusters the high-confidence matches (union-find), and proposes a disposition
per cluster. **Writes nothing** — reconcile/HITL disposes; the LLM judge runs ONLY on the `possible` band.
Imports `mnx_simindex` + `mnx_common`.

```
resolve(graph, atoms, team=None, match=0.85, possible=0.60)
    -> {clusters:[{canonical, members:[stg…], aliases:[…], disposition:CREATE|MERGE|COLLAPSE,
                   target_id:<graph-id|null>, confidence}],
        possible:[{a, b, score}],                # HITL band → ⚠ suggested at gate #2
        counts:{create, merge, collapse, possible}}
score_pair(a, b) -> float   # 0.4·alias/name token-Jaccard + 0.3·summary sim + 0.2·shared domain + 0.1·shared link
```
- **Block:** `mnx_simindex.pairs(graph, with_atoms=atoms, intra=True)` over {staged ∪ graph pages}.
- **Split:** `≥match` → same entity · `[possible, match)` → HITL band · `<possible` → distinct.
- **Cluster:** union-find over `≥match` pairs; `canonical` = longest existing graph-id, else the
  members' most-shared alias (tie: earliest list position — aliases[0] is the primary name), falling
  back to the best staged summary's slug only when no alias slugifies (G8: summary prose made noise
  like "connector-component"); UNION all aliases.
- **Dispose:** graph member in cluster → MERGE (target = that id); all-staged, >1 → COLLAPSE; lone staged → CREATE.
CLI: `resolve --graph r --atoms staged.json [--team t] [--match 0.85] [--possible 0.60]`.
**Invariants:** pure proposer (writes nothing, never mutates the graph); one entity → one node (intra-batch
duplicates collapse before staging); exact-resolves / fuzzy-proposes (the `possible` band never auto-merges);
runs per delta batch over {new atoms ∪ existing pages}, never globally over the whole graph.

### Additional functions on core scripts

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
  `clear`). `status()` carries a `held` block. CLI `hold | held-list | release-held | drop-held |
  clear-merged`. Config `held_max_age_days` (default 14).
- **`mnx_stage`** bulk profile + ingest-batch label (corpus ingest, DP8): `add` accepts
  `--ingest-batch <id>` (sets `bulk: true` + mirrors the label into provenance) plus the corpus-provenance
  flags `--source-repo/--commit-sha/--source-path/--anchor/--kind`; labeled atoms are counted under a
  **bulk** cap (`ingest_bulk_hard_atoms`, default 5000) and are exempt from the per-session soft/hard nag.
  `status()` reports `session_count` + `by_label:{<batch>:n, _session:n}` (the per-session budget is over
  SESSION atoms only). `list`/`overlay`/`clear` take `--ingest-batch <id>` (or `_session`); `clear
  --ingest-batch` drains only that batch, never the session atoms or another batch. Config
  `ingest_bulk_soft_atoms` (500) / `ingest_bulk_hard_atoms` (5000).
- **`mnx_index.regenerate_index`**: when `tier_files: true`, writes a slim ROUTER `index.md` (Hot +
  counts + freshness) plus `warm.md` / `cold.md` (+ `cold.NNN.md`) / `dead.md`. `mnx_common.parse_index`
  merges sibling tier files transparently, so all consumers see the full tier set either way.
- **`mnx_doctor`**: invariants **17** (derivability), **18** (phonebook completeness + path accuracy),
  **19** (unresolved mentions / red links), **20** (org-directory completeness), **21** (mesh mirror: every
  resolved `mentions[].resolved_id` appears in the node's `edges` — Link Reconciliation §8), and a
  merge-driver-registered check; `fix()` regenerates the phonebook + org directory and registers the
  merge driver.

---

## 🔌 Exit/IO contract (all scripts)

- stdout final line: `STATUS=OK` or `STATUS=FAIL`.
- stdout JSON payload (one object) for the skill to parse.
- non-zero process exit on `FAIL`.
- all mutating scripts are **no-ops without the team lock** (they verify the lock handle).
- **`--help` / `-h` on every script** emits `{"usage": [<one line per subcommand>]}` + `STATUS=OK`,
  and an **undeclared `--flag` is a usage error** (`{"error": "unknown flag: …", "usage": [...]}` +
  `STATUS=FAIL`) instead of being silently consumed as a positional path — a cold agent can
  self-correct from the payload (shared `mnx_common.cli_guard`; E2E 2026-07-12 findings F3 + G3).
  Two deliberate exceptions: `mnx_hooks` is help-only (advisory hooks fail open, never reject argv),
  and `mnx_regen merge` (the git merge-driver entrypoint) bypasses the guard so the driver path
  stays byte-transparent.
