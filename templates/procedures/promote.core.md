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

{{BLOCK:preflight}}{{BLOCK:retry_push}}{{BLOCK:step1_flush}}## Step 2 — Reconcile + merge staged atoms (clean-context sub-agent; HITL)
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
{{BLOCK:consolidate_invoke}} Surface its decisions in the **same** approval plan as the merge, so
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

{{BLOCK:step5_apply}}{{BLOCK:bulk_mode}}## Never
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
