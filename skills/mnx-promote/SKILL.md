---
name: mnx-promote
description: Promote the locally-staged Mnemex captures into the shared knowledge graph — the deliberate, batched, attention-heavy half of memory (the `git push`/PR to capture's `git commit`). Use this when the user wants to merge captured knowledge into the graph, says "promote", "flush staging", "merge my captures", "publish to the knowledge graph", or when a nag reports staged atoms pending / consolidation overdue. Flushes usage stamps, reconciles + merges every staged atom (clean-context sub-agent, human-in-the-loop on contradictions), consolidates the post-merge graph, runs the doctor, pushes, clears the promoted atoms per-atom, and holds any contradicting atom in a local queue for later HITL.
---

# mnx-promote — merge staging into the graph (the `git push`/PR of memory)

Promote is the **heavy, deliberate, occasional** half of the capture/promote split. It pulls the
attention-demanding merge *out* of the creative session: capture stays cheap and local, and the
reconciliation that needs care happens here, in a batch, when the user chooses to spend the attention.

**Promote disposes per-atom.** Every *clean* staged atom reaches a terminal disposition in the cycle —
*created / merged / dropped-as-duplicate / superseded* (or *resurrected*) — and is cleared per-atom on a
confirmed persist (`mnx_stage.clear_merged`). A staged atom whose reconcile flags a **contradiction** is
**held** for HITL — `mnx_stage.hold` moves it to a local held queue (with a reason + the graph id it
contradicts) rather than aborting the whole batch, so one contentious atom cannot starve the rest. A held
atom keeps its self-sufficient provenance and is re-promotable **cold**: at a later promote the human
`release_held`s it (re-reconciled) or `drop_held`s it (graph wins). Held state lives **entirely in the
local staging tier** — there is never any in-flight state on the graph. The human may still **abort** the
whole promote (staging untouched); holding is the softer default. `held_max_age_days` (default 14) nags a
lingering held atom at session start/end.

Background: `docs/staging-and-promotion.md` (the model, the reconcile sub-agent contract),
`docs/maintenance-pass-algorithm.md` (the folded consolidate), `docs/link-reconciliation.md` (the
wiki mesh / Step 2b). Helpers: `mnx_binding`, `mnx_stamp`, `mnx_stage`, `mnx_lock`, `mnx_resolve`,
`mnx_node` (the deterministic node writer), `mnx_index`, `mnx_doctor`, `mnx_common`, `mnx_mesh`,
`mnx_phonebook`, `mnx_simindex`; internal skill:
`mnx-consolidate`.

## Preflight
1. **Locate + sync:** `mnx_binding.py status --session <sid>` (the session id from session-start, if
   you have one — see mnx-init step 1; honors a mid-session graph switch). If `resolved` is false → **STOP**, point at
   `/mnemex:mnx-init`. **Echo the resolved graph before merging** — this is the irreversible write, so
   confirm the target: show the `resolution` line, e.g. *"Promoting into **payments-knowledge** (source:
   project .mnemex.md)."* If `default_fallback` is true, flag it prominently and **confirm with the user
   before merging** (*"⚠️ No project binding here — this will merge staged atoms into your personal graph
   **personal-notes**. Continue?"*) so a mis-resolved promote can't silently land in the wrong graph
   (LIMITATIONS.md #2). If `clone_present` is false → `mnx_binding.py sync` once. Operate on
   `graph_root`; note `kind`. If staging is empty (`mnx_stage.py status` → `count == 0`), promote is
   just "consolidate the graph" — say so and proceed (or stop if nothing is overdue either).
2. **Unpushed-promote guard (avoid double-apply):** if `status` reports `unpushed: true` (`ahead > 0`),
   a previous promote **committed the merge but did not push**. **Do NOT start a fresh merge** — that
   would re-apply staging on top of the existing commit. Go straight to the **Retry-push recovery**
   below (treat it as if `--retry-push` were given). A fresh promote is only safe when `ahead == 0`.
3. **Team lock:** `mnx_lock.acquire`. If a pass is already in progress, stop and tell the user.
   Recover any stranded `pass.plan.json` first.

### Retry-push recovery (`--retry-push`, or an unpushed prior promote)
The merge is already committed in the clone; only the push is missing. **Skip Steps 1–4 entirely** —
no flush, no reconcile, no consolidate, no new plan:
- `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/mnx_binding.py" push`.
- On `push: ok` → **now** do the deferred settle recorded in `pass.plan.json`: `mnx_stage.py hold` each
  contradicting atom, then `mnx_stage.py clear-merged --ids …` for the atoms the stranded commit
  promoted (if the plan predates per-atom settle and recorded no pid split, `mnx_stage.py clear` is the
  legacy fallback). Remove `pass.plan.json`, release any lock. Report success.
- On `conflict` / `failed` → surface the structured `recovery` block (its `guidance`, `clone_path`,
  `branch`, and `manual_fallback` commands). **Leave staging untouched.** Do not loop a full promote.

## Step 1 — Flush usage stamps
`python3 "${CLAUDE_PLUGIN_ROOT}/scripts/mnx_stamp.py" flush` so the reconcile and consolidate decisions
below see current usage. (Safe to no-op for local-kind graphs.)

## Step 2 — Reconcile + merge staged atoms (clean-context sub-agent; HITL)
Read the whole batch with `mnx_stage.py list` / `overlay`. Reconciliation runs as a **clean-context
sub-agent** (the live session's dirty context is irrelevant — atoms carry self-sufficient provenance):

**Reconcile sub-agent contract**
- **Input:** `{ staged atoms (with provenance), graph_root }`.
- **It reads** the routed cluster indexes + a few node bodies *in its own context* — not yours.
- **It returns only** a **change plan** + the **HITL items** (contradictions, ambiguous near-matches).
  It does **not** apply anything.
- **It may fork** per cluster / per org for scale. **Plan in parallel; apply serially under the team
  lock** (mirrors consolidate's MARK/SWEEP).

For each staged atom the plan assigns exactly one terminal disposition — **you decide which; the node
file is written deterministically by `mnx_node.py`, never by hand** (it mints the id, stamps the clock,
and enforces the front-matter shape, so the freshness invariants hold by construction):
`CREATE` (`mnx_node.py create` — new node, real slug minted by the script) · `MERGE`/`UPDATE`
(`mnx_node.py merge --id <id> [--meaning-change]` — fold into an existing node, **the default when a fact
simply changed**; keeps the id, edits in place) · `DROP-DUP` (duplicate — discard, no write) ·
`SUPERSEDE` (**tombstone-with-successor**: `mnx_node.py supersede --old-id <id>` creates the replacement
and retires the old one — `status: dead`, `superseded-by: <new-id>`, `died` stamped, **body kept**; then
repoint every referrer to the successor. Reserve this for when the old version must survive as its own
linkable node; otherwise prefer UPDATE-in-place) · `RESURRECT` (`mnx_node.py resurrect --id <id>` — a
cold/dead match revived). Honor the **node-size budget**: an over-budget body is split into multiple
nodes + an edge (Step 2b), never truncated.

**Freshness fields on apply (Freshness & Revalidation):** `mnx_node.py` stamps `verified = now` on every
node it writes for `CREATE`/`MERGE`/`UPDATE`/`SUPERSEDE`/`RESURRECT` (it was just re-derived under the
human gate) and bumps `updated` only when you pass `--meaning-change` — you never hand-write these
timestamps. Carry the atom's proposed **`volatility`** onto the node (a `create`/`merge` field), and
**surface it in the plan for the human to confirm or override** (e.g. downgrade a fast-rotting fact to
`volatile`, or mark a definition `timeless`). Default stays `default` (type-derived horizon).

**Contradictions are held, not force-resolved.** Present every contradiction to the human. If it can be
resolved in-cycle (edit the plan, supersede, or drop), do so. If it cannot be resolved now, mark that atom
**HELD** in the plan — it is moved to the local held queue in Step 5 (`mnx_stage.hold`) while the clean
atoms promote; it keeps its provenance and is re-promotable cold at a later promote. The human may still
choose to **abort the whole promote** (staging untouched) instead; holding is the default so one atom
does not starve the batch. Never body-merge over a contradiction.

## Step 2b — Link reconciliation (build the wiki mesh; Link Reconciliation)
After dispositions are assigned and **before** consolidate, wire the mesh. Promote — not capture — owns
this, because it is graph-aware. Full model + algorithm: `docs/link-reconciliation.md`. Helper:
`mnx_mesh`, `mnx_phonebook`, `mnx_simindex`.

1. **Split over-budget notes first.** Any staged note whose body exceeds `node_body_max_chars` is split
   here into sibling pages, with a `[[sibling]]` wiki-link inserted between them — never truncated
   (capture deliberately left this to you). *Where* to cut is your judgment; keep each piece a complete
   idea.
2. **Propose the link plan (deterministic core):** run `mnx_mesh.plan_links(notes, team)` over the
   post-disposition notes (each `{id, body, aliases, disposition}`). It:
   - resolves every inline `[[name]]` against the **team phonebook** (`mnx_phonebook.resolve`) — a hit
     becomes a **live link** on the note; a miss is kept as a **red-link** (a link to a page that does not
     exist yet — normal, never an error);
   - **back-fills** older notes: for every `CREATE`/`RESURRECT`/alias-add, `mnx_phonebook.backfill` finds
     existing notes whose outstanding red-links this new page now satisfies, and proposes a **back-link
     written onto that older note** (this is how existing atoms come to point at the new one).
3. **Fuzzy whisper (judgment, HITL):** consult `mnx_simindex.query` for near-matches the author did not
   explicitly link. Surface these as **`⚠ suggested`** rows — a similarity is **never** turned into a link
   without human confirm.
4. Links are **untyped by default** (wiki-native); carry an optional `type` only if the staged
   `mentions[].type` set one. Never invent a type.

Surface all of this in the **one** approval plan (Step 4) as a `LINKS` section; apply it in Step 5 via
`mnx_mesh.apply_links` (writes live links + back-links onto the source notes, records red-links). The
front-matter `edges:` list is a **generated mirror** of the resolved links — never hand-author it.

## Step 3 — Consolidate the post-merge graph (folded; same plan)
Invoke the **`mnx-consolidate`** skill over the now-merged graph (re-tier, death, edge hygiene, budget
split → index chaining). Surface its decisions in the **same** approval plan as the merge, so
consolidate's one HITL escape — a budget overflow that even chaining cannot resolve — is handled by the
human who is already present. This is why consolidate is promote's back half, not a separate command.

## Step 4 — One approval plan (STOP for the human)
Emit a single surgical plan covering **both** the merge and the consolidation, and **wait**:

```
PROMOTE PLAN  (staged: 14 atoms)
  MERGE
  CREATE    domain  iso8583-field124   in team-payments/settlement   (from stg-d3d3…)  vol:default
  MERGE     domain  ledger-routing     ← stg-9af1…   +edge routes-through→iso8583-field124
  SUPERSEDE         old-routing-note   → iso8583-field124   (from stg-1b2c…)
  DROP-DUP  stg-77aa…  (duplicate of pat-settle-recon)
  ⚠ CONTRADICTION  stg-44ee… vs settle-cutoff-time  → RESOLVE in-cycle, or HELD (promote the rest)
  LINKS
  link      ledger-routing   → iso8583-field124   (wiki-link, resolved)
  back-link rails-topology   → iso8583-field124   (red-link healed ✓ deterministic)
  red-link  iso8583-field124 → [[de124-legacy-map]]  (no page yet — kept latent)
  ⚠ suggested iso8583-field124 → settle-cutoff-time   (simindex 0.71)  → CONFIRM or DROP
  CONSOLIDATE
  RE-TIER   3 hot→warm, 1 warm→cold
  DEATH     legacy-de124-fmt (low score ∧ weak struct ∧ TTL expired)
  CHAIN     team-payments/settlement index → index.001.md (cold over budget)
```

`--dry-run` stops here. Resolve or drop each ⚠ suggested link in-cycle; an unresolved ⚠ CONTRADICTION is
marked HELD (the rest still promotes) unless the human chooses to abort the whole promote.

## Step 5 — Apply (serial, locked, atomic) → push → clear staging
After approval, apply the plan **serially** under the lock in fixed order (truth before derived):
1. Persist the node truth **through `mnx_node.py`, never by hand** — one call per disposition:
   `mnx_node.py create` (CREATE) · `merge --id <id> [--meaning-change]` (MERGE/UPDATE) ·
   `supersede --old-id <id>` (SUPERSEDE) · `resurrect --id <id>` (RESURRECT). The script mints the slug,
   stamps `created`/`updated`/`verified` from the one clock, and keeps a superseded/dead node's body — so
   inv 9b is satisfied by construction. **Then** `mnx_mesh.apply_links` writes the resolved wiki-links +
   back-links onto the source notes and records red-links (front-matter `edges:` is the generated mirror —
   never hand-authored); then apply consolidate's tombstones (`mnx_node.py tombstone`) + transactional
   edge severing + freshness advances (`mnx_node.py revalidate`).
2. Regenerate affected indexes (`mnx_index.regenerate_index` — denormalize summary/aliases; chain the
   cold tier when over `index_chunk_rows`); regenerate `cross-links.md` from the just-written boundary
   edges with `mnx_doctor.py regen-crosslinks <graph_root>` (`mnx_mesh.apply_links` writes the boundary
   edge into node front-matter but NOT into `cross-links.md`, so this is required whenever any
   cross-cluster link was created — else Step 3's check fails inv-4); advance high-water marks;
   stamp `last_compaction` + `config_version`.
3. **Doctor:** `mnx_doctor.py check <graph_root>` must pass (E == 0). (Step 2 already regenerated
   cross-links via the same `_boundary_rows` derivation this check gates on, so inv-4 is satisfied.)
4. **Persist:** `mnx_binding.py persist --message "mnx-promote: <plan summary>"` — kind-aware
   (git-remote → commit **+ push** with bounded retry; git-local → commit; plain-local → audit-append).
   On `push: failed`/`conflict` the merge **is already committed** in the clone — **do not clear
   staging** and **do not re-run the merge**. Surface the structured `recovery` block and tell the user
   to run `/mnemex:mnx-promote --retry-push` (push the existing commit); if it keeps failing, the
   `manual_fallback` git commands are the last resort. Stop here.
5. **Settle staging only on a confirmed persist:** move each contradicting atom to the held queue
   (`mnx_stage.py hold --id <pid> --reason … --contradicts <graph-id>`), then clear the atoms that
   promoted (`mnx_stage.py clear-merged --ids <pid,pid,…>`). Do **not** use the all-or-nothing
   `mnx_stage.py clear` on the per-atom path — it would discard the held atoms too. Remove
   `pass.plan.json`, release the lock. (`mnx_doctor.py check-staging` / `mnx_stage.py held-list` confirms what remains.)

## `--bulk` mode — drain a corpus ingest batch (gate #2)
`/mnemex:mnx-promote --bulk --ingest-batch <id>` is the volume-adapted promote that
[`/mnemex:mnx-ingest`](../mnx-ingest/SKILL.md) hands off to. **Same engine transaction as episodic
promote — literally the same `mnx_promote.py begin/context/apply` calls, just with `--ingest-batch <id>`
so the label-partitioned bulk batch is selected instead of the unlabeled `_session` batch** (never drains
a user's hand-captures). Only the *shape* of reconcile and the plan-drafting judgment change for scale —
the mechanics (lock, node writes, mesh, doctor gate, persist, settle) are the single tested path both
promote modes share; this SKILL does not hand-drive `mnx_node`/`mnx_lock`/`mnx_mesh`/`mnx_doctor` calls
itself for bulk (see Step 5 above — that is what `apply()` now does internally). Background:
`docs/corpus-ingestion.md` §6.

1. **Begin:** `mnx_promote.py begin --ingest-batch <id>` — preflight (unpushed guard, stranded-plan
   recovery) then the team lock, same as Step "Preflight" above. A `guard: empty-batch` result means
   nothing is staged under that id yet (stage some via `mnx_ingest`/`capture_add ingest_batch=<id>`, or
   check the id). A `guard: ingest-batch` result from a *plain* `begin()` (no `--ingest-batch`) means only
   bulk atoms are staged — it now **names this same command** as the fix, not a hand-driven fallback.
2. **Context + fork reconcile per cluster (judgment, unchanged from episodic).** `mnx_promote.py context
   --ingest-batch <id>` returns the batch + near-matches + cluster index + mesh preview, same shape as
   episodic. The reconcile sub-agent contract already permits forking — *plan in parallel, apply serially
   under the lock* — so draft the plan per cluster if the batch is large. ER already collapsed intra-batch
   duplicates before staging (one entity → one node), so each fork mostly assigns CREATE/MERGE.
3. **Summarized plan (gate #2).** The approval plan collapses to **per-cluster counts** (`CREATE 214 · MERGE
   31 · DROP-DUP 57`) and lists *only the exceptions in full*: **contradictions, ambiguous near-matches
   (the ER `possible` band → `⚠ suggested`), and new-cluster creation.** **Auto-accept the plain
   CREATE/MERGE** — there is no per-atom review at corpus scale. The plan JSON is the identical shape Step 4
   describes (`dispositions`/`splits`/`links`/`consolidate`) — every pid this batch's `begin()` returned
   must get exactly one disposition, same validation as episodic.
4. **Apply — one transaction call.** `mnx_promote.py apply <plan> --ingest-batch <id>`. This is the
   engine's Step-5 sequence in fixed order (node writes → `mnx_mesh` links → consolidate → regen indexes/
   cross-links/phonebook → doctor gate, rolls back on E>0 → `mnx_binding.py persist` → per-atom settle) —
   the same call episodic promote makes, just scoped to this batch's pids. A `committed-not-pushed` result
   means the merge landed but the push didn't — `/mnemex:mnx-promote --retry-push` (unchanged from episodic).
5. **Manifest write on confirmed persist (A5b, DP4).** *Only* after `apply()` returns `action: applied` (or
   a subsequent `retry_push` settles it), write `source_path@commit → node_ids` into
   `<graph_root>/.mnemex/ingest/<slug>.json` with `mnx_ingest.py manifest-write --graph <root>
   --source-slug <slug> --json` (stdin: the `{files:{path:{hash,nodes}}}` map). This is ingest-specific
   bookkeeping the generic transaction does not know about — it is what makes the next `/mnemex:mnx-ingest`
   diff correctly and stay idempotent. `apply()` already settled staging (cleared the promoted pids, held
   any contradiction) — do not call `mnx_stage.py clear`/`clear-merged` by hand.
6. **Crash recovery + resume.** `begin()`'s stranded-plan recovery is generic — it covers a bulk drain
   exactly as episodic promote, no bulk-specific handling needed. A partially-staged corpus (the
   `ingest_max_atoms_per_run` cost ceiling hit during `mnx-ingest` PASS 1, before any promote ran) resumes
   at the **ingest** layer (`--resume <ingest-batch>`, more atoms staged under the same id) — `apply()`
   itself always drains everything currently staged under `--ingest-batch <id>` in one call.

**Deferred (3.4b, not a scope cut — sequenced after this):** per-sub-batch incremental consolidate with a
frozen-snapshot checkpoint *within* one very large single-run corpus (so death/re-tier math never thrashes
against a moving target while thousands of atoms drain in waves). Today one `apply()` call settles an
entire `--ingest-batch` in one consolidate pass, which is correct and sufficient for realistic repo sizes
(the `ingest_bulk_hard_atoms` cap bounds a single batch); only an exceptionally large corpus would want the
finer-grained checkpointing. Track before relying on `--bulk` for a many-thousand-atom single import.

## Never
- Never apply without the single combined plan approved by the human.
- Never overwrite on a contradiction — supersede, resolve in-cycle, or HELD it; never body-merge a winner.
- Never leave a *clean* staged atom without a terminal disposition (created/merged/dropped/superseded);
  a contradicting atom that cannot be resolved now is HELD (local queue), never silently kept as staged.
- Never clear a promoted atom unless persist confirmed (push ok / committed / audit-recorded); use
  per-atom `clear-merged`, not the all-or-nothing `clear`, so held atoms survive. A full abort leaves
  staging untouched.
- Never leave a held atom on the graph — held state is purely local until a later promote resolves it.
- Never start a fresh merge when `status` reports `unpushed: true` — that double-applies staging over
  the already-committed merge. Use `--retry-push` (push the existing commit) instead.
- Never carry a provisional `stg-…` id into the graph — promotion mints a real slug id.
- Never auto-invent folder structure on overflow — split by sub-key, then chain; escalate last.
- Never hand-author `edges:` — it is a generated mirror of resolved `[[wiki-links]]` (`mnx_mesh` writes it).
- Never turn a fuzzy `mnx_simindex` similarity into a link without human confirm; exact phonebook matches
  resolve deterministically, red-links stay latent (never block the promote).
