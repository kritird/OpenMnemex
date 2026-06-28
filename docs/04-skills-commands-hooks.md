# 04 — Skills, Commands, and Hooks

Mnemex ships **four skills** (the agent-facing playbooks), **five commands** (the slash-command
surface), and a small set of **hooks** (deterministic event handlers that do what a skill cannot).
This document gives the phase breakdown for each. The authoritative skill text lives in
`skills/<name>/SKILL.md`; the command stubs in `commands/`; the hooks in `hooks/hooks.json`.

A note on division of labor: **skills reason; scripts decide deterministically.** Anything that must
be exact — decay math, id→path resolution, index regeneration, invariant checks, locking — is a
script (Doc 06), not skill prose. The skill calls the script and reasons about the result.

---

## 1. `mnx-read` — retrieval (pure w.r.t. knowledge)

**Command:** `/mnemex-protocol:mnx-read <question or task>`

**Phases:**

1. **Overdue check.** Call the cadence helper; if compaction is overdue or `config_version`/`λ` has
   drifted, emit a one-line notice (and optionally append a `__maintenance-due__` registry marker).
   Never compact here.
2. **Route.** Read the org `index.md` chunk 1 → pick team(s). Read team `index.md` chunk 1 → pick
   domain cluster(s). Pure directory + head reads.
3. **Scan tiers in chunks.** In each chosen cluster read **Hot** (chunk 1). If hot is insufficient,
   read **Warm** (chunk 2). Fall to **Cold** (chunk 3+) only on a deliberate deep search *or* when
   reconciling intent suggests a dormant concept. Fold in the registry tail if a true current score is
   needed (labels may be stale since last `gc`).
4. **Expand on commit.** Resolve candidate ids → paths via the resolver (local index for intra-cluster,
   `cross-links.md` for cross-cluster). Load **only** the bodies you commit to. Follow edges within a
   frontier budget (max nodes / max tokens per hop) — beam search, not BFS-load-everything.
5. **Emit usage manifest.** At the end, output `{id, role, why}` for every node whose **body was
   loaded**. Rule: no defensible one-line *why* ⇒ `traversed` (unstamped). Body-load ⇒ a disposition
   is mandatory.
