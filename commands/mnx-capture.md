---
description: Capture this session's durable knowledge (artifact + human review points) into the local Mnemex staging tier — extract atoms, score each now/later/not-needed, and stage them with self-sufficient provenance. Fast and local; it does NOT merge into the shared graph (that is /mnemex:mnx-promote). Also curates staging — review, drop one, or discard all un-promoted captures.
argument-hint: "[--drop <id> | --discard-all]"
---

Use the **mnx-capture** skill to stage the knowledge produced in THIS session — cheaply and locally.

Options: $ARGUMENTS

**Curate mode (no extraction).** If `--drop <provisional-id>` or `--discard-all` is present, skip
extraction entirely — this is the local "un-stage" path (the escape valve from hard-cap backpressure):
- `--drop <id>` → `mnx_stage.py clear-one --id <id>`; echo the dropped atom's id + summary.
- `--discard-all` → first show the staged atoms (`mnx_stage.py list`) and confirm, then `mnx_stage.py
  clear`; report how many were removed.
This only touches the local staging tier — never the graph. Then stop.

Otherwise (capture mode): run the **preflight** (`mnx_binding.py status` → resolve the graph; note
`staging_root`; if unresolved, stop and point at `/mnemex:mnx-init`). Then a budget pre-check
(`mnx_stage.py status` — if `hard`, stop and tell the user they can either run `/mnemex:mnx-promote` to
drain staging **or** make room by discarding with `/mnemex:mnx-capture --drop <id>` / `--discard-all`; if
`soft`, warn). Then: **Extract** the artifact +
transcript into atoms (tag domain vs pattern; turn review corrections into patterns with a `trigger`;
honor the node-size budget — **capture an oversized atom whole; never truncate and never split** (splitting
into sibling pages + a `[[link]]` is **promote's** graph-aware job, not capture's)), **Score** each atom
`now | later | not-needed` (intrinsic importance, NOT novelty — `now` ⇒ stage `--urgent`, `later` ⇒
stage, `not-needed` ⇒ silently drop), **Stage** each kept atom via `mnx_stage.py add` with
self-sufficient provenance (artifact, review ids, rejected alternatives, rationale). Capture is the
local `git commit` half of memory — it never reconciles, never opens cluster indexes, never takes the
lock, never commits or pushes. Reconcile + merge + consolidate + push happen later in
`/mnemex:mnx-promote`. Finish by reporting counts by score and the post-stage budget level.
