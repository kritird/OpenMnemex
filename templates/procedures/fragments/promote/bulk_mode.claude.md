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

