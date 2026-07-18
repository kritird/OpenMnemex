## Bulk mode — drain a corpus ingest batch (gate #2)
Pass `ingest_batch=<id>` to `promote_begin` / `promote_context` / `promote_apply` to drain a
labeled bulk batch (staged via `capture_add(ingest_batch=<id>)` or the ingest tools) instead
of the default unlabeled session batch — the identical transaction, just scoped to that
batch's pids; it never touches a concurrently-staged session capture. `promote_begin()` with
no `ingest_batch` while only bulk atoms are staged returns `guard: ingest-batch` naming
`promote_begin(ingest_batch=...)` as the next call.

Only the *shape* of reconcile and the plan-drafting judgment change for scale — the
mechanics (Preflight, Step 5) are identical:
1. `promote_begin(ingest_batch=<id>)` — same preflight + lock as episodic. `guard:
   empty-batch` means nothing is staged under that id yet.
2. `promote_context(ingest_batch=<id>)` — same batch/near-matches/cluster-index/mesh-preview
   shape. Draft the plan per cluster if the batch is large; ER already collapsed intra-batch
   duplicates before staging (one entity → one node).
3. Draft one **summarized** plan: per-cluster counts, auto-accept plain CREATE/MERGE, list
   only contradictions, the ER `possible` band, and new-cluster creation in full. Same plan
   JSON shape as episodic (`dispositions`/`splits`/`links`/`consolidate`).
4. `promote_apply(plan, approved=true, ingest_batch=<id>)` — the same Step-5 transaction,
   scoped to this batch's pids.
5. Only after `action: applied` (or a subsequent `promote_retry_push` settles it), record the
   ingest manifest (`ingest_manifest_write`) so the next re-import diffs correctly — this is
   ingest-specific bookkeeping the transaction itself does not do.

A single-run corpus large enough to want incremental per-sub-batch consolidate checkpointing
(rather than one `promote_apply` settling the whole `ingest_batch` at once) is not yet
supported — resume a cost-ceilinged corpus at the ingest layer instead (stage more atoms
under the same id, then promote again).
