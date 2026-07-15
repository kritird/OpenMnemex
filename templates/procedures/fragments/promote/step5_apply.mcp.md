## Step 5 — Apply (serial, locked, atomic) → push → clear staging
After Step 4's plan is approved by the human, call `promote_apply` with `plan` and
`approved: true`. The engine executes the identical sequence, serially, under the lock, as
one transaction:
1. Persist the node truth through `mnx_node` — one write per disposition (create / merge /
   supersede / resurrect / drop_dup / hold) — mints ids, stamps `created`/`updated`/`verified`
   from one clock, keeps a superseded/dead node's body. Then `mnx_mesh.apply_links` writes the
   resolved wiki-links + back-links onto the source notes and records red-links (front-matter
   `edges:` is the generated mirror — never hand-authored); then consolidate's tombstones +
   transactional edge severing + freshness advances apply.
2. Regenerate affected indexes (denormalize summary/aliases; chain the cold tier when over
   budget); regenerate `cross-links.md`; advance high-water marks; stamp `last_compaction` +
   `config_version`.
3. **Doctor gate:** the invariant suite must pass (`E == 0`), else the engine rolls back to
   the last good commit and returns the findings — nothing is left half-applied.
4. **Persist:** commit (+ push, with bounded retry, for git-remote graphs). On push
   failure/conflict the merge is already committed in the clone — `promote_apply` does **not**
   settle staging and does **not** re-run the merge; it returns the structured `recovery`
   block. Call `promote_retry_push` once resolved.
5. **Settle staging only on a confirmed persist:** hold each contradicting atom
   (`hold --reason … --contradicts <graph-id>`), clear-merge the atoms that promoted, remove
   the plan, release the lock.

`promote_apply`'s result is the applied summary, or the structured `recovery` block on push
failure — the plan (`pass.plan.json`) stays in place for `promote_retry_push`.

