---
name: mnx-write
description: Capture the durable knowledge produced in the current build session into a Mnemex Context Graph knowledge graph. Use this whenever a user finishes building or designing something and wants to persist what was learned — domain facts AND the patterns/decisions surfaced in human review — into the graph, or says "save this to the knowledge graph", "remember this for next time", "ingest this", or "write this up as knowledge". Runs in the same session so it can read the artifact and the review/clarification points from context. Extracts, reconciles against existing nodes, shows a change plan for approval, then applies atomically behind a team lock.
---

# mnx-write — ingest a session into the graph

Turn what this session produced — the artifact **and** the human review/clarification points — into
nodes, edges, and patterns. The *how* lives in the conversation (the corrections, the rejected
alternatives), so mine the transcript, not just the final artifact. Only the final phase mutates, and
it is gated by a human-reviewed plan.

Background: `docs/01-rationale-and-concepts.md` (node types, edges, ids),
`docs/04-skills-commands-hooks.md` (phases), `docs/03-data-model-and-schemas.md` (exact node format).
Edge vocabulary (controlled — use only these, extend deliberately): `routes-through`, `governs`,
`governed-by`, `defined-in`, `depends-on`, `supersedes`, `superseded-by`, `same-as`, `references`
(soft, cross-team only). Helpers: `mnx_resolve`, `mnx_index`, `mnx_lock`, `mnx_common`.

## Phase 1 — Extract
Decompose the artifact + transcript into candidate knowledge units. For each, decide:
- **`domain`** (a fact about the system/business — the *what*), or
- **`pattern`** (prescriptive *how*, with a `trigger` = the *when* it applies). **Mine human review
  points specifically**: a correction or a rejected alternative becomes a pattern — *"do X not Y,
  because…"* — with a trigger describing the situation it governs.
Draft `summary` (one line), `aliases` (other names the concept goes by), and `provenance` (artifact,
the specific review ids, session timestamp) for each candidate.

## Phase 2 — Reconcile (the hard part)
Route each candidate to a cluster (org→team→domain). Read that cluster's index (summaries + aliases)
and classify the candidate against existing nodes:
- **new** — no match → create.
- **update** — matches an existing node → add/adjust fields, append to body.
- **merge** — duplicates an existing node → fold in, keep one id.
- **contradiction** — conflicts with an existing node → **never silently overwrite**. Create a
  superseding version (`status: superseded` on the old, `supersedes`/`superseded-by` edges) or flag for
  the human.

Lazy cold reconciliation (per `reconcile_cold_on`): for **update-intent** candidates, also scan the
Cold section (a cold match is a **resurrection** — revive the node). For **create-intent** candidates,
scan cold only on alias/domain overlap. Load a handful of existing node bodies when summary+aliases are
not enough to judge a near-match.

## Phase 3 — Plan (STOP for human approval)
Emit a surgical change plan and **wait**. Do not apply yet. On `--dry-run`, stop after this phase.

```
CHANGE PLAN
CREATE  domain  iso8583-field124   in team-payments/settlement   (summary: …)
UPDATE  domain  visanet-routing    +edge routes-through→iso8583-field124
CREATE  pattern pat-settle-recon   trigger: "reviewing a settlement spec"  governs→iso8583-field124
RESURRECT cold  legacy-de124-fmt   (matched update candidate; promote)
SUPERSEDE       old-routing-note   → iso8583-field124
```

## Phase 4 — Apply (locked, atomic)
1. Acquire the team lock (`mnx_lock.acquire`). If a pass is in progress, stop and tell the user.
2. Write/CREATE node files (pure knowledge; stable slug ids via `mnx_common.slugify`, uniqueness
   checked). Write **outgoing** edges into each owning node's front-matter.
3. Append any usage stamps; delta-update the team `cross-links.md` for new boundary edges.
4. Regenerate affected `index.md` sections with `mnx_index.regenerate_index` — this denormalizes
   `summary`/`aliases` into the index.
5. If a cluster now exceeds `node_budget`, split its index along the `domain:` sub-key; if a single
   sub-key still overflows, **escalate to the human** — never invent folder structure.
6. Run `mnx-doctor` (must pass), then **one git commit**, then release the lock.

## Never
- Never auto-apply without showing the plan.
- Never overwrite on a contradiction — supersede or flag.
- Never put decay state (`strength`/`tier`) into a node — that lives in the index.
- Never create hard edges across teams — cross-team links are soft `references` with a disclaimer.