6. **Stamp.** Append `{id, ts, weight}` to **the home cluster's registry** for each `contributed` /
   `consulted` node (a cross-cluster use stamps the *foreign* cluster's registry). Append-only; no
   lock.

**Never:** rewrite a node, rewrite an index, or compact. The only write is the registry append.

---

## 2. `mnx-write` — ingest the session into the graph (gated)

**Command:** `/mnemex-protocol:mnx-write [--cluster <path>] [--dry-run]`

Runs **in the same session** that built the artifact, so it can read the artifact *and* the human
review/clarification points from context (those are where the *how* lives and exist only in the
conversation). Four phases; **only the last mutates.**

1. **Extract.** Decompose artifact + transcript into candidate units. Tag each `domain` or `pattern`.
   Mine human review points specifically — rejected alternatives and corrections become **patterns**
   with a `trigger` (*“do X not Y, because…”*).
2. **Reconcile** (the hard phase). For each candidate, route to a cluster and classify against existing
   nodes using index **summaries + aliases** (and a few body loads for near-matches): **new / update /
   merge / contradiction**. *Lazy cold reconciliation* per `reconcile_cold_on`: scan cold for
   update-intent candidates always; for create-intent only on alias/domain overlap (a cold match is a
   **resurrection**). Contradictions never silently overwrite — they flag or create a superseding
   version (`status: superseded`, edge to the new node).
3. **Plan** (the human gate). Emit a change plan: nodes to create, fields to update, edges to add,
   supersessions, patterns→domains attachments, and resurrections. Surgical and reviewable *because*
   it is a plan before a write. `--dry-run` stops here.
4. **Apply** (mechanical, locked, atomic). Take the team lock. Write nodes; write outgoing edges into
   front-matter; append registry; delta-update `cross-links.md`; regenerate affected `index.md`
   sections (denormalizing `summary`/`aliases`); run `mnx-doctor`; **one git commit**; release lock.

---

## 3. `mnx-gc` — the maintenance pass (locked, atomic, recoverable)

**Command:** `/mnemex-protocol:mnx-gc [--team <name>] [--apply] [--dry-run]`

Default is propose-then-confirm; `--apply` runs end-to-end non-interactively (for a scheduled job).
The algorithm is **snapshot-then-apply** and is specified in full in
[`05-maintenance-pass-algorithm.md`](05-maintenance-pass-algorithm.md). In brief:

- **Re-normalize first** if `config_version`/`λ` changed (continuity across a parameter change).
- **Phase A — MARK (read-only, parallelizable):** freeze snapshot + `cross-links.md`; replay registry
  deltas since high-water; compute decayed scores, structural strengths, retention, target tiers, and
  death candidates. Write `pass.plan.json`. **No mutation.**
- **Phase B — SWEEP (serial, locked):** apply tier relabels, tombstone dead nodes, **transactionally
  sever** their incident edges (intra + cross-cluster via the reverse map / cross-links), delta-update
  cross-links, advance high-water marks, stamp `last_compaction` + `config_version`, run `mnx-doctor`,
  **one git commit**.
- **Budget-split:** if a cluster index exceeds `node_budget` after demotion, split along the declared
  `domain:` sub-key; if a single sub-key alone exceeds budget, **escalate to the human** — never invent
  structure.

**Death policy:** tombstone-and-retain by default (`purge_dead: false`); `--purge` hard-deletes.

---

## 4. `mnx-doctor` — the validator (and self-healer)

**Command:** `/mnemex-protocol:mnx-doctor [--fix] [--team <name>]`

Checks every invariant (full list in [`08-invariants-and-failure-modes.md`](08-invariants-and-failure-modes.md)):
edge targets exist; front-matter schema valid; index node-set matches folder; `summary`/`aliases`
denormalized copies fresh; reverse-edge map consistent; no dangling edges (incl. cold and tombstoned);
`hot` section ≤ `hot_k`; cross-links complete and path-accurate; orphans flagged. With `--fix` it
**regenerates derived files** (indexes, reverse map, cross-links) from the nodes — the nodes are truth,
so regeneration is always safe. Runs automatically at the end of every `write` and `gc` apply, and is
available as a pre-commit hook.

---

## 5. `mnx-init` — scaffold a knowledge repo

**Command:** `/mnemex-protocol:mnx-init [--team <name>]`

Creates `index.md`, `mnemex.config.md` (from `config/mnemex.config.md` defaults), `.mnemex/`, and a
first `team-<name>/` skeleton. Idempotent.

---

## 6. Hooks — what skills cannot do

Skills run when the model chooses to consult them; hooks run **deterministically on events**. Mnemex
uses hooks only where determinism or guaranteed firing matters. All hook scripts use
`${CLAUDE_PLUGIN_ROOT}` for portable paths.

| Hook event | Purpose | Why a hook, not a skill |
|---|---|---|
| **SessionStart** | If the cwd is a Mnemex repo, inject a one-line status: last compaction, overdue?, lock held? | Guarantees the agent *knows* state at turn zero without being asked. |
| **SessionEnd** | If knowledge-bearing work happened, **prompt** “ingest into the knowledge graph? run `/mnx-write`”. Never auto-writes. | Captures the *how* while the session context still exists; the human still pulls the trigger. |
| **PreToolUse** (git commit, optional) | Run `mnx-doctor` as a gate; block the commit if invariants fail. | A skill can be skipped; a gate must be deterministic. |
| **PostToolUse** (optional) | After a `gc`/`write` apply, verify the lock was released and no `pass.plan.json` is stranded; surface crash-recovery if so. | Cleanup must run regardless of model attention. |

These hooks are **advisory and safe**: they warn, prompt, or gate — they never mutate knowledge on
their own. The mutation path is always an explicit command behind the human plan-gate.

> **Lifecycle note:** changes to a `SKILL.md` take effect immediately; changes to `hooks/`, `.mcp.json`,
> or `agents/` require `/reload-plugins` or a restart.
