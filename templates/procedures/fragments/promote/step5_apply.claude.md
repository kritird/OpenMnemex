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

