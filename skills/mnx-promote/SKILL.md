---
name: mnx-promote
description: Promote the locally-staged Mnemex captures into the shared knowledge graph — the deliberate, batched, attention-heavy half of memory (the `git push`/PR to capture's `git commit`). Use this when the user wants to merge captured knowledge into the graph, says "promote", "flush staging", "merge my captures", "publish to the knowledge graph", or when a nag reports staged atoms pending / consolidation overdue. Flushes usage stamps, reconciles + merges every staged atom (clean-context sub-agent, human-in-the-loop on contradictions), consolidates the post-merge graph, runs the doctor, pushes, and clears staging — atomically and totally.
---

# mnx-promote — merge staging into the graph (the `git push`/PR of memory)

Promote is the **heavy, deliberate, occasional** half of the capture/promote split. It pulls the
attention-demanding merge *out* of the creative session: capture stays cheap and local, and the
reconciliation that needs care happens here, in a batch, when the user chooses to spend the attention.

**Promote is atomic and total.** Every staged atom reaches a terminal disposition in one cycle —
*created / merged / dropped-as-duplicate / superseded*. Any contradiction is a **hard human-in-the-loop
(HITL) block**: resolve all in-cycle or **abort**. On success staging is **fully cleared**; on abort
staging is **untouched**. There is no lingering "held" state.

Background: `docs/11-staging-and-promotion.md` (the model, the reconcile sub-agent contract),
`docs/05-maintenance-pass-algorithm.md` (the folded consolidate). Helpers: `mnx_binding`, `mnx_stamp`,
`mnx_stage`, `mnx_lock`, `mnx_resolve`, `mnx_index`, `mnx_doctor`, `mnx_common`; internal skill:
`mnx-consolidate`.

## Preflight
1. **Locate + sync:** `mnx_binding.py status`. If `resolved` is false → **STOP**, point at
   `/mnemex:mnx-init`. If `clone_present` is false → `mnx_binding.py sync` once. Operate on
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
- On `push: ok` → **now** do the deferred clear: `mnx_stage.py clear`, remove `pass.plan.json`, release
  any lock. Report success.
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

For each staged atom the plan assigns exactly one terminal disposition:
`CREATE` (new node, real slug id via `mnx_common.slugify`) · `MERGE`/`UPDATE` (fold into an existing
node) · `DROP-DUP` (duplicate — discard) · `SUPERSEDE` (new version; `supersedes`/`superseded-by`
edges, old → `status: superseded`) · `RESURRECT` (a cold match — revive). Honor the **node-size budget**:
an over-budget body is split into multiple nodes + an edge, never truncated.

**Freshness fields on apply (Doc 14):** any node the plan writes fresh knowledge into —
`CREATE`/`MERGE`/`UPDATE`/`SUPERSEDE`/`RESURRECT` — gets `verified = now` (it was just re-derived under
the human gate); a meaning change also bumps `updated`. Carry the atom's proposed **`volatility`** onto
the node, and **surface it in the plan for the human to confirm or override** (e.g. downgrade a fast-rotting
fact to `volatile`, or mark a definition `timeless`). Default stays `default` (type-derived horizon).

**Contradictions are a hard block.** Present every contradiction to the human and resolve it in-cycle.
If any cannot be resolved now → **abort the whole promote** (staging untouched).

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
  ⚠ CONTRADICTION  stg-44ee… vs settle-cutoff-time  → RESOLVE or ABORT
  CONSOLIDATE
  RE-TIER   3 hot→warm, 1 warm→cold
  DEATH     legacy-de124-fmt (low score ∧ weak struct ∧ TTL expired)
  CHAIN     team-payments/settlement index → index.001.md (cold over budget)
```

`--dry-run` stops here. On any unresolved ⚠ → abort.

## Step 5 — Apply (serial, locked, atomic) → push → clear staging
After approval, apply the plan **serially** under the lock in fixed order (truth before derived):
1. Write/CREATE/MERGE/SUPERSEDE node files (real slug ids; outgoing edges in front-matter); apply
   consolidate's tombstones + transactional edge severing.
2. Regenerate affected indexes (`mnx_index.regenerate_index` — denormalize summary/aliases; chain the
   cold tier when over `index_chunk_rows`); delta-update `cross-links.md`; advance high-water marks;
   stamp `last_compaction` + `config_version`.
3. **Doctor:** `mnx_doctor.py check <graph_root>` must pass (E == 0).
4. **Persist:** `mnx_binding.py persist --message "mnx-promote: <plan summary>"` — kind-aware
   (git-remote → commit **+ push** with bounded retry; git-local → commit; plain-local → audit-append).
   On `push: failed`/`conflict` the merge **is already committed** in the clone — **do not clear
   staging** and **do not re-run the merge**. Surface the structured `recovery` block and tell the user
   to run `/mnemex:mnx-promote --retry-push` (push the existing commit); if it keeps failing, the
   `manual_fallback` git commands are the last resort. Stop here.
5. **Clear staging only on a confirmed persist:** `mnx_stage.py clear`. Remove `pass.plan.json`,
   release the lock. (Optionally `mnx_doctor.py check-staging` to confirm it is empty.)

## Never
- Never apply without the single combined plan approved by the human.
- Never overwrite on a contradiction — supersede or HITL-block; if unresolved, abort.
- Never leave a staged atom without a terminal disposition (created/merged/dropped/superseded).
- Never clear staging unless persist confirmed (push ok / committed / audit-recorded). Abort leaves
  staging untouched.
- Never start a fresh merge when `status` reports `unpushed: true` — that double-applies staging over
  the already-committed merge. Use `--retry-push` (push the existing commit) instead.
- Never carry a provisional `stg-…` id into the graph — promotion mints a real slug id.
- Never auto-invent folder structure on overflow — split by sub-key, then chain; escalate last.
