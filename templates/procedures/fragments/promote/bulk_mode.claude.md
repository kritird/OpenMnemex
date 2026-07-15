## `--bulk` mode — drain a corpus ingest batch (gate #2)
`/mnemex:mnx-promote --bulk [--ingest-batch <id>]` is the volume-adapted promote that
[`/mnemex:mnx-ingest`](../mnx-ingest/SKILL.md) hands off to. **Same transaction, same single-writer, same
`mnx_node` truth-writes, same lock + doctor gate** — only the *shape* of reconcile, the plan, and consolidate
change for scale. Read the batch with `mnx_stage.py list --ingest-batch <id>` (it is label-partitioned from
any hand-captures — never drain the session atoms here). Background: `docs/corpus-ingestion.md` §6.

1. **Fork reconcile per cluster.** The reconcile sub-agent contract already permits forking — *plan in
   parallel, apply serially under the lock*. Each fork reconciles its subtree's atoms against **one**
   cluster's index. ER already collapsed intra-batch duplicates before staging (one entity → one node), so
   each fork mostly assigns CREATE/MERGE.
2. **Summarized plan (gate #2).** The approval plan collapses to **per-cluster counts** (`CREATE 214 · MERGE
   31 · DROP-DUP 57`) and lists *only the exceptions in full*: **contradictions, ambiguous near-matches
   (the ER `possible` band → `⚠ suggested`), and new-cluster creation.** **Auto-accept the plain
   CREATE/MERGE** — there is no per-atom review at corpus scale. Everything else in Step 2/2b/4 is unchanged
   (deterministic `[[link]]` resolution via `mnx_mesh`, red-link backfill knits the mesh **within** the run
   as later batches land targets, fuzzy → `⚠ suggested`).
3. **Incremental consolidate + checkpoint over a FROZEN view.** Run consolidate (`mnx-consolidate`) **per
   drained batch over a snapshot**, not once at the end over a moving target — otherwise death/re-tier math
   thrashes as the graph grows mid-import. Snapshot → apply → checkpoint, batch by batch. Each batch runs the
   doctor gate (E == 0) before persist and regenerates inv-4 cross-links when any cross-cluster link was created.
4. **Manifest write on confirmed persist (A5b, DP4).** *Only* after push/commit confirms (mirrors
   `clear_merged`), write `source_path@commit → node_ids` into `<graph_root>/.mnemex/ingest/<slug>.json`
   with `mnx_ingest.py manifest-write --graph <root> --source-slug <slug> --json` (stdin: the
   `{files:{path:{hash,nodes}}}` map for this batch). This is what makes the next `/mnemex:mnx-ingest`
   diff correctly and stay idempotent.
5. **Settle the batch, not the session.** Clear only this batch's promoted atoms
   (`mnx_stage.py clear-merged --ids …`, or `clear --ingest-batch <id>` for a fully-clean batch); hold any
   contradicting atom (`mnx_stage.py hold`). **Never** `clear` all — that would discard a user's hand-captures.
6. **Crash recovery + resume.** `--bulk` takes the same `mnx_lock` and recovers a stranded `pass.plan.json`
   exactly as episodic promote; a partially-drained batch is resumable (`--resume <ingest-batch>`) from the
   manifest + the remaining staged atoms. Cost containment: honor `ingest_max_atoms_per_run` — excess resumes.

