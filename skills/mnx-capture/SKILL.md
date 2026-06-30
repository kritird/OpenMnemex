---
name: mnx-capture
description: Capture the durable knowledge produced in the current build session into the local Mnemex staging tier — cheap, fast, no lock, no graph mutation. Use this whenever a user finishes building or designing something and wants to persist what was learned — domain facts AND the patterns/decisions surfaced in human review — or says "save this to the knowledge graph", "remember this for next time", "capture this", "stage this". Runs in the same session so it can read the artifact and the review/clarification points from the transcript. Extracts atoms, scores each now/later/not-needed, and stages them with self-sufficient provenance. It also curates staging — review what is staged, drop one atom (--drop <id>), or discard all un-promoted captures (--discard-all) — which is the cheap escape valve when staging hits its hard cap. It does NOT reconcile or merge into the shared graph — that is the deliberate, batched /mnemex:mnx-promote step.
---

# mnx-capture — stage a session's knowledge (the `git commit` of memory)

Turn what this session produced — the artifact **and** the human review/clarification points — into
**staged atoms**: provisional, local, self-sufficient knowledge units. The *how* lives in the
conversation (the corrections, the rejected alternatives), so mine the transcript, not just the final
artifact, **now** — by promote time the transcript is gone.

Capture is the **fast, local half** of the capture/promote split. It is cheap, takes no lock, never
reads the graph's cluster indexes, and **never mutates the graph**. Reconcile / merge / consolidate /
push all happen later in `/mnemex:mnx-promote`. (Analogy: capture = `git commit`; promote = `git push`/PR.)

Background: `docs/11-staging-and-promotion.md` (the whole model), `docs/01-rationale-and-concepts.md`
(node types, ids), `docs/03-data-model-and-schemas.md` (staged-atom front-matter). Helper:
`mnx_stage` (the only writer here) and `mnx_binding` (locate the graph).

## Curate mode — review / drop / discard (no extraction)
If invoked with `--drop <provisional-id>` or `--discard-all`, this is the local **un-stage** path — the
cheap way to prune staging (and the escape valve when the hard cap is blocking new captures). It still
runs the locate preflight (to find the staging tier) but does **no** extraction, scoring, or staging:
- **Review first** when helpful: `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/mnx_stage.py" list` shows the
  staged atoms (provisional id · score · summary · age). (`/mnemex:mnx-status` shows the same list.)
- `--drop <id>` → `mnx_stage.py clear-one --id <id>`; report the dropped atom's id + summary (or that it
  was not found).
- `--discard-all` → show the list and **confirm with the user**, then `mnx_stage.py clear`; report the
  count removed.
This touches **only** the local staging tier — never the graph, never the stamp spill. Then stop.

## Preflight — locate the graph (always first)
Run `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/mnx_binding.py" status`.
- If `resolved` is false → **STOP**: *"No Mnemex graph configured. Run `/mnemex:mnx-init`."*
- Note `graph_root` (for routing intent only — capture writes **nothing** there) and `staging_root`
  (where atoms land). Capture is local; it does **not** need `clone_present` / a sync.

## Phase 0 — Budget pre-check (backpressure)
Run `python3 "${CLAUDE_PLUGIN_ROOT}/scripts/mnx_stage.py" status`.
- `budget.level == "hard"` → **STOP capturing** and give the user the two ways out (the backpressure
  bound): either *"run `/mnemex:mnx-promote` to merge + drain staging"* **or** *"make room by discarding
  with `/mnemex:mnx-capture --drop <id>` or `--discard-all`."* Show the staged list (`mnx_stage.py list`)
  so they can choose what to drop.
- `budget.level == "soft"` → proceed, but **warn** the user once that a promote is due.

## Phase 1 — Extract (mine the transcript, honor the node-size budget)
Decompose the artifact + transcript into candidate atoms. For each, decide:
- **`domain`** (a fact about the system/business — the *what*), or
- **`pattern`** (prescriptive *how*, with a `trigger` = the *when* it applies). **Mine human review
  points specifically**: a correction or a rejected alternative becomes a pattern — *"do X not Y,
  because…"* — with a trigger describing the situation it governs.

Draft `summary` (one line), `aliases` (other names the concept goes by), `domain` (routing key(s)),
and a tight body. **Node-size budget (completeness-of-atom, not brevity):** keep each atom's body
under the soft cap (`node_body_max_chars`, default ~6000). If a unit is genuinely bigger, **split it
into multiple atoms and capture an edge between them** (good hygiene) — never truncate to fit. Cap the
number of atoms per session to what the session actually produced; do not pad.

## Phase 2 — Score each atom (`now | later | not-needed`)
A momentary judgement of **intrinsic importance — NOT novelty**. Drift between sessions is fine; there
is no rigid rubric. Novelty/dedup is decided later at promote (reconcile may drop an atom as a
duplicate), so do **not** pre-judge "probably already known."
- **`now`** → stage **with `--urgent`**. (Urgent never inline-pushes — promote is still the only
  writer; urgent only sharpens the nag.)
- **`later`** → stage normally.
- **`not-needed`** → **silently drop.** No staging, no audit, no asking the user. Reserve this for the
  clearly ephemeral or trivially derivable.

## Phase 3 — Stage (the only write)
For each kept atom, write it to the staging tier. Provenance must be **self-sufficient for a cold
promote** — artifact ref, the specific review ids, rejected alternative(s), the rationale, and the
session timestamp:

```
python3 "${CLAUDE_PLUGIN_ROOT}/scripts/mnx_stage.py" add --json <<'JSON'
{ "type": "pattern",
  "summary": "Reconcile settlement before posting",
  "aliases": ["settle-recon"],
  "domain": ["settlement"],
  "trigger": "reviewing or curating a settlement spec",
  "score": "now", "urgent": true,
  "provenance": { "artifact": "tap-vic-settlement-spec", "reviews": ["r3","r7"],
                  "rejected": ["post-then-reconcile (causes orphaned legs)"],
                  "rationale": "human correction in review r7" },
  "body": "Always reconcile the settlement batch before posting legs, because …" }
JSON
```

(Or use flags for a simple atom: `add --type domain --summary "…" --domain settlement --score later
--aliases "a;b" --artifact <id> --reviews "r3;r7" --rationale "…" --body "…"`.) The helper mints the
**provisional id** (a content hash, `stg-…`) — never invent an id, never reuse a real node id. A
re-capture of identical content is idempotent.

## Phase 4 — Report
Summarize what was staged: counts by score, any `urgent`, and the post-stage `budget.level`. If the
helper **refused** an atom (`action: refused`), surface the hard-cap message and give both ways out —
`/mnemex:mnx-promote` to drain staging, or `/mnemex:mnx-capture --drop <id>` / `--discard-all` to make
room. Then stop — **do not** offer to push or merge; that is promote's job.

## Never
- Never reconcile, merge, re-tier, or open the graph's cluster indexes — capture is local-only.
- Never write into `graph_root`, never take the team lock, never commit or push.
- Never stamp a staged atom or give it a real node id (the `stg-` provisional id is content-derived).
- Never `not-needed`-drop on a *novelty* guess — only the clearly ephemeral/derivable.
- Never truncate an over-budget atom — split into atoms + an edge.
